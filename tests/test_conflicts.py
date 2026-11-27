"""Phase 4: conflict detection (overlap, predicted merge conflicts, shared interfaces, stale
base), the merge state column, local merge, pull requests and the notices that follow.

The first part needs no running agent: branches and worktrees are made with real git and
registered as stopped agents; hook events are fed in by hand. The last part runs the
"Conflict scenarios that must pass" from docs/PLAN.md end to end with the fake claude."""

import json
import sys
import time

import pytest

from ccdash import gitstate, github, gitutil, merge, messaging, worktree
from ccdash.conflicts import ConflictTracker, Row, matches
from ccdash.dashboard import Dashboard
from ccdash.model import AgentRecord
from ccdash.workspace import AgentView, Workspace

from conftest import HERE, wait_for

windows_runner = pytest.mark.skipif(sys.platform != "win32", reason="Windows runner (tmux variant runs in CI)")


# -- helpers --------------------------------------------------------------------------------
def add_agent(ws, name):
    """A branch, a worktree and a registry record, with no process: a stopped agent."""
    wt = worktree.create_worktree(ws.repo, ws.cfg, name, "main")
    ws.registry.put(AgentRecord(name=name, task=f"task of {name}", worktree=str(wt.path), branch=wt.branch,
                                base_ref="main", base_sha=wt.base_sha, lifecycle="stopped", runner=ws.runner.kind,
                                adopted=True))
    return wt.path


def tracked(ws, active=True, clock=None):
    """A Dashboard whose tracker records its notices instead of typing them into agents."""
    d = Dashboard(ws)
    sent = []
    d.conflicts = ConflictTracker(ws, active=active, clock=clock or time.time,
                                  notify=lambda to, text, exclude: sent.append((to, text, exclude)))
    return d, sent


def view(name, alive=True, **kw):
    base = dict(status="working" if alive else "stopped", lifecycle="running" if alive else "stopped", alive=alive,
                branch=f"cc/{name}", task=f"task of {name}", merge_state="clean")
    base.update(kw)
    return AgentView(name=name, **base)


def edit(agent, path, t=None):
    return {"agent": agent, "event": "PreToolUse", "tool": "Edit", "path": path, "t": t or time.time()}


def say(agent, to, text, t=None):
    return {"agent": agent, "event": "PreToolUse", "tool": "SendMessage", "t": t or time.time(),
            "msg": {"to": to, "summary": "", "text": text}}


def feed(d, *events):
    for e in events:
        d.activity.apply(e)
        d.messages.apply(e)
        d.conflicts.on_event(e)


def rows(d):
    return {r.key: r for r in d.conflicts.rows}


@pytest.fixture(params=["write-tree", "legacy"])
def merge_tree(request, monkeypatch, repo):
    """Both ways of predicting a merge: `merge-tree --write-tree` (git 2.38+) and the older three-way form."""
    modern = request.param == "write-tree"
    monkeypatch.setattr(gitstate, "_write_tree", None)
    if modern and not gitstate.supports_write_tree(repo.root):
        pytest.skip("this git has no merge-tree --write-tree")
    monkeypatch.setattr(gitstate, "_write_tree", modern)
    return request.param


@pytest.fixture
def fake_gh(sandbox, monkeypatch):
    store = sandbox / "gh.json"
    monkeypatch.setenv("CCDASH_GH", json.dumps([sys.executable, str(HERE / "fake_gh.py")]))
    monkeypatch.setenv("CCDASH_FAKE_GH", str(store))
    github._auth.clear()
    yield store
    github._auth.clear()


# -- merge predictions ----------------------------------------------------------------------
def test_merge_predictions_conflicts_and_clean_merges(repo, ws, merge_tree):
    repo.write("big.txt", "".join(f"line {i}\n" for i in range(1, 13)))
    repo.commit("big file")
    wt = {n: add_agent(ws, n) for n in ("same-line", "other-file", "deleter", "adder", "far-lines")}
    sha = {}
    repo.write("src/auth/session.ts", "export const session = 'agent';\n", cwd=wt["same-line"])
    sha["same-line"] = repo.commit("agent changes session", cwd=wt["same-line"])
    repo.write("src/ui/Toolbar.tsx", "export const Toolbar = () => 1;\n", cwd=wt["other-file"])
    sha["other-file"] = repo.commit("toolbar", cwd=wt["other-file"])
    (wt["deleter"] / "src" / "auth" / "session.ts").unlink()
    sha["deleter"] = repo.commit("drop session", cwd=wt["deleter"])
    repo.write("docs/notes.md", "from the agent\n", cwd=wt["adder"])
    sha["adder"] = repo.commit("notes", cwd=wt["adder"])
    repo.write("big.txt", "".join(f"line {i}\n" for i in range(1, 12)) + "line 12 by the agent\n", cwd=wt["far-lines"])
    sha["far-lines"] = repo.commit("last line", cwd=wt["far-lines"])

    repo.write("src/auth/session.ts", "export const session = 2;\n")
    repo.write("docs/notes.md", "from main\n")
    repo.write("big.txt", "line 1 on main\n" + "".join(f"line {i}\n" for i in range(2, 13)))
    main = repo.commit("main moves")

    cache = gitstate.MergeTreeCache(ws.repo)
    pred = cache.predict(main, sha["same-line"])
    assert pred.conflict and pred.files == ("src/auth/session.ts",)
    assert not cache.predict(main, sha["other-file"]).conflict
    assert cache.predict(main, sha["deleter"]).files == ("src/auth/session.ts",), "deleted here, changed there"
    assert cache.predict(main, sha["adder"]).files == ("docs/notes.md",), "added on both sides"
    assert not cache.predict(main, sha["far-lines"]).conflict, "different lines of one file merge"
    assert repo.git("status", "--porcelain").stdout.strip() == "", "predicting touches no worktree"


def test_predictions_are_cached_by_sha_pair(repo, ws, monkeypatch):
    path = add_agent(ws, "a")
    repo.write("README.md", "# a\n", cwd=path)
    head = repo.commit("a", cwd=path)
    main = repo.git("rev-parse", "main").stdout.strip()
    cache = gitstate.MergeTreeCache(ws.repo)
    first = cache.predict(main, head)

    def no_git(*args, **kw):
        raise AssertionError("a cached prediction must not run git")

    monkeypatch.setattr(gitutil, "git", no_git)
    assert cache.predict(main, head) is first


def test_legacy_parser_reads_merge_tree_output():
    out = "\n".join([
        "changed in both", "  base   100644 " + "1" * 40 + " src/a.ts", "  our    100644 " + "2" * 40 + " src/a.ts",
        "  their  100644 " + "3" * 40 + " src/a.ts", "@@ -1 +1,5 @@", "+<<<<<<< .our", " ours", "+=======", "+theirs",
        "+>>>>>>> .their",
        "changed in both", "  base   100644 " + "1" * 40 + " clean.ts", "  our    100644 " + "2" * 40 + " clean.ts",
        "  their  100644 " + "3" * 40 + " clean.ts", "@@ -1,3 +1,3 @@", " a", "-b", "+B",
        "removed in remote", "  base   100644 " + "1" * 40 + " untouched.ts", "  our    100644 " + "1" * 40 +
        " untouched.ts", "@@ -1 +0,0 @@", "-x",
        "removed in remote", "  base   100644 " + "1" * 40 + " dir with space/edited.ts",
        "  our    100644 " + "2" * 40 + " dir with space/edited.ts", "@@ -1 +0,0 @@", "-x",
        "added in remote", "  their  100644 " + "3" * 40 + " new.ts", "@@ -0,0 +1 @@", "+new",
    ])
    assert gitstate.legacy_conflicts(out) == ["src/a.ts", "dir with space/edited.ts"]


# -- rows from real branches ----------------------------------------------------------------
def test_merge_rows_set_state_conflict_and_pairwise_rows(repo, ws, merge_tree):
    a, b, c = (add_agent(ws, n) for n in ("a", "b", "c"))
    repo.write("src/auth/session.ts", "export const session = 'a';\n", cwd=a)
    repo.commit("a changes the session", cwd=a)
    repo.write("src/api/orders.ts", "export function orders() {\n  return [1];\n}\n", cwd=b)
    repo.commit("b changes orders()", cwd=b)
    repo.write("src/api/orders.ts", "export function orders() {\n  return [2];\n}\n", cwd=c)
    repo.commit("c changes orders()", cwd=c)
    repo.write("src/auth/session.ts", "export const session = 2;\n")
    repo.commit("main changes the same line")

    d, _ = tracked(ws)
    d.refresh_all()
    r = rows(d)
    main_row = r[("merge", "a", "main", "")]
    assert main_row.color == "red" and "src/auth/session.ts" in main_row.plain and "a × main" in main_row.plain
    assert d.get("a").merge_state == "conflict", "A's state = conflict"
    assert r[("merge", "b", "c", "")].files == ("src/api/orders.ts",), "incompatible changes to the same function"
    assert d.get("b").merge_state == "ready" and d.get("c").merge_state == "ready", "each merges alone"
    assert ("overlap", "b", "c", "") in r
    assert d.counts()["conflicts"] == 2, "the header counts predicted merge conflicts"
    assert {frozenset({"a", "main"}), frozenset({"b", "c"})} <= d.conflicts.red_pairs()
    assert [x.kind for x in d.conflicts.rows][:2] == ["merge", "merge"], "most severe first"
    assert json.loads(json.dumps(d.get("a").to_json()))["merge_state"] == "conflict"

    # the agent rebases (here: starts over from main): the prediction follows its new head
    repo.git("reset", "-q", "--hard", "main", cwd=a)
    repo.write("docs/a.md", "a\n", cwd=a)
    repo.commit("a, rebased", cwd=a)
    d.refresh_all()
    assert ("merge", "a", "main", "") not in rows(d) and d.get("a").merge_state == "ready"


def test_stale_base_row(repo, ws):
    repo.write(".ccdash.toml", "stale_base_commits = 2\n")
    repo.commit("config")
    ws = Workspace.open(repo.root)
    path = add_agent(ws, "old")
    repo.write("docs/old.md", "x\n", cwd=path)
    repo.commit("old work", cwd=path)
    for i in range(2):
        repo.write("README.md", f"# {i}\n")
        repo.commit(f"main {i}")
    d, _ = tracked(ws)
    d.refresh_all()
    assert ("stale", "old", "") not in rows(d), "2 behind is not more than 2"
    repo.write("README.md", "# more\n")
    repo.commit("main again")
    d.refresh_all()
    row = rows(d)[("stale", "old", "")]
    assert row.color == "grey" and "3 commits behind main" in row.plain
    assert d.counts()["conflicts"] == 0


# -- overlap and claims (hook events, no git) -----------------------------------------------
def test_overlap_from_edits_and_dirty_files(repo, ws):
    d, _ = tracked(ws)
    d.views = {v.name: v for v in (view("a"), view("b"), view("gone", alive=False),
                                   view("parked", alive=False, dirty=1, dirty_paths=["src/ui/Toolbar.tsx"]))}
    feed(d, edit("a", "src/ui/Toolbar.tsx"), edit("b", "src/ui/Toolbar.tsx"), edit("b", "src/ui/theme.ts"),
         edit("gone", "src/ui/Toolbar.tsx"))
    d.conflicts.update(d)
    r = rows(d)
    row = r[("overlap", "a", "b", "")]
    assert row.color == "yellow" and row.files == ("src/ui/Toolbar.tsx",)
    assert "overlap" in row.plain and "a × b" in row.plain and "src/ui/Toolbar.tsx" in row.plain
    assert ("overlap", "a", "parked", "") in r, "uncommitted files of a stopped agent are still there"
    assert not any("gone" in k for k in r), "what a stopped agent once edited no longer counts"
    assert frozenset({"a", "b"}) in d.conflicts.red_pairs()
    assert d.counts()["conflicts"] == 0, "an overlap is a warning, not a predicted conflict"
    assert d.conflicts.touch_map()["src/ui/Toolbar.tsx"] == ["a", "b", "parked"]
    plain = row.render().plain
    assert plain.startswith("⚠ overlap") and row.render(flash=True, slide=4).plain.startswith("    ⚠ overlap")


def test_claims_announced_by_the_agent_and_set_by_the_user(repo, ws):
    d, _ = tracked(ws)
    d.views = {v.name: v for v in (view("a"), view("b"))}
    feed(d, say("a", "b", "Files: src/reports/** src/ui/Toolbar.tsx\nI add the export button"),
         edit("b", "src/reports/export.ts"))
    d.conflicts.update(d)
    assert rows(d)[("overlap", "a", "b", "")].files == ("src/reports/export.ts",), "b edits inside a's announced scope"
    assert d.conflicts.owned("a") == ["src/reports/**", "src/ui/Toolbar.tsx"]
    assert d.conflicts.owned("b") == ["src/reports/export.ts"]

    assert d.conflicts.claim("b", ["src/auth/**", ".\\src\\ui\\theme.ts", "src/auth/**"]) == ["src/auth/**", "src/ui/theme.ts"]
    feed(d, edit("a", "src/auth/session.ts"))
    d.conflicts.update(d)
    assert "src/auth/session.ts" in rows(d)[("overlap", "a", "b", "")].files
    data = json.loads(ws.state.ownership_path.read_text(encoding="utf-8"))
    assert data["agents"]["b"]["user"] == ["src/auth/**", "src/ui/theme.ts"]
    assert data["agents"]["a"]["claimed"] == ["src/reports/**", "src/ui/Toolbar.tsx"]
    assert ws.claims()["a"][:2] == ["src/reports/**", "src/ui/Toolbar.tsx"], "a new agent's prompt lists them"

    reopened, _ = tracked(ws)
    assert reopened.conflicts.user_claims("b") == ["src/auth/**", "src/ui/theme.ts"], "claims survive a restart"
    d.conflicts.claim("b", [])
    d.conflicts.update(d)
    assert "user" not in json.loads(ws.state.ownership_path.read_text(encoding="utf-8"))["agents"].get("b", {})


def test_overlapping_claims_and_glob_matching(repo, ws):
    assert matches("src/api/**", "src/api/orders.ts") and matches("package.json", "package.json")
    assert matches("src/api/", "src/api/v2/orders.ts") and matches("*.lock", "web/yarn.lock")
    assert not matches("src/api/**", "src/apix/orders.ts") and not matches("src/api/**", "")
    d, _ = tracked(ws)
    d.views = {v.name: v for v in (view("a"), view("b"), view("c"))}
    feed(d, say("a", "all", "Files: src/ui/**"), say("b", "all", "Files: src/ui/Toolbar.tsx"),
         say("c", "all", "Files: docs/**"))
    d.conflicts.update(d)
    r = rows(d)
    assert r[("overlap", "a", "b", "")].files == ("src/ui/Toolbar.tsx",)
    assert not any("c" in k[1:3] for k in r)


# -- shared interfaces ----------------------------------------------------------------------
@pytest.fixture
def shared_ws(repo):
    repo.write(".ccdash.toml", 'shared = ["src/api/**", "package.json"]\n')
    return Workspace.open(repo.root)


def test_shared_change_row_then_one_notice_after_two_minutes(shared_ws):
    now = [1000.0]
    d, sent = tracked(shared_ws, clock=lambda: now[0])
    d.views = {v.name: v for v in (view("a"), view("b"), view("c"))}
    feed(d, edit("a", "src/api/orders.ts", t=1000.0), edit("b", "src/ui/Toolbar.tsx", t=1000.0))
    d.conflicts.update(d)
    row = rows(d)[("shared", "a", "src/api/**")]
    assert row.color == "yellow" and "a changed" in row.plain and "not announced" in row.plain
    assert "120s" in row.plain and sent == []
    now[0] = 1119.0
    d.conflicts.update(d)
    assert sent == []
    now[0] = 1121.0
    d.conflicts.update(d)
    assert len(sent) == 1, "ccdash broadcasts after shared_notice_after"
    to, text, exclude = sent[0]
    assert to == "all" and exclude == {"a"}
    assert "a changed the shared interface src/api/orders.ts" in text and "src/api/**" in text
    row = rows(d)[("shared", "a", "src/api/**")]
    assert row.color == "blue" and "2 agents notified" in row.plain
    now[0] = 1500.0
    d.conflicts.update(d)
    assert len(sent) == 1, "once"

    reopened, sent2 = tracked(shared_ws, clock=lambda: now[0])
    reopened.views = d.views
    feed(reopened, edit("a", "src/api/orders.ts", t=1000.0))
    reopened.conflicts.update(reopened)
    assert sent2 == [], "a reopened dashboard does not repeat the notice"
    assert rows(reopened)[("shared", "a", "src/api/**")].color == "blue"


def test_shared_change_the_agent_announced_needs_no_notice(shared_ws):
    now = [2000.0]
    d, sent = tracked(shared_ws, clock=lambda: now[0])
    d.views = {v.name: v for v in (view("a"), view("b"))}
    feed(d, say("a", "b", "Heads up: src/api/orders.ts will return pages, {items, next}", t=990.0),
         edit("a", "src/api/orders.ts", t=1000.0))
    d.conflicts.update(d)
    row = rows(d)[("shared", "a", "src/api/**")]
    assert sent == [] and row.color == "blue" and "announced" in row.plain


def test_shared_notice_waits_for_someone_to_tell_and_only_the_dashboard_sends(shared_ws):
    now = [5000.0]
    d, sent = tracked(shared_ws, clock=lambda: now[0])
    d.views = {"a": view("a")}
    feed(d, edit("a", "package.json", t=1000.0))
    d.conflicts.update(d)
    assert sent == [] and rows(d)[("shared", "a", "package.json")].color == "yellow", "nobody else is running"
    d.views = {"a": view("a"), "b": view("b")}
    passive, quiet = tracked(shared_ws, active=False, clock=lambda: now[0])
    passive.views = d.views
    feed(passive, edit("a", "package.json", t=1000.0))
    passive.conflicts.update(passive)
    assert quiet == [], "`cc ls` looks, it never sends"
    d.conflicts.update(d)
    assert len(sent) == 1


# -- local merge ------------------------------------------------------------------------------
def three_agents(repo, ws, monkeypatch):
    """a is ready and changes README and the session; b (running) changed the session too; c (running) is clean."""
    a, b, _c = (add_agent(ws, n) for n in ("a", "b", "c"))
    repo.write("README.md", "# my-app, by a\n", cwd=a)
    repo.write("src/auth/session.ts", "export const session = 'a';\n", cwd=a)
    repo.commit("a: readme and session", cwd=a)
    repo.write("src/auth/session.ts", "export const session = 'b';\n", cwd=b)
    repo.commit("b: session", cwd=b)
    monkeypatch.setattr(ws.runner, "is_alive", lambda rec: rec.name in ("b", "c"))


def test_local_merge_lands_notifies_the_others_and_refreshes(repo, ws, monkeypatch):
    three_agents(repo, ws, monkeypatch)
    d, sent = tracked(ws)
    d.refresh_all()
    assert d.get("a").merge_state == "ready"
    assert ("merge", "a", "b", "") in rows(d)
    before = repo.git("rev-parse", "main").stdout.strip()

    res = merge.merge_local(ws, "a", d)

    assert repo.git("rev-parse", "main^1").stdout.strip() == before, "main only gained a commit"
    assert len(repo.git("log", "-1", "--format=%P", "main").stdout.split()) == 2, "a merge commit, never a fast-forward"
    assert repo.git("status", "--porcelain", "--untracked-files=no").stdout.strip() == ""
    assert sorted(res.files) == ["README.md", "src/auth/session.ts"] and res.sha != before
    assert res.notified == ["b", "c"], "B and C receive the notice"
    texts = {to: text for to, text, _ in sent}
    assert "a (task of a) landed on main" in texts["b"] and "README.md" in texts["b"]
    assert "now conflicts with main in src/auth/session.ts" in texts["b"]
    assert "still merges cleanly" in texts["c"]
    assert d.get("a").merge_state == "merged"
    assert d.get("b").merge_state == "conflict", "their predictions refresh"
    assert ("merge", "b", "main", "") in rows(d) and ("merge", "a", "b", "") not in rows(d)
    d.refresh_all()
    reopened, sent2 = tracked(ws)
    reopened.refresh_all()
    assert len(sent) == 2 and sent2 == [], "told once"


def test_local_merge_refuses_what_is_not_ready(repo, ws, monkeypatch):
    three_agents(repo, ws, monkeypatch)
    d, _ = tracked(ws)
    d.refresh_all()
    with pytest.raises(merge.MergeError, match="not ready"):
        merge.merge_local(ws, "b", d)  # still running
    with pytest.raises(merge.MergeError, match="not ready"):
        merge.merge_local(ws, "c", d)  # nothing to merge
    main = repo.git("rev-parse", "main").stdout.strip()

    repo.write("README.md", "# edited by hand\n")
    with pytest.raises(merge.MergeError, match="uncommitted changes"):
        merge.merge_local(ws, "a", d)
    repo.git("checkout", "-q", "--", "README.md")
    repo.git("checkout", "-q", "-b", "side")
    with pytest.raises(merge.MergeError, match="is on side, not on main"):
        merge.merge_local(ws, "a", d)
    repo.git("checkout", "-q", "main")
    assert repo.git("rev-parse", "main").stdout.strip() == main


def test_local_merge_aborts_when_tests_fail_or_git_conflicts(repo, ws, monkeypatch):
    repo.write(".ccdash.toml", 'test = "exit 3"\n')
    repo.commit("config")
    ws = Workspace.open(repo.root)
    three_agents(repo, ws, monkeypatch)
    d, sent = tracked(ws)
    d.refresh_all()
    main = repo.git("rev-parse", "main").stdout.strip()

    with pytest.raises(merge.MergeError, match=r"failed \(exit 3\).*aborted"):
        merge.merge_local(ws, "a", d)
    assert repo.git("rev-parse", "main").stdout.strip() == main, "the default branch is never rewritten"
    assert not (repo.root / ".git" / "MERGE_HEAD").exists()
    assert repo.git("status", "--porcelain", "--untracked-files=no").stdout.strip() == ""
    assert "exit 3" in (ws.state.logs_dir / "a.merge-test.log").read_text(encoding="utf-8")

    # main moves under a dashboard that still says "ready": git finds the conflict, the merge is aborted
    ws.cfg.test = None
    repo.write("src/auth/session.ts", "export const session = 'main';\n")
    main = repo.commit("main changes the session")
    with pytest.raises(merge.MergeError, match="conflicts in src/auth/session.ts"):
        merge.merge_local(ws, "a", d)
    assert repo.git("rev-parse", "main").stdout.strip() == main
    assert not (repo.root / ".git" / "MERGE_HEAD").exists()
    assert repo.git("status", "--porcelain", "--untracked-files=no").stdout.strip() == ""
    assert sent == [], "nothing landed, nobody is told"


def test_local_merge_runs_the_tests_on_the_merged_tree(repo, ws, monkeypatch):
    # passes only if the merged tree has a's README, before the merge is committed
    cmd = f'"{sys.executable}" -c "import sys; sys.exit(0 if \'by a\' in open(\'README.md\').read() else 5)"'
    repo.write(".ccdash.toml", "test = " + json.dumps(cmd) + "\n")
    repo.commit("config")
    ws = Workspace.open(repo.root)
    three_agents(repo, ws, monkeypatch)
    d, _ = tracked(ws)
    d.refresh_all()
    res = merge.merge_local(ws, "a", d)
    assert res.tested and "by a" in (repo.root / "README.md").read_text()


# -- pull requests ------------------------------------------------------------------------------
def test_open_pr_pushes_the_branch_and_the_state_shows_the_pr(repo, ws, fake_gh):
    path = add_agent(ws, "a")
    repo.write("docs/a.md", "a\n", cwd=path)
    head = repo.commit("a", cwd=path)
    d, _ = tracked(ws)
    d.refresh_all()
    assert d.get("a").merge_state == "ready"
    assert [c[0] for c in merge.menu(ws, d.get("a"), d).choices] == ["l", "p"]

    url = merge.open_pr(ws, "a", d)
    assert url == "https://example.invalid/pull/1"
    assert repo.git("ls-remote", "--heads", "origin", "cc/a").stdout.split()[0] == head, "pushed, as it is"
    d.refresh_all()
    assert d.get("a").merge_state == "PR #1 …", "checks still running"
    assert [c[0] for c in merge.menu(ws, d.get("a"), d).choices] == [], "PR open: nothing left to offer for a stopped agent"

    prs = json.loads(fake_gh.read_text())
    prs["cc/a"]["statusCheckRollup"] = [{"status": "COMPLETED", "conclusion": "SUCCESS"}]
    fake_gh.write_text(json.dumps(prs))
    d.conflicts.prs.forget("cc/a")
    d.refresh_all()
    assert d.get("a").merge_state == "PR #1 ✓"
    prs["cc/a"]["state"] = "MERGED"
    fake_gh.write_text(json.dumps(prs))
    d.conflicts.prs.forget("cc/a")
    d.refresh_all()
    assert d.get("a").merge_state == "merged", "merged on the remote counts as merged"


def test_pr_states_and_missing_gh(repo, ws, monkeypatch):
    assert github.summarize_checks([]) == ""
    assert github.summarize_checks([{"status": "COMPLETED", "conclusion": "SUCCESS"}, {"state": "SUCCESS"}]) == "✓"
    assert github.summarize_checks([{"status": "COMPLETED", "conclusion": "FAILURE"}, {"status": "QUEUED"}]) == "✗"
    assert github.summarize_checks([{"status": "IN_PROGRESS"}, {"state": "SUCCESS"}]) == "…"
    assert github.PullRequest(12, "OPEN", "✓").label == "PR #12 ✓"

    monkeypatch.setenv("CCDASH_GH", json.dumps(["no-such-gh-binary-anywhere"]))
    github._auth.clear()
    path = add_agent(ws, "a")
    repo.write("docs/a.md", "a\n", cwd=path)
    repo.commit("a", cwd=path)
    d, _ = tracked(ws)
    d.refresh_all()
    assert d.get("a").merge_state == "ready", "no gh: no PR state, and nothing breaks"
    assert [c[0] for c in merge.menu(ws, d.get("a"), d).choices] == ["l"], "open PR is hidden"
    with pytest.raises(merge.MergeError, match="gh"):
        merge.open_pr(ws, "a", d)
    github._auth.clear()


def test_merge_menu_explains_itself(repo, ws, monkeypatch, fake_gh):
    three_agents(repo, ws, monkeypatch)
    repo.write("src/auth/session.ts", "export const session = 'main';\n")
    repo.commit("main changes the session")
    d, _ = tracked(ws)
    d.refresh_all()
    m = merge.menu(ws, d.get("b"), d)  # running, in conflict with main
    assert [c[0] for c in m.choices] == ["r", "p"]
    assert "Conflicts with main in src/auth/session.ts" in m.note and "No local merge: it conflicts with main" in m.note
    m = merge.menu(ws, d.get("c"), d)  # running, nothing committed
    assert [c[0] for c in m.choices] == ["r"] and "nothing to merge" in m.note
    assert merge.rebase_text("main") == "Rebase on main, resolve conflicts, run the tests, report back."


def test_nothing_ever_force_pushes():
    from pathlib import Path

    import ccdash

    for src in Path(ccdash.__file__).parent.rglob("*.py"):
        for line in src.read_text(encoding="utf-8").splitlines():
            if '"push"' in line:
                assert "force" not in line and '"-f"' not in line and "+" not in line.split('"push"')[1], (src.name, line)


def test_diff_command_uses_a_pager_that_waits(repo, ws):
    path = add_agent(ws, "a")
    assert not merge.has_diff(ws, "cc/a")
    repo.write("docs/a.md", "a\n", cwd=path)
    repo.commit("a", cwd=path)
    assert merge.has_diff(ws, "cc/a")
    argv, env = merge.diff_command(ws, "cc/a")
    assert argv[0] == "git" and "diff" in argv and argv[-1] == "main...cc/a"
    assert "--paginate" in argv and env["LESS"] == "-R", "less must not quit at once on a short diff"
    if sys.platform == "win32":
        assert any(a.startswith("core.pager=") for a in argv)


# -- end to end: the scenarios from docs/PLAN.md, with the fake claude ---------------------------
def live(ws, active=True):
    d = Dashboard(ws)
    d.conflicts.active = active
    d.refresh_all()
    return d


def poll(d, key, git=False, timeout=8.0):
    def check():
        d.refresh_all() if git else d.refresh_runtime()
        return rows(d).get(key)

    return wait_for(check, timeout=timeout, message=f"row {key}")


@windows_runner
def test_scenario_two_agents_edit_the_same_file_uncommitted(repo, ws, set_scenario):
    set_scenario({"a": [{"edit": "src/ui/Toolbar.tsx", "text": "// a\n"}, {"sleep": 30}],
                  "b": [{"sleep": 1.5}, {"edit": "src/ui/Toolbar.tsx", "text": "// b\n"}, {"sleep": 30}]})
    ws.new_agent("a edits the toolbar", name="a")
    d = live(ws)  # the dashboard is open and watching when b makes the second edit
    ws.new_agent("b edits the toolbar", name="b")
    row = poll(d, ("overlap", "a", "b", ""))
    seen = time.time()
    second_edit = max(t for (agent, path), t in d.conflicts._first_edit.items() if path == "src/ui/Toolbar.tsx")
    assert seen - second_edit < 2.0, "overlap row A × B within 2 s"
    assert row.files == ("src/ui/Toolbar.tsx",)
    assert repo.git("log", "--oneline", "main..cc/a").stdout.strip() == "", "nothing is committed: git alone would not know"


@windows_runner
def test_scenario_commit_conflicts_with_main_and_between_agents(repo, ws, set_scenario):
    set_scenario({"a": [{"edit": "src/auth/session.ts", "text": "export const redirect = 'a';\n"}, {"commit": "a"}],
                  "b": [{"write": "src/api/orders.ts", "text": "export function orders() {\n  return [1];\n}\n"},
                        {"commit": "b"}],
                  "c": [{"write": "src/api/orders.ts", "text": "export function orders() {\n  return [2];\n}\n"},
                        {"commit": "c"}]})
    for n in ("a", "b", "c"):
        ws.new_agent(f"{n} works", name=n)
    with open(repo.root / "src" / "auth" / "session.ts", "a", encoding="utf-8", newline="\n") as f:
        f.write("export const redirect = 'main';\n")
    repo.commit("main also adds redirect")
    d = live(ws)
    poll(d, ("merge", "a", "main", ""), git=True)
    assert d.get("a").merge_state == "conflict"
    poll(d, ("merge", "b", "c", ""), git=True)


@windows_runner
def test_scenario_shared_change_without_a_message_gets_a_ccdash_broadcast(repo, set_scenario):
    repo.write(".ccdash.toml", 'shared = ["src/api/**"]\nshared_notice_after = 2\n')
    set_scenario({"a": [{"edit": "src/api/orders.ts", "text": "// pages\n"}, {"sleep": 30}], "*": []})
    ws = Workspace.open(repo.root)
    ws.new_agent("a changes the api", name="a")
    ws.new_agent("b depends on it", name="b")
    d = live(ws)
    poll(d, ("shared", "a", "src/api/**"))  # yellow first, unless the 2 s are already over (see the unit tests)
    wait_for(lambda: d.refresh_runtime() or rows(d)[("shared", "a", "src/api/**")].color == "blue", timeout=8,
             message="notice sent")
    wait_for(lambda: "[ccdash] a changed the shared interface src/api/orders.ts" in "\n".join(ws.peek("b", 60)),
             timeout=6, message="b got the broadcast")
    assert "[ccdash]" not in "\n".join(ws.peek("a", 60)), "the agent that made the change is not told"
    d.refresh_events()
    assert any(m.kind == "ccdash" and m.to == "all" for m in d.messages.items), "shown in Messages as ccdash → all"


@windows_runner
def test_scenario_local_merge_tells_the_other_agents(repo, ws, set_scenario):
    set_scenario({"a": [{"edit": "src/ui/Toolbar.tsx", "text": "// export\n"}, {"commit": "a: toolbar"},
                        {"say": "DONE: toolbar has an export button"}], "*": []})
    for n in ("a", "b", "c"):
        ws.new_agent(f"{n} works", name=n)
    d = live(ws)
    wait_for(lambda: d.refresh_all() or d.get("a").merge_state == "ready", timeout=10, message="a ready")
    res = merge.merge_local(ws, "a", d)
    assert res.notified == ["b", "c"]
    for n in ("b", "c"):
        wait_for(lambda n=n: "[ccdash] a (a works) landed on main" in "\n".join(ws.peek(n, 60)), timeout=6,
                 message=f"{n} got the notice")
    assert d.get("a").merge_state == "merged"
    assert "src/ui/Toolbar.tsx" in messaging.one_line("\n".join(ws.peek("b", 60))).replace(" ", "")


@windows_runner
def test_scenario_killed_session_shows_crashed_and_keeps_the_worktree(repo, ws, set_scenario):
    set_scenario([{"write": "wip.txt", "text": "wip\n"}, {"sleep": 30}])
    rec = ws.new_agent("gets killed", name="victim")
    wt = repo.root / ".claude" / "worktrees" / "victim"
    wait_for(lambda: (wt / "wip.txt").exists(), message="agent wrote a file")
    ws.runner.kill(ws.record("victim"))
    d = live(ws)
    wait_for(lambda: d.refresh_runtime() or d.get("victim").status == "crashed", timeout=8, message="crashed")
    assert (wt / "wip.txt").read_text() == "wip\n", "worktree untouched"
    assert repo.git("branch", "--list", rec.branch).stdout.strip()


def test_row_is_what_the_panel_draws():
    row = Row("shared", ("api-pagination",), "src/api/**  (2 agents notified {when})", "blue", ident="src/api/**",
              when=time.time())
    assert row.key == ("shared", "api-pagination", "src/api/**")
    assert "{when}" not in row.plain and "api-pagination changed" in row.plain
    assert Row("merge", ("a", "main"), "x (both changed)", "red").render().plain.startswith("⚠ merge")
