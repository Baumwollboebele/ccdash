"""Stand-in for the `gh` CLI (never the real GitHub).

    gh auth status                              -> exit 0 (exit 1 if CCDASH_FAKE_GH_LOGGED_OUT is set)
    gh pr view <branch> --json ...              -> the stored PR as JSON, or exit 1
    gh pr create --fill --head <branch>         -> stores an OPEN PR and prints its URL

Pull requests live in the JSON file named by CCDASH_FAKE_GH: {"<branch>": {"number": 1, "state": "OPEN", ...}}.
"""

import json
import os
import sys

STORE = os.environ.get("CCDASH_FAKE_GH", "")


def load():
    try:
        with open(STORE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def main(argv):
    if argv[:2] == ["auth", "status"]:
        return 1 if os.environ.get("CCDASH_FAKE_GH_LOGGED_OUT") else 0
    prs = load()
    if argv[:2] == ["pr", "view"]:
        pr = prs.get(argv[2])
        if pr is None:
            sys.stderr.write(f'no pull requests found for branch "{argv[2]}"\n')
            return 1
        sys.stdout.write(json.dumps(pr))
        return 0
    if argv[:2] == ["pr", "create"]:
        branch = argv[argv.index("--head") + 1]
        number = len(prs) + 1
        prs[branch] = {"number": number, "state": "OPEN", "url": f"https://example.invalid/pull/{number}",
                       "statusCheckRollup": [{"status": "IN_PROGRESS", "conclusion": ""}]}
        with open(STORE, "w", encoding="utf-8") as f:
            json.dump(prs, f)
        sys.stdout.write(f"Creating pull request for {branch}\n\nhttps://example.invalid/pull/{number}\n")
        return 0
    sys.stderr.write(f"fake gh: unknown command {argv}\n")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
