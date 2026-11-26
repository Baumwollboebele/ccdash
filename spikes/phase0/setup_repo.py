"""Phase 0 spike: build a throwaway repo with project/local hooks, ccdash-style
worktrees and a --settings hooks file.

    python setup_repo.py <base_dir> <hook_python>

Creates <base>/repo (main checkout), worktrees .claude/worktrees/p0-{alpha,beta,gamma}
on branches cc/p0-*, and <base>/run/hooks.json.  Refuses to touch an existing <base>/repo.
"""

import json
import os
import subprocess
import sys

EVENTS = [
    "SessionStart", "SessionEnd", "UserPromptSubmit", "PreToolUse", "PostToolUse",
    "PostToolUseFailure", "Notification", "PermissionRequest", "PermissionDenied",
    "Stop", "StopFailure", "SubagentStart", "SubagentStop", "CwdChanged",
    "WorktreeCreate", "WorktreeRemove", "PreCompact", "TeammateIdle",
]


def git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def exec_hook(py, hooklog, label, **extra):
    return {"type": "command", "command": py, "args": [hooklog, label], **extra}


def main():
    base, py = os.path.abspath(sys.argv[1]), sys.argv[2]
    hooklog = os.path.join(os.path.dirname(os.path.abspath(__file__)), "hooklog.py")
    repo, run = os.path.join(base, "repo"), os.path.join(base, "run")
    if os.path.exists(repo):
        sys.exit(f"{repo} exists; pick a fresh base dir")
    os.makedirs(os.path.join(repo, ".claude"))
    os.makedirs(os.path.join(repo, "src"))
    os.makedirs(run, exist_ok=True)

    files = {
        "README.md": "# p0 spike repo\n",
        "src/app.py": "def greet(name):\n    return f'hello {name}'\n",
        ".gitignore": ".claude/settings.local.json\n.env\n",
        ".worktreeinclude": ".env\n",
        ".claude/settings.json": json.dumps(
            {"hooks": {"SessionStart": [{"hooks": [exec_hook(py, hooklog, "SessionStart-project")]}]}},
            indent=2,
        ),
    }
    for rel, text in files.items():
        with open(os.path.join(repo, rel), "w", encoding="utf-8") as f:
            f.write(text)
    with open(os.path.join(repo, ".claude", "settings.local.json"), "w", encoding="utf-8") as f:
        json.dump({"hooks": {"SessionStart": [{"hooks": [exec_hook(py, hooklog, "SessionStart-local")]}]}}, f, indent=2)
    with open(os.path.join(repo, ".env"), "w", encoding="utf-8") as f:
        f.write("SECRET=1\n")

    git("init", "-q", "-b", "main", cwd=repo)
    git("config", "user.email", "p0@example.invalid", cwd=repo)
    git("config", "user.name", "p0", cwd=repo)
    git("add", "-A", cwd=repo)
    git("commit", "-q", "-m", "init", cwd=repo)
    for name in ("p0-alpha", "p0-beta", "p0-gamma"):
        git("worktree", "add", "-q", "-b", f"cc/{name}", os.path.join(".claude", "worktrees", name), "main", cwd=repo)

    fwd = lambda p: p.replace("\\", "/")  # noqa: E731
    hooks = {ev: [{"hooks": [exec_hook(py, hooklog, ev)]}] for ev in EVENTS}
    # Shell form (no args): tells us which shell Claude Code uses for hook commands.
    hooks["SessionStart"][0]["hooks"].append(
        {"type": "command", "command": f'"{fwd(py)}" "{fwd(hooklog)}" SessionStart-shellform'}
    )
    # Async variant: does async work, and in what order do lines land?
    hooks["PostToolUse"][0]["hooks"].append(exec_hook(py, hooklog, "PostToolUse-async", **{"async": True}))
    with open(os.path.join(run, "hooks.json"), "w", encoding="utf-8") as f:
        json.dump({"hooks": hooks}, f, indent=2)
    print(json.dumps({"repo": repo, "run": run, "hooks": os.path.join(run, "hooks.json")}))


if __name__ == "__main__":
    main()
