# ccdash — Plan for a terminal dashboard to orchestrate parallel Claude Code agents

Nov 26, 2026 · @Felix

## Goal and scope

ccdash is one terminal command, `cc`, that you open in a git repo. It shows every Claude Code agent working on that repo, what each one does, which worktree and branch it uses, how they message each other, where they might collide, and how far each branch is from merged. Starting, stopping and attaching to agents is one key each.

This document is written as a build spec for a Claude Code instance. Section "Handoff prompt" at the end is the message to paste in.

**It wraps your own Claude Code, nothing else.** Every agent is the normal `claude` CLI you already use, started under your login and subscription. Your MCP servers, skills, plugins, `CLAUDE.md`, permissions and settings all load exactly as in a session you start by hand. ccdash adds only one extra settings file with its hooks (passed with `--settings`) and the coordination prompt. Usage counts against your subscription's limits, and several agents in parallel use them up faster.

**Target platform: native Windows, started from PowerShell** (Windows Terminal or the VS Code terminal). macOS, Linux and WSL 2 are supported too, through a second runner backend. Running natively matters: inside WSL, `claude` would be a separate install with its own login, MCP servers and skills.

**In scope**

- A full-screen TUI (terminal UI) per repository, launched with `cc` from any folder inside the repo or one of its worktrees.
- Spawning agents: each in its own git worktree and branch, running interactive `claude` inside a tmux session that ccdash owns.
- Live status per agent from Claude Code hooks: working, needs input, idle, finished, stopped, crashed.
- A message log of agent-to-agent `SendMessage` traffic, and a way for you to message one agent or all.
- Conflict detection: files touched by more than one agent, predicted merge conflicts, and changes to configured shared interfaces.
- Commit and merge state per branch, plus actions to rebase, merge, open a PR and clean up.
- Keeping the `cc "idea"` one-liner from the existing wrapper script: spawn without opening the TUI.

**Out of scope (v1)**

- Replacing Claude Code's own agent view (`claude agents`) or agent teams. ccdash sits next to them.
- A GUI or web UI. Terminal only; it must run in the VS Code integrated terminal.
- Multiple repos in one screen (Phase 5 at the earliest).
- Native Windows on ARM and Windows versions older than Windows 10 1809 (no ConPTY).

**Done means**

1. From an idea to a running agent takes under 5 seconds and one prompt line.
2. Every agent's state on screen is at most 2 seconds stale for hook-driven fields and 30 seconds for git-derived fields.
3. Two agents editing the same file, or branches that would conflict on merge, show a visible warning before either branch is merged.
4. Stopping an agent never deletes its work. Deleting a worktree with unmerged commits needs an explicit confirmation.
5. Closing the TUI never stops agents; reopening it restores the full picture.

## Architecture

ccdash has no server. The TUI starts agents through its runner (ConPTY on Windows, tmux elsewhere), agents report every step through Claude Code hooks into a shared state directory, and ccdash reads that directory plus git to draw the screen. Agents talk to each other directly with `SendMessage`; ccdash only observes and, when needed, adds its own notices.

```text
                    ┌──────────────────────────────┐
                    │        ccdash TUI (cc)       │
                    │ agents · messages · conflicts│
                    └──▲───────────▲───────────▲───┘
     start/stop/attach │           │ events,   │ status,
                       │           │ registry  │ merge-tree
     ┌─────────────────┴┐ ┌────────┴────────┐ ┌┴─────────────────┐
     │ Runner           │ │ State dir       │ │ git              │
     │ ConPTY or tmux   │ │ .git/ccdash/    │ │ worktrees,       │
     │ one per agent    │ │ agents, events  │ │ branches, main   │
     └────────┬─────────┘ └────────▲────────┘ └────────▲─────────┘
     launches │          hook events│        commits in│worktrees
     ┌────────▼────────────────────┴───────────────────┴─────────┐
     │ Agents: your claude CLI, one worktree each                 │
     │  [csv-export] <--SendMessage--> [login-redirect] <--> [dark-mode] │
     └────────────────────────────────────────────────────────────┘
```

Closing the TUI stops nothing: agents, hooks and the state directory keep working, and the next `cc` rebuilds the screen from them.

## Screen layout

One screen, four panels: the agent table on top, details of the selected agent on the left, the message log on the right, conflicts at the bottom. It must fit 120×35 characters and degrade to a stacked layout below 100 columns.

```text
 ccdash · my-app (main @ 3f2a9c1)            5 agents · 2 working · 1 needs input · 1 conflict      19:52
┌ Agents ─────────────────────────────────────────────────────────────────────────────────────────────────┐
│   NAME              STATUS        TASK                          BRANCH            ↑↓ MAIN   FILES  STATE  │
│ ▶ csv-export        ● working     Add CSV export to reports     cc/csv-export     +4 −0     7      dirty  │
│   login-redirect    ◐ needs input Fix login redirect bug        cc/login-redirect +2 −3     3      ⚠ conf │
│   dark-mode         ● working     Dark mode toggle              cc/dark-mode      +1 −0     12     dirty  │
│   api-pagination    ✓ finished    Paginate /orders endpoint     cc/api-pagination +6 −0     5      ready  │
│   flaky-test        ■ stopped     Fix flaky settings test       cc/flaky-test     +0 −0     0      clean  │
└─────────────────────────────────────────────────────────────────────────────────────────────────────────┘
┌ csv-export ─────────────────────────────────────┐┌ Messages ──────────────────────────────────────────────┐
│ Worktree  .claude/worktrees/csv-export          ││ 19:41 csv-export → dark-mode                           │
│ Branch    cc/csv-export  (base main @ 3f2a9c1)  ││   I'm adding ExportButton to src/ui/Toolbar.tsx …      │
│ Last      Edit src/reports/export.ts  12s ago   ││ 19:42 dark-mode → csv-export                           │
│ Commits   4 ahead, 0 behind · last "add writer" ││   Ack. I only touch theme.ts and Toolbar styles.       │
│ Owns      src/reports/**  src/ui/Toolbar.tsx    ││ 19:47 api-pagination → all                             │
│ Tests     not run                               ││   Landed: /orders now returns {items, next}. Rebase …  │
│ ── live output (tmux) ──                        ││ 19:50 you → login-redirect                             │
│ > Writing src/reports/export.ts …               ││   Use the existing AuthGuard, don't add a new one.     │
└─────────────────────────────────────────────────┘└────────────────────────────────────────────────────────┘
┌ Conflicts ──────────────────────────────────────────────────────────────────────────────────────────────┐
│ ⚠ merge   login-redirect × main         src/auth/session.ts (both changed)                               │
│ ⚠ overlap csv-export × dark-mode        src/ui/Toolbar.tsx                                               │
│ ● shared  api-pagination changed        src/api/** (2 agents depend on it, notified 19:47)               │
└─────────────────────────────────────────────────────────────────────────────────────────────────────────┘
 n new  s stop  r restart  a attach  p peek  t talk  b broadcast  m merge  d diff  x delete  ? help  q quit
```

**Keybindings**

| Key | Action | Notes |
| --- | --- | --- |
| `n` | New agent | Modal: task (required), name (prefilled slug), base `main`/`HEAD`, model, permission mode |
| `s` | Stop selected | Graceful, keeps worktree and branch |
| `r` | Restart / resume | Resumes the last conversation in the same worktree |
| `a` / `Enter` | Attach | TUI suspends, the agent takes the terminal; detach with Ctrl+\] on Windows, or in tmux with (`Ctrl-b d`) returns |
| `p` | Peek | Toggles live output in the details panel (last 30 lines of the pane) |
| `t` | Talk to agent | One-line message to the selected agent |
| `b` | Broadcast | One-line message to every running agent |
| `m` | Merge menu | Rebase via agent, merge locally, open PR |
| `d` | Diff | Opens `git diff main...branch` in a pager |
| `x` | Delete | Removes worktree and branch; refuses unmerged work without a second confirmation |
| `/` | Filter | By name, status or file path |
| `q` | Quit | Agents keep running |

Colors: working green, needs input yellow and sorted to the top, conflict red, finished blue, stopped grey. The header counts must match the table.

## Look and feel

ccdash should feel like the Claude Code CLI itself: calm, modern, mostly monochrome with one warm accent, rounded borders, generous spacing. Motion is used only to show what is happening right now; nothing moves without a reason.

**Visual style**

- Rounded box borders, dim borders for inactive panels, the accent color on the focused panel only.
- One warm accent (orange/coral) for focus and your own actions; status colors as in Screen layout; everything else in 2–3 greys. Light and dark terminal themes both supported.
- Unicode glyphs instead of words where obvious: `●` working, `◐` needs input, `✓` finished, `■` stopped, `✕` crashed, `⚠` conflict.
- A one-line footer of keys in dim text, like Claude Code's own prompt hints.

**Animations that carry meaning**

| Animation | What it tells you |
| --- | --- |
| Spinner next to each working agent (braille dots, \~10 fps), with a shimmer over its "Last" action text | This agent is busy and what it is doing |
| Row gently pulses yellow and moves to the top | This agent is waiting for you |
| Message flies along an arrow from sender to receiver in the Network view, then fades into the Messages panel | Who is talking to whom, as it happens |
| Conflict row slides in with a short red flash; the two involved agents get a red link in the Network view | A new collision, and between whom |
| Progress bar in the row during worktree setup (`git worktree add`, include-copy, `setup` command) | A new agent is getting ready |
| `+N` counter ticks up and the branch bar grows when a commit lands | Work is being committed |
| Merged branch flows into the main line and the row fades to blue | Landed on main |
| Toast in the corner for agent finished, PR opened, merge failed | Events you would otherwise miss |

**Network view (`g` toggles)** replaces the details panel with a graph: agents are nodes around a `main` node, edges are messages (thickness = recent message count), red edges are overlaps or predicted conflicts, and each node shows a tiny branch bar (`+ahead −behind`). This is the "how are they communicating and not breaking each other" picture at a glance.

**File map (`f` toggles)** shows the repo tree with each touched file colored by the agent that touched it; a file touched by two agents is striped and red. It answers "who is in which part of the code".

**Comms view (`c` toggles)** is a full screen for agent-to-agent communication, built from the `SendMessage` events:

- **Graph:** one node per agent plus `you` and `ccdash`; an edge per pair that has exchanged messages, width = message count, color = the conversation's state. Dots travel along an edge while a conversation is active.
- **Conversations list:** messages grouped into threads by pair and topic, newest first, each with a state: discussing (spinner, last message under 2 min ago), agreed (the last message is an acknowledgement or contains "agreed"/"ok"), acked (every recipient of a broadcast replied), open (no reply yet), delivered (your messages). Selecting a thread highlights its edge in the graph and its arrows in the timeline, and shows the transcript.
- **Timeline:** one lane per agent over the last 15 minutes; every message is a vertical arrow from sender lane to receiver lane at its time; a broadcast is one arrow with a dot on each receiving lane; a pulsing "now" line on the right.

Threading rule (v1, keep it simple): a new thread starts when a pair has been silent for 5 minutes or a message names a different file or interface; ccdash's own notices are always their own thread. The mockup's Comms screen shows the target look.

**Budget and fallbacks:** animations cap at 15 fps and the TUI must stay under 3% CPU when idle; `--no-anim` (or `NO_COLOR` / `CCDASH_REDUCED_MOTION=1`) turns all motion into static state changes. Textual supports this directly: CSS themes, `styles.animate()`, timers for spinners. If the result does not feel smooth enough in practice, the alternative is the Charm stack in Go (Bubble Tea + Lip Gloss + Harmonica springs).

## Data sources and state model

ccdash reads three sources and owns one state directory. It never parses Claude's terminal output to decide status; terminal output is only shown, in peek.

| Source | What it gives | How | Freshness |
| --- | --- | --- | --- |
| Claude Code hooks | Status, last tool call, files edited, messages sent and received, needs-input | A settings file passed with `--settings` registers `ccdash hook <event>` for each event, in exec form (`command` + `args`, absolute paths, no shell) and `async: true` except where noted under Hook rules; the hook appends one JSON line | Instant (file watch) |
| git | Branch, ahead/behind, dirty files, commits, predicted conflicts | `git worktree list --porcelain`, `git status --porcelain=v2`, `git rev-list --left-right --count`, `git merge-tree --write-tree` | Poll every 10 s, plus after each commit event |
| tmux | Process alive, live output | `tmux has-session`, `tmux capture-pane -p -S -30` | Poll every 2 s |

**State directory:** `$(git rev-parse --git-common-dir)/ccdash/`. It is shared by all worktrees of the repo and never committed.

| File | Contents | Writer |
| --- | --- | --- |
| `agents.json` | Registry of agents (schema below) | ccdash only, atomic write via temp file + rename |
| `events.jsonl` | Append-only hook events | `ccdash hook`, one `write()` per line under a stdlib file lock (`msvcrt.locking` on Windows, `fcntl.flock` elsewhere). Async hooks can land a few ms out of order, so readers order by `ts` |
| `ownership.json` | File/glob claims per agent | ccdash, from events and the agent's announced scope |
| `hooks.json` | Generated Claude settings with the hook entries | ccdash at startup |

**Agent record (`agents.json`)**

```json
{
  "name": "csv-export",
  "task": "Add CSV export to the reports page",
  "created_at": "2026-11-26T19:40:12+01:00",
  "worktree": ".claude/worktrees/csv-export",
  "branch": "cc/csv-export",
  "base_ref": "main",
  "base_sha": "3f2a9c1",
  "tmux_session": "ccdash-my-app-csv-export",
  "claude_session_id": "set from the SessionStart hook",
  "messaging_socket": "set from CLAUDE_CODE_MESSAGING_SOCKET in the SessionStart hook",
  "model": null,
  "permission_mode": null,
  "lifecycle": "running | stopped | deleted"
}
```

**Event line (`events.jsonl`)**

```json
{"ts": "2026-11-26T19:41:03.120+01:00", "agent": "csv-export", "event": "PreToolUse",
 "tool": "Edit", "path": "src/reports/export.ts", "session_id": "…", "raw": {"…": "hook stdin, truncated to 4 KB"}}
```

The runner sets `CCDASH_AGENT=<name>` and `CCDASH_STATE=<state dir>` in the tmux session's environment, so the hook knows who it is without guessing from the working directory.

**Status derivation** (latest event wins; tmux liveness overrides; verified in Phase 0, see `docs/findings.md`)

| Signal | Status shown |
| --- | --- |
| `UserPromptSubmit`, `PreToolUse`, `PostToolUse`, `PostToolUseFailure`, `PermissionDenied` | working (with the tool and path as "Last") |
| `PermissionRequest` (fires as the dialog opens) | needs input |
| `Notification` with `notification_type` `permission_prompt`, `elicitation_dialog` or `agent_needs_input` | needs input (backup only: arrives ~6 s after `PermissionRequest`) |
| `Notification` `idle_prompt` (fires 60 s after every `Stop`) | no change; it only confirms idle |
| `Stop` | idle; shown as finished if `last_assistant_message` says it is done or a final commit happened |
| Transcript (`transcript_path` from the hook input) ends with `[Request interrupted by user]` | idle. Esc interrupts fire no hook, so while working with no event for 3 s, ccdash reads the transcript's last few KB |
| `SubagentStart`, `SubagentStop` | ignored for status (Claude's own helpers fire them after every turn) |
| `SessionEnd` (`reason` `prompt_input_exit` for `/exit`), or tmux session gone after a clean exit | stopped |
| tmux session gone without `SessionEnd` | crashed |
| No event for 10 minutes while tmux is alive | working (stale), grey timestamp |

A `UserPromptSubmit` whose prompt starts with `<cross-session-message` is an incoming message, not user input. It goes to the Messages panel and still means working.

**Hook rules:** the hook must never block the agent. Observation hooks are registered with `async: true`; only `SessionEnd` (so it is not lost on exit) and guard-mode `PreToolUse` are synchronous. The hook's own work must stay under 10 ms in process. Phase 0 measured 33–45 ms just for Python startup on Windows, so a synchronous 50 ms total is not reachable. `hook.py` imports only the stdlib. The command must point at a python.org or uv-managed interpreter, never the Microsoft Store alias (139 ms startup). The hook must always exit 0, must never write to stdout (except in guard mode, see Coordination), and must survive a missing or locked state directory by dropping the event.

## Coordination and conflict detection

Agents avoid breaking each other in four layers. Each is a backstop for the one before it.

1. **Protocol prompt (prevention).** Every agent starts with the coordination protocol from the existing `cc` script, plus the live agent list from `agents.json` with each agent's task and owned paths. The agent must announce its scope with `SendMessage` before editing, and warn dependents before changing a shared interface. Keep the protocol text in `src/ccdash/protocol.md` so it can be edited without code changes.
2. **File ownership (detection, live).** Every `Edit`/`Write`/`MultiEdit`/`NotebookEdit` event adds its path to that agent's touched set. A path touched by two running agents produces an **overlap** row. A path may also be claimed up front: the agent's announced scope is parsed from its first `SendMessage` if it contains a `Files:` line, and the user can claim paths for an agent from the details panel.
3. **Predicted merge conflicts (detection, git).** For every agent branch, run `git merge-tree --write-tree --name-only <default-branch> <branch>`; exit code 1 means a conflict, and the output lists the files. Also run it pairwise between agent branches that share a touched file. Cache results by the commit-SHA pair. Uncommitted work is not covered by this check; the overlap check covers it.
4. **Guard mode (enforcement, optional).** With `guard = "warn"` or `"block"` in config, the `PreToolUse` hook checks the target path against other agents' claims. On a hit it returns a hook decision (`hookSpecificOutput.permissionDecision` plus `permissionDecisionReason` / `additionalContext`): `warn` lets the edit through but adds context telling Claude which agent owns the file and to message it; `block` denies the edit with that reason. This needs a synchronous `PreToolUse` hook, so it costs one Python start (~60 ms on Windows) per tool call while enabled. Default is `off` until Phase 5.

**Shared interfaces.** The repo config lists globs that many agents depend on, for example `src/api/**`, `migrations/**`, `src/types/**`, `package.json`. When an agent's branch changes one, the Conflicts panel shows a **shared** row. If that agent did not send a message mentioning the change within 2 minutes, ccdash broadcasts a short notice to all other running agents itself.

**After a merge.** When any agent branch lands on the default branch, ccdash messages every other running agent: what landed, which files changed, and whether their branch now has predicted conflicts.

**Messages panel.** Shows every `SendMessage` recorded by `PreToolUse` (`tool_input.to`, `tool_input.summary`, first line of `tool_input.message`; `Enter` expands), plus messages you sent with `t`/`b`, plus ccdash's automatic notices marked `ccdash →`. The receiving side is visible too. Claude delivers an incoming message as a `UserPromptSubmit` whose prompt is `<cross-session-message from="…" from-name="<sender>" from-mode="…">body</cross-session-message>`, so a message is marked delivered when that event arrives. Delivery from ccdash to an agent types the message into the agent's terminal (host pipe on Windows, `tmux send-keys` elsewhere). Phase 0 could not confirm the messaging socket's protocol: the docs define only an auth line. Claude queues text typed while it is busy and submits it after the turn, so ccdash may type at any time except while the agent is in needs input, because keystrokes would answer the open dialog.

**Conflict row severities**

| Kind | Trigger | Severity |
| --- | --- | --- |
| merge | `git merge-tree` reports a conflict against the default branch or another agent branch | red |
| overlap | Same path touched or claimed by 2+ running agents | yellow |
| shared | Branch changes a configured shared-interface glob | yellow, blue once dependents were notified |
| stale base | Branch is more than 20 commits behind the default branch | grey |

## Agent lifecycle

ccdash creates the worktree itself and runs plain interactive `claude` inside it, in a terminal session ccdash names and owns (a ConPTY host on Windows, tmux elsewhere). This gives known paths, known branch names, and reliable stop and attach, without depending on Claude Code's research-preview background mode. A backend for `claude --bg` can be added later behind the same interface (Phase 5).

**Runner backends.** Everything below goes through one `Runner` interface (`start`, `stop`, `restart`, `attach`, `peek`, `send_text`, `is_alive`), with two implementations:

|  | `WinPtyRunner` (Windows, primary) | `TmuxRunner` (macOS, Linux, WSL) |
| --- | --- | --- |
| Keeps the agent alive | A detached `ccdash host <name>` process per agent owns a ConPTY (via `pywinpty`) running `claude` | A tmux session per agent |
| Survives closing the TUI | Yes: host is started with `DETACHED_PROCESS` + `CREATE_NEW_PROCESS_GROUP` | Yes |
| Peek | Host feeds the ConPTY output into a VT screen emulator and keeps the rendered screen plus 2,000 lines of scrollback, served over a named pipe `\\.\pipe\ccdash-<repo>-<name>`. Claude redraws in place, so raw output lines are not readable. The emulator dependency is still to be approved, see findings | `tmux capture-pane` |
| Type into the agent (stop, messages) | Write to the host's pipe, which writes to the ConPTY input | `tmux send-keys` |
| Attach | `cc attach <name>`: puts the PowerShell console in raw VT mode and proxies keys and output through the pipe; `Ctrl+]` detaches. ConPTY output turns on win32-input-mode (`ESC[?9001h`) and focus events (`ESC[?1004h`) in the real terminal, so detach must turn them off again and restore the console mode | `tmux attach`; `Ctrl-b d` detaches |
| Liveness | Host PID + the `claude` child PID in the agent record | `tmux has-session` |

On Windows the step lists below read "host" wherever they say "tmux session". From inside the TUI, attach suspends the TUI and runs the same proxy in the current console.

**Start (`n`, or `cc "task"` from the shell)**

1. Slugify the task into a unique name (rules from the existing script: lowercase, first 4 words, max 30 chars, `-2` suffix on collision).
2. `git worktree add .claude/worktrees/<name> -b cc/<name> <base>`, where base is the default branch (`origin/HEAD`, fetched if older than 24 h) or `HEAD` if chosen. Before the first one, ccdash adds `.claude/worktrees/` to `.git/info/exclude`, so the main checkout does not show it as untracked. On Windows, it sets `core.longpaths=true` in the repo config, with a notice, when the longest tracked path plus the worktree prefix (~50 chars) exceeds 259. If the add fails, it removes the half-made worktree and deletes the branch, which git creates even when the checkout fails. Name collisions also check existing directories and branches, since Claude's own `--worktree` and background agents use `.claude/worktrees/` too.
3. Copy gitignored files listed in `.worktreeinclude` (same semantics as Claude Code's), then run the optional `setup` command from config (for example `npm ci`) inside the worktree, showing progress in the table.
4. Write the agent record, render the prompt (protocol + agent list + task).
5. `tmux new-session -d -s ccdash-<repo>-<name> -c <worktree> -e CCDASH_AGENT=<name> -e CCDASH_STATE=<dir> claude --name <name> --settings <hooks.json> [--model …] [--permission-mode …] "<prompt>"`. The runner removes the parent Claude session's variables from the agent's environment (`CLAUDECODE`, `CLAUDE_PID`, `CLAUDE_CODE_SESSION_ID`, `CLAUDE_CODE_MESSAGING_SOCKET`, `CLAUDE_CODE_MESSAGING_TOKEN`, … see findings) in case `cc` itself runs inside Claude Code, and sets `COLORTERM=truecolor`. On Windows the prompt is part of the command line, which is capped at 32,767 characters.
6. Wait up to 15 s for the `SessionStart` event; if none arrives, mark the agent "starting (no hooks)" and show a hint to check the hooks setup. `SessionStart` only fires after the workspace trust dialog, whose default answer is "No, exit". So before the first spawn, ccdash checks that the repo is trusted (`~/.claude.json`, `projects[<main checkout root>].hasTrustDialogAccepted`). If it is not, ccdash asks you to run `claude` once in the repo. Worktrees inherit the main checkout's trust.

**Stop (`s`)**: send `Escape`, then `/exit` + `Enter` to the pane; wait 10 s for `SessionEnd`; if the pane is still alive, `tmux kill-session`. Worktree, branch and record stay. Lifecycle becomes `stopped`.

**Restart (`r`)**: new tmux session in the same worktree running `claude --resume <claude_session_id> --name <name> --settings <hooks.json>`. `--resume` keeps the session id and the name. `--continue` would pick up any newer session started by hand in that folder, and with no conversation it exits 1 ("No conversation found to continue"). If the record has no session id yet, start with the original prompt instead.

**Attach (`a` / `Enter`)**: suspend the TUI (Textual `App.suspend()`), run `tmux attach -t <session>`; when the user detaches, the TUI resumes and refreshes. If ccdash itself runs inside tmux, use `tmux switch-client` instead and bind a key to come back.

**Delete (`x`)**: stop if running, then check for uncommitted changes, unpushed commits and an unmerged branch. Clean and merged: delete without asking. Otherwise show what would be lost and require typing the agent name. Then `git worktree remove [--force]` and `git branch -D cc/<name>`; mark the record `deleted` and hide it (`H` shows hidden).

**Crash recovery**: on TUI start, reconcile `agents.json` with `git worktree list` and `tmux ls`. A record whose tmux session is gone becomes stopped or crashed; a worktree under `.claude/worktrees/` with no record is shown as "untracked worktree" with an adopt action.

## Commit and merge state

Every agent row shows one merge state, computed from git (and `gh` when available). The STATE column uses the first matching row, top to bottom.

| State | Rule | Shown as |
| --- | --- | --- |
| merged | Branch tip is an ancestor of the default branch, or its PR is merged | blue "merged", offers cleanup |
| conflict | `git merge-tree` against the default branch reports conflicts | red "⚠ conf" + file list in Conflicts |
| PR open | `gh pr view cc/<name> --json state,statusCheckRollup` returns open | "PR #123" + checks ✓/✗/… |
| ready | Clean worktree, ≥1 commit ahead, no conflict, agent idle or finished | green "ready" |
| dirty | Uncommitted changes in the worktree | "dirty" + count |
| committed | Commits ahead, agent still working | "+N" |
| clean | Nothing ahead, nothing dirty | grey "clean" |

The `↑↓ MAIN` column is `git rev-list --left-right --count <default>...<branch>`, shown as `+ahead −behind`.

**Merge menu (`m`)**

- **Ask agent to rebase**: sends "Rebase on \<default>, resolve conflicts, run the tests, report back" to the agent. This is the default for conflicts, since the agent knows its own changes.
- **Merge locally**: only offered when the state is ready and the main checkout is clean and on the default branch. Runs `git merge --no-ff cc/<name>` in the main checkout, then the optional `test` command from config. On failure, `git merge --abort` and report.
- **Open PR**: `git push -u origin cc/<name>` then `gh pr create --fill --head cc/<name>`. Hidden when `gh` is missing or not authenticated.

After any successful merge: refresh all branches' conflict predictions, send the "what landed" notice from the Coordination section, and offer cleanup (delete worktree and branch) for the merged agent.

ccdash never force-pushes, never rewrites the default branch, and never merges without a keypress from you.

## Tech stack, config and project structure

**Stack:** Python 3.11+, [Textual](https://textual.textualize.io) for the TUI, `watchfiles` for the events file, `tomllib` for config, pywinpty for the Windows runner, stdlib file locks (`msvcrt` / `fcntl`; portalocker was dropped after Phase 0 because its import alone takes 165 ms), stdlib `multiprocessing.connection` for the host's named pipe, plain `subprocess` for git, tmux and gh. Install with `uv tool install .` or `pipx install .`; the entry point is `cc`. On Windows the interpreter must be a python.org or uv-managed CPython, not the Microsoft Store one, and ccdash warns at startup if it runs on the Store build. Runtime dependencies besides Python: git 2.38+ (for `merge-tree --write-tree`), Claude Code (the native Windows install on Windows), tmux 3.2+ on macOS/Linux only; `gh` optional.

Why Python and Textual: fastest to build and iterate, good widgets (DataTable, modals, suspend for attach), and snapshot tests. A Go + Bubble Tea version is the alternative if a single static binary matters later.

**Config:** `.ccdash.toml` in the repo root (committed, optional), overridden by `~/.config/ccdash/config.toml`.

```toml
default_branch = "main"          # auto-detected from origin/HEAD if omitted
branch_prefix  = "cc/"
worktree_dir   = ".claude/worktrees"
setup          = "npm ci"         # run once in each new worktree
test           = "npm test"       # used by local merge
# permission_mode = "default"     # unset: agents use your own defaultMode (same for all agents, see Risks);
                                  # set it to pass --permission-mode (default|manual|acceptEdits|auto|dontAsk|plan|bypassPermissions)
guard          = "off"            # off | warn | block
shared         = ["src/api/**", "migrations/**", "src/types/**", "package.json"]
max_agents     = 6
```

**Project structure**

```text
ccdash/
  pyproject.toml
  src/ccdash/
    cli.py            # `cc`, `cc "task"`, `cc ls`, `cc stop <name>`, `cc hook <event>`
    config.py
    state.py          # state dir, agents.json, events.jsonl reader, locking
    hook.py           # the hook entry point: fast, no Textual import
    hooks_settings.py # renders hooks.json for --settings
    runner.py         # Runner protocol + WinPtyRunner + TmuxRunner (BgRunner later)
    host.py           # `ccdash host <name>`: owns one agent's ConPTY + named pipe (Windows)
    attach.py         # `cc attach <name>`: raw-console proxy to a host (Windows)
    worktree.py       # create, include-copy, setup, remove
    gitstate.py       # status, ahead/behind, merge-tree, cached by SHA pair
    conflicts.py      # overlap, merge, shared, stale-base rows
    messaging.py      # deliver to agents: socket or tmux send-keys
    protocol.md       # coordination prompt template
    tui/
      app.py
      agents_table.py  details.py  messages.py  conflicts.py  modals.py
  tests/
    fake_claude.py    # stand-in for claude, see Testing
    test_*.py
    snapshots/
```

`hook.py` must import nothing heavy: it runs on every tool call of every agent.

## Implementation phases

Build in six phases. Each phase ends with a working tool and its acceptance checks passing; do not start the next phase before that.

1. **Phase 0: Spike and verify (half a day).** Answer every question in Risks and open questions with a small script against the real `claude` CLI, and write the answers into `docs/findings.md`. Adjust this plan where an answer differs.
   - Done when: findings recorded for all items; hook events from an interactive `claude` started with `--settings` arrive in a test file.
2. **Phase 1: Runner and table (MVP).** Config, state dir, `WinPtyRunner (with ccdash host and cc attach) first, TmuxRunner second`, worktree creation with include-copy and setup, `cc "task"`, `cc ls`, `cc stop`; TUI with the agents table (git-derived columns only), new/stop/restart/attach/delete, reconcile on start.
   - Done when: 3 agents can be started, attached, stopped, restarted and deleted from the TUI; quitting and reopening the TUI shows the same state; deleting unmerged work asks for confirmation.
3. **Phase 2: Hooks and live status.** `hook.py`, generated `hooks.json`, events tail, status derivation, details panel, peek, Messages panel, talk and broadcast.
   - Done when: status changes show within 2 s; a `SendMessage` between two agents appears in Messages; a permission prompt turns the row to needs input and sorts it to the top.
4. **Phase 3: Look and feel.** Theme, spinners, pulses, toasts, setup progress bars, Network view, reduced-motion mode.
   - Done when: every animation in the Look and feel table exists; `--no-anim` removes all motion; idle CPU under 3%.
5. **Phase 4: Conflicts and merge.** Ownership tracking, overlap, `merge-tree` predictions with SHA cache, shared-interface rows and auto-notice, merge state column, merge menu, PR via `gh`, post-merge notices and cleanup, File map view.
   - Done when: the conflict scenarios in Testing all show the right rows; a local merge of a ready agent works and notifies the others; nothing ever force-pushes.
6. **Phase 5: Extras.** Guard mode (`warn`/`block`), a `claude --bg` runner backend, multiple repos in one screen, opening an agent in a new VS Code terminal tab.
   - Done when: each extra is behind a config flag and off by default.

## Testing strategy

All tests run without the real Claude and without network access. Real-Claude checks are a short manual script, run once per phase.

- **Fake Claude.** `tests/fake_claude.py` is put on `PATH` as `claude`. It reads a scenario file (via an environment variable) and, with realistic delays, pipes hook JSON into `ccdash hook <event>`, edits files in its worktree, commits, and "sends messages" by emitting `PreToolUse` events for `SendMessage`. It reacts to `/exit` on stdin.
- **Temp repos.** A pytest fixture creates a bare origin plus a clone with a `main` branch and a few files, so worktree, merge-tree, merge and push code run against real git.
- **Unit tests:** slugify and naming, status derivation from event sequences, conflict detection, merge-state rules, config precedence, hook speed (a test fails if the hook's in-process work takes over 10 ms, or a full hook call over 150 ms including interpreter startup).
- **TUI tests:** Textual's `Pilot` drives keys; `pytest-textual-snapshot` covers the main screen, each modal, the Network view and the reduced-motion mode.
- **tmux tests:** marked `@tmux`, run in CI on Linux with tmux installed, skipped elsewhere.

* **Windows first.** CI runs the full suite on `windows-latest` (PowerShell) and `ubuntu-latest`. Runner tests for `WinPtyRunner` run on Windows only; the attach proxy gets a scripted test that sends keys through the pipe and reads them back from a fake agent.
* **Manual check per phase on Windows:** start `cc` from PowerShell in Windows Terminal and in the VS Code terminal; start 3 agents; close the window; reopen `cc` and confirm all 3 still run.

**Conflict scenarios that must pass**

| Scenario | Expected |
| --- | --- |
| Agents A and B both edit `src/ui/Toolbar.tsx`, uncommitted | overlap row A × B within 2 s |
| A commits a change to a line that `main` also changed | merge row A × main; A's state = conflict |
| A and B commit incompatible changes to the same function | merge row A × B |
| A changes `src/api/orders.ts` (shared) and sends no message | shared row, then a ccdash broadcast after 2 min |
| A is merged locally | B and C receive the "what landed" notice; their predictions refresh |
| A's tmux session is killed | A shows crashed; worktree untouched |

## Risks and open questions to verify first

These are assumptions about Claude Code that this plan relies on. Phase 0 checked each one against Claude Code 2.1.317 on native Windows and the docs at code.claude.com; evidence is in `docs/findings.md`.

| # | Question | If the answer is no | Phase 0 answer |
| --- | --- | --- | --- |
| 1 | Do hooks passed via `claude --settings <file>` merge with the user's and project's own hooks, rather than replacing them? | Write the hook entries into the main checkout's `.claude/settings.local.json` instead, marked so ccdash can remove them | Yes (project and local verified, user level per docs). The fallback would not work: worktree sessions do not read the main checkout's `settings.local.json` |
| 2 | What are the exact `tool_input` field names of the `SendMessage` tool in `PreToolUse` hook input (recipient, text)? | Log the raw input and display it unparsed | `to`, `message`, `summary` (PreToolUse adds `recipient`, `content`, `type`, `recipient_kind`); the receiver gets a `<cross-session-message>` `UserPromptSubmit` |
| 3 | Does the `Notification` hook fire for permission prompts and idle-waiting in interactive mode, and with which `notification_type` or message? | Detect "needs input" by matching the tmux pane for the permission prompt as a fallback | Yes: `permission_prompt` (~6 s late) and `idle_prompt` (60 s after Stop). `PermissionRequest` is the immediate signal |
| 4 | Is `CLAUDE_CODE_MESSAGING_SOCKET` visible to hooks, and what line protocol does the socket accept from an outside process? | Deliver ccdash's messages with `tmux send-keys` while the agent is idle | Visible (`\\.\pipe\LOCAL\cc-msg-…`); protocol undocumented beyond an auth line, so the fallback is used: typing, allowed any time except in needs input |
| 5 | Does `claude --name <n> "<prompt>"` start an interactive session with that name and initial prompt, visible to other sessions' `ListAgents`? | Send the prompt with `send-keys` after start, and name with `/rename` | Yes |
| 6 | Does Claude Code's worktree isolation treat a ccdash-created worktree under `.claude/worktrees/` like its own? | Start agents with `claude --worktree <name>` and let Claude create the worktree, then read its path from `git worktree list` | No, and that is safe: no keep/remove prompt on exit, nothing deleted, trust inherited |
| 7 | Does `claude --continue` in a worktree resume that worktree's last conversation? | Store `session_id` from `SessionStart` and use `--resume <id>` | Yes, but restart uses `--resume <id>` anyway (see Agent lifecycle) |

**Windows-specific questions (also Phase 0)**

- Which shell does Claude Code on native Windows use to run hook commands? **Answer:** Git Bash for shell-form commands. ccdash uses exec form (`command` + `args`, absolute paths), which runs with no shell.
- Does a `claude` started in a ConPTY by a detached host render and accept input correctly (colors, prompts, permission dialogs), and does `Ctrl+C` reach it? **Answer:** yes to all. Peek needs a VT emulator, and attach must reset terminal modes (see Agent lifecycle).
- Do Claude Code's worktree paths and the `.git/ccdash` state dir hit the 260-character path limit in deep repos? If so, set `core.longpaths` and keep worktree names short. **Answer:** yes; `git worktree add` fails without `core.longpaths=true` and works with it.
- Does cross-session messaging (named pipes on Windows, Claude Code v2.1.234+) list ccdash-started sessions, and can the host's pipe coexist with it? **Answer:** yes to both.

**Open after Phase 0 (needs a decision)**

- A VT screen emulator for peek on Windows (for example `pyte`) is a dependency the plan did not name. Needed from Phase 2.

**Other risks**

- **Mixed permission modes block messages.** Messages from a session that bypasses permissions to one that doesn't (and the reverse) are held for approval. ccdash uses one `permission_mode` for all agents and warns when an agent differs.
- **Token cost.** Each agent is a full session, and the protocol adds messages. `max_agents` (default 6) caps it, and the header shows the running count.
- **The `.git` state dir.** Some tools clean unknown files in `.git`. Fall back to `~/.local/state/ccdash/<repo-hash>/` if writing fails.
- **tmux on macOS in VS Code.** Attach must work in the VS Code integrated terminal; test it there in Phase 1.
- **Claude Code changes fast.** Keep every Claude-specific assumption in `runner.py`, `hook.py` and `messaging.py`, so a CLI change touches three files. The event-to-status mapping, the `<cross-session-message>` parser and the transcript interrupt check belong in `hook.py`; the env scrub list and trust check belong in `runner.py`.

## Handoff prompt for Claude Code

Export this doc as Markdown, save it as `docs/PLAN.md` in a new empty repo named `ccdash`, start `claude` there, and paste:

```text
Read docs/PLAN.md fully. It is the spec for ccdash, a terminal dashboard that
starts, monitors and coordinates parallel Claude Code agents in git worktrees.

Work phase by phase, exactly as the "Implementation phases" section says:
- Start with Phase 0. Verify every item in "Risks and open questions" against
  the installed claude CLI (claude --version, claude --help, and the docs at
  code.claude.com). Write the results to docs/findings.md. Where a finding
  contradicts the plan, update docs/PLAN.md and tell me what changed before
  you continue.
- For each phase: write the tests first where the plan lists acceptance checks,
  implement, run the full test suite, and stop at the end of the phase with a
  short summary and the manual check I should do. Don't start the next phase
  until I say so.
- Keep every Claude-Code-specific assumption in runner.py, hook.py and
  messaging.py.
- Target: native Windows, started from PowerShell. Build and test the Windows runner first. Never run destructive git commands against a real repo in tests; use the
  temp-repo fixtures.
- Look and feel matters: it should feel like the Claude Code CLI itself. Follow
  the "Look and feel" section, and show me a screenshot (Textual's
  save_screenshot) at the end of Phases 1, 3 and 4.

Ask me before adding any dependency not named in the plan.
```

For later phases, start a fresh session with "Continue with Phase N of docs/PLAN.md; findings are in docs/findings.md."
