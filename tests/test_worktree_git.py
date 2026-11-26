"""Phase 1: worktree creation, include-copy, setup, removal and git-derived state."""

import sys

import pytest

from ccdash import gitstate, worktree
from ccdash.config import load_config
from ccdash.repo import discover


@pytest.fixture
def ctx(repo):
    return discover(repo.root), load_config(repo.root)


def test_create_worktree_branch_exclude_and_includes(repo, ctx):
    r, cfg = ctx
    wt = worktree.create_worktree(r, cfg, "csv-export", base="main")
    assert wt.path == (repo.root / ".claude" / "worktrees" / "csv-export").resolve()
    assert wt.branch == "cc/csv-export"
    assert wt.base_sha == repo.git("rev-parse", "main").stdout.strip()
    assert (wt.path / "src" / "ui" / "Toolbar.tsx").exists()
    # .env is gitignored and listed in .worktreeinclude -> copied; README is tracked -> checked out, not copied
    copied = worktree.copy_includes(r.main_root, wt.path)
    assert copied == [".env"]
    assert (wt.path / ".env").read_text() == "SECRET=1\n"
    # the main checkout must not see the worktrees dir as untracked
    assert repo.git("status", "--porcelain").stdout.strip() == ""
    exclude = (repo.root / ".git" / "info" / "exclude").read_text()
    assert exclude.count("/.claude/worktrees/") == 1
    worktree.create_worktree(r, cfg, "second", base="main")
    assert (repo.root / ".git" / "info" / "exclude").read_text().count("/.claude/worktrees/") == 1


def test_include_copy_only_gitignored_matches(repo, ctx):
    r, cfg = ctx
    repo.write(".worktreeinclude", ".env\nconfig/*.json\nREADME.md\n")
    repo.write(".gitignore", ".env\nnode_modules/\nconfig/secrets.json\n")
    repo.commit("include rules")
    repo.write("config/secrets.json", "{}")  # ignored + listed -> copied
    repo.write("config/public.json", "{}")  # untracked, NOT ignored -> not copied
    repo.write("node_modules/x/index.js", "")  # ignored, not listed -> not copied
    wt = worktree.create_worktree(r, cfg, "inc", base="main")
    copied = worktree.copy_includes(r.main_root, wt.path)
    assert sorted(copied) == [".env", "config/secrets.json"]
    assert not (wt.path / "config" / "public.json").exists()
    assert not (wt.path / "node_modules").exists()


def test_failed_worktree_add_rolls_back_branch(repo, ctx):
    r, cfg = ctx
    (repo.root / ".claude" / "worktrees" / "taken").mkdir(parents=True)
    (repo.root / ".claude" / "worktrees" / "taken" / "file").write_text("occupied")
    with pytest.raises(worktree.WorktreeError):
        worktree.create_worktree(r, cfg, "taken", base="main")
    assert repo.git("branch", "--list", "cc/taken").stdout.strip() == ""
    assert (repo.root / ".claude" / "worktrees" / "taken" / "file").read_text() == "occupied"


def test_bad_base_rolls_back(repo, ctx):
    r, cfg = ctx
    with pytest.raises(worktree.WorktreeError):
        worktree.create_worktree(r, cfg, "nobase", base="does-not-exist")
    assert repo.git("branch", "--list", "cc/nobase").stdout.strip() == ""
    assert not (repo.root / ".claude" / "worktrees" / "nobase").exists()


@pytest.mark.skipif(sys.platform != "win32", reason="MAX_PATH is a Windows limit")
def test_longpaths_enabled_when_needed(repo, ctx):
    r, cfg = ctx
    deep = "/".join(["deeply-nested-directory-level"] * 6) + "/file-with-a-long-name.txt"
    repo.git("config", "core.longpaths", "true")  # needed to create the fixture itself
    repo.write(deep, "x\n")
    repo.commit("deep")
    repo.git("config", "--unset", "core.longpaths")
    # the sandbox's global config sets core.longpaths; hide it to see ccdash set it locally
    repo.git("config", "--global", "--unset", "core.longpaths")
    assert worktree.needs_longpaths(r, cfg, "a" * 30)
    wt = worktree.create_worktree(r, cfg, "a" * 30, base="main")
    assert repo.git("config", "--local", "core.longpaths").stdout.strip() == "true"
    assert (wt.path / deep).exists()


def test_run_setup_logs_output_and_exit_code(repo, ctx, tmp_path):
    r, cfg = ctx
    wt = worktree.create_worktree(r, cfg, "setup", base="main")
    log = tmp_path / "setup.log"
    lines = []
    code = worktree.run_setup("echo installing && echo done", wt.path, log, on_line=lines.append)
    assert code == 0
    assert "installing" in log.read_text() and any("done" in x for x in lines)
    assert worktree.run_setup("exit 3", wt.path, log) == 3


def test_remove_worktree_keeps_nothing(repo, ctx):
    r, cfg = ctx
    wt = worktree.create_worktree(r, cfg, "gone", base="main")
    (wt.path / "scratch.txt").write_text("dirty")
    worktree.remove_worktree(r, wt.path, wt.branch, force=True)
    assert not wt.path.exists()
    assert repo.git("branch", "--list", "cc/gone").stdout.strip() == ""
    assert "gone" not in repo.git("worktree", "list").stdout


# -- git-derived state ---------------------------------------------------------
def test_branch_state_ahead_behind_dirty_merged(repo, ctx):
    r, cfg = ctx
    wt = worktree.create_worktree(r, cfg, "feat", base="main")
    st = gitstate.branch_state(r, "main", wt.path, wt.branch)
    assert (st.ahead, st.behind, st.dirty, st.merged) == (0, 0, 0, True)

    repo.write("src/ui/Toolbar.tsx", "changed\n", cwd=wt.path)
    repo.write("new.txt", "new\n", cwd=wt.path)
    st = gitstate.branch_state(r, "main", wt.path, wt.branch)
    assert st.dirty == 2 and st.files == 2

    repo.commit("add writer", cwd=wt.path)
    repo.write("README.md", "# my-app v2\n")
    repo.commit("main moves")
    st = gitstate.branch_state(r, "main", wt.path, wt.branch)
    assert (st.ahead, st.behind, st.dirty) == (1, 1, 0)
    assert st.files == 2 and st.last_subject == "add writer" and not st.merged

    repo.git("merge", "-q", "--no-ff", "-m", "merge feat", wt.branch)
    st = gitstate.branch_state(r, "main", wt.path, wt.branch)
    assert st.merged and st.ahead == 0


def test_unpushed_and_unmerged_detection(repo, ctx):
    r, cfg = ctx
    wt = worktree.create_worktree(r, cfg, "risky", base="main")
    loss = gitstate.work_at_risk(r, "main", wt.path, wt.branch)
    assert loss.empty
    repo.write("a.txt", "a\n", cwd=wt.path)
    loss = gitstate.work_at_risk(r, "main", wt.path, wt.branch)
    assert loss.uncommitted == 1 and not loss.empty
    repo.commit("a", cwd=wt.path)
    loss = gitstate.work_at_risk(r, "main", wt.path, wt.branch)
    assert loss.uncommitted == 0 and loss.unmerged_commits == 1 and loss.unpushed_commits == 1
    repo.git("push", "-q", "-u", "origin", wt.branch, cwd=wt.path)
    loss = gitstate.work_at_risk(r, "main", wt.path, wt.branch)
    assert loss.unpushed_commits == 0 and loss.unmerged_commits == 1
