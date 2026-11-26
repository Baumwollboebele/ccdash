"""TUI tests driven by Textual's Pilot (real git, real runner, fake claude)."""

import sys

import pytest

from ccdash.tui.app import CCDashApp
from ccdash.workspace import Workspace

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows runner (tmux variant runs in CI)")
SIZE = (120, 35)


async def settle(pilot, app, rounds=3):
    """Wait for the app's finite workers (not the endless event watcher)."""
    for _ in range(rounds):
        await app.workers.wait_for_complete([w for w in app.workers if w.group not in ("watch",)])
        await pilot.pause(0.05)


async def until(pilot, app, predicate, timeout=10.0):
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        await app.refresh_now()
        await pilot.pause(0.05)
        if predicate():
            return
    raise AssertionError("condition not reached in the TUI")


def table_names(app):
    return [str(k.value) for k in app.table.rows]


async def test_table_lists_agents_and_header_counts(repo, ws):
    ws.new_agent("Fix login redirect bug")
    ws.new_agent("Dark mode toggle")
    app = CCDashApp(Workspace.open(repo.root))
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot, app)
        assert set(table_names(app)) == {"fix-login-redirect-bug", "dark-mode-toggle"}
        header = app.header_text()
        assert "my-app" in header and "2 agents" in header
        assert "cc/dark-mode-toggle" in app.row_text("dark-mode-toggle")
        assert "+0 −0" in app.row_text("dark-mode-toggle")
    for n in ("fix-login-redirect-bug", "dark-mode-toggle"):
        ws.stop(n)


async def test_new_agent_modal_creates_agent(repo, ws):
    app = CCDashApp(Workspace.open(repo.root))
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot, app)
        await pilot.press("n")
        await pilot.pause()
        assert app.screen.id == "new-agent"
        await pilot.click("#task")
        await pilot.press(*"Paginate orders endpoint")
        await pilot.pause()
        assert app.screen.query_one("#name").value == "paginate-orders-endpoint"  # prefilled slug
        await pilot.press("enter")
        await settle(pilot, app)
        await until(pilot, app, lambda: "paginate-orders-endpoint" in table_names(app))
        await until(pilot, app, lambda: ws.runner.is_alive(ws.record("paginate-orders-endpoint")))
    ws.stop("paginate-orders-endpoint")


async def test_stop_restart_delete_from_tui(repo, ws, set_scenario):
    set_scenario({"busy-agent": [{"write": "wip.txt", "text": "wip\n"}], "*": []})
    ws.new_agent("busy agent", name="busy-agent")
    ws.new_agent("idle agent", name="idle-agent")
    app = CCDashApp(Workspace.open(repo.root))
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot, app)
        app.select("idle-agent")
        await pilot.press("s")
        await until(pilot, app, lambda: "stopped" in app.row_text("idle-agent"))
        await pilot.press("r")
        await until(pilot, app, lambda: ws.runner.is_alive(ws.record("idle-agent")))

        await pilot.press("x")  # clean + merged: deleted after a simple yes
        await settle(pilot, app)
        assert app.screen.id == "confirm-delete"
        await pilot.press("y")
        await until(pilot, app, lambda: "idle-agent" not in table_names(app))

        app.select("busy-agent")
        await pilot.press("x")  # uncommitted work: must type the name
        await settle(pilot, app)
        assert app.screen.id == "confirm-delete"
        assert "uncommitted" in app.screen.query_one("#lost").render().plain
        await pilot.press("y", "enter")
        await pilot.pause()
        assert app.screen.id == "confirm-delete", "a plain yes is not enough for unmerged work"
        await pilot.click("#confirm-name")
        await pilot.press(*"busy-agent", "enter")
        await until(pilot, app, lambda: "busy-agent" not in table_names(app))
    assert not (repo.root / ".claude" / "worktrees" / "busy-agent").exists()


async def test_attach_suspends_and_quit_keeps_agents(repo, ws):
    ws.new_agent("attach me", name="attach-me")
    calls = []
    app = CCDashApp(Workspace.open(repo.root), attach_fn=lambda name: calls.append(name) or 0)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot, app)
        app.select("attach-me")
        await pilot.press("enter")
        await settle(pilot, app)
        assert calls == ["attach-me"]
        await pilot.press("q")
    assert ws.runner.is_alive(ws.record("attach-me")), "quitting the TUI never stops agents"
    app2 = CCDashApp(Workspace.open(repo.root))
    async with app2.run_test(size=SIZE) as pilot:
        await settle(pilot, app2)
        assert table_names(app2) == ["attach-me"]
        assert "running" in app2.row_text("attach-me") or "idle" in app2.row_text("attach-me")
    ws.stop("attach-me")


async def test_narrow_terminal_stacks_panels(repo, ws):
    app = CCDashApp(Workspace.open(repo.root))
    async with app.run_test(size=(90, 40)) as pilot:
        await settle(pilot, app)
        assert app.has_class("narrow")
    app = CCDashApp(Workspace.open(repo.root))
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot, app)
        assert not app.has_class("narrow")


# -- Phase 2: live status, messages, talk, broadcast, peek -------------------------------
async def wait_auto(pilot, predicate, timeout=4.0):
    """Wait WITHOUT forcing a refresh: the app's own event watching must get there."""
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        await pilot.pause(0.1)
        if predicate():
            return
    raise AssertionError("the TUI did not update by itself")


async def test_live_status_messages_and_needs_input_on_top(repo, ws, set_scenario):
    set_scenario({"sender": [{"sleep": 1.5}, {"send": {"to": "asker", "message": "Files: src/api/**\nheads up"}},
                             {"say": "DONE: sent"}],
                  "asker": [{"sleep": 2.5}, {"permission": "Write", "path": "x.txt"}], "*": []})
    ws.new_agent("asks", name="asker")
    ws.new_agent("sends", name="sender")
    app = CCDashApp(Workspace.open(repo.root))
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot, app)
        await wait_auto(pilot, lambda: "finished" in app.row_text("sender"), timeout=6)
        await wait_auto(pilot, lambda: "needs input" in app.row_text("asker"), timeout=6)
        assert table_names(app)[0] == "asker", "needs input sorts to the top"
        assert "1 needs input" in app.header_text()
        assert any(m.sender == "sender" and m.to == "asker" for m in app.dash.messages.items)
        assert "sender → asker" in app.messages_text()
        app.select("sender")
        assert "SendMessage → asker" in app.details_text()
    for n in ("asker", "sender"):
        ws.stop(n)


async def test_talk_broadcast_and_peek_keys(repo, ws):
    ws.new_agent("one", name="one")
    ws.new_agent("two", name="two")
    app = CCDashApp(Workspace.open(repo.root))
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot, app)
        await until(pilot, app, lambda: "idle" in app.row_text("one") and "idle" in app.row_text("two"))
        app.select("one")
        await pilot.press("t")
        await pilot.pause()
        assert app.screen.id == "talk"
        await pilot.press(*"use the AuthGuard", "enter")
        await settle(pilot, app)
        await until(pilot, app, lambda: "ack: [from the user] use the AuthGuard" in "\n".join(ws.peek("one", 60)))
        await pilot.press("b")
        await pilot.pause()
        assert app.screen.id == "broadcast"
        await pilot.press(*"rebase please", "enter")
        await settle(pilot, app)
        await until(pilot, app, lambda: "rebase please" in "\n".join(ws.peek("two", 60)))
        await until(pilot, app, lambda: "you → all" in app.messages_text() and "you → one" in app.messages_text())
        await pilot.press("p")
        await until(pilot, app, lambda: "live output" in app.details_text() and "rebase please" in app.details_text())
        await pilot.press("p")
        await until(pilot, app, lambda: "live output" not in app.details_text())
    for n in ("one", "two"):
        ws.stop(n)
