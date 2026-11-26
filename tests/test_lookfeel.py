"""Phase 3: animations, reduced motion, network and comms views, idle CPU.

These tests freeze the dashboard data (no agents, no refresh) so what is drawn is deterministic."""

import time

import pytest

from ccdash import comms
from ccdash.messaging import Message
from ccdash.tui.app import CCDashApp
from ccdash.tui.theme import SPINNER
from ccdash.workspace import AgentView, Workspace

SIZE = (120, 35)
NOW = time.time()


@pytest.fixture(autouse=True)
def motion_on(sandbox, monkeypatch):
    """These tests are about motion: undo the sandbox's reduced-motion default (and an inherited NO_COLOR)."""
    monkeypatch.delenv("CCDASH_REDUCED_MOTION", raising=False)
    monkeypatch.delenv("NO_COLOR", raising=False)


def view(name, status="idle", **kw):
    base = dict(task=f"task of {name}", lifecycle="running", alive=status not in ("stopped", "crashed"),
                branch=f"cc/{name}", worktree=f"/w/{name}", base_ref="main", base_sha="3f2a9c1", created_at=name,
                merge_state="clean")
    base.update(kw)
    return AgentView(name=name, status=status, **base)


def frozen_app(repo, views, messages=(), no_anim=False, **app_kw):
    app = CCDashApp(Workspace.open(repo.root), no_anim=no_anim, **app_kw)
    d = app.dash
    d.refresh_runtime = lambda: None
    d.refresh_git = lambda: None
    d.refresh_events = lambda: 0
    d.views = {v.name: v for v in views}
    d.messages.items = list(messages)
    d.default_sha = "3f2a9c1"
    return app


def msg(sender, to, text, ago, kind="agent", recipients=()):
    return Message(t=NOW - ago, sender=sender, to=to, text=text, kind=kind, delivered=True,
                   recipients=list(recipients))


async def ticks(pilot, n=5, dt=0.08):
    for _ in range(n):
        await pilot.pause(dt)


# -- spinner, shimmer, ticker lifecycle --------------------------------------------------
async def test_spinner_animates_working_rows_and_ticker_stops_when_idle(repo):
    app = frozen_app(repo, [view("csv-export", "working"), view("flaky-test", "idle")])
    async with app.run_test(size=SIZE) as pilot:
        await ticks(pilot)
        assert app.motion.running, "something works: the ticker runs"
        seen = set()
        for _ in range(12):
            await pilot.pause(0.07)
            seen.add(app.table.get_cell("csv-export", "status").plain[0])
        assert len(seen & set(SPINNER)) >= 2, f"spinner frames should change, saw {seen}"
        assert "○" in app.row_text("flaky-test"), "idle rows do not animate"
        app.dash.views["csv-export"].status = "idle"
        app.render_all()
        await ticks(pilot)
        assert not app.motion.running, "nothing moves: no timer, no CPU"


async def test_no_anim_is_static(repo):
    app = frozen_app(repo, [view("csv-export", "working", last="Edit src/x.ts"), view("asker", "needs_input")],
                     no_anim=True)
    async with app.run_test(size=SIZE) as pilot:
        await ticks(pilot)
        rows = {app.row_text("csv-export"), app.row_text("asker")}
        await ticks(pilot, 6)
        assert {app.row_text("csv-export"), app.row_text("asker")} == rows, "nothing changes by itself"
        assert "●" in app.row_text("csv-export")
        assert not app.motion.running


async def test_needs_input_row_pulses(repo):
    app = frozen_app(repo, [view("asker", "needs_input")])
    async with app.run_test(size=SIZE) as pilot:
        styles = set()
        for _ in range(14):
            await pilot.pause(0.1)
            cell = app.table.get_cell("asker", "status")
            styles.add(str(cell.spans[0].style) if cell.spans else "")
        assert len(styles) >= 3, f"pulse needs changing shades, saw {styles}"


async def test_commit_tick_and_merged_fade(repo):
    v = view("csv-export", "working", ahead=1)
    app = frozen_app(repo, [v])
    async with app.run_test(size=SIZE) as pilot:
        await ticks(pilot)
        v.ahead = 2
        app.render_all()
        await pilot.pause(0.05)
        assert app.motion.active(("ahead", "csv-export")), "+N ticks up with a flash"
        v.status, v.merge_state, v.merged, v.ahead = "finished", "merged", True, 0
        app.render_all()
        await pilot.pause(0.05)
        assert app.motion.active(("merged", "csv-export")), "row fades to blue"
        await pilot.pause(1.6)
        assert not app.motion.active(("merged", "csv-export"))
        assert "merged" in app.row_text("csv-export")


async def test_setup_progress_bar_in_row(repo):
    from ccdash.dashboard import Progress

    app = frozen_app(repo, [view("new-one", "setup")])
    app.dash.progress["new-one"] = Progress("setup", 0.5)
    async with app.run_test(size=SIZE) as pilot:
        await ticks(pilot, 2)
        assert "▰▰▱▱ setup" in app.row_text("new-one")


# -- network view ---------------------------------------------------------------------
async def test_network_view_nodes_edges_and_flight(repo):
    views = [view("csv-export", "working", ahead=4), view("dark-mode", "working", ahead=1),
             view("login-redirect", "needs_input", ahead=2, behind=3)]
    app = frozen_app(repo, views, messages=[msg("csv-export", "dark-mode", "Files: src/ui/Toolbar.tsx", 30)])
    app.red_pairs = lambda: {frozenset({"csv-export", "login-redirect"})}
    async with app.run_test(size=SIZE) as pilot:
        await pilot.press("g")
        await ticks(pilot, 2)
        net = app.network_text()
        for name in ("main", "csv-export", "dark-mode", "login-redirect", "+4"):
            assert name in net, name
        assert app.network.edge_style("csv-export", "login-redirect") == "red"
        assert app.network.edge_width("csv-export", "dark-mode") >= 1
        app.add_flight("dark-mode", "csv-export")
        await pilot.pause(0.1)
        assert app.network.flights, "a message flies along its edge"
        await pilot.pause(1.6)
        assert not app.network.flights
        await pilot.press("g")
        await pilot.pause(0.05)
        assert not app.network.display


# -- comms view -------------------------------------------------------------------------
def test_threading_rule_pairs_files_silence_and_states():
    msgs = [
        msg("csv-export", "dark-mode", "Files: src/ui/Toolbar.tsx\nI add ExportButton", 900),
        msg("dark-mode", "csv-export", "ok, I only touch the theme styles", 880),
        msg("csv-export", "dark-mode", "now src/api/orders.ts changes too", 860),  # different file: new thread
        msg("csv-export", "dark-mode", "Toolbar.tsx done", 200),  # 5 min silence: new thread
        msg("api-pagination", "all", "Landed: /orders returns {items, next}", 300,
            recipients=["csv-export", "dark-mode"]),
        msg("csv-export", "api-pagination", "thanks, will rebase", 290),
        msg("dark-mode", "api-pagination", "ok", 280),
        msg("you", "login-redirect", "Use the existing AuthGuard", 60, kind="you"),
        msg("ccdash", "all", "api-pagination changed src/api/**", 50, kind="ccdash"),
        msg("login-redirect", "csv-export", "which session helper do you use?", 20),
    ]
    threads = comms.build_threads(msgs, now=NOW)
    states = {(t.pair_label, t.messages[0].text[:12]): t.state for t in threads}
    assert len([t for t in threads if t.pair == frozenset({"csv-export", "dark-mode"})]) == 3
    assert states[("csv-export ↔ dark-mode", "Files: src/u")] == "agreed"
    assert states[("api-pagination → all", "Landed: /ord")] == "acked"
    assert states[("you → login-redirect", "Use the exis")] == "delivered"
    assert states[("csv-export ↔ login-redirect", "which sessio")] == "discussing"
    assert states[("csv-export ↔ dark-mode", "Toolbar.tsx ")] == "open"
    ccdash = [t for t in threads if t.kind == "ccdash"]
    assert len(ccdash) == 1 and ccdash[0].state == "delivered"
    assert threads[0].last_t >= threads[-1].last_t, "newest first"


async def test_comms_view_graph_list_timeline(repo):
    views = [view("csv-export", "working"), view("dark-mode", "idle"), view("api-pagination", "finished")]
    msgs = [msg("csv-export", "dark-mode", "Files: src/ui/Toolbar.tsx", 120),
            msg("dark-mode", "csv-export", "ok", 100),
            msg("api-pagination", "all", "Landed", 60, recipients=["csv-export", "dark-mode"]),
            msg("csv-export", "api-pagination", "ok", 50), msg("dark-mode", "api-pagination", "ok", 40)]
    app = frozen_app(repo, views, messages=msgs)
    async with app.run_test(size=SIZE) as pilot:
        await pilot.press("c")
        await ticks(pilot, 2)
        assert app.has_class("view-comms")
        text = app.comms_text()
        assert "csv-export ↔ dark-mode" in text and "agreed" in text
        assert "api-pagination → all" in text and "acked" in text
        timeline = app.comms.timeline_text()
        for lane in ("csv-export", "dark-mode", "api-pagination"):
            assert lane in timeline
        assert "now" in timeline
        await pilot.press("c")
        await pilot.pause(0.05)
        assert not app.has_class("view-comms")


# -- conflicts flash ---------------------------------------------------------------------
async def test_new_conflict_row_flashes_red(repo):
    from rich.text import Text

    class Row:
        key = ("overlap", "a", "b")

        def render(self, flash=False, slide=0):
            return Text(" " * slide + "⚠ overlap a × b", style="bold white on red" if flash else "yellow")

    app = frozen_app(repo, [view("a", "working"), view("b", "working")])
    async with app.run_test(size=SIZE) as pilot:
        await ticks(pilot, 6)  # after the first refresh: rows appearing now are new collisions
        app.show_conflict_rows([Row()])
        await pilot.pause(0.05)
        assert app.motion.active(("conflict", Row.key))
        await pilot.pause(1.2)
        assert not app.motion.active(("conflict", Row.key))


# -- idle CPU -------------------------------------------------------------------------------
async def test_idle_cpu_under_3_percent(repo):
    app = frozen_app(repo, [view(f"agent-{i}", "idle") for i in range(5)])
    async with app.run_test(size=SIZE) as pilot:
        await ticks(pilot, 5)
        c0, t0 = time.process_time(), time.monotonic()
        await pilot.pause(5.0)
        cpu = (time.process_time() - c0) / (time.monotonic() - t0)
        assert cpu < 0.03, f"idle CPU {cpu:.1%}"
