# ccdash: where we stopped and what is left

Last updated Nov 26, 2026. Read this together with `docs/PLAN.md` (the spec, updated with Phase 0 results) and `docs/findings.md` (what was verified against the real Claude Code 2.1.317 on this machine).

## Paste this to continue

```text
Continue building ccdash. Read docs/PLAN.md, docs/findings.md and docs/NEXT.md fully first.
Work through "What is left" in docs/NEXT.md in order: finish Phase 3, then Phase 4, then Phase 5.
For each phase: tests first for its acceptance checks, implement, run the full suite, commit.
Don't stop between phases; stop only when Phase 5 is done or you are blocked on something only I can do.
Decide details yourself; when in doubt, choose what keeps the CLI fastest.
Never run destructive git commands outside the test fixtures.
Installing software is blocked for you: tell me the exact command and I run it with "!".
Show me a screenshot at the end of Phases 3 and 4 (scripts/screenshot.py).
If a git remote exists, push after each commit.
```

## State

| Phase | Commit | Status |
| --- | --- | --- |
| 0: verify assumptions | `106b2c0` | Done. Results in `docs/findings.md`; PLAN updated |
| 1: runner, worktrees, CLI, agents table | `31e5c28` | Done |
| 2: hooks, live status, messages, talk/broadcast | `e98643a` | Done, plus a real-Claude check (`scripts/real_claude_check.py`, result in findings) |
| 3: look and feel | `8e9070d` (WIP) | Implemented and its tests pass; two visual bugs open, see below |
| 4: conflicts and merge | not started | |
| 5: extras | not started | |

Pushed to the private repo https://github.com/Baumwollboebele/ccdash, branch `main` (renamed from `master`). `gh` is installed at `C:\Program Files\GitHub CLI\gh.exe` and logged in; this shell may need the full path.

## What is left

### 1. Push to GitHub

Done. Push after each commit with `git push`.

### 2. Finish Phase 3 (look and feel)

1. **Network view (`g`) puts agents off the visible canvas.** In `tests/__snapshots__/test_snapshots/test_network_view.raw` (120×35), `dark-mode` and `api-pagination` are missing and the drawing sits in the lower half. Check `NetworkView.show` in `src/ccdash/tui/network.py`:
   - the canvas size comes from `self.size`, which may still be the default before layout; re-render on `on_resize`
   - `ellipse_layout` radii
   - `_label` clipping at the edges
2. **Comms view (`c`) graph labels overlap** (for example `flaky-test` and `api-pagination` render as `flaky-test-pagination`), and the graph uses only the top of its panel. Same causes. Lay out on the real panel size, and keep labels apart: shift a label sideways when it collides, or put names next to nodes rather than centered on them.
3. Re-run the full suite: `.venv\Scripts\python.exe -m pytest -q`.
4. Regenerate snapshots with `--snapshot-update` on `tests/test_snapshots.py`, then look at the SVGs (see "Viewing screenshots" below).
5. Make the screenshot with `.venv\Scripts\python.exe scripts\screenshot.py docs\screenshots\phase3.svg` (also try `--view network` and `--view comms`).
6. Commit "Phase 3: look and feel".

Phase 3 acceptance already covered by `tests/test_lookfeel.py`:
- every animation in the PLAN table: spinner, shimmer, pulse, flight, conflict flash/slide, progress bar, `+N` tick, merged fade, toasts
- `--no-anim` is static
- idle CPU < 3%

### 3. Phase 4: conflicts and merge (PLAN sections "Coordination and conflict detection", "Commit and merge state")

Tests first for the "Conflict scenarios that must pass" table in PLAN.md (fake claude scenarios plus real git in the temp repo fixture).

**`src/ccdash/conflicts.py`: a `ConflictTracker`.** Set `Dashboard.conflicts = ConflictTracker(...)` in `dashboard.py`; the hooks are already wired.
- The interface the dashboard and app already call:
  - `rows` (each row has `.key` and `.render(flash=False, slide=0) -> rich Text`)
  - `on_event(line)`
  - `update(dashboard, git: bool)`
  - `owned(name) -> list[str]`
  - `red_pairs() -> set[frozenset]`
- **overlap:** the same path is touched (`ActivityTracker.touched`, plus each worktree's `dirty_paths`) or claimed (`AgentActivity.claimed` from `Files:` lines, plus user claims) by 2+ running agents. Yellow. Must appear within 2 s.
- **merge:** `gitstate.MergeTreeCache.predict(default_sha, branch_sha)` for each agent branch, and pairwise for branches that share touched files. Red. It also sets the STATE column to `conflict` (`⚠ conf`).
- **shared:** the branch diff matches `cfg.shared` globs. Yellow, blue once dependents were notified. If the agent sent no message mentioning it within `cfg.shared_notice_after` (120 s), ccdash broadcasts a notice with `messaging.notice(ws, "all", ...)`.
- **stale base:** behind > `cfg.stale_base_commits` (20). Grey.
- Persist claims in `ownership.json`. Let the user claim paths for an agent from the details panel.

**Merge state column.** Add `conflict` and `PR #n` (from `gh pr view cc/<name> --json state,number,statusCheckRollup`, cached; hidden when gh is missing) on top of `workspace.merge_state`.

**Merge menu (`m`, use `modals.ChoiceModal`).**
- Ask the agent to rebase: `messaging.notice` with the text from PLAN.
- Merge locally: only when the state is ready, the main checkout is clean and on the default branch. Run `git merge --no-ff --no-commit cc/<name>`, then `cfg.test`. Commit on success, `git merge --abort` on failure, so the default branch is never rewritten.
- Open PR: `git push -u origin cc/<name>` and `gh pr create --fill --head cc/<name>`.

**After a successful merge.**
- Refresh predictions.
- Send a "what landed" notice to every other running agent: files changed, and whether their branch now conflicts.
- Toast, and offer cleanup (delete worktree and branch).
- Never force-push, never rewrite the default branch, never merge without a keypress.

**Other keys and views.**
- `d`: `git diff <default>...cc/<name>` in a pager. Suspend the TUI like attach does. On Windows use `git -c core.pager=less` if less exists, else `more`.
- `/` filter: `Dashboard.filter` already filters; add a `TextInputModal`.
- File map view (`f`): the repo tree, each touched file colored by the agent that touched it; files touched by 2+ agents are red and striped.

Then the screenshot (`docs/screenshots/phase4.svg`) and a commit.

### 4. Phase 5: extras (each behind a config flag, off by default)

- **Guard mode** (`guard = "warn" | "block"`):
  - `hookserver.start()` already imports `ccdash.guard.decider_for(state_dir, name)` if it exists. Create `src/ccdash/guard.py`: on PreToolUse for Edit/Write/MultiEdit/NotebookEdit, check the path against other agents' claims and touched files.
  - Return `hookSpecificOutput` with `permissionDecision` `allow` plus `additionalContext` (warn), or `deny` plus `permissionDecisionReason` (block).
  - The command transport already makes PreToolUse synchronous when guard is on (`hooks_settings.render_command(guard=True)`); `hook.main` must print the decision only in that case.
- **`claude --bg` runner** (`runner = "bg"`): `src/ccdash/bgrunner.py` (`get_runner` already imports it), using `claude --bg`, `claude agents --json --all`, `claude attach`, `claude logs`, `claude stop`. Research preview: keep everything Claude-specific in this file.
- **Several repos in one screen** (`repos = [...]` in user config): a repo column or switcher.
- **Open an agent in a new terminal tab** (`open_in = "wt"`): `wt.exe -w 0 new-tab --title <name> cc attach <name>`. VS Code has no command line for opening a terminal tab; document that and offer `code --new-window`? Decide.

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

## Pitfalls seen in this environment

- **Running tests:** use PowerShell, `.venv\Scripts\python.exe -m pytest -q`. It takes about 2.5 min; the tmux test is skipped on Windows. A watchdog ends a hung test after `CCDASH_TEST_TIMEOUT` seconds (default 120).
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
- **Real-Claude check:** `.venv\Scripts\python.exe scripts\real_claude_check.py <a repo you trusted in Claude Code>` (uses Haiku, a few cents of quota).
