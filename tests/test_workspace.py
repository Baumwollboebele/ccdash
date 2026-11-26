"""Phase 1 acceptance: start, stop, restart, delete and reconcile agents end to end
(real git, real runner backend, fake claude)."""

import json
import sys
import time

import pytest

from ccdash.workspace import ConfirmationRequired, NotTrusted, Workspace, WorkspaceError

from conftest import wait_for

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows runner (tmux variant runs in CI)")


def screen(ws, name):
    return "\n".join(ws.peek(name, lines=60))


def view(ws, name):
    return {v.name: v for v in ws.views()}[name]


def test_new_agent_end_to_end(repo, ws):
    t = time.monotonic()
    rec = ws.new_agent("Add CSV export to the reports page")
    assert time.monotonic() - t < 5, "idea to running agent must take under 5 s"
    assert rec.name == "add-csv-export-to"
    assert rec.branch == "cc/add-csv-export-to"
    assert rec.lifecycle == "running" and rec.claude_session_id
    wt = repo.root / ".claude" / "worktrees" / rec.name
    assert (wt / ".env").exists(), "include-copy ran"
    wait_for(lambda: "ack:" in screen(ws, rec.name), message="prompt reached the agent")
    assert "Add CSV export to the reports page" in screen(ws, rec.name)
    v = view(ws, rec.name)
    assert v.alive and v.status in ("running", "working", "idle")
    assert (v.ahead, v.behind) == (0, 0)


def test_three_agents_stop_restart_delete_and_reopen(repo, ws):
    names = [ws.new_agent(t).name for t in ("Fix login redirect bug", "Dark mode toggle", "Paginate orders endpoint")]
    for n in names:
        wait_for(lambda n=n: "ack:" in screen(ws, n), message=f"{n} ready")
    assert {v.name for v in ws.views() if v.alive} == set(names)

    # quitting the TUI is just dropping the Workspace; a fresh one sees the same picture
    ws2 = Workspace.open(repo.root / "src")
    assert {v.name for v in ws2.views() if v.alive} == set(names)

    ws.stop(names[0])
    v = view(ws, names[0])
    assert not v.alive and v.status == "stopped" and v.lifecycle == "stopped"
    assert (repo.root / ".claude" / "worktrees" / names[0]).is_dir(), "stop keeps the worktree"
    assert repo.git("branch", "--list", f"cc/{names[0]}").stdout.strip()

    sid = ws.registry.get(names[0]).claude_session_id
    ws.restart(names[0])
    wait_for(lambda: "resume" in screen(ws, names[0]), message="resumed session")
    assert ws.registry.get(names[0]).claude_session_id == sid
    assert view(ws, names[0]).alive

    ws.delete(names[1])  # clean and merged: no confirmation
    assert names[1] not in {v.name for v in ws.views()}
    assert not (repo.root / ".claude" / "worktrees" / names[1]).exists()
    assert names[1] in {v.name for v in ws.views(show_hidden=True)}

    for n in (names[0], names[2]):
        ws.stop(n)


def test_delete_unmerged_work_needs_typed_confirmation(repo, ws, set_scenario):
    set_scenario([{"edit": "src/ui/Toolbar.tsx", "text": "// csv\n"}, {"commit": "add writer"},
                  {"write": "notes.txt", "text": "wip\n"}])
    rec = ws.new_agent("Add CSV export")
    wt = repo.root / ".claude" / "worktrees" / rec.name
    wait_for(lambda: (wt / "notes.txt").exists(), message="agent did work")
    check = ws.delete_check(rec.name)
    assert check.needs_confirmation
    assert any("uncommitted" in x for x in check.lost) and any("not merged" in x for x in check.lost)
    with pytest.raises(ConfirmationRequired):
        ws.delete(rec.name)
    with pytest.raises(ConfirmationRequired):
        ws.delete(rec.name, confirm="wrong-name")
    assert wt.exists() and repo.git("branch", "--list", rec.branch).stdout.strip()
    ws.delete(rec.name, confirm=rec.name)
    assert not wt.exists()
    assert not repo.git("branch", "--list", rec.branch).stdout.strip()


def test_crashed_agent_and_untracked_worktree_reconcile(repo, ws, set_scenario):
    set_scenario({"crashy": [{"sleep": 0.3}, {"crash": 3}], "*": []})
    rec = ws.new_agent("crashy", name="crashy")
    wait_for(lambda: not view(ws, "crashy").alive, message="crash")
    v = view(ws, "crashy")
    assert v.status == "crashed"
    assert (repo.root / ".claude" / "worktrees" / "crashy").is_dir(), "worktree untouched"

    repo.git("worktree", "add", "-q", "-b", "cc/hand-made", str(repo.root / ".claude" / "worktrees" / "hand-made"))
    ws2 = Workspace.open(repo.root)
    v = {x.name: x for x in ws2.views()}
    assert v["hand-made"].status == "untracked"
    ws2.adopt("hand-made")
    assert ws2.registry.get("hand-made").branch == "cc/hand-made"
    assert view(ws2, "hand-made").status == "stopped"


def test_untrusted_repo_is_refused(repo, ws, sandbox):
    (sandbox / "claude.json").write_text(json.dumps({"projects": {}}))
    with pytest.raises(NotTrusted):
        ws.new_agent("anything")
    assert not (repo.root / ".claude" / "worktrees").exists() or not any((repo.root / ".claude" / "worktrees").iterdir())


def test_max_agents_and_name_collisions(repo, ws):
    repo.write(".ccdash.toml", "max_agents = 2\n")
    ws = Workspace.open(repo.root)
    a = ws.new_agent("Same task here")
    b = ws.new_agent("Same task here")
    assert (a.name, b.name) == ("same-task-here", "same-task-here-2")
    with pytest.raises(WorkspaceError, match="max_agents"):
        ws.new_agent("third")
    ws.stop(a.name)
    ws.stop(b.name)


def test_setup_command_runs_and_is_logged(repo, ws):
    repo.write(".ccdash.toml", 'setup = "echo setting-up"\n')
    ws = Workspace.open(repo.root)
    steps = []
    rec = ws.new_agent("With setup", progress=lambda step, frac: steps.append(step))
    log = ws.state.logs_dir / f"{rec.name}.setup.log"
    assert "setting-up" in log.read_text()
    assert steps[0] == "worktree" and "setup" in steps and steps[-1] == "started"
    ws.stop(rec.name)
