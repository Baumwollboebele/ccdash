"""The lead: one coordinator session you talk to; it turns requests into agents by running
`ccdash new`, and they show up in the dashboard (key l, or `cc lead "..."`)."""

import json
import sys
from pathlib import Path

import pytest

from ccdash import lead, worktree
from ccdash.dashboard import Dashboard
from ccdash.naming import slugify, unique_name
from ccdash.prompt import render_lead_prompt
from ccdash.runner import allow_patterns, claude_args
from ccdash.tui.app import CCDashApp
from ccdash.workspace import Workspace, WorkspaceError

from conftest import wait_for
from test_cli import cc
from test_tui import SIZE, settle, table_names, until

windows_runner = pytest.mark.skipif(sys.platform != "win32", reason="Windows runner (tmux variant runs in CI)")


def screen(ws, name):
    return "\n".join(ws.peek(name, 60))


def test_lead_prompt_tells_it_how_to_delegate():
    text = render_lead_prompt(repo_name="my-app", default_branch="main", cmd="ccdash", prefix="cc/", max_agents=6,
                              others=[], request="Add CSV export to the reports page")
    for needle in ('ccdash new "<task>" --name <short-name>', "ccdash ls", "ccdash peek <name>", "At most 6 agents",
                   "`my-app`", "`cc/<short-name>`", "Do not edit files in this checkout",
                   "Add CSV export to the reports page", "[from the user]"):
        assert needle in text, needle
    assert "$" not in text, "every placeholder is filled"
    waiting = render_lead_prompt(repo_name="my-app", default_branch="main", cmd="ccdash", prefix="cc/", max_agents=6,
                                 others=[], request=None)
    assert "None yet" in waiting and "wait for the user" in waiting


def test_the_lead_may_run_only_harmless_commands_without_asking():
    allow = allow_patterns("ccdash", lead.HARMLESS)
    for pattern in ("Bash(ccdash new *)", "Bash(ccdash ls*)", "Bash(ccdash send *)", "PowerShell(ccdash new *)"):
        assert pattern in allow, pattern
    assert "stop" not in allow and " rm" not in allow and "git" not in allow, "those still ask the user"
    args = claude_args("lead", session_id="s", prompt="the brief", allowed_tools=allow)
    assert args[:3] == ["--allowedTools", allow, "--name"], "before another flag, so the list cannot swallow the prompt"
    assert args[-1] == "the brief"
    assert "--allowedTools" not in claude_args("worker", session_id="s", prompt="x")


def test_lead_is_a_reserved_name(repo, ws):
    with pytest.raises(WorkspaceError, match="reserved"):
        ws.new_agent("anything", name="lead")
    assert unique_name(slugify("Lead"), ws._taken(ws.registry.load())) == "lead-2", "a task called lead gets another name"
    assert ws.lead() is None


def test_removing_a_worktree_never_touches_the_main_checkout(repo, ws):
    with pytest.raises(worktree.WorktreeError, match="main checkout"):
        worktree.remove_worktree(ws.repo, repo.root, "", force=True)
    assert (repo.root / "README.md").exists() and (repo.root / ".git").is_dir()


@windows_runner
def test_lead_turns_a_request_into_an_agent_that_shows_up(repo, ws, set_scenario):
    set_scenario({"lead": [{"run": 'ccdash new "Add CSV export to the reports page" --name csv-export'},
                           {"say": "Started csv-export for the CSV export."}], "*": []})
    assert lead.tell(ws, "Add CSV export to the reports page") == "Started the lead with your request."
    rec = ws.lead()
    assert rec.role == "lead" and rec.worktree == "" and rec.branch == ""
    assert not (repo.root / ".claude" / "worktrees" / "lead").exists(), "the lead works in the main checkout"
    spec = json.loads((ws.state.run_dir / "lead.spec.json").read_text(encoding="utf-8"))
    assert Path(spec["cwd"]).resolve() == repo.root.resolve()
    assert spec["args"][0] == "--allowedTools" and "Bash(ccdash new *)" in spec["args"][1]
    assert "Add CSV export to the reports page" in spec["args"][-1], "the request is in its first prompt"

    wait_for(lambda: any(v.name == "csv-export" and v.alive for v in ws.views(git=False)), timeout=25,
             message="the lead started an agent")
    worker = json.loads((ws.state.run_dir / "csv-export.spec.json").read_text(encoding="utf-8"))
    assert "**lead**" in worker["args"][-1], "the agent knows whom to report to"
    d = Dashboard(ws)
    d.refresh_all()
    assert [v.name for v in d.sorted_views()] == ["lead", "csv-export"], "the lead stays on top"
    assert d.get("csv-export").branch == "cc/csv-export" and d.get("lead").role == "lead"
    assert d.counts()["agents"] == 1, "the header counts the agents, not their coordinator"

    assert lead.tell(ws, "Also add a dark mode toggle", dashboard=d) == "Told the lead."
    wait_for(lambda: "[from the user] Also add a dark mode toggle" in screen(ws, "lead"), timeout=8,
             message="a second request goes into the running lead")

    ws.stop("lead")
    sid = ws.lead().claude_session_id
    assert lead.tell(ws, "And paginate the orders endpoint") == "Started the lead with your request."
    wait_for(lambda: "And paginate the orders endpoint" in screen(ws, "lead"), timeout=10, message="resumed with it")
    assert ws.lead().claude_session_id == sid and "resume" in screen(ws, "lead"), "same conversation"

    ws.delete("lead")
    assert ws.lead() is None
    assert (repo.root / "README.md").exists() and (repo.root / "src" / "ui" / "Toolbar.tsx").exists(), \
        "deleting the lead removes nothing from disk"
    ws.stop("csv-export", wait=False)


@windows_runner
async def test_l_key_tells_the_lead(repo, ws, set_scenario):
    set_scenario({"lead": [{"say": "Ready."}], "*": []})
    app = CCDashApp(Workspace.open(repo.root))
    async with app.run_test(size=SIZE) as pilot:
        await settle(pilot, app)
        await pilot.press("l")
        await pilot.pause()
        assert app.screen.id == "lead"
        await pilot.press(*"Add CSV export", "enter")
        await until(pilot, app, lambda: table_names(app)[:1] == ["lead"], timeout=25)
        assert "main checkout" in app.row_text("lead")
        await until(pilot, app, lambda: "Add CSV export" in screen(ws, "lead"))
        app.select("lead")
        await pilot.press("m")
        await pilot.pause(0.2)
        assert app.screen.id != "merge", "the lead has no branch to merge"
    ws.stop("lead", wait=False)


@windows_runner
def test_cc_lead_and_cc_new_from_the_shell(repo, set_scenario):
    set_scenario({"*": []})
    p = cc("lead", "Paginate the orders endpoint", cwd=repo.root)
    assert p.returncode == 0 and "Started the lead" in p.stdout, p.stderr
    p = cc("new", "stop the flaky settings test from failing", "--name", "flaky", cwd=repo.root)
    assert p.returncode == 0 and "started flaky" in p.stdout, p.stderr
    rows = json.loads(cc("ls", "--json", cwd=repo.root).stdout)
    assert [(r["name"], r["role"]) for r in rows] == [("lead", "lead"), ("flaky", "")]
    ws = Workspace.open(repo.root)
    wait_for(lambda: "Paginate the orders endpoint" in screen(ws, "lead"), timeout=8, message="the request reached it")
