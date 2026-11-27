"""Stand-in for the `claude` CLI, used by every test (never the real Claude).

It understands the flags ccdash passes, fires hooks from the --settings file the
way Claude Code does (command hooks in exec or shell form, async or not, and
http hooks), runs a scenario, then behaves like an idle interactive session:
each stdin line is a prompt, `/exit` ends the session.

Environment:
  CCDASH_FAKE_SCENARIO  JSON file: a list of steps for every agent, or
                        {"<agent name>": [...], "*": [...]}.
  CCDASH_FAKE_HOME      where sessions and transcripts are kept (default: temp dir).

Steps: {"sleep": s} {"edit": path, "text": "..."} {"write": path, "text": "..."}
       {"commit": "msg"} {"send": {"to": name, "message": "..."}}
       {"permission": "Write", "path": p}  (waits for "1"/"y" or anything else = deny)
       {"say": "final message"} {"crash": code} {"print": "text"}
"""

import argparse
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import uuid

HOME = os.environ.get("CCDASH_FAKE_HOME") or os.path.join(tempfile.gettempdir(), "fake-claude")
CWD = os.getcwd()


def out(text):
    sys.stdout.write(text + "\r\n")
    sys.stdout.flush()


class Session:
    def __init__(self, args):
        self.args = args
        self.name = args.name or "fake"
        self.hooks = {}
        if args.settings:
            src = args.settings
            data = json.loads(src) if src.lstrip().startswith("{") else json.load(open(src, encoding="utf-8"))
            self.hooks = data.get("hooks", {})
        os.makedirs(HOME, exist_ok=True)
        self.index_path = os.path.join(HOME, "sessions.json")
        self.source = "startup"
        if args.resume:
            self.session_id = args.resume
            self.source = "resume"
        elif args.cont:
            last = self._index().get(CWD.lower())
            if not last:
                out("No conversation found to continue")
                sys.exit(1)
            self.session_id = last
            self.source = "resume"
        else:
            self.session_id = args.session_id or str(uuid.uuid4())
        idx = self._index()
        idx[CWD.lower()] = self.session_id
        with open(self.index_path, "w", encoding="utf-8") as f:
            json.dump(idx, f)
        # Same place Claude Code keeps transcripts: <config dir>/projects/<cwd with non-alphanumerics as '-'>/
        config_dir = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(HOME, "claude-config")
        mangled = "".join(c if c.isascii() and c.isalnum() else "-" for c in os.path.realpath(CWD))
        os.makedirs(os.path.join(config_dir, "projects", mangled), exist_ok=True)
        self.transcript = os.path.join(config_dir, "projects", mangled, f"{self.session_id}.jsonl")
        self.last_message = ""
        self.pending = []

    def _index(self):
        try:
            return json.load(open(self.index_path, encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def log_transcript(self, kind, text):
        with open(self.transcript, "a", encoding="utf-8") as f:
            f.write(json.dumps({"type": kind, "message": {"content": [{"type": "text", "text": text}]},
                                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}) + "\n")

    # -- hooks -------------------------------------------------------------
    def fire(self, event, **fields):
        payload = {
            "session_id": self.session_id,
            "transcript_path": self.transcript,
            "cwd": CWD,
            "hook_event_name": event,
            "permission_mode": self.args.permission_mode or "default",
            **fields,
        }
        body = json.dumps(payload).encode("utf-8")
        threads, outputs = [], []
        for group in self.hooks.get(event, []):
            matcher = group.get("matcher") or "*"
            tool = fields.get("tool_name")
            if tool and matcher not in ("*", "") and tool not in matcher.split("|"):
                continue
            for h in group.get("hooks", []):
                sync = not h.get("async")  # an async hook cannot decide anything: its output is ignored
                t = threading.Thread(target=self._run_hook, args=(h, body, outputs if sync else None), daemon=True)
                t.start()
                if sync:
                    threads.append(t)
        for t in threads:
            t.join(timeout=10)
        return [o["hookSpecificOutput"] for o in outputs if isinstance(o.get("hookSpecificOutput"), dict)]

    def _run_hook(self, h, body, outputs=None):
        try:
            if h.get("type") == "http":
                req = urllib.request.Request(h["url"], data=body, headers={"Content-Type": "application/json"})
                raw = urllib.request.urlopen(req, timeout=h.get("timeout", 5)).read()
            elif "args" in h:
                raw = subprocess.run([h["command"], *h["args"]], input=body, capture_output=True,
                                     timeout=h.get("timeout", 60)).stdout
            else:
                raw = subprocess.run(h["command"], shell=True, input=body, capture_output=True,
                                     timeout=h.get("timeout", 60)).stdout
            if outputs is not None and raw.strip():
                parsed = json.loads(raw)
                if isinstance(parsed, dict):
                    outputs.append(parsed)
        except Exception:  # noqa: BLE001  (hook failures never break the session)
            pass

    # -- behaviour ---------------------------------------------------------
    def tool(self, name, tool_input, action=None, response=None):
        """Like Claude Code: a PreToolUse hook may deny the call (the tool does not run and Claude sees
        the reason), or add context that Claude gets next to the tool result."""
        verdicts = self.fire("PreToolUse", tool_name=name, tool_input=tool_input,
                             tool_use_id=f"toolu_{uuid.uuid4().hex[:12]}")
        for v in verdicts:
            if v.get("permissionDecision") == "deny":
                out(f"x {name} denied: {v.get('permissionDecisionReason', '')}")
                return False
        if action:
            action()
        self.fire("PostToolUse", tool_name=name, tool_input=tool_input, tool_response=response or {"ok": True},
                  tool_use_id=f"toolu_{uuid.uuid4().hex[:12]}")
        for v in verdicts:
            if v.get("additionalContext"):
                out(f"  note: {v['additionalContext']}")
        return True

    def run_steps(self, steps):
        for step in steps:
            if "sleep" in step:
                time.sleep(step["sleep"])
            elif "print" in step:
                out(step["print"])
            elif "edit" in step or "write" in step:
                rel = step.get("edit") or step.get("write")
                path = os.path.join(CWD, rel)
                kind = "Edit" if "edit" in step else "Write"
                out(f"> {'Editing' if kind == 'Edit' else 'Writing'} {rel} …")

                def act(path=path, step=step):
                    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
                    with open(path, "a" if step.get("edit") else "w", encoding="utf-8", newline="\n") as f:
                        f.write(step.get("text", "change\n"))

                self.tool(kind, {"file_path": path, "content": step.get("text", "")}, act)
            elif "commit" in step:
                msg = step["commit"]

                def act(msg=msg):
                    subprocess.run(["git", "add", "-A"], cwd=CWD, capture_output=True)
                    subprocess.run(["git", "commit", "-q", "-m", msg], cwd=CWD, capture_output=True)

                out(f"> git commit -m {msg!r}")
                self.tool("Bash", {"command": f"git add -A && git commit -m {json.dumps(msg)}"}, act)
            elif "send" in step:
                s = step["send"]
                ti = {"to": s["to"], "summary": s.get("summary", s["message"][:40]), "message": s["message"],
                      "type": "message", "recipient": s["to"], "recipient_kind": "name", "content": s["message"]}
                out(f"> SendMessage → {s['to']}")
                self.tool("SendMessage", ti, response={"success": True, "msg_id": str(uuid.uuid4())})
            elif "permission" in step:
                tool = step["permission"]
                ti = {"file_path": os.path.join(CWD, step.get("path", "x.txt")), "content": "x"}
                self.fire("PreToolUse", tool_name=tool, tool_input=ti, tool_use_id="toolu_perm")
                self.fire("PermissionRequest", tool_name=tool, tool_input=ti)
                out(f"Do you want to use {tool}?  1. Yes  2. No")
                reminder = threading.Timer(0.5, lambda: self.fire("Notification", notification_type="permission_prompt",
                                                                  message="Claude needs your permission"))
                reminder.start()
                answer = read_line()
                reminder.cancel()  # no "needs your permission" after the dialog was answered
                reminder.join(timeout=5)  # one already on its way lands before the events that follow
                if answer is None:
                    return
                if answer.strip().lower() in ("", "1", "y", "yes"):
                    self.fire("PostToolUse", tool_name=tool, tool_input=ti, tool_response={"ok": True}, tool_use_id="toolu_perm")
                else:
                    self.fire("PostToolUseFailure", tool_name=tool, tool_input=ti, error="denied", tool_use_id="toolu_perm")
            elif "say" in step:
                self.last_message = step["say"]
            elif "crash" in step:
                out("fake-claude: simulated crash")
                os._exit(int(step["crash"]))

    def turn(self, prompt, steps=()):
        self.fire("UserPromptSubmit", prompt=prompt)
        self.log_transcript("user", prompt)
        self.last_message = ""
        self.run_steps(steps)
        if not self.last_message:
            self.last_message = f"ack: {prompt[:60]}"
        out(f"● {self.last_message}")
        self.log_transcript("assistant", self.last_message)
        self.fire("Stop", last_assistant_message=self.last_message, stop_hook_active=False)


def read_line():
    """Read one line from the terminal; control chars except printable text are dropped."""
    buf = []
    while True:
        ch = sys.stdin.read(1)
        if ch == "":
            return None
        if ch in ("\r", "\n"):
            return "".join(buf)
        if ch == "\x03":
            return "\x03"
        if ch == "\x1b":
            buf.append("\x1b")
            continue
        buf.append(ch)


def load_steps(name):
    path = os.environ.get("CCDASH_FAKE_SCENARIO")
    if not path or not os.path.exists(path):
        return []
    data = json.load(open(path, encoding="utf-8"))
    if isinstance(data, list):
        return data
    return data.get(name, data.get("*", []))


def main():
    p = argparse.ArgumentParser(prog="claude")
    p.add_argument("prompt", nargs="?")
    p.add_argument("-n", "--name")
    p.add_argument("--settings")
    p.add_argument("--model")
    p.add_argument("--permission-mode")
    p.add_argument("--session-id")
    p.add_argument("-r", "--resume")
    p.add_argument("-c", "--continue", dest="cont", action="store_true")
    args, _unknown = p.parse_known_args()
    s = Session(args)
    s.fire("SessionStart", source=s.source, model=args.model or "fake-model", session_title=s.name)
    out(f"fake-claude {s.name} · session {s.session_id[:8]} · {s.source}")
    if args.prompt:
        lines = [x for x in args.prompt.splitlines() if x.strip()]
        out(f"> {lines[0][:100]}")
        if len(lines) > 1:
            out(f"  … {lines[-1][:100]}")
        s.turn(args.prompt, load_steps(s.name))
    ctrl_c = 0
    while True:
        try:
            line = read_line()
        except KeyboardInterrupt:  # a console in processed-input mode turns Ctrl+C into a signal
            line = "\x03"
        if line is None:
            s.fire("SessionEnd", reason="other")
            return 0
        if line == "\x03":
            ctrl_c += 1
            if ctrl_c >= 2:
                s.fire("SessionEnd", reason="other")
                return 0
            out("Press Ctrl-C again to exit")
            continue
        ctrl_c = 0
        text = line.replace("\x1b", "").strip()
        if not text:
            if "\x1b" in line:
                s.log_transcript("user", "[Request interrupted by user]")
            continue
        if text == "/exit":
            out("Bye!")
            s.fire("SessionEnd", reason="prompt_input_exit")
            return 0
        out(f"> {text}")
        s.turn(text)


if __name__ == "__main__":
    sys.exit(main())
