"""Where agents collide (docs/PLAN.md, "Coordination and conflict detection").

One ConflictTracker per Dashboard. Its rows, most severe first:

  merge    red     `git merge-tree` predicts a conflict with the default branch or between
                   two agent branches; against the default branch it also sets STATE = conflict
  overlap  yellow  the same path is touched or claimed by two agents
  shared   yellow  a branch changes a configured shared interface; blue once the agent
                   announced it or ccdash told the others
  stale    grey    the branch is far behind the default branch

`update(dashboard, git=False)` runs after every runtime refresh and never calls git: it
recomputes overlaps from hook events and reuses the last predictions, so an overlap shows
within a refresh of the edit. `update(dashboard, git=True)` runs after a git refresh and
recomputes the predictions (cached by SHA pair) and pull request states.

Only an active tracker (the dashboard's) sends notices and asks gh; `cc ls` only looks.
Claims by the user, and what was already announced, live in <state>/ownership.json.
"""

from __future__ import annotations

import json
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from functools import lru_cache
from itertools import combinations

from . import comms, gitstate, gitutil, hook
from .gitignore import PatternSet
from .state import FileLock, LockTimeout, atomic_write_json

GLYPH = {"merge": "⚠", "overlap": "⚠", "shared": "●", "stale": "·"}
ANNOUNCE_WINDOW = 900.0  # a message up to 15 min before the change counts as its announcement


def _clock(t: float) -> str:
    theme = sys.modules.get("ccdash.tui.theme")  # the dashboard's clock when it is loaded; never imports Textual
    return theme.fmt_clock(t) if theme is not None else time.strftime("%H:%M", time.localtime(t))


def file_list(files, limit: int = 3) -> str:
    files = list(files)
    more = len(files) - limit
    return ", ".join(files[:limit]) + (f" (+{more} more)" if more > 0 else "")


@lru_cache(maxsize=1024)
def _pattern(glob: str) -> PatternSet:
    return PatternSet([glob])


def matches(glob: str, path: str) -> bool:
    """Does a claim or shared-interface glob (.gitignore syntax) cover `path`?"""
    return bool(glob) and bool(path) and _pattern(glob).matches(path)


def _stem(glob: str) -> str:
    """The literal start of a glob: `src/ui/**` -> `src/ui`."""
    for i, ch in enumerate(glob):
        if ch in "*?[":
            glob = glob[:i]
            break
    return glob.strip("/")


def clean_claims(paths) -> list[str]:
    out = []
    for p in paths or []:
        p = str(p).strip().strip(",").replace("\\", "/")
        if p.startswith("./"):
            p = p[2:]
        if p and p not in out:
            out.append(p)
    return out


@dataclass
class Row:
    kind: str  # merge | overlap | shared | stale
    who: tuple[str, ...]  # the agents; for a merge row against the default branch, the agent and that branch
    detail: str  # may contain {when}
    color: str = "yellow"  # red | yellow | blue | grey
    ident: str = ""  # what tells two rows of the same agents apart (the shared glob)
    files: tuple[str, ...] = ()
    when: float | None = None

    @property
    def key(self) -> tuple:
        return (self.kind, *self.who, self.ident)

    @property
    def label(self) -> str:
        return f"{self.who[0]} changed" if self.kind == "shared" else " × ".join(self.who)

    @property
    def text(self) -> str:
        return self.detail.replace("{when}", _clock(self.when)) if self.when else self.detail

    @property
    def plain(self) -> str:
        return f"{GLYPH[self.kind]} {self.kind:<8}{self.label:<32} {self.text}"

    def render(self, flash: bool = False, slide: int = 0):
        from rich.text import Text

        from .tui.theme import DIM

        t = Text(" " * slide, no_wrap=True, overflow="ellipsis")
        if flash:
            t.append(self.plain, style="bold white on red")
            return t
        color = {"red": "red", "yellow": "yellow", "blue": "blue", "grey": DIM}[self.color]
        t.append(f"{GLYPH[self.kind]} {self.kind:<8}", style=f"bold {color}" if self.color == "red" else color)
        t.append(f"{self.label:<32} ", style=DIM if self.kind == "stale" else "bold")
        t.append(self.text, style=DIM if self.color in ("grey", "blue") else "default")
        return t


def _merge_note(ours: dict | None, theirs: dict | None) -> dict:
    ours, theirs = dict(ours or {}), dict(theirs or {})
    out = {"first": min(float(x["first"]) for x in (ours, theirs) if x.get("first"))} \
        if (ours.get("first") or theirs.get("first")) else {}
    for key in ("announced", "notified"):
        vals = [float(x[key]) for x in (ours, theirs) if x.get(key)]
        if vals:
            out[key] = max(vals)
    n = ours.get("recipients") or theirs.get("recipients")
    if n and out.get("notified"):
        out["recipients"] = int(n)
    return out


class ConflictTracker:
    def __init__(self, ws, active: bool = False, notify=None, clock=time.time):
        self.ws = ws
        self.active = active
        self.clock = clock
        self.rows: list[Row] = []
        self.cache = gitstate.MergeTreeCache(ws.repo)
        self.prs = None  # github.PRCache, made on first use
        self.landings: dict[str, list[str]] = {}  # agent -> who was told that it landed (this session)
        self._notify_fn = notify  # (to, text, exclude) -> None; tests record instead of typing into agents
        self._lock = threading.RLock()
        self._known: set[str] | None = None  # agents seen in the last update
        self._merged: set[str] = set()
        self._paths: dict[str, set[str]] = {}  # agent -> files its branch or worktree changes
        self._announced: dict[str, list[str]] = {}  # agent -> claims from its `Files:` line
        self._first_edit: dict[tuple[str, str], float] = {}
        self._vs_default: dict[str, tuple[str, tuple[str, ...]]] = {}  # agent -> (head sha, conflicting files)
        self._pairs: dict[frozenset, tuple[tuple[str, str], tuple[str, ...]]] = {}
        self._pr: dict[str, object] = {}
        self._unmerged: dict[str, float] = {}  # agent -> when it was last seen with unmerged commits
        self._changed: dict[str, list[str]] = {}  # agent -> what its branch changed, kept past the merge
        self._user: dict[str, list[str]] = {}  # persisted: paths the user claimed for an agent
        self._notes: dict[str, dict] = {}  # persisted: "agent\tglob" -> first / announced / notified
        self._landed: dict[str, float] = {}  # persisted: agent -> when its landing was announced
        self._saved: str | None = None
        self._mtime = -1
        self._load(self._read())

    # -- queries --------------------------------------------------------------------
    @property
    def count(self) -> int:
        """Predicted merge conflicts: the number in the header."""
        return sum(r.color == "red" for r in self.rows)

    def red_pairs(self) -> set[frozenset]:
        return {frozenset(r.who) for r in self.rows if r.kind in ("merge", "overlap") and len(set(r.who)) == 2}

    def claims(self, name: str) -> list[str]:
        return clean_claims([*self._announced.get(name, []), *self._user.get(name, [])])

    def user_claims(self, name: str) -> list[str]:
        return list(self._user.get(name, []))

    def owned(self, name: str) -> list[str]:
        """What an agent owns: its claims, then the files it touched outside them."""
        claims = self.claims(name)
        rest = sorted(p for p in self._paths.get(name, ()) if not any(matches(c, p) for c in claims))
        return claims + rest

    def touch_map(self) -> dict[str, list[str]]:
        """path -> the agents whose branch or worktree changes it (for the file map)."""
        out: dict[str, list[str]] = {}
        for name, paths in self._paths.items():
            for p in paths:
                out.setdefault(p, []).append(name)
        return {p: sorted(names) for p, names in out.items()}

    def conflict_files(self, name: str) -> tuple[str, ...]:
        hit = self._vs_default.get(name)
        return hit[1] if hit else ()

    def pull_request(self, name: str):
        return self._pr.get(name)

    # -- input ----------------------------------------------------------------------
    def on_event(self, line: dict) -> None:
        """Remember when an agent first edited each path (for the shared-interface timer)."""
        if line.get("event") == "PostToolUse" and line.get("tool") in hook.EDIT_TOOLS and line.get("path"):
            key = (line.get("agent"), line["path"])
            t = float(line.get("t") or self.clock())
            if t < self._first_edit.get(key, float("inf")):
                self._first_edit[key] = t

    def claim(self, name: str, paths) -> list[str]:
        """Set the paths the user claims for an agent (this replaces the earlier ones)."""
        paths = clean_claims(paths)

        def mutate(data: dict) -> None:
            agents = data.setdefault("agents", {})
            entry = agents.setdefault(name, {})
            if paths:
                entry["user"] = paths
            else:
                entry.pop("user", None)

        with self._lock:
            if paths:
                self._user[name] = paths
            else:
                self._user.pop(name, None)
            self._sync(mutate)
        return paths

    def update(self, dashboard, git: bool = False) -> None:
        with self._lock:
            with dashboard._lock:
                views = [v for v in dashboard.views.values() if v.status not in ("untracked", "deleted")]
            self._collect(dashboard, views)
            if git:
                self._predict(views, getattr(dashboard, "default_ref", "") or self.ws.compare_ref())
                self._pull_requests(views)
                self._landings(dashboard, views)
            shared = self._shared(dashboard, views)
            self._apply_states(views)
            self.rows = self._build(views, shared)
            self._sync()

    # -- what each agent touches and claims ----------------------------------------------
    def _collect(self, dashboard, views) -> None:
        self._known = {v.name for v in views}
        self._merged = {v.name for v in views if v.merge_state == "merged"}
        paths, announced = {}, {}
        for v in views:
            merged = v.name in self._merged
            ps = set(v.dirty_paths)
            if not merged:
                ps |= set(v.changed_paths)
            a = dashboard.activity.get(v.name)
            if a is not None and v.alive and not merged:  # what the hooks saw counts while the agent runs
                ps |= set(a.touched)
                if a.claimed:
                    announced[v.name] = clean_claims(a.claimed)
            if ps:
                paths[v.name] = ps
        self._paths, self._announced = paths, announced

    def _overlap(self, a: str, b: str) -> list[str]:
        pa, pb = self._paths.get(a, set()), self._paths.get(b, set())
        ca, cb = self.claims(a), self.claims(b)
        files = set(pa & pb)
        files |= {p for p in pb if any(matches(c, p) for c in ca)}
        files |= {p for p in pa if any(matches(c, p) for c in cb)}
        for x in ca:
            for y in cb:
                if x == y or matches(x, _stem(y)):
                    files.add(y)
                elif matches(y, _stem(x)):
                    files.add(x)
        return sorted(files)

    # -- predicted merge conflicts ---------------------------------------------------------
    def _predict(self, views, ref: str) -> None:
        root = self.ws.repo.main_root
        default_sha = gitutil.out(["rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"], root, check=False)
        cands = sorted((v for v in views if v.branch and v.head_sha and v.ahead > 0 and v.name not in self._merged),
                       key=lambda v: v.name)
        jobs: dict[tuple, tuple[str, str]] = {}
        if default_sha:
            for v in cands:
                jobs[(v.name,)] = (default_sha, v.head_sha)
        for a, b in combinations(cands, 2):  # pairwise only where two branches changed the same file
            if set(a.changed_paths) & set(b.changed_paths):
                jobs[(a.name, b.name)] = (a.head_sha, b.head_sha)
        if len(jobs) > 1:
            with ThreadPoolExecutor(max_workers=min(8, len(jobs))) as pool:
                results = dict(zip(jobs, pool.map(lambda shas: self.cache.predict(*shas), jobs.values())))
        else:
            results = {k: self.cache.predict(*shas) for k, shas in jobs.items()}
        vs_default, pairs = {}, {}
        for key, pred in results.items():
            if not pred.conflict:
                continue
            if len(key) == 1:
                vs_default[key[0]] = (jobs[key][1], pred.files)
            else:
                pairs[frozenset(key)] = (jobs[key], pred.files)
        self._vs_default, self._pairs = vs_default, pairs

    def _pull_requests(self, views) -> None:
        if not self.active:
            return
        from . import github

        root = self.ws.repo.main_root
        if not github.available(root):
            self._pr = {}
            return
        if self.prs is None:
            self.prs = github.PRCache(root)
        pushed = set(gitutil.out(["for-each-ref", "--format=%(refname:lstrip=3)",
                                  f"refs/remotes/origin/{self.ws.cfg.branch_prefix}"], root, check=False).splitlines())
        found = {}
        for v in views:
            if v.branch in pushed:
                pr = self.prs.lookup(v.branch)
                if pr is not None:
                    found[v.name] = pr
        self._pr = found

    def _apply_states(self, views) -> None:
        """Layer conflict and pull request states over the git-only STATE (PLAN: first match wins)."""
        for v in views:
            if not v.branch or v.name in self._merged:
                continue
            pr = self._pr.get(v.name)
            hit = self._vs_default.get(v.name)
            if pr is not None and pr.merged:
                v.merge_state = "merged"
            elif hit is not None and hit[0] == v.head_sha:
                v.merge_state = "conflict"
            elif pr is not None and pr.open:
                v.merge_state = pr.label

    # -- a branch landed on the default branch ----------------------------------------------
    def _landings(self, dashboard, views) -> None:
        now = self.clock()
        for v in views:
            if v.branch and v.ahead > 0 and v.name not in self._merged:
                self._unmerged[v.name] = now
                self._changed[v.name] = list(v.changed_paths)
        if not self.active:
            return
        landed = [v for v in views if v.name in self._merged and v.name in self._unmerged]
        if not landed:
            return
        self._load(self._read())  # another dashboard may have announced it already
        for v in landed:
            if self._landed.get(v.name, 0.0) < self._unmerged[v.name]:
                self._announce_landing(dashboard, v, views, now)

    def _announce_landing(self, dashboard, v, views, now: float) -> None:
        default = self.ws.default_branch
        task = (v.task.splitlines() or [""])[0][:60]
        sha = getattr(dashboard, "default_sha", "")
        head = f"{v.name}" + (f" ({task})" if task else "") + f" landed on {default}" + (f" @ {sha}" if sha else "")
        files = self._changed.get(v.name) or []
        told = []
        for o in views:
            if o.name == v.name or not o.alive:
                continue
            hit = self._vs_default.get(o.name)
            if hit is not None and hit[0] == o.head_sha:
                tail = (f"Your branch {o.branch} now conflicts with {default} in {file_list(hit[1])}: rebase on "
                        f"{default} and resolve it.")
            else:
                tail = f"Your branch still merges cleanly; rebase on {default} when it suits you."
            try:
                self._notify(dashboard, o.name, f"{head}. Files changed: {file_list(files, 6) or 'none'}. {tail}")
                told.append(o.name)
            except Exception:  # noqa: BLE001  (an agent that just exited must not stop the others' notices)
                pass
        self._landed[v.name] = now
        self._unmerged.pop(v.name, None)
        self.landings[v.name] = told

    # -- shared interfaces ----------------------------------------------------------------------
    def _shared(self, dashboard, views) -> list[Row]:
        globs = self.ws.cfg.shared
        if not globs:
            return []
        now = self.clock()
        wait = self.ws.cfg.shared_notice_after
        alive = [v.name for v in views if v.alive]
        rows = []
        for v in sorted(views, key=lambda v: v.name):
            if v.name in self._merged:
                continue
            for glob in globs:
                hit = sorted(p for p in self._paths.get(v.name, ()) if matches(glob, p))
                if not hit:
                    continue
                key = f"{v.name}\t{glob}"
                first = min([self._first_edit[(v.name, p)] for p in hit if (v.name, p) in self._first_edit] or [now])
                note = self._notes.setdefault(key, {"first": first})
                note["first"] = min(float(note.get("first") or first), first)
                others = [n for n in alive if n != v.name]
                if not note.get("announced") and not note.get("notified"):
                    said = self._announcement(dashboard, v.name, glob, hit, note["first"])
                    if said:
                        note["announced"] = said
                    elif self.active and others and now >= note["first"] + wait:
                        text = (f"{v.name} changed the shared interface {file_list(hit)} ({glob}) on {v.branch} without "
                                f"announcing it. If your work depends on it, look at the change and message {v.name}.")
                        try:
                            self._notify(dashboard, "all", text, exclude={v.name})
                            note["notified"], note["recipients"] = now, len(others)
                        except Exception:  # noqa: BLE001  (try again on the next refresh)
                            pass
                if note.get("announced"):
                    detail, color, when = f"{glob}  (announced {{when}})", "blue", note["announced"]
                elif note.get("notified"):
                    n = int(note.get("recipients") or 0)
                    detail = f"{glob}  ({n} agent{'s' if n != 1 else ''} notified {{when}})"
                    color, when = "blue", note["notified"]
                else:
                    left = int(note["first"] + wait - now)
                    soon = f"; ccdash tells the others in {max(0, left)}s" if (self.active and others) else ""
                    detail, color, when = f"{glob}  (not announced{soon})", "yellow", None
                rows.append(Row("shared", (v.name,), detail, color, ident=glob, files=tuple(hit), when=when))
        return rows

    def _announcement(self, dashboard, name: str, glob: str, hit: list[str], first: float) -> float | None:
        """When did the agent tell someone about this change? (a message of its own naming the path or glob)"""
        targets = {glob.lower(), *(p.lower() for p in hit)}
        for m in dashboard.messages.items:
            if m.kind == "agent" and m.sender == name and m.t >= first - ANNOUNCE_WINDOW and \
                    comms._overlap(comms.files_in(m.text), targets):
                return m.t
        return None

    def _notify(self, dashboard, to: str, text: str, exclude=()) -> None:
        if self._notify_fn is not None:
            self._notify_fn(to, text, set(exclude))
            return
        from . import messaging

        if to == "all":
            messaging.broadcast_from_user(self.ws, text, dashboard=dashboard, sender="ccdash", exclude=set(exclude))
        else:
            messaging.notice(self.ws, to, text, dashboard=dashboard)

    # -- rows -------------------------------------------------------------------------------------
    def _build(self, views, shared: list[Row]) -> list[Row]:
        default = self.ws.default_branch
        by = {v.name: v for v in views}
        rows = []
        for name, (head, files) in sorted(self._vs_default.items()):
            v = by.get(name)
            if v is not None and v.head_sha == head and name not in self._merged:
                rows.append(Row("merge", (name, default), f"{file_list(files)} (both changed)", "red", files=files))
        for pair, (shas, files) in sorted(self._pairs.items(), key=lambda kv: sorted(kv[0])):
            a, b = sorted(pair)
            if a in by and b in by and (by[a].head_sha, by[b].head_sha) == shas and not {a, b} & self._merged:
                rows.append(Row("merge", (a, b), f"{file_list(files)} (both changed)", "red", files=files))
        names = sorted(n for n in by if n in self._paths or self.claims(n))
        for a, b in combinations(names, 2):
            files = self._overlap(a, b)
            if files:
                rows.append(Row("overlap", (a, b), file_list(files), "yellow", files=tuple(files)))
        rows += shared
        limit = self.ws.cfg.stale_base_commits
        for v in sorted(views, key=lambda v: v.name):
            if v.branch and v.behind > limit and v.name not in self._merged:
                rows.append(Row("stale", (v.name,), f"{v.behind} commits behind {default}: ask it to rebase (m)", "grey"))
        return rows

    # -- ownership.json -------------------------------------------------------------------------------
    def _read(self) -> dict:
        try:
            data = json.loads(self.ws.state.ownership_path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _load(self, data: dict) -> None:
        """Take what other dashboards wrote: the user's claims (the file wins), notes and landings (merged)."""
        agents = data.get("agents") if isinstance(data.get("agents"), dict) else {}
        self._user = {n: clean_claims(a.get("user")) for n, a in agents.items()
                      if isinstance(a, dict) and a.get("user")}
        for key, theirs in (data.get("shared") or {}).items():
            if isinstance(theirs, dict):
                self._notes[key] = _merge_note(self._notes.get(key), theirs)
        for name, t in (data.get("landed") or {}).items():
            try:
                self._landed[name] = max(float(t), self._landed.get(name, 0.0))
            except (TypeError, ValueError):
                pass

    def _document(self) -> dict:
        known = self._known
        if known is not None:  # forget agents that are gone
            self._user = {n: c for n, c in self._user.items() if n in known}
            self._notes = {k: n for k, n in self._notes.items() if k.split("\t")[0] in known}
            self._landed = {n: t for n, t in self._landed.items() if n in known}
        agents = {}
        for name in sorted(set(self._paths) | set(self._announced) | set(self._user)):
            entry = {"claimed": self.claims(name), "touched": sorted(self._paths.get(name, ()))}
            if self._user.get(name):
                entry["user"] = self._user[name]
            agents[name] = entry
        return {"version": 1, "agents": agents, "shared": self._notes, "landed": self._landed}

    def _sync(self, mutate=None) -> None:
        """Write our picture to ownership.json, merged with what is there, when either changed."""
        state = self.ws.state
        path = state.ownership_path
        try:
            mtime = path.stat().st_mtime_ns
        except OSError:
            mtime = 0
        if mutate is None and mtime == self._mtime and json.dumps(self._document(), sort_keys=True) == self._saved:
            return
        try:
            with FileLock(state.root / "ownership.lock", timeout=2):
                data = self._read()
                if mutate is not None:
                    mutate(data)
                self._load(data)
                doc = self._document()
                if doc != data:
                    atomic_write_json(path, doc)
                self._saved = json.dumps(doc, sort_keys=True)
                self._mtime = path.stat().st_mtime_ns if path.exists() else 0
        except (LockTimeout, OSError):
            pass  # next refresh
