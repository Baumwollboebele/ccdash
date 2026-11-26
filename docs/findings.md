# Phase 0 findings

Nov 26, 2026 · verified against the installed CLI on the target machine.

| Component | Version |
| --- | --- |
| Claude Code | 2.1.317 (native Windows install via winget) |
| OS / shell | Windows 11 Home 10.0.26200, PowerShell 7.7.0, Windows Terminal |
| git | 2.56.1.windows.1 (Git for Windows, Git Bash present) |
| Python | 3.12.10 (python.org build and Microsoft Store build both installed) |
| pywinpty | 3.1.2 (ConPTY backend) |
| uv | 0.13.10 |
| gh, tmux | not installed |

Method: every interactive check ran a real `claude` inside a **detached ConPTY host** (`spikes/phase0/host.py`, the prototype of `ccdash host`), driven over its named pipe by `spikes/phase0/client.py`. Each session got a `--settings` file that logs every hook event to JSONL (`spikes/phase0/hooklog.py`). The test repo, worktrees and logs lived in a throwaway temp directory. Docs were read at code.claude.com (hooks, settings, cross-session-messaging, worktrees, sessions, cli-reference, permission-modes, env-vars). Sessions used `--model haiku` to keep the cost down.

## Results at a glance

| # | Question | Answer | Plan impact |
| --- | --- | --- | --- |
| 1 | `--settings` hooks merge with user/project hooks? | **Yes.** `--settings`, project `.claude/settings.json` and local `.claude/settings.local.json` SessionStart hooks all fired in one session. User level not tested because the user has no user hooks; the docs say hook entries merge across all levels. | No change to the approach. The fallback in the plan would not work anyway (see 1b). |
| 2 | `SendMessage` `tool_input` field names | **`to`, `message`, `summary`.** PreToolUse also carries normalized copies `type`, `recipient`, `recipient_kind`, `content`. The **receiver** side is visible too, as a `UserPromptSubmit` whose prompt wraps the message in `<cross-session-message …>`. | Messages panel records both ends and can mark delivery. |
| 3 | `Notification` for permission prompts / idle? | **Yes, but late.** `PermissionRequest` fires 0.16 s after PreToolUse. `Notification` `permission_prompt` follows 6 s later. `idle_prompt` fires 60 s after every Stop. | needs input comes from `PermissionRequest`. `idle_prompt` must **not** mean needs input. |
| 4 | `CLAUDE_CODE_MESSAGING_SOCKET` visible to hooks; protocol? | Visible: `\\.\pipe\LOCAL\cc-msg-<hex>`, plus `CLAUDE_CODE_MESSAGING_TOKEN`. Protocol **not confirmed**: the docs specify only an auth line (`{"type":"auth","token":…}`, required on Windows), not the message body. | Use the fallback: type into the agent's terminal. |
| 5 | `claude --name n "<prompt>"` interactive, named, visible to `ListAgents`? | **Yes.** The prompt is submitted at start (`UserPromptSubmit`). `SessionStart.session_title` is the name. Other sessions' `ListAgents` list it as `p0-alpha · interactive · idle`. | None. |
| 6 | Does Claude treat a ccdash worktree under `.claude/worktrees/` as its own? | **No.** `/exit` showed no keep/remove prompt for a clean or a dirty worktree. Worktree, branch and untracked file all stayed. Trust is inherited from the main checkout. | None. Plain `git worktree add` is safe. |
| 7 | `claude --continue` in a worktree resumes that worktree's last conversation? | **Yes.** `SessionStart.source = "resume"` with the same `session_id`, and the context was kept. With no prior conversation it prints `No conversation found to continue` and **exits 1**. `--resume <session_id>` also works and keeps `--name`. | Restart prefers `--resume <id>` and starts fresh when no id is recorded. |
| W1 | Which shell runs hook commands on Windows? | Shell-form hooks (a `command` string without `args`) run under **Git Bash** (`bash.exe` parent). Exec form (`command` + `args`) has **no shell**: the hook's parent is `claude.exe`. | Use exec form with absolute paths. |
| W2 | `claude` in a ConPTY owned by a detached host: rendering, input, Ctrl+C? | **Works.** Truecolor output, prompt box, trust dialog and permission dialog all render. Typed input and Enter/Esc/arrows work. Ctrl+C reaches it ("Press Ctrl-C again to exit"). The host survives its launcher exiting. | Peek needs a VT screen emulator (see W2b). |
| W3 | 260-char path limit? | **Hit.** A 169-char in-repo path became 335 chars inside a worktree. `git worktree add` failed with "Filename too long" without `core.longpaths`, and succeeded with it. The `LongPathsEnabled` registry key was already 1. | Set `core.longpaths=true` when needed. Roll back failed adds. |
| W4 | Cross-session messaging lists ccdash sessions; host pipe coexists? | **Yes to both.** A ConPTY-hosted alpha sent to a hosted beta through `SendMessage` while each host served `\\.\pipe\ccdash-p0-<name>` next to Claude's `\\.\pipe\LOCAL\cc-msg-…`. | None. |

## Details and evidence

### 1. Hook merging

The `--settings` file registered every event. The repo's committed `.claude/settings.json` registered `SessionStart-project` and its gitignored `.claude/settings.local.json` registered `SessionStart-local`. In the main checkout all three fired for the same `session_id` within 70 ms.

**1b. Local settings and worktrees.** In a worktree session, the main checkout's `.claude/settings.local.json` hook did **not** fire (the file is gitignored, so the worktree has none). The plan's fallback for "no" ("write the hook entries into the main checkout's `.claude/settings.local.json`") would not reach agents in worktrees. It isn't needed, because `--settings` merges.

Hook input fields seen (all events): `session_id`, `transcript_path`, `cwd`, `scratchpad_dir`, `hook_event_name`. All events except SessionStart and Notification also carried `permission_mode` (`"default"` for manual) and `prompt_id`. SessionStart added `source` (`startup`/`resume`), `model` (`claude-haiku-5-5`) and `session_title` (the `--name`). Stop added `last_assistant_message`, `stop_hook_active`, `background_tasks` and `session_crons`.

Environment the hook sees: everything the host passed (`CCDASH_AGENT`, `CCDASH_STATE` arrived intact), plus Claude's own `CLAUDE_PROJECT_DIR` (= the worktree), `CLAUDE_CODE_SESSION_ID`, `CLAUDE_CODE_MESSAGING_SOCKET`, `CLAUDE_CODE_MESSAGING_TOKEN`, `CLAUDE_ENV_FILE`, `CLAUDE_PID`, `CLAUDE_EFFORT`, `CLAUDECODE=1`, `TERM=xterm-256color`, `SHELL=…\Git\bin\bash.exe`.

### 2. SendMessage, both ends

Sender (alpha), PreToolUse:

```json
{"tool_name": "SendMessage",
 "tool_input": {"to": "p0-beta", "summary": "Ping from alpha to p0-beta",
                "message": "ping from alpha. Files: src/app.py",
                "type": "message", "recipient": "p0-beta", "recipient_kind": "name",
                "content": "ping from alpha. Files: src/app.py"},
 "tool_use_id": "toolu_…"}
```

PostToolUse has only `to`/`summary`/`message` in `tool_input`, plus `tool_response: {"success": true, "message": "…", "display": "…", "msg_id": "c2a78b11-…"}`. In `default` permission mode `SendMessage` needed no approval.

Receiver (beta), 0.19 s later, `UserPromptSubmit.prompt`:

```text
<cross-session-message from="uds:\\.\pipe\LOCAL\cc-msg-c5315910…" from-name="p0-alpha" from-mode="prompting">
ping from alpha. Files: src/app.py
</cross-session-message>
```

The receiver then works on it like a normal prompt (tool calls, Stop). `from-mode` shows the sender's permission class (`prompting` vs bypass). The docs confirm that messages crossing those classes are held for approval, so the plan's "one permission_mode for all agents" rule stands.

### 3. Needs-input signals and status

Observed sequence for a `Write` in `default` mode:

| Time | Event |
| --- | --- |
| 20:16:49.854 | PreToolUse `Write` |
| 20:16:50.018 | **PermissionRequest** `Write` (dialog shown) |
| 20:16:56.041 | Notification `notification_type: "permission_prompt"`, `message: "Claude needs your permission"` |
| 20:17:12.313 | PostToolUse `Write` (after approval with Enter) |
| 20:17:13.943 | Stop, `last_assistant_message: "FINISHED"` |
| 20:18:14.052 | Notification `idle_prompt`, `"Claude is waiting for your input"` (60 s after Stop) |

Other status-relevant observations:

- **Esc interrupt fires no hook at all.** No Stop, no other event; the screen shows "Interrupted · What should Claude do instead?". The transcript (`transcript_path`) gains a `user` entry with text `[Request interrupted by user]`. Without handling, ccdash would show "working" forever.
- **SubagentStop fires without any user subagent**, about 1 s after each Stop (Claude's prompt-suggestion helper; one carried `last_assistant_message: "commit this"`). It must not affect status.
- `/exit` → `SessionEnd.reason = "prompt_input_exit"`, process exit code 0.
- A trust dialog delays `SessionStart` until it is answered. Its default choice is **"No, exit"**.

### 4. Messaging socket

Each session gets its own `\\.\pipe\LOCAL\cc-msg-<32 hex>` and a per-session token, both exported to hooks. The docs document only an optional/required auth line for "a script posting to its own session's socket", not a message format, and don't present outside processes as supported senders. Treated as **not confirmed**, so ccdash uses the fallback and types into the agent.

Typing results:
- Text typed while the agent is **busy** is queued by Claude and submitted right after the current turn: the prompt typed 2 s into a turn arrived as `UserPromptSubmit` 70 ms after that turn's Stop. So no need to wait for idle.
- Typing while a **permission dialog** is open would answer the dialog (Enter selects "1. Yes"). Never type while the agent is in needs input.

### 5. Name, initial prompt, ListAgents

`claude --name p0-alpha --settings … --model haiku --permission-mode default "<prompt>"` opened the normal interactive UI, submitted the prompt (`UserPromptSubmit`), and showed `p0-alpha` in its prompt bar. The listing from another session:

```text
Peer sessions (2):
  p0-beta [7141be]  ·  interactive  ·  idle  ·  started 17s ago
  p0-alpha [8f9417]  ·  interactive  ·  idle  ·  started 57s ago
```

`--permission-mode default` is accepted even though `--help` lists only `acceptEdits, auto, bypassPermissions, manual, dontAsk, plan`. A bogus value is rejected before any API call. Without the flag, this account's sessions start in **auto** mode (the user's own default), so passing `default` would override the user's setting.

### 6. Worktrees

- `git worktree add .claude/worktrees/p0-alpha -b cc/p0-alpha main`, then `claude` in it: no trust dialog (inherited from the main checkout), no keep/remove prompt on `/exit`, nothing deleted (dirty worktree with an untracked `hello.txt` kept).
- Transcripts are stored per worktree path (`~/.claude/projects/<mangled worktree path>/<session>.jsonl`).
- After `git worktree add`, the **main checkout shows `?? .claude/worktrees/`** as untracked, which breaks "main checkout is clean" checks. ccdash should add `.claude/worktrees/` to `.git/info/exclude`.
- Claude's own `--worktree` and background agents also create worktrees under `.claude/worktrees/` (branch `worktree-<name>`). The name-collision check must look at existing directories and branches, not only `agents.json`.

### 7. Continue and resume

| Command (in worktree) | Result |
| --- | --- |
| `claude --continue --name p0-alpha …` after a session there | `SessionStart source=resume`, same `session_id` 6863fe3b…, prior context remembered |
| `claude --continue …` in a worktree with no session | prints `No conversation found to continue`, exit code 1, no hooks fire |
| `claude --resume 6863fe3b-… --name p0-alpha …` | `source=resume`, same `session_id`, still listed as `p0-alpha` |

`--resume <id>` is the safer restart: `--continue` would pick any newer session someone else started in that folder.

### W1. Hook shell and hook cost

- Shell form, `"command": "\"C:/…/python.exe\" \"C:/…/hooklog.py\" X"`, ran with parent chain `bash.exe → bash.exe` (Git Bash). Backslash paths in shell form would be mangled by bash.
- Exec form, `"command": "C:\\…\\python.exe", "args": ["…\\hooklog.py", "X"]`, ran as a direct child of `claude.exe`. This is the form to use.
- `"async": true` works: the async PostToolUse copy logged within 2–10 ms of the sync one.

Cost of a Python hook on this machine (median, 10–15 runs, payload on stdin, append one line):

| Variant | ms |
| --- | --- |
| `python -c pass`, python.org CPython | 33 |
| same through a venv `Scripts\python.exe` launcher | 43 |
| same, Microsoft Store Python (WindowsApps alias) | 139 |
| lean hook (json + `msvcrt.locking`), base CPython `-I -S` | 58 |
| lean hook, base CPython | 66 |
| lean hook, venv | 74 |
| lean hook, venv, with **portalocker** | 221 (`import portalocker` alone: 165 ms) |

**Consequence:** a synchronous Python hook cannot meet "under 50 ms" on Windows; interpreter startup alone eats most of it. And portalocker is far too slow to import in the hook.

### W2. ConPTY host details

- pywinpty `PtyProcess.spawn(argv, cwd, env, dimensions=(35, 120))` with a reader thread works from a process started with `DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP`. The host and `claude` kept running after the launcher and its shell exited. The tool shell was not in a job object.
- The named pipe was served with stdlib `multiprocessing.connection.Listener(family="AF_PIPE")` with byte framing. No extra dependency.
- Output arrives as ConPTY's re-encoding:
  - runs of spaces become `ESC[<n>G` (absolute column)
  - truecolor `ESC[38;2;r;g;bm` passes through
  - ConPTY emits `ESC[?9001h` (win32-input-mode request), `ESC[?1004h` (focus events) and a DA1 query `ESC[c`
  - no alternate screen
- Claude printed "Try setting environment variable COLORTERM=truecolor for richer colors", so the host should set `COLORTERM=truecolor`.
- **W2b. Peek.** Claude redraws its UI in place with cursor movement, so "the last N lines" of the raw stream are not readable text. A readable peek on Windows needs a VT screen emulator in the host (for example `pyte`), which the plan does not name. tmux gets this for free from `capture-pane`.
- **Env hygiene.** A shell inside a Claude Code session exports `CLAUDECODE`, `CLAUDE_PID`, `CLAUDE_CODE_ENTRYPOINT`, `CLAUDE_CODE_CHILD_SESSION`, `CLAUDE_CODE_SESSION_ID`, `CLAUDE_CODE_SESSION_ATTENDED`, `CLAUDE_CODE_MESSAGING_SOCKET`, `CLAUDE_CODE_MESSAGING_TOKEN`, `CLAUDE_CODE_BRIDGE_SESSION_ID`. The host removed these before spawning, and each agent got its own session id and socket. Spawning without scrubbing was not tested; the scrub is a precaution for `cc` run from inside Claude Code.
- On Windows the venv `python.exe` is a launcher that starts the real interpreter as a child, so the host must record `os.getpid()` itself, not the PID `Popen` returned.
- The initial prompt travels on the command line. CreateProcess caps a command line at 32,767 characters, so protocol + agent list + task must stay under that (or move to a file).

### W3. Long paths

The repo path was 117 chars. The file `deeply-nested-directory-level/…` (169 chars) became 335 chars inside `.claude/worktrees/add-csv-export-to-the-reports/`.

- Without `core.longpaths`: `fatal: cannot create directory at '…': Filename too long`, rc 128, **and the branch `cc/<name>` was left behind**.
- With `core.longpaths=true` (repo config): worktree created, `git status` clean, Python read and write fine (python.org build; `LongPathsEnabled=1`).

A ccdash worktree adds about 50 chars (`\.claude\worktrees\<name≤30>\`) to every path.

### W4. Coexistence

Two hosted sessions each served their own `\\.\pipe\ccdash-p0-<name>`, while Claude bound `\\.\pipe\LOCAL\cc-msg-…`. Different namespaces, no interference. `SendMessage` alpha → beta was delivered in 0.19 s.

### Addendum: HTTP hooks (checked after Phase 0, `spikes/phase0/httphook.py`)

A session with `--settings` registering `type: "http"` hooks to `http://127.0.0.1:<port>/hook/…` (served by a stdlib `ThreadingHTTPServer`) delivered UserPromptSubmit, PreToolUse, PostToolUse and Stop as POSTs (`Content-Type: application/json`, `User-Agent: axios/…`; same JSON as command stdin). It did **not** deliver SessionStart. The docs say SessionStart (and Setup) support only `command` and `mcp_tool` hooks. SessionEnd, Notification and PermissionRequest support `http`. HTTP hooks have no `async`. They cost no process start, so they are fast, synchronous and in order. An `allowedHttpHookUrls` setting, if the user or their org defines one, would block them.

**Decision:** on Windows, the per-agent `ccdash host` serves the agent's hook endpoint on localhost. SessionStart stays a command hook (async, once per session). The tmux runner uses command hooks throughout. If `allowedHttpHookUrls` is set, command hooks are used everywhere.

### Real-Claude check after Phase 2 (`scripts/real_claude_check.py`)

Two Haiku agents started through ccdash itself, using the Windows host, HTTP hooks and `--session-id`, in the trusted spike repo:

- Each agent was running about 1 s after `new_agent`.
- Both sessions used the session id ccdash passed.
- Status went starting → idle → working → idle/finished within a second of each hook.
- `rc-beta` announced `Files: notes-rc-beta.txt` to `rc-alpha` with `SendMessage`. The receiving `UserPromptSubmit` marked it delivered.
- Stop produced `SessionEnd` (stopped), and restart resumed the same session id with `--resume`.
- Delete removed both worktrees and branches.

## Side effects on this machine

- Accepting the trust dialog once added the temp repo to `~/.claude.json` (`projects["C:/…/scratchpad/p0/repo"].hasTrustDialogAccepted = true`). This is also where ccdash can check trust before spawning.
- The spike sessions left transcripts under `~/.claude/projects/C--Users-felix-AppData-Local-Temp-claude-…-p0-repo*`. `claude purge <path>` removes them if wanted.
- No other files outside the temp directory were written. All hosts and `claude` processes exited cleanly (exit code 0, `SessionEnd`).

## Open items needing a decision

1. **VT emulator for peek on Windows** (W2b): a dependency not named in the plan, for example `pyte`. Needed in Phase 2 (peek). The attach proxy (Phase 1) does not need it.
2. **Mockups**: `docs/mockup-main.png` and `docs/mockup-comms.png` are not in the repo. The ASCII layout in PLAN.md is used as the target until they are added.

## Reproduce

```powershell
uv venv p0venv --python <python.org python.exe>; uv pip install --python p0venv\Scripts\python.exe pywinpty
p0venv\Scripts\python.exe spikes\phase0\setup_repo.py <tmp> <python.org python.exe>
p0venv\Scripts\python.exe spikes\phase0\client.py launch p0-alpha <tmp>\repo\.claude\worktrees\p0-alpha <tmp>\run -- claude --name p0-alpha --settings <tmp>\run\hooks.json "<prompt>"
p0venv\Scripts\python.exe spikes\phase0\client.py screen p0-alpha
p0venv\Scripts\python.exe spikes\phase0\events.py <tmp>\run\events.jsonl
```

Run the client from PowerShell, not Git Bash: MSYS path conversion rewrites arguments like `/exit` into `C:/Program Files/Git/exit`.
