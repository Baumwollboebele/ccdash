"""Snapshot tests (pytest-textual-snapshot): main screen, each modal, the Network and
Comms views, the narrow layout, and reduced motion. Deterministic: frozen demo data,
fixed clock, RGB theme, no animation. Update with: pytest tests/test_snapshots.py --snapshot-update
"""

import pytest

from ccdash.conflicts import Row
from ccdash.lead import TASK as LEAD_TASK
from ccdash.messaging import Message
from ccdash.tui import theme
from ccdash.tui.app import CCDashApp
from ccdash.workspace import AgentView, DeleteCheck, Workspace

BASE = 1_795_000_000.0
CLOCK = {}


def at(minute: int, second: int = 0) -> float:
    ts = BASE + minute * 60 + second
    CLOCK[round(ts)] = f"19:{minute:02d}"
    return ts


@pytest.fixture(autouse=True)
def frozen(monkeypatch, sandbox):
    monkeypatch.setenv("CCDASH_THEME", "dark-rgb")
    monkeypatch.setenv("CCDASH_REDUCED_MOTION", "1")
    monkeypatch.setattr(theme, "fmt_clock", lambda ts: CLOCK.get(round(ts), "19:52"))
    monkeypatch.setattr("time.time", lambda: BASE + 52 * 60)


def demo_views(root):
    def v(name, status, task, ahead, behind, files, state, **kw):
        return AgentView(name=name, status=status, task=task, lifecycle="running", alive=status != "stopped",
                         branch=f"cc/{name}", worktree=str(root / ".claude" / "worktrees" / name), base_ref="main",
                         base_sha="3f2a9c1", created_at=name, ahead=ahead, behind=behind, files=files,
                         merge_state=state, **kw)

    return [
        AgentView(name="lead", role="lead", status="idle", task=LEAD_TASK, lifecycle="running", alive=True),
        v("csv-export", "working", "Add CSV export to reports", 4, 0, 7, "dirty", dirty=2,
          dirty_paths=["src/reports/export.ts", "src/ui/Toolbar.tsx"], last_subject="add writer"),
        v("login-redirect", "needs_input", "Fix login redirect bug", 2, 3, 3, "conflict"),
        v("dark-mode", "working", "Dark mode toggle", 1, 0, 12, "dirty", dirty=5),
        v("api-pagination", "finished", "Paginate /orders endpoint", 6, 0, 5, "ready"),
        v("flaky-test", "stopped", "Fix flaky settings test", 0, 0, 0, "clean"),
    ]


def demo_messages():
    return [
        Message(t=at(41), sender="csv-export", to="dark-mode", delivered=True,
                text="Files: src/reports/** src/ui/Toolbar.tsx\nI'm adding ExportButton to src/ui/Toolbar.tsx"),
        Message(t=at(42), sender="dark-mode", to="csv-export", delivered=True,
                text="Ack. I only touch theme.ts and Toolbar styles."),
        Message(t=at(47), sender="api-pagination", to="all", delivered=True,
                recipients=["csv-export", "dark-mode", "login-redirect"],
                text="Landed: /orders now returns {items, next}. Rebase if you call it."),
        Message(t=at(50), sender="you", to="login-redirect", kind="you", delivered=True,
                text="Use the existing AuthGuard, don't add a new one."),
    ]


def demo_conflicts():
    return [
        Row("merge", ("login-redirect", "main"), "src/auth/session.ts (both changed)", "red",
            files=("src/auth/session.ts",)),
        Row("overlap", ("csv-export", "dark-mode"), "src/ui/Toolbar.tsx", "yellow", files=("src/ui/Toolbar.tsx",)),
        Row("shared", ("api-pagination",), "src/api/**  (3 agents notified {when})", "blue", ident="src/api/**",
            files=("src/api/orders.ts",), when=at(47)),
    ]


TOUCHED = {
    "csv-export": {"src/reports/export.ts", "src/reports/csv/writer.ts", "src/ui/Toolbar.tsx"},
    "dark-mode": {"src/ui/theme.ts", "src/ui/Toolbar.tsx", "src/ui/Toolbar.css"},
    "login-redirect": {"src/auth/session.ts", "src/auth/guard.ts"},
    "api-pagination": {"src/api/orders.ts", "src/api/paging.ts", "README.md"},
}


def demo_app(repo, **kw):
    app = CCDashApp(Workspace.open(repo.root), no_anim=True, **kw)
    d = app.dash
    d.refresh_runtime = lambda: None
    d.refresh_git = lambda: None
    d.refresh_events = lambda: 0
    d.views = {x.name: x for x in demo_views(repo.root)}
    d.messages.items = demo_messages()
    d.default_sha = "3f2a9c1"
    d.conflicts.rows = demo_conflicts()  # the tracker is never updated here: the refreshes above do nothing
    d.conflicts._paths = {name: set(paths) for name, paths in TOUCHED.items()}
    d.conflicts._announced = {"csv-export": ["src/reports/**", "src/ui/Toolbar.tsx"]}
    return app


async def select_csv(pilot):
    pilot.app.select("csv-export")
    await pilot.pause(0.1)


def test_main_screen(repo, snap_compare):
    assert snap_compare(demo_app(repo), terminal_size=(120, 35), run_before=select_csv)


def test_narrow_layout(repo, snap_compare):
    assert snap_compare(demo_app(repo), terminal_size=(90, 40), run_before=select_csv)


def test_new_agent_modal(repo, snap_compare):
    async def before(pilot):
        await select_csv(pilot)
        await pilot.press("n")
        await pilot.pause(0.1)
        await pilot.press(*"Add audit log to the orders API")
        await pilot.pause(0.1)

    assert snap_compare(demo_app(repo), terminal_size=(120, 35), run_before=before)


def test_delete_modal_unmerged(repo, snap_compare):
    from ccdash.tui.modals import ConfirmDeleteModal

    async def before(pilot):
        await select_csv(pilot)
        check = DeleteCheck(name="csv-export", needs_confirmation=True, running=True,
                            lost=["2 uncommitted change(s) in the worktree", "4 commit(s) not merged into main",
                                  "4 commit(s) not pushed to any remote"])
        pilot.app.push_screen(ConfirmDeleteModal(check))
        await pilot.pause(0.1)

    assert snap_compare(demo_app(repo), terminal_size=(120, 35), run_before=before)


def test_talk_modal(repo, snap_compare):
    async def before(pilot):
        pilot.app.select("login-redirect")
        await pilot.pause(0.05)
        await pilot.press("t")
        await pilot.pause(0.1)

    assert snap_compare(demo_app(repo), terminal_size=(120, 35), run_before=before)


def test_help_modal(repo, snap_compare):
    assert snap_compare(demo_app(repo), terminal_size=(120, 35), press=["question_mark"])


def test_network_view(repo, snap_compare):
    async def before(pilot):
        await select_csv(pilot)
        await pilot.press("g")
        await pilot.pause(0.1)

    assert snap_compare(demo_app(repo), terminal_size=(120, 35), run_before=before)


def test_comms_view(repo, snap_compare):
    async def before(pilot):
        await pilot.press("c")
        await pilot.pause(0.1)

    assert snap_compare(demo_app(repo), terminal_size=(120, 35), run_before=before)


def test_file_map_view(repo, snap_compare):
    async def before(pilot):
        await select_csv(pilot)
        await pilot.press("f")
        await pilot.pause(0.1)

    assert snap_compare(demo_app(repo), terminal_size=(120, 35), run_before=before)


def test_merge_menu(repo, snap_compare):
    from ccdash.tui.modals import ChoiceModal

    async def before(pilot):
        pilot.app.select("api-pagination")
        await pilot.pause(0.05)
        choices = [("r", "rebase", "ask api-pagination to rebase on main"),
                   ("l", "local", "merge into main locally, if `npm test` passes"),
                   ("p", "pr", "push the branch and open a pull request")]
        pilot.app.push_screen(ChoiceModal("Merge api-pagination", choices, modal_id="merge",
                                          note="cc/api-pagination · +6 −0 main · ready"))
        await pilot.pause(0.1)

    assert snap_compare(demo_app(repo), terminal_size=(120, 35), run_before=before)
