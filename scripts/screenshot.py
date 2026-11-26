"""Render the dashboard with demo agents (fake claude) and save an SVG screenshot.

    python scripts/screenshot.py docs/screenshots/main.svg [--size 120x35] [--view comms|network|filemap]

Builds a throwaway repo in a temp dir, so nothing touches a real repo or Claude.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))


def git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def demo_repo(base: Path) -> Path:
    home = base / "home"
    home.mkdir()
    (home / "gitconfig").write_text("[user]\n\tname = demo\n\temail = demo@example.invalid\n"
                                    "[init]\n\tdefaultBranch = main\n[core]\n\tautocrlf = false\n")
    os.environ.update({
        "GIT_CONFIG_GLOBAL": str(home / "gitconfig"), "GIT_CONFIG_NOSYSTEM": "1",
        "CCDASH_CONFIG": str(home / "cfg.toml"), "CCDASH_CLAUDE_JSON": str(home / "claude.json"),
        "CCDASH_CLAUDE": json.dumps([sys.executable, str(ROOT / "tests" / "fake_claude.py")]),
        "CCDASH_FAKE_HOME": str(home / "fake"), "CLAUDE_CONFIG_DIR": str(home / "claude-config"),
        "CCDASH_CLAUDE_SETTINGS": str(home / "claude-settings.json"),
    })
    for k in ("CLAUDECODE", "CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_MESSAGING_SOCKET", "NO_COLOR"):
        os.environ.pop(k, None)
    origin, root = base / "origin.git", base / "my-app"
    git("init", "-q", "--bare", "-b", "main", str(origin), cwd=base)
    git("clone", "-q", str(origin), str(root), cwd=base)
    files = {
        "README.md": "# my-app\n", ".gitignore": ".env\n",
        "src/ui/Toolbar.tsx": "export const Toolbar = () => null;\n",
        "src/reports/export.ts": "export {}\n", "src/auth/session.ts": "export const session = 1;\n",
        "src/api/orders.ts": "export function orders() {\n  return [];\n}\n",
        "src/settings.test.ts": "test('x', () => {})\n", "src/ui/theme.ts": "export const theme = {}\n",
    }
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, newline="\n")
    (root / ".ccdash.toml").write_text('shared = ["src/api/**", "package.json"]\n')
    git("add", "-A", cwd=root)
    git("commit", "-q", "-m", "init", cwd=root)
    git("push", "-q", "-u", "origin", "main", cwd=root)
    (home / "claude.json").write_text(json.dumps(
        {"projects": {str(root).replace("\\", "/"): {"hasTrustDialogAccepted": True}}}))
    return root


SCENARIO = {
    "csv-export": [
        {"send": {"to": "dark-mode", "message": "Files: src/reports/** src/ui/Toolbar.tsx\nI'm adding ExportButton to src/ui/Toolbar.tsx"}},
        {"edit": "src/reports/export.ts", "text": "export const toCsv = () => '';\n"}, {"commit": "add writer"},
        {"edit": "src/ui/Toolbar.tsx", "text": "// export button\n"},
        {"edit": "src/reports/export.ts", "text": "// streaming\n"}, {"sleep": 600}],
    "login-redirect": [
        {"edit": "src/auth/session.ts", "text": "export const redirect = 1;\n"}, {"commit": "fix redirect"},
        {"permission": "Bash", "path": "x"}],
    "dark-mode": [
        {"send": {"to": "csv-export", "message": "Ack. I only touch theme.ts and Toolbar styles."}},
        {"edit": "src/ui/theme.ts", "text": "export const dark = true;\n"}, {"commit": "dark theme"},
        {"edit": "src/ui/Toolbar.tsx", "text": "// dark styles\n"}, {"sleep": 600}],
    "api-pagination": [
        {"edit": "src/api/orders.ts", "text": "export const page = 1;\n"}, {"commit": "paginate /orders"},
        {"send": {"to": "all", "message": "Landed: /orders now returns {items, next}. Rebase if you call it."}},
        {"say": "DONE: /orders is paginated and tested."}],
    "flaky-test": [],
}
TASKS = [("csv-export", "Add CSV export to reports"), ("login-redirect", "Fix login redirect bug"),
         ("dark-mode", "Dark mode toggle"), ("api-pagination", "Paginate /orders endpoint"),
         ("flaky-test", "Fix flaky settings test")]


async def shoot(out: Path, size: tuple[int, int], view: str | None) -> None:
    from ccdash.tui.app import CCDashApp
    from ccdash.workspace import Workspace

    with tempfile.TemporaryDirectory(prefix="ccdash-demo-", ignore_cleanup_errors=True) as tmp:
        root = demo_repo(Path(tmp))
        scen = Path(tmp) / "home" / "scenario.json"
        scen.write_text(json.dumps(SCENARIO))
        os.environ["CCDASH_FAKE_SCENARIO"] = str(scen)
        os.environ["CCDASH_REDUCED_MOTION"] = "1"
        os.environ.setdefault("CCDASH_THEME", "dark-rgb")
        ws = Workspace.open(root)
        for name, task in TASKS:
            ws.new_agent(task, name=name)
        # main moves on, so login-redirect conflicts with it
        (root / "src" / "auth" / "session.ts").write_text("export const session = 2;\n", newline="\n")
        git("commit", "-qam", "session v2", cwd=root)
        time.sleep(4)
        ws.stop("flaky-test")
        try:
            app = CCDashApp(Workspace.open(root))
            async with app.run_test(size=size) as pilot:
                await app.refresh_now()
                await pilot.pause(0.5)
                await app.refresh_now()
                if view:
                    await pilot.press({"comms": "c", "network": "g", "filemap": "f"}[view])
                    await pilot.pause(0.3)
                app.select("csv-export")
                await pilot.pause(0.3)
                out.parent.mkdir(parents=True, exist_ok=True)
                app.save_screenshot(filename=out.name, path=str(out.parent))
                print(f"saved {out}")
        finally:
            for v in ws.views():
                if v.alive:
                    ws.runner.kill(v.record)
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and any(v.alive for v in ws.views(git=False)):
                time.sleep(0.2)
            time.sleep(1)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("out", type=Path)
    p.add_argument("--size", default="120x35")
    p.add_argument("--view")
    a = p.parse_args()
    w, h = (int(x) for x in a.size.split("x"))
    asyncio.run(shoot(a.out, (w, h), a.view))


if __name__ == "__main__":
    main()
