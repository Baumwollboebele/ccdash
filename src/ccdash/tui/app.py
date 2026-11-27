"""The ccdash dashboard (Textual)."""

from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path

from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal
from textual.screen import ModalScreen
from textual.widgets import DataTable, Static

from ..dashboard import Dashboard, Progress
from ..workspace import Workspace, WorkspaceError
from .agents_table import AgentsTable
from .anim import Animator, lerp_hex, pulse_color, spinner_glyph
from .filemap import FileMapView
from .modals import ChoiceModal, ConfirmDeleteModal, HelpModal, MessageModal, NewAgentModal, TextInputModal
from .network import CommsView, NetworkView
from .panels import ConflictsPanel, DetailsPanel, MessagesPanel
from . import theme
from .theme import ACCENT, DIM, THEMES, pick_theme, reduced_motion

MAIN_ACTIONS = {"lead", "new", "stop", "restart", "attach", "peek", "talk", "broadcast", "merge", "diff", "delete",
                "filter", "network", "filemap", "comms", "claim", "toggle_hidden", "adopt", "next_repo", "help",
                "quit_dash"}
VIEW_TITLES = {"network": "Network", "filemap": "File map"}
VIEW_KEYS = (("g", "network", "network"), ("f", "filemap", "file map"), ("c", "comms", "comms"))


def views_hint(mode: str) -> str:
    """The panel's subtitle: the other views are one key away, and the same key comes back."""
    return " · ".join(f"{key} {'details' if view == mode else label}" for key, view, label in VIEW_KEYS)

FOOTER_KEYS = [("l", "lead"), ("n", "new"), ("s", "stop"), ("r", "restart"), ("a", "attach"), ("p", "peek"),
               ("t", "talk"), ("b", "broadcast"), ("m", "merge"), ("d", "diff"), ("x", "delete"), ("?", "help"),
               ("q", "quit")]


class CCDashApp(App):
    CSS_PATH = "app.tcss"
    TITLE = "ccdash"
    ENABLE_COMMAND_PALETTE = False
    BINDINGS = [
        Binding("l", "lead", "lead"),
        Binding("n", "new", "new"), Binding("s", "stop", "stop"), Binding("r", "restart", "restart"),
        Binding("a", "attach", "attach"), Binding("p", "peek", "peek"), Binding("t", "talk", "talk"),
        Binding("b", "broadcast", "broadcast"), Binding("m", "merge", "merge"), Binding("d", "diff", "diff"),
        Binding("x", "delete", "delete"), Binding("slash", "filter", "filter"), Binding("g", "network", "network"),
        Binding("f", "filemap", "file map"), Binding("c", "comms", "comms"), Binding("o", "claim", "owns"),
        Binding("H", "toggle_hidden", "hidden"), Binding("A", "adopt", "adopt"), Binding("R", "next_repo", "repo"),
        Binding("question_mark", "help", "help"), Binding("q", "quit_dash", "quit"),
    ]

    def __init__(self, ws: Workspace, attach_fn=None, no_anim: bool = False, external_fn=None, others=()):
        super().__init__()
        self.spaces: list[tuple[Workspace, Dashboard]] = []  # the repo you are in, then `repos` from the config
        for w in (ws, *others):
            d = Dashboard(w)
            d.conflicts.active = True  # this dashboard sends the notices (shared interface, what landed)
            self.spaces.append((w, d))
        self.space = 0
        self.ws, self.dash = self.spaces[0]
        self._tab_hint_shown = False
        self.attach_fn = attach_fn
        self._attach_explained = attach_fn is not None  # the first real attach explains itself (see action_attach)
        self.external_fn = external_fn  # tests: receives the argv of a pager instead of running it
        self._demo_rows: list | None = None
        self._refreshing = self._refresh_again = self._git_refreshing = self._git_again = False
        self.animate = ws.cfg.animations and not reduced_motion(no_anim)
        self.frame = 0
        self.peek_on = False
        self.peek_lines: list[str] | None = None
        self._booted = False
        self._watch_stop = threading.Event()
        self.motion = Animator(self, enabled=self.animate)
        self.view_mode = "details"  # details | network | filemap   (comms is a separate full-screen class)
        self._prev: dict[str, tuple[int, str]] = {}
        self._seen_messages = 0
        self._conflict_keys: set = set()
        for theme in THEMES:  # before the stylesheet is parsed: it uses $ccdash-bg / $ccdash-dim
            self.register_theme(theme)
        self.theme = pick_theme()

    # -- layout -------------------------------------------------------------------------
    def compose(self) -> ComposeResult:
        yield Static(id="header")
        with Container(id="agents-panel", classes="panel") as p:
            p.border_title = "Agents"
            yield AgentsTable(id="agents")
        with Horizontal(id="middle"):
            with Container(id="details-panel", classes="panel") as p:
                p.border_title = "Details"
                p.border_subtitle = views_hint("details")
                yield DetailsPanel(id="details")
                yield NetworkView(id="network")
                yield FileMapView(id="filemap")
            with Container(id="messages-panel", classes="panel") as p:
                p.border_title = "Messages"
                yield MessagesPanel(id="messages")
        with Container(id="conflicts-panel", classes="panel") as p:
            p.border_title = "Conflicts"
            yield ConflictsPanel(id="conflicts")
        yield CommsView(id="comms")
        yield Static(id="footer")

    @property
    def table(self) -> AgentsTable:
        return self.query_one("#agents", AgentsTable)

    def on_mount(self) -> None:
        self._layout(self.size.width)
        self.table.focus()
        self.render_all()
        self.request_refresh()
        self.refresh_git_worker()
        self.watch_events()
        self.set_interval(2.0, self.request_refresh, name="runtime")
        self.set_interval(10.0, self.refresh_git_worker, name="git")
        self.set_interval(1.0, self._peek_tick, name="peek")
        self.set_interval(30.0, self._render_header, name="clock")
        if len(self.spaces) > 1:
            self.set_interval(10.0, self._refresh_others, name="others")
            self._refresh_others()

    def watch_events(self) -> None:
        """Refresh as soon as a hook appends an event (the 2 s poll is the fallback).

        A plain daemon thread with a stop event and a 1 s native timeout, so quitting
        never waits on the watcher."""
        try:
            from watchfiles import watch
        except ImportError:
            return
        root = self.ws.state.root
        root.mkdir(parents=True, exist_ok=True)
        stop = self._watch_stop

        def run() -> None:
            try:
                for _changes in watch(root, watch_filter=lambda change, path: path.endswith("events.jsonl"),
                                      debounce=60, step=30, rust_timeout=1000, yield_on_timeout=False,
                                      stop_event=stop, recursive=False, raise_interrupt=False):
                    if stop.is_set():
                        return
                    try:
                        self.call_from_thread(self.request_refresh)
                    except Exception:  # noqa: BLE001  (app is gone)
                        return
            except Exception:  # noqa: BLE001  (unwatchable file system: polling still works)
                return

        threading.Thread(target=run, name="ccdash-watch", daemon=True).start()

    def on_unmount(self) -> None:
        self._watch_stop.set()
        self.motion.enabled = False
        self.motion.ensure()

    def request_refresh(self) -> None:
        """Coalesce refresh requests: never more than one runtime refresh in flight."""
        if self._refreshing:
            self._refresh_again = True
            return
        self._refreshing = True
        self.refresh_runtime_worker()

    def _refresh_done(self) -> None:
        self._refreshing = False
        self.render_all()
        self._booted = True
        self._toast_transitions()
        if self.dash.git_wanted:
            self.refresh_git_worker()
        if self._refresh_again:
            self._refresh_again = False
            self.request_refresh()

    def _toast_transitions(self) -> None:
        for tr in self.dash.take_transitions():
            if tr.new == "finished":
                self.notify(f"{tr.name} finished.", title="Agent finished")
            elif tr.new == "needs_input":
                self.notify(f"{tr.name} is waiting for you.", title="Needs input", severity="warning")
            elif tr.new == "crashed":
                self.notify(f"{tr.name} exited unexpectedly; its worktree is untouched.", title="Crashed",
                            severity="error")

    def on_resize(self, event) -> None:
        self._layout(event.size.width)

    def _layout(self, width: int) -> None:
        self.set_class(width < 100, "narrow")
        try:
            self.table.fit_width(width - 4)
        except Exception:  # noqa: BLE001  (before mount)
            pass

    def check_action(self, action: str, parameters) -> bool | None:
        if action in MAIN_ACTIONS and isinstance(self.screen, ModalScreen):
            return False
        return True

    # -- refresh ------------------------------------------------------------------------
    @work(thread=True, group="runtime")
    def refresh_runtime_worker(self) -> None:
        ws, dash = self.ws, self.dash  # one repo for the whole refresh, even if you switch meanwhile
        try:
            dash.refresh_runtime()
            if (ws.state.root / "outbox.json").exists():
                from .. import messaging

                if messaging.flush_outbox(ws, dash):
                    dash.refresh_events()
            self._update_peek()
        except Exception as exc:  # noqa: BLE001  (git or a file busy for a moment: the next refresh tries again)
            self._refresh_failed(exc)
        finally:
            self.call_from_thread(self._refresh_done)

    def _refresh_failed(self, exc: Exception) -> None:
        """A failed refresh must not take the dashboard down; say what failed, once per cause."""
        text = f"{type(exc).__name__}: {exc}"[:300]
        if text != getattr(self, "_last_refresh_error", None):
            self._last_refresh_error = text
            try:
                self.call_from_thread(self.notify, text, title="Refresh failed, trying again", severity="warning",
                                      timeout=8)
            except Exception:  # noqa: BLE001  (the app is closing)
                pass

    # -- several repos (`repos` in the user config) ------------------------------------------
    def action_next_repo(self) -> None:
        if len(self.spaces) < 2:
            self.notify("Only this repository. List more under repos = [...] in your user config "
                        "(~/.config/ccdash/config.toml) to switch between them here.")
            return
        self._switch_repo((self.space + 1) % len(self.spaces))

    def _switch_repo(self, index: int) -> None:
        self._watch_stop.set()  # the watcher follows one state dir
        self._watch_stop = threading.Event()
        self.space = index
        self.ws, self.dash = self.spaces[index]
        self._prev.clear()
        self._seen_messages = 0
        self._conflict_keys = set()
        self._booted = False
        self.peek_lines = None
        self.network.flights.clear()
        self.watch_events()
        if self.view_mode == "details":
            self.query_one("#details-panel").border_title = "Details"
        self.render_all()
        self.request_refresh()
        self.refresh_git_worker()

    @work(thread=True, exclusive=True, group="others")
    def _refresh_others(self) -> None:
        """Keep an eye on the repos you are not looking at: who needs you there."""
        self._others_round = getattr(self, "_others_round", 0) + 1
        waiting = []
        for i, (ws, dash) in enumerate(self.spaces):
            if i == self.space:
                continue
            try:
                dash.refresh_runtime()
                if self._others_round % 6 == 1:
                    dash.refresh_git()
            except Exception:  # noqa: BLE001  (a repo that went away must not stop the dashboard)
                continue
            waiting += [(ws.repo.name, tr.name) for tr in dash.take_transitions() if tr.new == "needs_input"]
        try:
            for repo_name, agent in waiting:
                self.call_from_thread(self.notify, f"{agent} in {repo_name} is waiting for you. R switches repos.",
                                      title="Needs input", severity="warning")
            self.call_from_thread(self._render_header)
        except Exception:  # noqa: BLE001  (the app is closing)
            pass

    def _peek_tick(self) -> None:
        if self.peek_on:
            self._peek_worker()

    @work(thread=True, exclusive=True, group="peek")
    def _peek_worker(self) -> None:
        self._update_peek()
        self.call_from_thread(self._render_details)

    def refresh_git_worker(self) -> None:
        """Coalesce git refreshes too: one in flight, at most one more queued behind it."""
        if self._git_refreshing:
            self._git_again = True
            return
        self._git_refreshing = True
        self._git_worker()

    @work(thread=True, group="git")
    def _git_worker(self) -> None:
        dash = self.dash
        try:
            dash.refresh_git()
        except Exception as exc:  # noqa: BLE001
            self._refresh_failed(exc)
        finally:
            self.call_from_thread(self._git_done)

    def _git_done(self) -> None:
        self._git_refreshing = False
        self.render_all()
        if self._git_again:
            self._git_again = False
            self.refresh_git_worker()

    async def refresh_now(self) -> None:
        await asyncio.to_thread(self.dash.refresh_all)
        self.render_all()

    def _update_peek(self) -> None:
        name = self.selected_name()
        v = self.dash.get(name) if name else None
        if self.peek_on and v is not None and v.alive:
            from ..runner import strip_prompt_box

            self.peek_lines = strip_prompt_box(self.ws.peek(name, lines=60))  # what it wrote, not its input box
        else:
            self.peek_lines = None

    # -- rendering ----------------------------------------------------------------------
    def render_all(self) -> None:
        views = self.dash.sorted_views()
        self._detect_effects(views)
        self._render_header()
        self.table.update_views(views, self.status_kw_for)
        self._render_details()
        self._render_messages()
        self._render_conflicts()
        self._render_views(views)
        self._render_footer()
        self.motion.ensure()

    def status_kw_for(self, v) -> dict:
        kw: dict = {"progress": self.dash.progress.get(v.name)}
        if not self.animate:
            return kw
        if v.status in ("working", "running", "starting") and not v.stale:
            kw["glyph"] = spinner_glyph()
        if v.status == "needs_input":
            kw["pulse"] = pulse_color()
        if self.motion.active(("ahead", v.name)):
            kw["flash"] = True
        p = self.motion.progress(("merged", v.name))
        if p is not None:
            kw["fade"] = lerp_hex("#9a9a9a", "#5f87ff", p)
        return kw

    # -- animation ----------------------------------------------------------------------
    def anything_moving(self) -> bool:
        if not self.animate:
            return False
        for v in self.dash.views.values():
            if v.status in ("working", "running", "starting", "needs_input", "setup") and not v.stale:
                return True
        if self.dash.progress:
            return True
        if self.view_mode == "network" and self.network.flights:
            return True
        if self.has_class("view-comms"):
            return any(t.state == "discussing" for t in getattr(self.comms, "threads", []))
        return False

    def animate_frame(self) -> None:
        """One animation tick: redraw only what moves."""
        from textual.css.query import NoMatches

        try:
            self._animate_frame()
        except NoMatches:  # the app is closing
            self.motion.enabled = False

    def _animate_frame(self) -> None:
        self.frame += 1
        views = self.dash.sorted_views()
        moving = [v for v in views if v.status in ("working", "running", "starting", "needs_input", "setup")
                  or self.motion.active(("ahead", v.name)) or self.motion.active(("merged", v.name))]
        if moving:
            self.table.refresh_rows(moving, self.status_kw_for)
        name = self.selected_name()
        sel = self.dash.get(name) if name else None
        if self.view_mode == "details" and sel is not None and sel.status == "working":
            self._render_details()
        if any(isinstance(k, tuple) and k[0] == "conflict" for k in self.motion.effects):
            self._render_conflicts()
        self._render_views(views)

    def _detect_effects(self, views) -> None:
        """Start effects for changes worth seeing: commits, merges, new messages."""
        for v in views:
            prev = self._prev.get(v.name)
            if prev is not None:
                if v.ahead > prev[0]:
                    self.motion.start(("ahead", v.name), 1.0)
                if v.merge_state == "merged" and prev[1] != "merged":
                    self.motion.start(("merged", v.name), 1.2)
            self._prev[v.name] = (v.ahead, v.merge_state)
        items = self.dash.messages.items
        if self._seen_messages and len(items) > self._seen_messages:
            for m in items[self._seen_messages:]:
                for to in (m.recipients if m.to in ("all", "*") else [m.to]):
                    self.add_flight(m.sender, to)
                if m.to == "you":  # the lead answered: say so, the answer itself is in Messages and in its details
                    self.notify(m.first_line[:240], title=f"{m.sender} answered", timeout=8)
        self._seen_messages = len(items)

    def add_flight(self, sender: str, to: str) -> None:
        if self.animate:
            self.network.add_flight(sender, to)
            self.motion.start(("flight", sender, to, time.monotonic()), 1.3)

    def red_pairs(self) -> set:
        if self.dash.conflicts is None:
            return set()
        return self.dash.conflicts.red_pairs()

    def touch_map(self) -> dict:
        return self.dash.conflicts.touch_map() if self.dash.conflicts is not None else {}

    def show_conflict_rows(self, rows: list) -> None:
        """Draw these rows instead of the tracker's (demos and tests)."""
        self._demo_rows = rows
        self._render_conflicts()
        self.motion.ensure()

    def conflict_rows(self) -> list:
        if self._demo_rows is not None:
            return self._demo_rows
        return self.dash.conflicts.rows if self.dash.conflicts is not None else []

    def conflicts_text(self) -> str:
        return "\n".join(r.render().plain for r in self.conflict_rows())

    @property
    def network(self) -> NetworkView:
        return self.query_one("#network", NetworkView)

    @property
    def filemap(self) -> FileMapView:
        return self.query_one("#filemap", FileMapView)

    @property
    def comms(self) -> CommsView:
        return self.query_one("#comms", CommsView)

    def network_text(self) -> str:
        return self.network.last_text.plain

    def filemap_text(self) -> str:
        return self.filemap.last_text.plain

    def comms_text(self) -> str:
        return self.comms.list_text()

    def _render_views(self, views) -> None:
        if self.view_mode == "network":
            self.network.show(views, self.dash.messages.items, self.red_pairs(), animate=self.animate,
                              merged_progress=lambda n: self.motion.progress(("merged", n)),
                              default_branch=self.ws.default_branch)
        elif self.view_mode == "filemap":
            self.filemap.show(self.touch_map(), selected=self.selected_name())
        if self.has_class("view-comms"):
            self.comms.show(views, self.dash.messages.items, animate=self.animate, flights=self.network.flights)

    def _set_view(self, mode: str) -> None:
        """What the left middle panel shows: the selected agent's details, the network or the file map."""
        self.view_mode = mode
        self.network.display = mode == "network"
        self.filemap.display = mode == "filemap"
        self.query_one("#details", DetailsPanel).display = mode == "details"
        panel = self.query_one("#details-panel")
        panel.border_title = VIEW_TITLES.get(mode) or self.selected_name() or "Details"
        panel.border_subtitle = views_hint(mode)
        self.render_all()

    def action_network(self) -> None:
        self._set_view("details" if self.view_mode == "network" else "network")

    def action_filemap(self) -> None:
        self._set_view("details" if self.view_mode == "filemap" else "filemap")

    def action_comms(self) -> None:
        self.toggle_class("view-comms")
        if self.has_class("view-comms"):
            self.query_one("#comms-threads").focus()
        else:
            self.table.focus()
        self.render_all()
    def header_text(self) -> str:
        return self._header().plain

    def _header(self) -> Text:
        c = self.dash.counts()
        default = self.ws.default_branch
        several = len(self.spaces) > 1
        left = Text.assemble((" ccdash", f"bold {ACCENT}"), (" · ", DIM), (self.ws.repo.name, "bold"),
                             (f" {self.space + 1}/{len(self.spaces)}" if several else "", ACCENT),
                             (f" ({default} @ {self.dash.default_sha or '…'})", DIM))
        parts = [f"{c['agents']} agent{'s' if c['agents'] != 1 else ''}", f"{c['working']} working",
                 f"{c['needs_input']} needs input", f"{c['conflicts']} conflict{'s' if c['conflicts'] != 1 else ''}"]
        mid = Text()
        styles = ["default", "green", "yellow", "red"]
        for i, (part, st) in enumerate(zip(parts, styles)):
            if i:
                mid.append(" · ", style=DIM)
            zero = part.startswith("0 ")
            mid.append(part, style=DIM if zero and i else st)
        for i, (ws, dash) in enumerate(self.spaces):  # a repo you are not looking at where an agent waits for you
            n = dash.counts()["needs_input"] if i != self.space else 0
            if n:
                mid.append(" · ", style=DIM)
                mid.append(f"{ws.repo.name}: {n} needs input", style="yellow")
        right = Text(theme.fmt_clock(time.time()), style=DIM)
        width = max(40, self.size.width - 2)
        gap = width - len(left) - len(mid) - len(right)
        pad1 = max(2, gap // 2)
        pad2 = max(1, gap - pad1)
        return Text.assemble(left, " " * pad1, mid, " " * pad2, right)

    def _render_header(self) -> None:
        self.query_one("#header", Static).update(self._header())

    def _render_footer(self) -> None:
        t = Text(" ")
        for key, label in FOOTER_KEYS:
            t.append(key, style=ACCENT)
            t.append(f" {label}  ", style=DIM)
        if self.dash.filter:
            t.append(f"filter: {self.dash.filter}", style=ACCENT)
        self.query_one("#footer", Static).update(t)

    def _render_details(self) -> None:
        name = self.selected_name()
        v = self.dash.get(name) if name else None
        panel = self.query_one("#details-panel")
        if self.view_mode != "details":
            return
        panel.border_title = name or "Details"
        activity = self.dash.activity.get(name) if (self.dash.activity is not None and name) else None
        tracker = self.dash.conflicts if name else None
        self.query_one("#details", DetailsPanel).show(
            v, repo_rel=self.ws.repo.rel, default_branch=self.ws.default_branch, default_sha=self.dash.default_sha,
            activity=activity, peek=self.peek_lines if self.peek_on else None, frame=self.frame,
            animate=self.animate, owns=self._owns(name), tests=getattr(activity, "tests", "") if activity else "",
            conflict=tracker.conflict_files(name) if tracker is not None else (),
            pr=tracker.pull_request(name) if tracker is not None else None)

    def _owns(self, name: str | None) -> list[str]:
        if not name or self.dash.conflicts is None:
            return []
        return self.dash.conflicts.owned(name)

    def _render_messages(self) -> None:
        msgs = self.dash.messages.items if self.dash.messages is not None else None
        self.query_one("#messages", MessagesPanel).show(msgs, highlight=self.selected_name())

    def _render_conflicts(self) -> None:
        rows = self.conflict_rows()
        if self._booted:  # a collision that appears while you watch slides in with a red flash
            for r in rows:
                if r.key not in self._conflict_keys:
                    self.motion.start(("conflict", r.key), 0.9)
        self._conflict_keys = {r.key for r in rows}
        self.query_one("#conflicts", ConflictsPanel).show(rows, progress=self.motion.progress)

    # -- selection ------------------------------------------------------------------------
    def selected_name(self) -> str | None:
        try:
            return self.table.selected()
        except Exception:  # noqa: BLE001
            return None

    def select(self, name: str) -> None:
        self.table.select(name)
        self.render_all()

    def row_text(self, name: str) -> str:
        return self.table.row_plain(name)

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if self.peek_on:
            self.peek_lines = None
        self.render_all()

    def _selected_view(self, need_alive: bool | None = None):
        name = self.selected_name()
        v = self.dash.get(name) if name else None
        if v is None:
            self.notify("No agent selected.", severity="warning")
            return None
        if v.status == "untracked" and need_alive is not None:
            self.notify(f"{name} is an untracked worktree; press A to adopt it first.", severity="warning")
            return None
        if need_alive is True and not v.alive:
            self.notify(f"{name} is not running. Press r to restart it.", severity="warning")
            return None
        if need_alive is False and v.alive:
            self.notify(f"{name} is still running.", severity="warning")
            return None
        return v

    # -- actions --------------------------------------------------------------------------
    def action_lead(self) -> None:
        """Say what you want built; the lead turns it into agents (lead.py)."""
        modal = TextInputModal("Tell the lead", modal_id="lead",
                               placeholder="what do you want built? e.g. Add CSV export to the reports page",
                               hint="The lead splits it into tasks and starts an agent for each; they appear in the "
                                    "table. Leave it empty to just start the lead, then attach to it with a.")
        self.push_screen(modal, self._tell_lead)

    def _tell_lead(self, text: str | None) -> None:
        if text is None:
            return
        from .. import lead

        v = self.dash.get(lead.LEAD)
        if v is None or not v.alive:
            self.notify("Starting the lead…")
        self._run(lambda: lead.tell(self.ws, text, self.dash))

    def action_new(self) -> None:
        taken = set(self.dash.views) | self.ws._taken(self.ws.registry.load())
        modal = NewAgentModal(taken, self.ws.default_branch, self.ws.cfg.model, self.ws.cfg.permission_mode)
        self.push_screen(modal, callback=self._create)

    def _create(self, params: dict | None) -> None:
        if params:
            self.dash.progress[params["name"]] = Progress("worktree", 0.0)
            self.create_agent(params)

    @work(thread=True)
    def create_agent(self, params: dict) -> None:
        name = params["name"]

        def progress(step: str, frac: float) -> None:
            self.dash.progress[name] = Progress(step, frac)
            self.call_from_thread(self.request_refresh)

        try:
            self.ws.new_agent(params["task"], name=name, base=params.get("base"), model=params.get("model"),
                              permission_mode=params.get("permission_mode"), progress=progress)
            self.call_from_thread(self.notify, f"Started {name}.")
        except (WorkspaceError, RuntimeError) as exc:
            self.call_from_thread(self.notify, str(exc), title="Could not start agent", severity="error", timeout=8)
        finally:
            self.dash.progress.pop(name, None)
            self.dash.refresh_all()
            self.call_from_thread(self._select_and_render, name)

    def _select_and_render(self, name: str) -> None:
        self.render_all()
        self.table.select(name)
        self.render_all()

    def action_stop(self) -> None:
        v = self._selected_view(need_alive=True)
        if v:
            self.notify(f"Stopping {v.name}…")
            self._run(lambda: self.ws.stop(v.name), done=f"Stopped {v.name}; worktree and branch kept.")

    def action_restart(self) -> None:
        v = self._selected_view(need_alive=False)
        if v:
            self._run(lambda: self.ws.restart(v.name), done=f"Restarted {v.name}.")

    @work(thread=True)
    def _run(self, fn, done: str = "") -> None:
        try:
            result = fn()
            msg = done or (result if isinstance(result, str) else "")
            if msg:
                self.call_from_thread(self.notify, msg)
        except (WorkspaceError, RuntimeError) as exc:
            self.call_from_thread(self.notify, str(exc), severity="error", timeout=8)
        self.dash.refresh_all()
        self.call_from_thread(self.render_all)

    def action_attach(self) -> None:
        v = self._selected_view(need_alive=True)
        if not v:
            return
        mode = self.ws.cfg.open_in
        if mode != "none":  # a new terminal tab instead of handing over this terminal
            from .. import terminals

            if terminals.open_tab(mode, v.name, self.ws.repo.main_root):
                self.notify(f"Opened {v.name} in a new tab.")
                return
            if not self._tab_hint_shown:
                self._tab_hint_shown = True
                self.notify(terminals.VSCODE_NOTE.format(name=v.name) if mode == "vscode" else
                            "Windows Terminal (wt.exe) was not found; attaching here.", timeout=10)
        if self._attach_explained:
            self._attach(v.name)
            return
        # The agent's screen takes over the whole terminal and shows no sign of the dashboard: say
        # once, before it happens, what this is and which key comes back.
        hint = self.detach_hint()
        note = (f"This hands the terminal to {v.name}'s own Claude Code session. The dashboard waits behind it, "
                f"and the agent keeps running either way.\n\nTo come back: {hint}.")
        modal = ChoiceModal(f"Attach to {v.name}", [("enter", "attach", f"attach · {hint} comes back")], note=note,
                            modal_id="attach")
        self.push_screen(modal, lambda answer: answer == "attach" and self._attach(v.name, explained=True))

    def detach_hint(self) -> str:
        """The keys that leave an attached session, for this runner and this keyboard."""
        kind = self.ws.runner.kind
        if kind == "winpty":
            from ..attach import detach_hint

            return detach_hint()
        return "Ctrl-b d" if kind == "tmux" else "← on an empty prompt, or Ctrl+Z"

    def _attach(self, name: str, explained: bool = False) -> None:
        if explained:
            self._attach_explained = True
        fn = self.attach_fn or self.ws.attach
        driver = getattr(self, "_driver", None)
        if self.attach_fn is None and driver is not None and driver.can_suspend:
            with self.suspend():
                print(f"\x1b[2mattached to {name} · {self.detach_hint()} comes back\x1b[0m", flush=True)
                fn(name)
        else:
            fn(name)
        self.request_refresh()

    def action_delete(self) -> None:
        v = self._selected_view()
        if v is None:
            return
        if v.status == "untracked":
            self.notify("Untracked worktrees are not deleted by ccdash; adopt it first (A).", severity="warning")
            return
        self._check_delete(v.name)

    @work(thread=True)
    def _check_delete(self, name: str) -> None:
        try:
            check = self.ws.delete_check(name)
        except WorkspaceError as exc:
            self.call_from_thread(self.notify, str(exc), severity="error")
            return
        self.call_from_thread(self.push_screen, ConfirmDeleteModal(check),
                              lambda confirm: confirm and self._run(lambda: self.ws.delete(name, confirm=confirm),
                                                                    done=f"Deleted {name}."))

    def action_toggle_hidden(self) -> None:
        self.dash.show_hidden = not self.dash.show_hidden
        self.notify("Showing deleted agents." if self.dash.show_hidden else "Hiding deleted agents.")
        self.request_refresh()

    def action_adopt(self) -> None:
        name = self.selected_name()
        v = self.dash.get(name) if name else None
        if v is None or v.status != "untracked":
            self.notify("Select an untracked worktree to adopt.", severity="warning")
            return
        self._run(lambda: self.ws.adopt(name), done=f"Adopted {name}; press r to start it.")

    def action_help(self) -> None:
        self.push_screen(HelpModal())

    def action_quit_dash(self) -> None:
        self.exit(0)

    def action_peek(self) -> None:
        self.peek_on = not self.peek_on
        if not self.peek_on:
            self.peek_lines = None
            self._render_details()
        else:
            self._peek_worker()

    def action_talk(self) -> None:
        from .. import messaging

        v = self._selected_view(need_alive=True)
        if not v:
            return
        modal = TextInputModal(f"Talk to {v.name}", placeholder="one line; the agent sees it as [from the user] …",
                               hint="Typed into the agent's prompt. Queued while it waits for a permission answer.",
                               modal_id="talk")
        self.push_screen(modal, lambda text: text and text.strip() and self._run(
            lambda: messaging.send_from_user(self.ws, v.name, text, dashboard=self.dash)))

    def action_broadcast(self) -> None:
        from .. import messaging

        if not any(v.alive for v in self.dash.views.values()):
            self.notify("No running agents to broadcast to.", severity="warning")
            return
        modal = TextInputModal("Broadcast to every running agent", placeholder="one line",
                               hint="Each agent sees it as [from the user] …", modal_id="broadcast")
        self.push_screen(modal, lambda text: text and text.strip() and self._run(
            lambda: "; ".join(messaging.broadcast_from_user(self.ws, text, dashboard=self.dash)) or "no recipients"))

    def on_option_list_option_selected(self, event) -> None:
        panel = self.query_one("#messages", MessagesPanel)
        if event.option_list is not panel or event.option.id is None:
            return
        m = panel.messages[int(event.option.id)]
        title = f"{m.sender} → {m.to} · {time.strftime('%H:%M:%S', time.localtime(m.t))}"
        self.push_screen(MessageModal(title, m.text, m.summary))

    def messages_text(self) -> str:
        return self.query_one("#messages", MessagesPanel).plain()

    def details_text(self) -> str:
        t = getattr(self.query_one("#details", DetailsPanel), "last_text", None)
        return t.plain if t is not None else ""

    # -- merge menu ----------------------------------------------------------------------
    def action_merge(self) -> None:
        v = self._selected_view()
        if v is None:
            return
        if v.status == "untracked" or not v.branch:
            self.notify(f"{v.name} has no agent branch to merge.", severity="warning")
            return
        self._open_merge_menu(v.name)

    @work(thread=True)
    def _open_merge_menu(self, name: str) -> None:
        from .. import merge

        v = self.dash.get(name)
        if v is None:
            return
        menu = merge.menu(self.ws, v, self.dash)  # asks git (and gh) what is possible right now
        if not menu.choices:
            self.call_from_thread(self.notify, menu.note, title=f"Nothing to do for {name}", severity="warning",
                                  timeout=8)
            return
        self.call_from_thread(self.push_screen, ChoiceModal(menu.title, menu.choices, note=menu.note, modal_id="merge"),
                              lambda action: action and self._merge_action(name, action))

    def _merge_action(self, name: str, action: str) -> None:
        from .. import merge

        if action == "rebase":
            self._run(lambda: merge.ask_rebase(self.ws, name, self.dash))
        elif action == "local":
            self.notify(f"Merging {name} into {self.ws.default_branch}…")
            self._merge_local(name)
        elif action == "pr":
            self.notify(f"Pushing {name}'s branch and opening a pull request…")
            self._open_pr(name)
        elif action == "cleanup":
            self._cleanup(name)

    @work(thread=True)
    def _merge_local(self, name: str) -> None:
        from .. import merge

        try:
            res = merge.merge_local(self.ws, name, self.dash)
        except (WorkspaceError, RuntimeError) as exc:
            self.call_from_thread(self.notify, str(exc), title="Merge failed", severity="error", timeout=12)
            self.dash.refresh_all()
            self.call_from_thread(self.render_all)
            return
        told = f" Told {', '.join(res.notified)}." if res.notified else ""
        self.call_from_thread(self.notify, f"{name} landed on {self.ws.default_branch} ({res.sha[:7]}).{told}",
                              title="Merged")
        self.dash.refresh_runtime()
        self.call_from_thread(self.render_all)
        self.call_from_thread(self._offer_cleanup, name)

    def _offer_cleanup(self, name: str) -> None:
        modal = ChoiceModal(f"{name} is merged", [("y", "yes", "delete its worktree and branch"),
                                                  ("n", "no", "keep them (x deletes later)")], modal_id="cleanup")
        self.push_screen(modal, lambda answer: answer == "yes" and self._cleanup(name))

    @work(thread=True)
    def _cleanup(self, name: str) -> None:
        """Delete a merged agent; if work turned up that would be lost, ask the usual way."""
        try:
            check = self.ws.delete_check(name)
            if check.needs_confirmation:
                self.call_from_thread(self.push_screen, ConfirmDeleteModal(check),
                                      lambda confirm: confirm and self._run(
                                          lambda: self.ws.delete(name, confirm=confirm), done=f"Deleted {name}."))
                return
            self.ws.delete(name)
            self.call_from_thread(self.notify, f"Deleted {name}: worktree and branch removed.")
        except (WorkspaceError, RuntimeError) as exc:
            self.call_from_thread(self.notify, str(exc), severity="error", timeout=8)
        self.dash.refresh_all()
        self.call_from_thread(self.render_all)

    @work(thread=True)
    def _open_pr(self, name: str) -> None:
        from .. import merge

        try:
            url = merge.open_pr(self.ws, name, self.dash)
        except (WorkspaceError, RuntimeError) as exc:
            self.call_from_thread(self.notify, str(exc), title="Pull request failed", severity="error", timeout=12)
            return
        self.call_from_thread(self.notify, url or f"Opened a pull request for {name}.", title="PR opened", timeout=10)
        self.dash.refresh_git()
        self.call_from_thread(self.render_all)

    # -- diff, filter, claims -----------------------------------------------------------------
    def action_diff(self) -> None:
        v = self._selected_view()
        if v is None:
            return
        if not v.branch:
            self.notify(f"{v.name} has no branch to diff.", severity="warning")
            return
        self._diff(v.name, v.branch, v.dirty)

    @work(thread=True)
    def _diff(self, name: str, branch: str, dirty: int) -> None:
        from .. import merge

        default = self.ws.default_branch
        if not merge.has_diff(self.ws, branch):
            tail = f" Its {dirty} uncommitted change(s) are only in the worktree." if dirty else ""
            self.call_from_thread(self.notify, f"{branch} has no commits that differ from {default}.{tail}")
            return
        argv, env = merge.diff_command(self.ws, branch)
        self.call_from_thread(self.run_external, argv, env, f"{default}...{branch} · q returns to ccdash")

    def run_external(self, argv: list[str], env: dict | None = None, banner: str = "") -> None:
        """Give the terminal to another program (a pager), like attach does, then come back."""
        import subprocess

        if self.external_fn is not None:
            self.external_fn(argv)
            return
        driver = getattr(self, "_driver", None)
        if driver is None or not driver.can_suspend:
            self.notify("This terminal cannot be handed over; run it yourself: " + " ".join(argv), severity="warning",
                        timeout=10)
            return
        with self.suspend():
            if banner:
                print(f"\x1b[2m{banner}\x1b[0m", flush=True)
            subprocess.call(argv, cwd=str(self.ws.repo.main_root), env=env)
        self.request_refresh()

    def action_filter(self) -> None:
        modal = TextInputModal("Filter agents", placeholder="name, status or file path · empty shows all",
                               value=self.dash.filter, modal_id="filter",
                               hint="Matches the name, the status, the task, and files the agent changed.")
        self.push_screen(modal, self._set_filter)

    def _set_filter(self, text: str | None) -> None:
        if text is None:  # esc keeps the filter
            return
        self.dash.filter = text.strip()
        self.render_all()

    def action_claim(self) -> None:
        v = self._selected_view()
        tracker = self.dash.conflicts
        if v is None or tracker is None:
            return
        if v.status == "untracked":
            self.notify(f"{v.name} is an untracked worktree; press A to adopt it first.", severity="warning")
            return
        modal = TextInputModal(f"Paths {v.name} owns", value=" ".join(tracker.user_claims(v.name)), modal_id="claim",
                               placeholder="paths or globs, separated by spaces · empty clears them",
                               hint="Another agent that touches or claims one of them shows as an overlap. "
                                    "Agents announce their own scope with a `Files:` line.")
        self.push_screen(modal, lambda text: text is not None and self._claim(v.name, text))

    def _claim(self, name: str, text: str) -> None:
        paths = self.dash.conflicts.claim(name, text.split())
        self.notify(f"{name} owns {', '.join(paths)}." if paths else f"Cleared your claims for {name}.")
        self.request_refresh()


def open_workspaces(cwd: Path | str | None = None) -> list[Workspace]:
    """The repo you are in, then the `repos` of the config: each once, and a path that is not
    a usable repository is left out rather than stopping the dashboard."""
    ws = Workspace.open(cwd)
    out, seen = [ws], {ws.repo.main_root}
    for path in ws.cfg.repos:
        try:
            other = Workspace.open(Path(path).expanduser())
        except Exception:  # noqa: BLE001  (not a repo, a broken config, no runner)
            continue
        if other.repo.main_root not in seen:
            seen.add(other.repo.main_root)
            out.append(other)
    return out


def run(no_anim: bool = False, cwd: Path | None = None) -> int:
    spaces = open_workspaces(cwd)
    app = CCDashApp(spaces[0], no_anim=no_anim, others=spaces[1:])
    result = app.run()
    return int(result or 0)
