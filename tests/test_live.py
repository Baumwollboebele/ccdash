"""Phase 2 acceptance: live status, needs input, messages, talk/broadcast and peek,
end to end through real hosts and hooks (fake claude)."""

import sys
import time

import pytest

from ccdash import messaging
from ccdash.dashboard import Dashboard
from ccdash.workspace import Workspace

from conftest import wait_for

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows runner (tmux variant runs in CI)")


def live(ws):
    d = Dashboard(ws)
    d.refresh_runtime()
    return d


def status(d, name):
    d.refresh_events()
    d.refresh_runtime()
    v = d.get(name)
    return v.status if v else None


@pytest.mark.parametrize("transport", ["http", "command"])
def test_status_follows_hooks_within_2s(repo, set_scenario, transport):
    repo.write(".ccdash.toml", f'hook_transport = "{transport}"\n')
    set_scenario({"alpha": [{"sleep": 1.0}, {"edit": "src/ui/Toolbar.tsx", "text": "// x\n"}, {"sleep": 1.0},
                            {"say": "DONE: toolbar updated"}], "*": []})
    ws = Workspace.open(repo.root)
    d = live(ws)
    t0 = time.monotonic()
    ws.new_agent("alpha task", name="alpha")
    wait_for(lambda: status(d, "alpha") == "working", timeout=4, message="working")
    wait_for(lambda: d.refresh_events() is not None and d.activity.get("alpha").last.startswith("Edit src/ui/Toolbar.tsx"),
             timeout=4, message="last edit")
    wait_for(lambda: status(d, "alpha") == "finished", timeout=6, message="finished")
    assert time.monotonic() - t0 < 8
    assert d.activity.get("alpha").touched == {"src/ui/Toolbar.tsx"}
    if transport == "http":
        assert ws.runner.host_info(ws.record("alpha")).get("hook_port")
    ws.stop("alpha")
    wait_for(lambda: status(d, "alpha") == "stopped", timeout=4, message="stopped")


def test_permission_prompt_needs_input_and_sorts_first(repo, ws, set_scenario):
    set_scenario({"asker": [{"sleep": 0.5}, {"permission": "Write", "path": "out.txt"}], "*": []})
    ws.new_agent("quiet one", name="aaa-quiet")
    ws.new_agent("asks for permission", name="asker")
    d = live(ws)
    wait_for(lambda: status(d, "asker") == "needs_input", timeout=5, message="needs input")
    t_seen = time.monotonic()
    assert d.sorted_views()[0].name == "asker"
    assert d.counts()["needs_input"] == 1
    # typing must not answer the dialog: user messages are held while it is open
    out = messaging.send_from_user(ws, "asker", "hello", dashboard=d)
    assert "queued" in out
    ws.runner.send_text(ws.record("asker"), "1")  # the user answers the dialog
    wait_for(lambda: status(d, "asker") in ("working", "idle"), timeout=4, message="answered")
    assert time.monotonic() - t_seen < 6
    messaging.flush_outbox(ws, d)
    wait_for(lambda: "ack: [from the user] hello" in "\n".join(ws.peek("asker", 60)), timeout=6, message="queued delivery")


def test_send_message_between_agents_appears(repo, ws, set_scenario):
    set_scenario({"sender": [{"sleep": 0.5}, {"send": {"to": "receiver", "message": "Files: src/api/**\nchanging orders"}}],
                  "*": []})
    ws.new_agent("receiver task", name="receiver")
    ws.new_agent("sender task", name="sender")
    d = live(ws)
    wait_for(lambda: d.refresh_events() is not None and any(m.sender == "sender" and m.to == "receiver"
                                                            for m in d.messages.items),
             timeout=5, message="message in log")
    m = [m for m in d.messages.items if m.sender == "sender"][0]
    assert "changing orders" in m.text
    assert d.activity.get("sender").claimed == ["src/api/**"]


def test_talk_and_broadcast(repo, ws):
    ws.new_agent("one", name="one")
    ws.new_agent("two", name="two")
    d = live(ws)
    wait_for(lambda: status(d, "one") == "idle" and status(d, "two") == "idle", timeout=5, message="idle")
    messaging.send_from_user(ws, "one", "use the AuthGuard", dashboard=d)
    wait_for(lambda: "ack: [from the user] use the AuthGuard" in "\n".join(ws.peek("one", 60)), timeout=5,
             message="talk delivered")
    sent = messaging.broadcast_from_user(ws, "rebase on main please", dashboard=d)
    assert len(sent) == 2
    for n in ("one", "two"):
        wait_for(lambda n=n: "rebase on main please" in "\n".join(ws.peek(n, 60)), timeout=5, message=f"{n} broadcast")
    d.refresh_events()
    kinds = [(m.sender, m.to) for m in d.messages.items]
    assert ("you", "one") in kinds and ("you", "all") in kinds


def test_peek_shows_live_output(repo, ws, set_scenario):
    set_scenario([{"print": "Writing src/reports/export.ts …"}])
    ws.new_agent("peek me", name="peeky")
    wait_for(lambda: "Writing src/reports/export.ts" in "\n".join(ws.peek("peeky", 30)), timeout=5, message="peek")
    assert len(ws.peek("peeky", 30)) <= 30


def test_no_hooks_hint_when_sessionstart_never_arrives(repo, monkeypatch):
    repo.write(".ccdash.toml", 'hook_transport = "command"\n')
    ws = Workspace.open(repo.root)
    monkeypatch.setattr("ccdash.hooks_settings.for_agent", lambda ws, rec: None)  # agent without hooks
    ws.new_agent("no hooks", name="nohooks")
    d = live(ws)
    d.no_hooks_after = 1.0
    wait_for(lambda: status(d, "nohooks") == "running" and d.get("nohooks").hooks_missing, timeout=5,
             message="no-hooks hint")
