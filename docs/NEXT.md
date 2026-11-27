# ccdash: where we stopped and what is left

Last updated Nov 27, 2026. Read this together with `docs/PLAN.md` (the spec, updated as phases landed) and `docs/findings.md` (what was verified against the real Claude Code, and what a second machine taught us).

## Paste this to continue

```text
Continue building ccdash. Read docs/PLAN.md, docs/findings.md and docs/NEXT.md fully first.
Work through "What is left" in docs/NEXT.md in order.
For each item: tests first, implement, run the full suite, commit.
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
| 4: conflicts and merge | this commit | Done. Screenshots: `docs/screenshots/phase4*.svg` |
| 5: extras | not started | |

Pushed to the private repo https://github.com/Baumwollboebele/ccdash, branch `main`. Push after each commit with `git push`.

## What is left

### Phase 5: extras (each behind a config flag, off by default)

- **Guard mode** (`guard = "warn" | "block"`):
  - `hookserver.start()` already imports `ccdash.guard.decider_for(state_dir, name)` if it exists. Create `src/ccdash/guard.py`: on PreToolUse for Edit/Write/MultiEdit/NotebookEdit, check the path against other agents' claims and touched files (`ownership.json`, written by the ConflictTracker).
  - Return `hookSpecificOutput` with `permissionDecision` `deny` plus `permissionDecisionReason` (block). For warn, return only `additionalContext`: `allow` would skip the user's own permission prompt.
  - The command transport already makes PreToolUse synchronous when guard is on (`hooks_settings.render_command(guard=True)`); `hook.main` must print the decision only in that case.
- **`claude --bg` runner** (`runner = "bg"`): `src/ccdash/bgrunner.py` (`get_runner` already imports it), using `claude --bg`, `claude agents --json --all`, `claude attach`, `claude logs`, `claude stop`. Research preview: keep everything Claude-specific in this file.
- **Several repos in one screen** (`repos = [...]` in user config): a repo column or switcher.
- **Open an agent in a new terminal tab** (`open_in = "wt"`): `wt.exe -w 0 new-tab --title <name> cc attach <name>`. VS Code has no command line for opening a terminal tab.

## What Phase 4 added

- **`conflicts.py`: `ConflictTracker`**, one per `Dashboard` (`dash.conflicts`). The TUI sets `.active = True`; only an active tracker sends notices or asks gh, so `cc ls` never types into agents.
  - Rows: merge (red), overlap (yellow), shared (yellow, blue once announced or notified), stale (grey). The header's conflict count is the red rows.
  - `update(dashboard, git=False)` never calls git, so an overlap shows as fast as the hook event arrives.
  - State lives in `<state>/ownership.json`: the user's claims, what was already notified, and each agent's claims and touched files (new agents' prompts read these).
- **`gitstate.MergeTreeCache`** has a fallback for git older than 2.38 (see findings).
- **`merge.py`**: `menu`, `ask_rebase`, `merge_local`, `open_pr`, `diff_command`. **`github.py`**: `gh` behind `CCDASH_GH` (tests use `tests/fake_gh.py`).
- **Keys**: `m` merge menu, `d` diff in a pager, `/` filter, `f` file map, `o` claim paths for the selected agent.

## Decisions already made (don't revisit)

- **Stack:** Python + Textual stays. Go was the faster option, but installing a toolchain is blocked here.
- **Hooks:**
  - On Windows, the agent's host serves HTTP hooks on 127.0.0.1, so no process starts per event.
  - SessionStart, and everything under tmux, are exec-form command hooks on the base interpreter with `-I -S`.
  - `hook.py` is stdlib-only.
- **Dropped portalocker** (165 ms import). Locks use `msvcrt` / `fcntl`.
- **Peek** uses `pyte` in the host.
- **Session ids:** ccdash passes `--session-id`. Restart uses `--resume <id>` when the transcript exists.
- **Agent environment:** `permission_mode` is unset by default, so agents use the user's own default mode. The agent env drops the parent Claude session's variables, and `NO_COLOR` too when `CLAUDECODE` is set.
- **Look:**
  - Terminal-native ANSI themes plus `-rgb` variants: screenshots use `CCDASH_THEME=dark-rgb`.
  - Accent `#D97757`.
  - DataTable's own cursor is hidden; selection is `▶` plus coral.
  - Graph views paint on their real size and again on resize (`network.Surface`); labels are placed by `network.place_labels`.
- **Conflicts:**
  - The header counts predicted merge conflicts only; overlaps and shared changes are warnings.
  - A stopped agent's old hook events no longer count as touched; what git still shows does.
  - A local merge is `--no-ff --no-commit`, tests, commit; "clean main checkout" means no uncommitted changes to tracked files.
  - Notices (shared change, what landed) go out once, from an open dashboard only.

## Pitfalls seen in this environment

- **Running tests:** use PowerShell, `.venv\Scripts\python.exe -m pytest -q`. It takes about 9 min on the second machine (2.5 min on the first); the tmux test is skipped on Windows, and two merge-prediction tests skip on git older than 2.38. A watchdog ends a hung test after `CCDASH_TEST_TIMEOUT` seconds (default 120).
- **No `.venv` yet?** `uv sync` creates it from `uv.lock`.
- **Never edit `src/ccdash` while the suite runs:** agent hosts are separate processes that import the package when they start, so a half-edited module breaks unrelated tests.
- **Timing tests on a slow machine:** `test_cc_task_ls_stop` (5 s from task to running agent) can fail on the first run after a change and pass on the next. The hook timing test scales its budget with the machine.
- **Claude Code environment:** its tool shells export `NO_COLOR=1` and `CLAUDECODE=1`. The test sandbox removes them; scripts that render must remove them too.
- **Viewing screenshots:** render the SVG to PNG with Edge, then Read the PNG:
  ```
  & "${env:ProgramFiles(x86)}\Microsoft\Edge\Application\msedge.exe" --headless=new --disable-gpu --hide-scrollbars --window-size=1560,1000 --screenshot=<out.png> file:///<svg path>
  ```
- **Git Bash** rewrites arguments that start with `/` (MSYS path conversion). Drive spikes from PowerShell.
- **Commit messages** with some words (an exit slash command, `rm|`) trip the shell safety check. Write the message to a file in the scratchpad and use `git commit -F <file>`.
- **Hanging Textual tests:**
  - Never wait for all workers: the event watcher thread never finishes. Use `settle()` in `tests/test_tui.py`.
  - Textual's `App` already has an `animator` attribute, so ccdash's ticker is called `motion`.
- **Orphaned test agents:** after killing a test run, fake agents and hosts may stay alive. Stop python processes whose command line contains `fake_claude.py` under `pytest-of-<user>`, and their `from ccdash.host import` hosts.
- **Mockups:** `docs/mockup-main.png` and `docs/mockup-comms.png` were never added. The ASCII layout in PLAN.md is the target.

## Manual checks still owed to the user (Windows)

- **Terminal survival:** start `cc` from PowerShell in Windows Terminal and in the VS Code terminal, start 3 agents, close the window, reopen `cc`; all 3 must still run.
- **Attach:** `a` or Enter on a running agent, type into it, Ctrl+] detaches; the terminal must come back clean.
- **Diff:** `d` on an agent with commits; the pager must take the terminal and `q` must bring the dashboard back.
- **Real-Claude check:** `.venv\Scripts\python.exe scripts\real_claude_check.py <a repo you trusted in Claude Code>` (uses Haiku, a few cents of quota).
- **Pull requests:** `gh` is not installed on the second machine, so "open PR" and the `PR #n` state ran only against the fake `gh`. Try `m` then `p` once where `gh auth status` works.
