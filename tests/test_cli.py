"""Phase 1: the `cc` command line (`cc "task"`, `cc ls`, `cc stop`)."""

import json
import os
import subprocess
import sys
import time

import pytest

from conftest import wait_for

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows runner (tmux variant runs in CI)")


def cc(*args, cwd, timeout=30):
    return subprocess.run([sys.executable, "-m", "ccdash", *args], cwd=cwd, capture_output=True, text=True,
                          timeout=timeout, env=os.environ.copy(), encoding="utf-8")


def test_cc_task_ls_stop(repo):
    t = time.monotonic()
    p = cc("Add CSV export to the reports page", cwd=repo.root / "src")
    assert p.returncode == 0, p.stderr
    assert time.monotonic() - t < 5
    assert "add-csv-export-to" in p.stdout

    ls = cc("ls", "--json", cwd=repo.root)
    rows = json.loads(ls.stdout)
    assert [r["name"] for r in rows] == ["add-csv-export-to"]
    assert rows[0]["alive"] is True and rows[0]["branch"] == "cc/add-csv-export-to"

    human = cc("ls", cwd=repo.root)
    assert "add-csv-export-to" in human.stdout and "cc/add-csv-export-to" in human.stdout

    st = cc("stop", "add-csv-export-to", cwd=repo.root)
    assert st.returncode == 0, st.stderr
    rows = json.loads(cc("ls", "--json", cwd=repo.root).stdout)
    assert rows[0]["alive"] is False and rows[0]["status"] == "stopped"


def test_cc_errors_are_short_and_nonzero(repo, tmp_path):
    p = cc("ls", cwd=tmp_path)
    assert p.returncode != 0 and "not inside a git repository" in p.stderr
    p = cc("stop", "nope", cwd=repo.root)
    assert p.returncode != 0 and "no agent named 'nope'" in p.stderr


def test_cli_fast_paths_do_not_import_textual(repo):
    code = ("import sys; from ccdash import cli; cli.main(['ls', '--json']); "
            "print('TEXTUAL' if 'textual' in sys.modules else 'LEAN', file=sys.stderr)")
    p = subprocess.run([sys.executable, "-c", code], cwd=repo.root, capture_output=True, text=True)
    assert "LEAN" in p.stderr, p.stderr
