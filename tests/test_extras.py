"""Phase 5: the extras, each behind a config flag and off by default: guard mode
(`guard`), the `claude --bg` runner (`runner = "bg"`), several repos in one screen
(`repos`), and opening an agent in a new terminal tab (`open_in`)."""

import io
import json
import os
import subprocess
import sys

import pytest

from ccdash import guard, hook, hooks_settings, messaging, terminals
from ccdash.config import load_config
from ccdash.dashboard import Dashboard
from ccdash.tui.app import CCDashApp, open_workspaces
from ccdash.workspace import Workspace, WorkspaceError

from conftest import HERE, git, wait_for
from test_conflicts import add_agent
from test_tui import SIZE, settle, table_names

windows_runner = pytest.mark.skipif(sys.platform != "win32", reason="Windows runner (tmux variant runs in CI)")
WT = r"C:\repo\.claude\worktrees\b" if sys.platform == "win32" else "/repo/.claude/worktrees/b"


def test_every_extra_is_off_by_default(repo, ws):
    cfg = load_config(repo.root)
    assert (cfg.guard, cfg.runner, cfg.repos, cfg.open_in) == ("off", "auto", [], "none")
    assert ws.runner.kind != "bg"
    assert not (ws.state.root / "guard.json").exists()
    assert hooks_settings.render_command()["hooks"]["PreToolUse"][0]["hooks"][0]["async"] is True
    assert [w.repo.name for w in open_workspaces(repo.root)] == ["my-app"]


# -- guard mode -------------------------------------------------------------------------------
def guard_state(tmp_path, mode, ownership, lifecycles=None):
    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    if mode:
        (state / "guard.json").write_text(json.dumps({"mode": mode}))
    (state / "ownership.json").write_text(json.dumps({"version": 1, "agents": ownership}))
    lifecycles = lifecycles or {"a": "running", "b": "running"}
    (state / "agents.json").write_text(json.dumps(
        {"version": 1, "agents": [{"name": n, "lifecycle": lc} for n, lc in lifecycles.items()]}))
    return state


def pre(tool, path, agent="b"):
    return {"event": "PreToolUse", "tool": tool, "path": path, "agent": agent}


OWNED = {"a": {"claimed": ["src/ui/**"], "touched": ["src/api/orders.ts"]}, "b": {"claimed": ["docs/**"], "touched": []}}


def test_guard_block_denies_edits_in_another_agents_files(tmp_path):
    g = guard.Guard(guard_state(tmp_path, "block", OWNED), "b")
    out = g.decide(pre("Edit", "src/ui/Toolbar.tsx"))["hookSpecificOutput"]
    assert out["hookEventName"] == "PreToolUse" and out["permissionDecision"] == "deny"
    reason = out["permissionDecisionReason"]
    assert "src/ui/Toolbar.tsx" in reason and "a" in reason.split() and "src/ui/**" in reason and "SendMessage" in reason
    touched = g.decide(pre("Write", "src/api/orders.ts"))["hookSpecificOutput"]
    assert touched["permissionDecision"] == "deny" and "editing" in touched["permissionDecisionReason"]
    assert g.decide(pre("Edit", "docs/readme.md")) is None, "its own scope"
    assert g.decide(pre("NotebookEdit", "src/other.ipynb")) is None, "nobody's"
    assert g.decide(pre("Read", "src/ui/Toolbar.tsx")) is None, "reading is free"
    assert g.decide(pre("Bash", None)) is None
    assert g.decide(pre("Edit", "C:/elsewhere/file.ts")) is None, "outside the worktree: not ours to judge"
    assert guard.Guard(tmp_path / "state", "a").decide(pre("Edit", "src/ui/Toolbar.tsx", agent="a")) is None, "the owner may"


def test_guard_warn_adds_context_and_leaves_the_permission_flow_alone(tmp_path):
    g = guard.Guard(guard_state(tmp_path, "warn", OWNED), "b")
    out = g.decide(pre("MultiEdit", "src/ui/Toolbar.tsx"))["hookSpecificOutput"]
    assert "permissionDecision" not in out, "allow would skip the user's own permission prompt"
    assert "src/ui/Toolbar.tsx" in out["additionalContext"] and "SendMessage" in out["additionalContext"]
    assert " a " in f" {out['additionalContext']} ".replace(".", " ").replace(",", " ")


def test_guard_block_only_warns_an_agent_that_already_changed_the_file(tmp_path):
    both = {"a": {"claimed": [], "touched": ["src/ui/Toolbar.tsx"]}, "b": {"claimed": [], "touched": ["src/ui/Toolbar.tsx"]}}
    state = guard_state(tmp_path, "block", both)
    for agent in ("a", "b"):
        out = guard.Guard(state, agent).decide(pre("Edit", "src/ui/Toolbar.tsx", agent=agent))["hookSpecificOutput"]
        assert "permissionDecision" not in out and "SendMessage" in out["additionalContext"], "neither is locked out"


def test_guard_follows_the_config_and_ignores_deleted_agents(tmp_path):
    state = guard_state(tmp_path, None, OWNED)
    g = guard.Guard(state, "b")
    assert g.decide(pre("Edit", "src/ui/Toolbar.tsx")) is None, "no guard.json: off"
    (state / "guard.json").write_text(json.dumps({"mode": "block"}))
    assert g.decide(pre("Edit", "src/ui/Toolbar.tsx")) is not None, "switched on while the agent runs"
    (state / "guard.json").write_text(json.dumps({"mode": "off"}))
    assert g.decide(pre("Edit", "src/ui/Toolbar.tsx")) is None
    gone = guard_state(tmp_path, "block", OWNED, {"a": "deleted", "b": "running"})
    assert guard.Guard(gone, "b").decide(pre("Edit", "src/ui/Toolbar.tsx")) is None
    (gone / "ownership.json").write_text("not json")
    assert guard.Guard(gone, "b").decide(pre("Edit", "src/ui/Toolbar.tsx")) is None, "never break a tool call"


def test_guard_in_the_host_follows_events_without_a_dashboard(tmp_path):
    state = guard_state(tmp_path, "block", {})  # no dashboard ever wrote ownership.json
    decide = guard.decider_for(state, "b")
    assert decide(pre("Edit", "src/ui/Toolbar.tsx")) is None

    def event(agent, name, **kw):
        hook.append(str(state), {"agent": agent, "event": name, "t": 1.0, **kw})

    event("a", "PostToolUse", tool="Edit", path="src/ui/Toolbar.tsx")
    event("a", "PreToolUse", tool="SendMessage", msg={"to": "b", "text": "Files: src/reports/**\nplan"})
    assert decide(pre("Edit", "src/ui/Toolbar.tsx"))["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert decide(pre("Write", "src/reports/export.ts"))["hookSpecificOutput"]["permissionDecision"] == "deny"
    # b is denied; its attempt must not make the file b's, or a would be locked out of its own file
    event("b", "PreToolUse", tool="Edit", path="src/ui/Toolbar.tsx")
    assert guard.decider_for(state, "a")(pre("Edit", "src/ui/Toolbar.tsx", agent="a")) is None
    event("b", "PostToolUse", tool="Edit", path="src/b.ts")
    assert decide(pre("Edit", "src/b.ts")) is None, "its own edits never stop it"


def test_workspace_writes_the_guard_mode_for_hosts_and_hooks(repo):
    repo.write(".ccdash.toml", 'guard = "warn"\n')
    ws = Workspace.open(repo.root)
    assert json.loads((ws.state.root / "guard.json").read_text())["mode"] == "warn"
    settings = hooks_settings.render_command(guard=True)
    assert settings["hooks"]["PreToolUse"][0]["hooks"][0]["async"] is False, "a decision needs a synchronous hook"
    assert settings["hooks"]["PostToolUse"][0]["hooks"][0]["async"] is True
    repo.write(".ccdash.toml", 'guard = "off"\n')
    Workspace.open(repo.root)
    assert json.loads((ws.state.root / "guard.json").read_text())["mode"] == "off"


def run_hook_main(monkeypatch, capsys, argv, payload):
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(json.dumps(payload).encode())))
    hook.main(argv)
    return capsys.readouterr().out


def test_command_hook_prints_a_decision_only_in_guard_mode(tmp_path, monkeypatch, capsys):
    for var in ("CCDASH_AGENT", "CCDASH_STATE", "CCDASH_WORKTREE"):
        monkeypatch.delenv(var, raising=False)
    state = guard_state(tmp_path, "block", OWNED)
    payload = {"hook_event_name": "PreToolUse", "tool_name": "Edit", "session_id": "s",
               "tool_input": {"file_path": os.path.join(WT, "src", "ui", "Toolbar.tsx")}}
    argv = ["PreToolUse", "b", str(state), WT]  # identity as arguments: no environment needed
    out = run_hook_main(monkeypatch, capsys, argv, payload)
    assert json.loads(out)["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert [e["agent"] for e in map(json.loads, (state / "events.jsonl").read_text().splitlines())] == ["b"]
    assert run_hook_main(monkeypatch, capsys, ["PostToolUse", "b", str(state), WT], payload) == "", "only PreToolUse decides"
    payload["tool_input"]["file_path"] = os.path.join(WT, "docs", "x.md")
    assert run_hook_main(monkeypatch, capsys, argv, payload) == "", "nothing to say: nothing on stdout"
    (state / "guard.json").unlink()
    payload["tool_input"]["file_path"] = os.path.join(WT, "src", "ui", "Toolbar.tsx")
    assert run_hook_main(monkeypatch, capsys, argv, payload) == "", "guard off: the hook stays silent"


@windows_runner
@pytest.mark.parametrize("mode", ["block", "warn"])
def test_guard_end_to_end(repo, set_scenario, mode):
    repo.write(".ccdash.toml", f'guard = "{mode}"\n')
    set_scenario({"owner": [{"send": {"to": "all", "message": "Files: src/ui/**\nI own the toolbar"}}, {"sleep": 60}],
                  "intruder": [{"sleep": 0.5}, {"edit": "src/ui/Toolbar.tsx", "text": "// intruder\n"},
                               {"edit": "docs/free.md", "text": "ok\n"}, {"say": "DONE: tried"}]})
    ws = Workspace.open(repo.root)
    ws.new_agent("owns the ui", name="owner")
    d = Dashboard(ws)
    wait_for(lambda: d.refresh_events() is not None and d.activity.get("owner") is not None
             and d.activity.get("owner").claimed, timeout=8, message="owner announced its scope")
    ws.new_agent("wants the toolbar", name="intruder")
    wt = repo.root / ".claude" / "worktrees" / "intruder"
    wait_for(lambda: (wt / "docs" / "free.md").exists(), timeout=10, message="the unclaimed file is written either way")
    toolbar = (wt / "src" / "ui" / "Toolbar.tsx").read_text()
    wait_for(lambda: "ccdash guard" in "\n".join(ws.peek("intruder", 60)), timeout=5, message="guard spoke")
    screen = messaging.one_line("\n".join(ws.peek("intruder", 60)))
    assert "owner" in screen and "src/ui/**" in screen.replace(" ", "")
    if mode == "block":
        assert "// intruder" not in toolbar, "the edit was denied"
        assert "denied" in screen
    else:
        assert "// intruder" in toolbar, "warn lets the edit through"
    d.refresh_events()
    d.refresh_runtime()
    touched = d.activity.get("intruder").touched
    assert ("src/ui/Toolbar.tsx" in touched) is (mode == "warn"), "a denied edit touched nothing"


# -- the claude --bg runner ---------------------------------------------------------------------
@pytest.fixture
def bg_ws(repo, sandbox, monkeypatch):
    repo.write(".ccdash.toml", 'runner = "bg"\n')
    monkeypatch.setenv("CCDASH_CLAUDE", json.dumps([sys.executable, str(HERE / "fake_claude_bg.py")]))
    yield Workspace.open(repo.root)
    jobs = sandbox / "fake-claude" / "jobs"
    for rec in jobs.glob("*.json") if jobs.is_dir() else []:
        pid = json.loads(rec.read_text()).get("pid")
        if pid and sys.platform == "win32":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True)
        elif pid:
            try:
                os.kill(pid, 9)
            except OSError:
                pass


def test_bg_runner_start_status_peek_stop_restart(bg_ws, repo, set_scenario):
    ws = bg_ws
    # The pause is Claude thinking between the tool result and its last message. Without it the async
    # command hooks of PostToolUse and Stop start within a millisecond and may finish in either order.
    set_scenario([{"edit": "src/ui/Toolbar.tsx", "text": "// bg\n"}, {"sleep": 0.8}, {"say": "DONE: in the background"}])
    rec = ws.new_agent("work in the background", name="bg-one")
    assert ws.runner.kind == "bg" and rec.runner == "bg"
    assert len(rec.session) == 8 and rec.claude_session_id.startswith(rec.session), "ids read back from claude"
    assert hooks_settings.for_agent(ws, rec)["hooks"]["Stop"][0]["hooks"][0]["type"] == "command"

    d = Dashboard(ws)

    def status():
        d.refresh_runtime()
        return d.get("bg-one").status

    wait_for(lambda: status() == "finished", timeout=10, message="hooks arrive without any CCDASH_ variable")
    assert d.get("bg-one").alive
    assert d.activity.get("bg-one").touched == {"src/ui/Toolbar.tsx"}
    assert (repo.root / ".claude" / "worktrees" / "bg-one" / "src" / "ui" / "Toolbar.tsx").read_text().endswith("// bg\n")
    assert "DONE: in the background" in "\n".join(ws.peek("bg-one", 40)), "peek is `claude logs`"

    with pytest.raises(WorkspaceError, match="cannot type into"):
        messaging.send_from_user(ws, "bg-one", "hello", dashboard=d)
    assert messaging.broadcast_from_user(ws, "hello all", dashboard=d) == ["failed for bg-one"]

    assert ws.stop("bg-one")
    wait_for(lambda: status() == "stopped", timeout=6, message="stopped")
    assert (repo.root / ".claude" / "worktrees" / "bg-one").is_dir(), "stop keeps the worktree"
    sid = ws.record("bg-one").claude_session_id
    ws.restart("bg-one")
    wait_for(lambda: status() in ("idle", "working", "finished") and d.get("bg-one").alive, timeout=10, message="resumed")
    assert ws.record("bg-one").claude_session_id == sid, "restart resumes the same conversation"
    assert "resume" in "\n".join(ws.peek("bg-one", 40))
    ws.delete("bg-one", confirm="bg-one")
    assert not (repo.root / ".claude" / "worktrees" / "bg-one").exists()


def test_bg_runner_reports_a_failed_start(bg_ws, monkeypatch):
    monkeypatch.setenv("CCDASH_CLAUDE", json.dumps([sys.executable, "-c", "import sys; sys.exit('Workspace not trusted.')"]))
    with pytest.raises(RuntimeError, match="Workspace not trusted"):
        bg_ws.new_agent("never starts", name="nope")
    assert bg_ws.record("nope").lifecycle == "stopped"


# -- several repos in one screen ------------------------------------------------------------------
@pytest.fixture
def second_repo(tmp_path, sandbox):
    root = tmp_path / "work" / "api-server"
    root.mkdir(parents=True)
    git("init", "-q", "-b", "main", ".", cwd=root)
    (root / "README.md").write_text("# api-server\n")
    git("add", "-A", cwd=root)
    git("commit", "-q", "-m", "init", cwd=root)
    return root


def test_repos_from_the_user_config(repo, second_repo, sandbox, tmp_path):
    cfg = sandbox / "ccdash-config.toml"
    cfg.write_text("repos = " + json.dumps([str(second_repo), str(repo.root), str(tmp_path / "not-a-repo")]) + "\n")
    spaces = open_workspaces(repo.root)
    assert [w.repo.name for w in spaces] == ["my-app", "api-server"], "the current repo first, no duplicates, no errors"


async def test_repo_switcher_shows_the_other_repo(repo, ws, second_repo, sandbox):
    (sandbox / "ccdash-config.toml").write_text("repos = " + json.dumps([str(second_repo)]) + "\n")
    add_agent(ws, "front")
    spaces = open_workspaces(repo.root)
    add_agent(spaces[1], "back")
    app = CCDashApp(spaces[0], others=spaces[1:])
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot, app)
        assert table_names(app) == ["front"] and "my-app" in app.header_text() and "1/2" in app.header_text()
        await pilot.press("R")
        await settle(pilot, app)
        assert table_names(app) == ["back"], "the other repo's agents"
        assert "api-server" in app.header_text() and "2/2" in app.header_text()
        assert app.ws.repo.name == "api-server" and app.dash.conflicts.active
        await pilot.press("R")
        await settle(pilot, app)
        assert table_names(app) == ["front"] and "my-app" in app.header_text()


async def test_one_repo_has_nothing_to_switch_to(repo, ws):
    app = CCDashApp(ws)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot, app)
        assert "1/1" not in app.header_text()
        await pilot.press("R")
        await pilot.pause()
        assert app.ws is ws


# -- open an agent in a new terminal tab ----------------------------------------------------------
def test_new_tab_command_for_windows_terminal(monkeypatch):
    monkeypatch.setattr(terminals.shutil, "which", lambda name: r"C:\wt\wt.exe" if name.startswith("wt") else None)
    argv = terminals.new_tab_command("wt", "csv-export", r"C:\repo")
    assert argv == [r"C:\wt\wt.exe", "-w", "0", "new-tab", "--title", "csv-export", "-d", r"C:\repo",
                    sys.executable, "-m", "ccdash", "attach", "csv-export"]
    assert terminals.new_tab_command("none", "x", ".") is None
    assert terminals.new_tab_command("vscode", "x", ".") is None, "VS Code has no command line for a terminal tab"
    monkeypatch.setattr(terminals.shutil, "which", lambda name: None)
    assert terminals.new_tab_command("wt", "x", ".") is None, "no Windows Terminal: attach in place"


@windows_runner
@pytest.mark.parametrize("open_in, tab", [("wt", True), ("vscode", False)])
async def test_attach_honours_open_in(repo, monkeypatch, open_in, tab):
    repo.write(".ccdash.toml", f'open_in = "{open_in}"\n')
    ws = Workspace.open(repo.root)
    ws.new_agent("attach me", name="tabbed")
    spawned, attached = [], []
    monkeypatch.setattr(terminals, "spawn", spawned.append)
    monkeypatch.setattr(terminals.shutil, "which", lambda name: "wt.exe" if name.startswith("wt") else None)
    app = CCDashApp(Workspace.open(repo.root), attach_fn=lambda name: attached.append(name) or 0)
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot, app)
        app.select("tabbed")
        await pilot.press("enter")
        await settle(pilot, app)
        if tab:
            assert attached == [] and spawned[0][:4] == ["wt.exe", "-w", "0", "new-tab"] and spawned[0][-1] == "tabbed"
        else:
            assert attached == ["tabbed"] and spawned == [], "falls back to attaching in place"
    ws.stop("tabbed", wait=False)
