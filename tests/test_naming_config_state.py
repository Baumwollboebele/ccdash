"""Phase 1 unit tests: naming, config precedence, state dir and registry."""

import json
import threading

import pytest

from ccdash.config import ConfigError, load_config
from ccdash.model import AgentRecord
from ccdash.naming import is_valid_name, slugify, unique_name
from ccdash.repo import RepoError, default_branch, discover
from ccdash.state import FileLock, LockTimeout, Registry, State


# -- naming -----------------------------------------------------------------
@pytest.mark.parametrize(
    "task, slug",
    [
        ("Add CSV export to the reports page", "add-csv-export-to"),
        ("Fix login redirect bug", "fix-login-redirect-bug"),
        ("  Dark mode   toggle!! ", "dark-mode-toggle"),
        ("Füge Größe hinzu", "fuge-grosse-hinzu"),
        ("Internationalization internationalization internationalization", "internationalization-internati"),
        ("!!!", "agent"),
        ("Paginate /orders endpoint", "paginate-orders-endpoint"),
    ],
)
def test_slugify(task, slug):
    assert slugify(task) == slug
    assert len(slug) <= 30
    assert is_valid_name(slug)


def test_unique_name_adds_suffix_within_limit():
    assert unique_name("csv-export", set()) == "csv-export"
    assert unique_name("csv-export", {"csv-export"}) == "csv-export-2"
    assert unique_name("csv-export", {"csv-export", "csv-export-2"}) == "csv-export-3"
    long = "a" * 30
    got = unique_name(long, {long})
    assert got == "a" * 28 + "-2" and len(got) == 30


def test_is_valid_name():
    assert is_valid_name("csv-export-2")
    for bad in ("", "-x", "UPPER", "a b", "a/b", "x" * 31, "..", "con"):
        assert not is_valid_name(bad), bad


def test_registry_load_waits_out_a_file_that_is_being_replaced(tmp_path, monkeypatch):
    """Windows: for the instant a writer replaces agents.json, opening it fails with PermissionError."""
    from pathlib import Path

    state = State(tmp_path)
    reg = Registry(state)
    reg.put(AgentRecord(name="a", task="t"))
    real, calls = Path.read_text, []

    def flaky(self, *args, **kw):
        if self == state.agents_path and len(calls) < 3:
            calls.append(1)
            raise PermissionError(13, "Permission denied")
        return real(self, *args, **kw)

    monkeypatch.setattr(Path, "read_text", flaky)
    assert list(reg.load()) == ["a"] and len(calls) == 3


# -- config -----------------------------------------------------------------
def test_config_defaults(repo):
    cfg = load_config(repo.root)
    assert cfg.branch_prefix == "cc/"
    assert cfg.worktree_dir == ".claude/worktrees"
    assert cfg.permission_mode is None
    assert cfg.max_agents == 6
    assert cfg.guard == "off"


def test_config_user_overrides_repo(repo):
    repo.write(".ccdash.toml", 'setup = "npm ci"\nmax_agents = 3\nshared = ["src/api/**"]\n')
    (repo.home / "ccdash-config.toml").write_text('max_agents = 4\nmodel = "sonnet"\n', encoding="utf-8")
    cfg = load_config(repo.root)
    assert cfg.setup == "npm ci"  # repo only
    assert cfg.max_agents == 4  # user wins
    assert cfg.model == "sonnet"
    assert cfg.shared == ["src/api/**"]


def test_config_rejects_bad_values(repo):
    repo.write(".ccdash.toml", 'guard = "maybe"\n')
    with pytest.raises(ConfigError, match="guard"):
        load_config(repo.root)
    repo.write(".ccdash.toml", "max_agents = 'six'\n")
    with pytest.raises(ConfigError, match="max_agents"):
        load_config(repo.root)


# -- repo discovery -----------------------------------------------------------
def test_discover_from_subdir_and_worktree(repo):
    r = discover(repo.root / "src" / "ui")
    assert r.main_root == repo.root.resolve()
    assert r.state_dir == (repo.root / ".git" / "ccdash").resolve()
    assert r.name == "my-app"
    wt = repo.root.parent / "other-wt"
    repo.git("worktree", "add", "-q", "-b", "side", str(wt))
    r2 = discover(wt)
    assert r2.main_root == r.main_root and r2.state_dir == r.state_dir and r2.key == r.key


def test_discover_outside_repo(tmp_path, sandbox):
    with pytest.raises(RepoError):
        discover(tmp_path)


def test_default_branch(repo):
    r = discover(repo.root)
    assert default_branch(r, load_config(repo.root)) == "main"
    repo.write(".ccdash.toml", 'default_branch = "develop"\n')
    assert default_branch(r, load_config(repo.root)) == "develop"


# -- state ------------------------------------------------------------------
def test_registry_roundtrip_and_atomic_write(repo):
    st = State(discover(repo.root).state_dir)
    reg = Registry(st)
    assert reg.load() == {}
    rec = AgentRecord(name="csv-export", task="Add CSV export", worktree=".claude/worktrees/csv-export",
                      branch="cc/csv-export", base_ref="main", base_sha="abc1234")
    reg.put(rec)
    with reg.edit() as agents:
        agents["csv-export"].lifecycle = "stopped"
    loaded = reg.load()["csv-export"]
    assert loaded.lifecycle == "stopped" and loaded.task == "Add CSV export"
    raw = json.loads(st.agents_path.read_text(encoding="utf-8"))
    assert raw["agents"][0]["name"] == "csv-export"
    assert not list(st.root.glob("*.tmp"))


def test_registry_keeps_unknown_fields(repo):
    st = State(discover(repo.root).state_dir)
    st.root.mkdir(parents=True, exist_ok=True)
    st.agents_path.write_text(json.dumps({"version": 1, "agents": [{"name": "x", "task": "t", "future_field": 7}]}))
    reg = Registry(st)
    reg.put(reg.load()["x"])
    assert json.loads(st.agents_path.read_text())["agents"][0]["future_field"] == 7


def test_registry_concurrent_edits_do_not_lose_updates(repo):
    st = State(discover(repo.root).state_dir)
    reg = Registry(st)
    for i in range(4):
        reg.put(AgentRecord(name=f"a{i}", task="t"))

    def bump(name):
        for _ in range(10):
            with reg.edit() as agents:
                agents[name].restarts += 1

    threads = [threading.Thread(target=bump, args=(f"a{i}",)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert all(r.restarts == 10 for r in reg.load().values())


def test_file_lock_times_out(tmp_path):
    path = tmp_path / "x.lock"
    with FileLock(path, timeout=1):
        with pytest.raises(LockTimeout):
            with FileLock(path, timeout=0.1):
                pass
    with FileLock(path, timeout=0.1):
        pass
