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
from .modals import ConfirmDeleteModal, HelpModal, MessageModal, NewAgentModal, TextInputModal
from .network import CommsView, NetworkView
from .panels import ConflictsPanel, DetailsPanel, MessagesPanel
from . import theme
from .theme import ACCENT, DIM, THEMES, pick_theme, reduced_motion

MAIN_ACTIONS = {"new", "stop", "restart", "attach", "peek", "talk", "broadcast", "merge", "diff", "delete", "filter",
                "network", "filemap", "comms", "toggle_hidden", "adopt", "help", "quit_dash"}

FOOTER_KEYS = [("n", "new"), ("s", "stop"), ("r", "restart"), ("a", "attach"), ("p", "peek"), ("t", "talk"),
               ("b", "broadcast"), ("m", "merge"), ("d", "diff"), ("x", "delete"), ("?", "help"), ("q", "quit")]


class CCDashApp(App):
    CSS_PATH = "app.tcss"
    TITLE = "ccdash"
    ENABLE_COMMAND_PALETTE = False
    BINDINGS = [
        Binding("n", "new", "new"), Binding("s", "stop", "stop"), Binding("r", "restart", "restart"),
        Binding("a", "attach", "attach"), Binding("p", "peek", "peek"), Binding("t", "talk", "talk"),
        Binding("b", "broadcast", "broadcast"), Binding("m", "merge", "merge"), Binding("d", "diff", "diff"),
        Binding("x", "delete", "delete"), Binding("slash", "filter", "filter"), Binding("g", "network", "network"),
        Binding("f", "filemap", "file map"), Binding("c", "comms", "comms"),
        Binding("H", "toggle_hidden", "hidden"), Binding("A", "adopt", "adopt"),
        Binding("question_mark", "help", "help"), Binding("q", "quit_dash", "quit"),
    ]

    def __init__(self, ws: Workspace, attach_fn=None, no_anim: bool = False):
        super().__init__()
        self.ws = ws
        self.dash = Dashboard(ws)
        self.attach_fn = attach_fn
        self.animate = ws.cfg.animations and not reduced_motion(no_anim)
        self.frame = 0
        self.peek_on = False
        self.peek_lines: list[str] | None = None
        self._booted = False
        self._watch_stop = threading.Event()
        self.motion = Animator(self, enabled=self.animate)
        self.view_mode = "details"  # details | network   (comms is a separate full-screen class)
        self._prev: dict[str, tuple[int, str]] = {}
        self._seen_messages = 0
        self._conflict_rows: list = []
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
                yield DetailsPanel(id="details")
                yield NetworkView(id="network")
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
        self._refreshing = False
        self._refresh_again = False
        self.request_refresh()
        self.refresh_git_worker()
        self.watch_events()
        self.set_interval(2.0, self.request_refresh, name="runtime")
        self.set_interval(10.0, self.refresh_git_worker, name="git")
        self.set_interval(1.0, self._peek_tick, name="peek")
        self.set_interval(30.0, self._render_header, name="clock")

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
        if getattr(self, "_refreshing", False):
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
        try:
            self.dash.refresh_runtime()
            if (self.ws.state.root / "outbox.json").exists():
                from .. import messaging

                if messaging.flush_outbox(self.ws, self.dash):
                    self.dash.refresh_events()
            self._update_peek()
        finally:
            self.call_from_thread(self._refresh_done)

    def _peek_tick(self) -> None:
        if self.peek_on:
            self._peek_worker()

    @work(thread=True, exclusive=True, group="peek")
    def _peek_worker(self) -> None:
        self._update_peek()
        self.call_from_thread(self._render_details)

    @work(thread=True, exclusive=True, group="git")
    def refresh_git_worker(self) -> None:
        self.dash.refresh_git()
        self.call_from_thread(self.render_all)

    async def refresh_now(self) -> None:
        await asyncio.to_thread(self.dash.refresh_all)
        self.render_all()

    def _update_peek(self) -> None:
        name = self.selected_name()
        v = self.dash.get(name) if name else None
        self.peek_lines = self.ws.peek(name, lines=30) if (self.peek_on and v is not None and v.alive) else None

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
        self._seen_messages = len(items)

    def add_flight(self, sender: str, to: str) -> None:
        if self.animate:
            self.network.add_flight(sender, to)
            self.motion.start(("flight", sender, to, time.monotonic()), 1.3)

    def red_pairs(self) -> set:
        if self.dash.conflicts is None:
            return set()
        return self.dash.conflicts.red_pairs()

    def show_conflict_rows(self, rows: list) -> None:
        self._conflict_rows = rows
        self._render_conflicts()
        self.motion.ensure()

    @property
    def network(self) -> NetworkView:
        return self.query_one("#network", NetworkView)

    @property
    def comms(self) -> CommsView:
        return self.query_one("#comms", CommsView)

    def network_text(self) -> str:
        return self.network.last_text.plain

    def comms_text(self) -> str:
        return self.comms.list_text()

    def _render_views(self, views) -> None:
        if self.view_mode == "network":
            self.network.show(views, self.dash.messages.items, self.red_pairs(), animate=self.animate,
                              merged_progress=lambda n: self.motion.progress(("merged", n)),
                              default_branch=self.ws.default_branch)
        if self.has_class("view-comms"):
            self.comms.show(views, self.dash.messages.items, animate=self.animate, flights=self.network.flights)

    def action_network(self) -> None:
        self.view_mode = "network" if self.view_mode != "network" else "details"
        net = self.view_mode == "network"
        self.network.display = net
        self.query_one("#details", DetailsPanel).display = not net
        self.query_one("#details-panel").border_title = "Network" if net else (self.selected_name() or "Details")
        self.render_all()

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
        left = Text.assemble((" ccdash", f"bold {ACCENT}"), (" · ", DIM), (self.ws.repo.name, "bold"),
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
        if self.view_mode == "network":
            return
        panel.border_title = name or "Details"
        activity = self.dash.activity.get(name) if (self.dash.activity is not None and name) else None
        self.query_one("#details", DetailsPanel).show(
            v, repo_rel=self.ws.repo.rel, default_branch=self.ws.default_branch, default_sha=self.dash.default_sha,
            activity=activity, peek=self.peek_lines if self.peek_on else None, frame=self.frame,
            animate=self.animate, owns=self._owns(name), tests=getattr(activity, "tests", "") if activity else "")

    def _owns(self, name: str | None) -> list[str]:
        if not name or self.dash.conflicts is None:
            return []
        return self.dash.conflicts.owned(name)

    def _render_messages(self) -> None:
        msgs = self.dash.messages.items if self.dash.messages is not None else None
        self.query_one("#messages", MessagesPanel).show(msgs, highlight=self.selected_name())

    def _render_conflicts(self) -> None:
        rows = self.dash.conflicts.rows if self.dash.conflicts is not None else self._conflict_rows
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
        fn = self.attach_fn or self.ws.attach
        driver = getattr(self, "_driver", None)
        if self.attach_fn is None and driver is not None and driver.can_suspend:
            with self.suspend():
                print(f"\x1b[2mattached to {v.name} · Ctrl+] detaches\x1b[0m", flush=True)
                fn(v.name)
        else:
            fn(v.name)
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

    def _not_yet(self, what: str) -> None:
        self.notify(f"{what} is not available yet.", severity="warning")

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

    def action_merge(self) -> None:
        self._not_yet("The merge menu")

    def action_diff(self) -> None:
        self._not_yet("Diff")

    def action_filter(self) -> None:
        self._not_yet("Filter")


    def action_filemap(self) -> None:
        self._not_yet("The file map")




def run(no_anim: bool = False, cwd: Path | None = None) -> int:
    ws = Workspace.open(cwd)
    app = CCDashApp(ws, no_anim=no_anim)
    result = app.run()
    return int(result or 0)
