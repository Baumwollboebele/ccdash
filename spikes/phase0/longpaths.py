"""Phase 0 spike: does a worktree under .claude/worktrees/ hit MAX_PATH (260)?

    python longpaths.py <base_dir>

Builds <base>/lp/repo with one file whose path is ~150 chars inside the repo, then
tries `git worktree add` with core.longpaths unset and set, and Python file I/O.
"""

import os
import subprocess
import sys


def run(*args, cwd):
    p = subprocess.run(args, cwd=cwd, capture_output=True, text=True)
    return p.returncode, (p.stdout + p.stderr).strip()


def main():
    base = os.path.abspath(sys.argv[1])
    repo = os.path.join(base, "lp", "repo")
    if os.path.exists(repo):
        sys.exit(f"{repo} exists")
    os.makedirs(repo)
    rel = os.path.join(*(["deeply-nested-directory-level"] * 4), "a-component-with-a-long-file-name-for-testing.txt")
    print("repo path length       :", len(repo))
    print("in-repo file path len  :", len(rel))
    name = "add-csv-export-to-the-reports"  # 29 chars, near the 30-char slug cap
    wt_file = os.path.join(repo, ".claude", "worktrees", name, rel)
    print("file path in worktree  :", len(wt_file), "(limit 260)")

    # Create the file in the main checkout through git's own long-path support.
    run("git", "init", "-q", "-b", "main", cwd=repo)
    run("git", "config", "user.email", "p0@example.invalid", cwd=repo)
    run("git", "config", "user.name", "p0", cwd=repo)
    run("git", "config", "core.longpaths", "true", cwd=repo)
    os.makedirs(os.path.dirname(os.path.join(repo, rel)))
    with open(os.path.join(repo, rel), "w") as f:
        f.write("x\n")
    run("git", "add", "-A", cwd=repo)
    print("commit                 :", run("git", "commit", "-q", "-m", "deep", cwd=repo)[0])

    run("git", "config", "--unset", "core.longpaths", cwd=repo)
    rc, out = run("git", "worktree", "add", "-q", "-b", f"cc/{name}", os.path.join(".claude", "worktrees", name), "main", cwd=repo)
    print("worktree add (no longpaths): rc", rc, "|", out.splitlines()[-1] if out else "")
    print("  file present          :", os.path.exists(wt_file))
    print("  branch left behind    :", bool(run("git", "branch", "--list", f"cc/{name}", cwd=repo)[1]))
    # Roll back: a failed `worktree add -b` still creates the branch (and maybe a partial dir).
    run("git", "worktree", "remove", "--force", os.path.join(".claude", "worktrees", name), cwd=repo)
    run("git", "worktree", "prune", cwd=repo)
    run("git", "branch", "-D", f"cc/{name}", cwd=repo)

    run("git", "config", "core.longpaths", "true", cwd=repo)
    rc, out = run("git", "worktree", "add", "-q", "-b", f"cc/{name}", os.path.join(".claude", "worktrees", name), "main", cwd=repo)
    print("worktree add (longpaths=true): rc", rc, "|", out.splitlines()[-1] if out else "")
    print("  file present          :", os.path.exists(wt_file))
    rc, out = run("git", "status", "--porcelain=v2", cwd=os.path.join(repo, ".claude", "worktrees", name))
    print("  git status in worktree: rc", rc, repr(out[:80]))
    try:
        with open(wt_file, "a") as f:
            f.write("y\n")
        print("  python append         : ok")
    except OSError as e:
        print("  python append         : FAILED", e)


if __name__ == "__main__":
    main()
