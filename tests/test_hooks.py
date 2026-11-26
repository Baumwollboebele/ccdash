"""Phase 2 unit tests: hook entry point, event normalization, settings, status derivation, messages."""

import json
import os
import subprocess
import sys
import time

import pytest

from ccdash import hook, hooks_settings
from ccdash.activity import ActivityTracker
from ccdash.messaging import MessageLog
from ccdash.state import EventReader, State

WT = r"C:\repo\.claude\worktrees\csv-export" if sys.platform == "win32" else "/repo/.claude/worktrees/csv-export"


def ev(event, agent="csv-export", ts=None, **payload):
    line = hook.normalize(event, {"hook_event_name": event, "session_id": "s1", **payload}, agent, WT)
    if ts is not None:
        line["t"] = ts
    return line


# -- normalize ------------------------------------------------------------------
def test_normalize_edit_path_relative_to_worktree():
    p = os.path.join(WT, "src", "reports", "export.ts")
    line = ev("PreToolUse", tool_name="Edit", tool_input={"file_path": p, "old_string": "x" * 10000, "new_string": "y"})
    assert line["agent"] == "csv-export" and line["event"] == "PreToolUse" and line["tool"] == "Edit"
    assert line["path"] == "src/reports/export.ts"
    assert line["session_id"] == "s1"
    assert len(json.dumps(line)) < 4500, "raw is capped at about 4 KB"
    assert "ts" in line and isinstance(line["t"], float)


def test_normalize_send_message_and_cross_session_receive():
    line = ev("PreToolUse", tool_name="SendMessage",
              tool_input={"to": "dark-mode", "summary": "toolbar", "message": "Files: src/ui/Toolbar.tsx\nI add a button"})
    assert line["msg"] == {"to": "dark-mode", "summary": "toolbar", "text": "Files: src/ui/Toolbar.tsx\nI add a button"}
    prompt = ('<cross-session-message from="uds:\\\\.\\pipe\\LOCAL\\cc-msg-1" from-name="csv-export" from-mode="prompting">\n'
              "Files: src/ui/Toolbar.tsx\nI add a button\n</cross-session-message>")
    recv = ev("UserPromptSubmit", agent="dark-mode", prompt=prompt)
    assert recv["incoming"] == {"from": "csv-export", "mode": "prompting", "text": "Files: src/ui/Toolbar.tsx\nI add a button"}


def test_normalize_misc_fields():
    assert ev("Notification", notification_type="permission_prompt", message="Claude needs your permission")["kind"] == \
        "permission_prompt"
    assert ev("Stop", last_assistant_message="DONE: exported")["text"] == "DONE: exported"
    assert ev("SessionEnd", reason="prompt_input_exit")["kind"] == "prompt_input_exit"
    assert ev("SessionStart", source="resume", transcript_path="t.jsonl")["kind"] == "resume"
    assert ev("SessionStart", source="startup", transcript_path="t.jsonl")["transcript"] == "t.jsonl"
    bash = ev("PreToolUse", tool_name="Bash", tool_input={"command": "git commit -m 'add writer'"})
    assert bash["cmd"].startswith("git commit")


# -- the hook process -------------------------------------------------------------
def test_hook_main_appends_and_is_silent(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CCDASH_AGENT", "csv-export")
    monkeypatch.setenv("CCDASH_STATE", str(tmp_path))
    monkeypatch.setenv("CCDASH_WORKTREE", WT)
    payload = json.dumps({"hook_event_name": "Stop", "session_id": "s1", "last_assistant_message": "ok"})
    monkeypatch.setattr(sys, "stdin", __import__("io").TextIOWrapper(__import__("io").BytesIO(payload.encode())))
    hook.main(["Stop"])
    assert capsys.readouterr().out == ""
    events = EventReader(State(tmp_path).events_path).read_new()
    assert [(e["agent"], e["event"]) for e in events] == [("csv-export", "Stop")]


def test_hook_survives_missing_state_and_garbage(tmp_path, monkeypatch):
    monkeypatch.setenv("CCDASH_AGENT", "x")
    monkeypatch.setattr(sys, "stdin", __import__("io").TextIOWrapper(__import__("io").BytesIO(b"not json")))
    monkeypatch.setenv("CCDASH_STATE", str(tmp_path / "does" / "not" / "exist"))
    hook.main(["Stop"])  # missing state dir: dropped, no exception
    (tmp_path / "a-file").write_text("x")
    monkeypatch.setenv("CCDASH_STATE", str(tmp_path / "a-file"))
    hook.main(["Stop"])  # state path is a file: dropped, no exception
    assert not (tmp_path / "does").exists()


def test_hook_in_process_cost_under_10ms(tmp_path):
    payload = json.dumps({"hook_event_name": "PreToolUse", "tool_name": "Edit", "session_id": "s",
                          "tool_input": {"file_path": os.path.join(WT, "a.ts"), "new_string": "x" * 3000}}).encode()
    hook.handle("PreToolUse", payload, "a", str(tmp_path), WT)  # warm up
    t = time.perf_counter()
    for _ in range(20):
        hook.handle("PreToolUse", payload, "a", str(tmp_path), WT)
    per_call = (time.perf_counter() - t) / 20 * 1000
    assert per_call < 10, f"{per_call:.1f} ms per hook"


def test_hook_full_process_under_150ms(tmp_path):
    spec = hooks_settings.command_hook("Stop")
    env = dict(os.environ, CCDASH_AGENT="a", CCDASH_STATE=str(tmp_path), CCDASH_WORKTREE=WT)
    payload = json.dumps({"hook_event_name": "Stop", "session_id": "s", "last_assistant_message": "ok"}).encode()
    subprocess.run([spec["command"], *spec["args"]], input=payload, env=env, check=True)  # warm caches
    times = []
    for _ in range(5):
        t = time.perf_counter()
        p = subprocess.run([spec["command"], *spec["args"]], input=payload, env=env, capture_output=True)
        times.append((time.perf_counter() - t) * 1000)
        assert p.returncode == 0 and p.stdout == b""
    assert sorted(times)[2] < 150, times
    assert len(EventReader(State(tmp_path).events_path).read_new()) == 6


# -- settings ---------------------------------------------------------------------
def test_command_settings_exec_form_async():
    s = hooks_settings.render_command()
    for event in ("SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse", "PermissionRequest",
                  "Notification", "Stop", "SessionEnd"):
        h = s["hooks"][event][0]["hooks"][0]
        assert h["type"] == "command" and isinstance(h["args"], list), "exec form: no shell"
        assert os.path.isabs(h["command"])
        assert h.get("async") is (event != "SessionEnd")
    assert "SubagentStop" not in s["hooks"]


def test_http_settings_keep_sessionstart_as_command():
    s = hooks_settings.render_http(port=5555, token="tok")
    start = s["hooks"]["SessionStart"][0]["hooks"][0]
    assert start["type"] == "command"
    pre = s["hooks"]["PreToolUse"][0]["hooks"][0]
    assert pre == {"type": "http", "url": "http://127.0.0.1:5555/hook/tok/PreToolUse", "timeout": 5}
    assert s["hooks"]["SessionEnd"][0]["hooks"][0]["type"] == "http"


def test_transport_choice(repo, ws, sandbox):
    from ccdash.model import AgentRecord

    rec = AgentRecord(name="x", worktree=str(repo.root))
    expected = "http" if ws.runner.kind == "winpty" else "command"
    got = hooks_settings.for_agent(ws, rec)
    assert (got if isinstance(got, str) else "command") == expected
    (sandbox / "claude-settings.json").write_text(json.dumps({"allowedHttpHookUrls": ["https://x/*"]}))
    assert isinstance(hooks_settings.for_agent(ws, rec), dict), "allowlist set: fall back to command hooks"


# -- status derivation ------------------------------------------------------------------
def statuses(seq):
    tr = ActivityTracker()
    out = []
    for i, e in enumerate(seq):
        e["t"] = 1000.0 + i
        tr.apply(e)
        out.append(tr.get("csv-export").status)
    return tr, out


def test_status_sequence_working_needs_input_idle_finished():
    tr, out = statuses([
        ev("SessionStart", source="startup"),
        ev("UserPromptSubmit", prompt="task"),
        ev("PreToolUse", tool_name="Edit", tool_input={"file_path": os.path.join(WT, "src/x.ts")}),
        ev("PermissionRequest", tool_name="Bash", tool_input={"command": "npm test"}),
        ev("Notification", notification_type="permission_prompt", message="Claude needs your permission"),
        ev("PostToolUse", tool_name="Bash", tool_input={"command": "npm test"}),
        ev("SubagentStop"),
        ev("Stop", last_assistant_message="Looking good so far."),
        ev("Notification", notification_type="idle_prompt", message="Claude is waiting for your input"),
        ev("UserPromptSubmit", prompt="finish it"),
        ev("Stop", last_assistant_message="All tests pass.\nDONE: CSV export works"),
        ev("SessionEnd", reason="prompt_input_exit"),
    ])
    assert out == ["idle", "working", "working", "needs_input", "needs_input", "working", "working", "idle", "idle",
                   "working", "finished", "stopped"]
    a = tr.get("csv-export")
    assert a.touched == {"src/x.ts"}
    assert a.last.startswith("Bash")
    assert a.ended


def test_incoming_message_means_working_and_tests_are_tracked():
    tr, out = statuses([
        ev("Stop", last_assistant_message="ok"),
        ev("UserPromptSubmit", prompt='<cross-session-message from="x" from-name="dark-mode">hi</cross-session-message>'),
        ev("PreToolUse", tool_name="Bash", tool_input={"command": "pytest -q"}),
        ev("PostToolUseFailure", tool_name="Bash", tool_input={"command": "pytest -q"}, error="exit 1"),
        ev("PreToolUse", tool_name="Bash", tool_input={"command": "pytest -q"}),
        ev("PostToolUse", tool_name="Bash", tool_input={"command": "pytest -q"}),
    ])
    assert out[1] == "working"
    assert tr.get("csv-export").tests == "passed"


def test_files_claim_from_first_announcement():
    tr, _ = statuses([ev("PreToolUse", tool_name="SendMessage",
                         tool_input={"to": "all", "message": "Files: src/reports/** src/ui/Toolbar.tsx\nplan"})])
    assert tr.get("csv-export").claimed == ["src/reports/**", "src/ui/Toolbar.tsx"]


def test_interrupt_detected_from_transcript(tmp_path):
    transcript = tmp_path / "t.jsonl"
    transcript.write_text(json.dumps({"type": "user", "message": {"content": [{"type": "text", "text": "go"}]}}) + "\n")
    tr = ActivityTracker()
    tr.apply({**ev("SessionStart", source="startup", transcript_path=str(transcript)), "t": time.time() - 30})
    tr.apply({**ev("UserPromptSubmit", prompt="go"), "t": time.time() - 20})
    assert tr.get("csv-export").status == "working"
    tr.check_interrupts(now=time.time())
    assert tr.get("csv-export").status == "working"
    with open(transcript, "a") as f:
        f.write(json.dumps({"type": "user", "message": {"content": [{"type": "text",
                                                                    "text": "[Request interrupted by user]"}]}}) + "\n")
    tr.check_interrupts(now=time.time())
    assert tr.get("csv-export").status == "idle"


def test_stale_working_after_ten_minutes():
    tr = ActivityTracker()
    tr.apply({**ev("UserPromptSubmit", prompt="go"), "t": time.time() - 601})
    assert tr.get("csv-export").stale(time.time())


# -- messages -------------------------------------------------------------------------------
def test_message_log_send_receive_and_user_messages():
    log = MessageLog()
    log.apply(ev("PreToolUse", tool_name="SendMessage", tool_input={"to": "dark-mode", "message": "I'm adding ExportButton"}))
    assert [(m.sender, m.to, m.delivered) for m in log.items] == [("csv-export", "dark-mode", False)]
    log.apply(ev("UserPromptSubmit", agent="dark-mode",
                 prompt='<cross-session-message from="x" from-name="csv-export">I\'m adding ExportButton</cross-session-message>'))
    assert log.items[0].delivered
    log.apply({"agent": "login-redirect", "event": "ccdash.message", "from": "you", "to": "login-redirect",
               "text": "Use the existing AuthGuard", "t": 5.0, "ts": "x"})
    log.apply({"agent": "*", "event": "ccdash.message", "from": "ccdash", "to": "all", "text": "api landed", "t": 6.0,
               "ts": "x"})
    assert [(m.sender, m.to, m.kind) for m in log.items[1:]] == [("you", "login-redirect", "you"),
                                                                 ("ccdash", "all", "ccdash")]
    plain = log.items[0].render().plain
    assert "csv-export → dark-mode" in plain and "ExportButton" in plain
