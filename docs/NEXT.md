# ccdash: where we stopped and what is left

Last updated Nov 27, 2026. All six phases of `docs/PLAN.md` are built and committed. What is left is checking a few things against the real Claude Code and on a real terminal, which no automated test here can do. Read this together with `docs/PLAN.md` (the spec, updated as phases landed) and `docs/findings.md` (what was verified against the real Claude Code, what the docs say, and what a second machine taught us).

## Paste this to continue

```text
Continue with ccdash. Read docs/PLAN.md, docs/findings.md and docs/NEXT.md fully first.
Work through "What is left" in docs/NEXT.md in order: I run the manual checks and tell you
what I saw; you fix what they turn up (tests first, full suite, commit).
Decide details yourself; when in doubt, choose what keeps the CLI fastest.
Never run destructive git commands outside the test fixtures.
Installing software is blocked for you: tell me the exact command and I run it with "!".
If a git remote exists, push after each commit.
```

## State

| Phase | Commit | Status |
| --- | --- | --- |
| 0: verify assumptions | `106b2c0` | Done. Results in `docs/findings.md`; PLAN updated |
| 1: runner, worktrees, CLI, agents table | `31e5c28` | Done |
| 2: hooks, live status, messages, talk/broadcast | `e98643a` | Done, plus a real-Claude check (`scripts/real_claude_check.py`, result in findings) |
| 3: look and feel | `3338e55` | Done. Screenshots: `docs/screenshots/phase3*.svg` |
| 4: conflicts and merge | `97d710e` | Done. Screenshots: `docs/screenshots/phase4*.svg` |
| 5: extras | `86a75d2` | Done, all off by default. Tested against fakes only (see below) |

Pushed to the private repo https://github.com/Baumwollboebele/ccdash, branch `main`. Push after each commit with `git push`.

## What is left

### 1. Manual checks on Windows (owed since Phases 1 to 4)

- **Terminal survival:** start `cc` from PowerShell in Windows Terminal and in the VS Code terminal, start 3 agents, close the window, reopen `cc`; all 3 must still run.
- **Attach:** `a` or Enter on a running agent, type into it, Ctrl+] detaches; the terminal must come back clean.
- **Diff:** `d` on an agent with commits; the pager must take the terminal and `q` must bring the dashboard back.
- **Real-Claude check:** `.venv\Scripts\python.exe scripts\real_claude_check.py <a repo you trusted in Claude Code>` (uses Haiku, a few cents of quota).
- **Pull requests:** `gh` is not installed on the second machine, so "open PR" and the `PR #n` state ran only against the fake `gh`. Try `m` then `p` once where `gh auth status` works.

### 2. Phase 5 against the real Claude Code

Each extra works against a fake that follows the documentation (`docs/findings.md`, "Phase 5"). None has run against a real session yet. Try them in this order, each in a throwaway repo:

1. **Guard, block** (`guard = "block"` in `.ccdash.toml`): start two agents, let the first announce `Files: src/ui/**`, ask the second to edit a file under `src/ui/`. Expected: the edit is denied and the second agent messages the first.
2. **Guard, warn** (`guard = "warn"`): same setup. Expected: the edit goes through, your usual permission prompt still appears, and the agent mentions the note about the owner. This is the one assumption the docs do not state outright: a PreToolUse output with `additionalContext` and no `permissionDecision`. If Claude ignores it, `guard.py` is the only file to change.
3. **`runner = "bg"`:** start one agent. Check first that it works in ccdash's worktree and does not move into a second one of its own (the open risk in findings). Then check status, `p` (peek), `s`, `r` and `a`. Talk and broadcast are expected to fail with "cannot type into".
4. **`open_in = "wt"`** in Windows Terminal: `a` must open a new tab running the agent.
5. **`repos = [...]`** in `~/.config/ccdash/config.toml`: `R` must switch, and the header must show `1/2`.

### 3. Ideas, not planned

- A `cc merge <name>` command for the local merge (today it is only in the dashboard).
- Guard: a way for the owner to hand a file over (today the first agent to edit a file holds it until it is merged or deleted).
- Faster `cc "task"` where starting a process is slow (see findings): fewer git calls before the host starts.

## What Phases 4 and 5 added

- **`conflicts.py`: `ConflictTracker`**, one per `Dashboard` (`dash.conflicts`). The TUI sets `.active = True`; only an active tracker sends notices or asks gh, so `cc ls` never types into agents.
  - Rows: merge (red), overlap (yellow), shared (yellow, blue once announced or notified), stale (grey). The header's conflict count is the red rows.
  - `update(dashboard, git=False)` never calls git, so an overlap shows as fast as the hook event arrives.
  - State lives in `<state>/ownership.json`: the user's claims, what was already notified, and each agent's claims and touched files (new agents' prompts and guard mode read these).
- **`gitstate.MergeTreeCache`** has a fallback for git older than 2.38 (see findings).
- **`merge.py`**: `menu`, `ask_rebase`, `merge_local`, `open_pr`, `diff_command`. **`github.py`**: `gh` behind `CCDASH_GH` (tests use `tests/fake_gh.py`).
- **Keys**: `m` merge menu, `d` diff in a pager, `/` filter, `f` file map, `o` claim paths for the selected agent, `R` next repo.
- **`guard.py`**: the PreToolUse decision. The mode is published to `<state>/guard.json` by every `Workspace`, so hosts and hooks need no config.
- **`bgrunner.py`**: `claude --bg` sessions behind the `Runner` interface. Its hooks carry the agent's identity as arguments (`hooks_settings.render_command(ident=...)`, read by `hook.main`).
- **`terminals.py`**: the Windows Terminal new-tab command for `open_in`.
- **`tui/app.py`**: `open_workspaces()` and the repo switcher (`self.spaces`, one `Dashboard` per repo).

## Decisions already made (don't revisit)

- **Stack:** Python + Textual stays. Go was the faster option, but installing a toolchain is blocked here.
- **Hooks:**
  - On Windows, the agent's host serves HTTP hooks on 127.0.0.1, so no process starts per event.
  - SessionStart, and everything under tmux or `claude --bg`, are exec-form command hooks on the base interpreter with `-I -S`.
  - `hook.py` is stdlib-only, and so is `guard.py`, which it loads only when guard is on.
- **Dropped portalocker** (165 ms import). Locks use `msvcrt` / `fcntl`.
- **Peek** uses `pyte` in the host.
- **Session ids:** ccdash passes `--session-id`. Restart uses `--resume <id>` when the transcript exists. The bg runner cannot pass an id and reads it back.
- **Agent environment:** `permission_mode` is unset by default, so agents use the user's own default mode. The agent env drops the parent Claude session's variables, and `NO_COLOR` too when `CLAUDECODE` is set.
- **Look:**
  - Terminal-native ANSI themes plus `-rgb` variants: screenshots use `CCDASH_THEME=dark-rgb`.
  - Accent `#D97757`.
  - DataTable's own cursor is hidden; selection is `▶` plus coral.
  - Graph views paint on their real size and again on resize (`network.Surface`); labels are placed by `network.place_labels`.
- **Conflicts:**
  - The header counts predicted merge conflicts only; overlaps and shared changes are warnings.
  - "Touched" means edited: it comes from PostToolUse, so a denied edit touches nothing. A stopped agent's old hook events no longer count; what git still shows does.
  - A local merge is `--no-ff --no-commit`, tests, commit; "clean main checkout" means no uncommitted changes to tracked files.
  - Notices (shared change, what landed) go out once, from an open dashboard only.
- **Guard:** `warn` returns only `additionalContext`, never `allow`, because `allow` skips the user's permission prompt.
- **Several repos:** a switcher (`R`), not a repo column. The header names another repo only when an agent there needs input.
- **`open_in = "vscode"`** attaches in place: VS Code has no command line for a terminal tab, and `code --new-window` would open an empty window.
- **Messages that cannot be delivered** raise "cannot type into ..." instead of being logged as sent.

## Pitfalls seen in this environment

- **Running tests:** use PowerShell, `.venv\Scripts\python.exe -m pytest -q`. It takes about 11 min on the second machine (2.5 min on the first, with fewer tests); the tmux test is skipped on Windows, and two merge-prediction tests skip on git older than 2.38. A watchdog ends a hung test after `CCDASH_TEST_TIMEOUT` seconds (default 120).
- **No `.venv` yet?** `uv sync` creates it from `uv.lock`.
- **Never edit `src/ccdash` or `tests/fake_claude*.py` while the suite runs:** agent hosts are separate processes that import the package when they start, so a half-edited module breaks unrelated tests.
- **Timing tests on a slow machine:** `test_cc_task_ls_stop` (5 s from task to running agent) can fail on the first run after a change and pass on the next. The hook timing test scales its budget with the machine.
- **Claude Code environment:** its tool shells export `NO_COLOR=1` and `CLAUDECODE=1`. The test sandbox removes them; scripts that render must remove them too.
- **Tests never call the real `gh` or `claude`:** the sandbox points `CCDASH_GH` and `CCDASH_CLAUDE` at fakes (`fake_gh.py`, `fake_claude.py`, `fake_claude_bg.py`).
- **Viewing screenshots:** render the SVG to PNG with Edge, then Read the PNG:
  ```
  & "${env:ProgramFiles(x86)}\Microsoft\Edge\Application\msedge.exe" --headless=new --disable-gpu --hide-scrollbars --window-size=1560,1000 --screenshot=<out.png> file:///<svg path>
  ```
- **Git Bash** rewrites arguments that start with `/` (MSYS path conversion). Drive spikes from PowerShell.
- **Commit messages** with some words (an exit slash command, `rm|`) trip the shell safety check. Write the message to a file in the scratchpad and use `git commit -F <file>`.
- **Hanging Textual tests:**
  - Never wait for all workers: the event watcher thread never finishes. Use `settle()` in `tests/test_tui.py`, which also tolerates an exclusive worker that was replaced.
  - Textual's `App` already has an `animator` attribute, so ccdash's ticker is called `motion`.
- **Orphaned test agents:** after killing a test run, fake agents and hosts may stay alive. Stop python processes whose command line contains `fake_claude` under `pytest-of-<user>`, and their `from ccdash.host import` hosts.
- **Mockups:** `docs/mockup-main.png` and `docs/mockup-comms.png` were never added. The ASCII layout in PLAN.md is the target.
