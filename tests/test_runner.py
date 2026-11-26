"""Phase 1: runner backends against the fake claude.

WinPtyRunner tests run on Windows only; TmuxRunner tests need tmux (Linux/macOS CI)."""

import json
import shutil
import sys
import time

import pytest

from ccdash import runner as runner_mod
from ccdash.model import AgentRecord
from ccdash.repo import discover
from ccdash.state import State

from conftest import wait_for

WINDOWS = sys.platform == "win32"


def _record(repo, name="alpha"):
    return AgentRecord(name=name, task="t", worktree=str(repo.root), branch="main", base_ref="main")


def _argv(prompt=None, name="alpha"):
    """(command, args) for the fake claude."""
    return runner_mod.claude_command(), runner_mod.claude_args(name, prompt=prompt)


def _screen(r, rec):
    return "\n".join(r.peek(rec, lines=60))


# -- environment ---------------------------------------------------------------
def test_agent_env_scrubs_parent_claude_session(monkeypatch, repo):
    for k in ("CLAUDECODE", "CLAUDE_PID", "CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_MESSAGING_SOCKET",
              "CLAUDE_CODE_MESSAGING_TOKEN", "CLAUDE_CODE_ENTRYPOINT"):
        monkeypatch.setenv(k, "parent")
    monkeypatch.setenv("CLAUDE_CODE_GIT_BASH_PATH", r"C:\Git\bin\bash.exe")  # user setting: keep
    monkeypatch.setenv("NO_COLOR", "1")
    st = State(discover(repo.root).state_dir)
    env = runner_mod.agent_env(st, "alpha")
    assert env["CCDASH_AGENT"] == "alpha" and env["CCDASH_STATE"] == str(st.root)
    assert env["COLORTERM"] == "truecolor"
    assert env["CLAUDE_CODE_GIT_BASH_PATH"] == r"C:\Git\bin\bash.exe"
    assert "NO_COLOR" not in env  # leaked from the parent Claude Code session
    for k in ("CLAUDECODE", "CLAUDE_PID", "CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_MESSAGING_SOCKET",
              "CLAUDE_CODE_MESSAGING_TOKEN", "CLAUDE_CODE_ENTRYPOINT"):
        assert k not in env


def test_trust_check(repo, sandbox):
    assert runner_mod.is_trusted(repo.root) is True
    (sandbox / "claude.json").write_text(json.dumps({"projects": {}}))
    assert runner_mod.is_trusted(repo.root) is False
    (sandbox / "claude.json").unlink()
    assert runner_mod.is_trusted(repo.root) is None  # unknown: do not block


# -- WinPtyRunner ----------------------------------------------------------------
winonly = pytest.mark.skipif(not WINDOWS, reason="ConPTY runner is Windows only")


@pytest.fixture
def winrunner(repo):
    st = State(discover(repo.root).state_dir)
    return runner_mod.WinPtyRunner(st, discover(repo.root))


@winonly
def test_winpty_start_peek_type_stop(repo, winrunner):
    rec = _record(repo)
    info = winrunner.start(rec, *_argv("hello there"), runner_mod.agent_env(winrunner.state, "alpha"), repo.root)
    rec.host_pid, rec.claude_pid = info["host_pid"], info["claude_pid"]
    assert winrunner.is_alive(rec)
    wait_for(lambda: "ack: hello there" in _screen(winrunner, rec), message="initial prompt handled")

    winrunner.send_text(rec, "second prompt")
    wait_for(lambda: "ack: second prompt" in _screen(winrunner, rec), message="typed prompt handled")

    t = time.monotonic()
    assert winrunner.stop(rec, timeout=10) is True
    assert time.monotonic() - t < 8
    wait_for(lambda: not winrunner.is_alive(rec), message="host exit")
    ex = winrunner.exit_info(rec)
    assert ex is not None and ex["exitstatus"] == 0


@winonly
def test_winpty_host_outlives_launcher_and_has_no_live_parent(repo, winrunner):
    rec = _record(repo)
    info = winrunner.start(rec, *_argv(), runner_mod.agent_env(winrunner.state, "alpha"), repo.root)
    rec.host_pid = info["host_pid"]
    parent = runner_mod.parent_pid(info["host_pid"])
    # the intermediate launcher has exited, so a process-tree kill of our shell cannot reach the host
    assert parent is None or not runner_mod.pid_alive(parent)
    assert winrunner.is_alive(rec)
    winrunner.kill(rec)
    wait_for(lambda: not winrunner.is_alive(rec), message="killed")


@winonly
def test_winpty_kill_is_not_a_clean_exit(repo, winrunner):
    rec = _record(repo)
    info = winrunner.start(rec, *_argv(), runner_mod.agent_env(winrunner.state, "alpha"), repo.root)
    rec.host_pid = info["host_pid"]
    wait_for(lambda: "fake-claude" in _screen(winrunner, rec), message="banner")
    winrunner.kill(rec)
    wait_for(lambda: not winrunner.is_alive(rec), message="killed")
    ex = winrunner.exit_info(rec)
    assert ex is None or ex["exitstatus"] != 0


@winonly
def test_winpty_attach_proxy_roundtrip(repo, winrunner):
    """Scripted attach: keys go through the pipe into the agent and its output comes back."""
    from ccdash.attach import AttachSession

    rec = _record(repo)
    info = winrunner.start(rec, *_argv(), runner_mod.agent_env(winrunner.state, "alpha"), repo.root)
    rec.host_pid = info["host_pid"]
    wait_for(lambda: "fake-claude" in _screen(winrunner, rec), message="banner")
    seen = []
    with AttachSession(winrunner.host_info(rec), rows=30, cols=100, on_output=seen.append) as sess:
        assert "fake-claude" in "".join(seen)  # initial paint of the current screen
        sess.send_input(b"through the pipe\r")
        wait_for(lambda: "ack: through the pipe" in "".join(seen), message="echo via attach")
        out = "".join(seen)
        assert "\x1b[?9001h" not in out and "\x1b[?1004h" not in out
    assert winrunner.is_alive(rec)  # detaching never stops the agent
    assert winrunner.stop(rec, timeout=10)


@winonly
def test_winpty_peek_unknown_agent_is_empty(repo, winrunner):
    rec = _record(repo, "ghost")
    assert not winrunner.is_alive(rec)
    assert winrunner.peek(rec) == []
    assert winrunner.stop(rec) is True  # nothing to stop


# -- TmuxRunner ------------------------------------------------------------------
tmuxonly = pytest.mark.skipif(WINDOWS or shutil.which("tmux") is None, reason="needs tmux")


@pytest.mark.tmux
@tmuxonly
def test_tmux_start_peek_type_stop(repo):
    r = discover(repo.root)
    tr = runner_mod.TmuxRunner(State(r.state_dir), r)
    rec = _record(repo)
    tr.start(rec, *_argv("hello there"), runner_mod.agent_env(tr.state, "alpha"), repo.root)
    assert tr.is_alive(rec)
    wait_for(lambda: "ack: hello there" in "\n".join(tr.peek(rec)), message="prompt")
    tr.send_text(rec, "second prompt")
    wait_for(lambda: "ack: second prompt" in "\n".join(tr.peek(rec)), message="typed")
    assert tr.stop(rec, timeout=10)
    assert not tr.is_alive(rec)
    assert tr.exit_info(rec)["exitstatus"] == 0


def test_agent_env_keeps_users_own_no_color(monkeypatch, repo):
    monkeypatch.delenv("CLAUDECODE", raising=False)
    monkeypatch.setenv("NO_COLOR", "1")
    env = runner_mod.agent_env(State(discover(repo.root).state_dir), "alpha")
    assert env["NO_COLOR"] == "1"