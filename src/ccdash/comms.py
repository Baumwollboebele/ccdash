"""Conversations for the Comms view: messages grouped into threads, each with a state.

Threading rule (v1): a new thread starts when a pair has been silent for 5 minutes,
or when a message names files and none of them overlap the thread's files (unless it
opens with an acknowledgement of the other side's last message: that is a reply).
ccdash's own notices are always their own thread. A broadcast starts a thread that
collects the recipients' replies to the broadcaster.

States (first match): delivered (your messages and ccdash notices), acked (every
recipient of a broadcast replied), agreed (the last message is an acknowledgement),
discussing (last message under 2 minutes ago), open (no reply yet; or quiet and unresolved).
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field

SILENCE = 300.0
DISCUSSING = 120.0
_FILE = re.compile(r"(?<![\w/])((?:[\w.-]+/)+[\w.*-]*|[\w-]+\.[A-Za-z][A-Za-z0-9]{0,5})(?![\w/])")
_ACK = re.compile(r"\b(agreed|ok|okay|ack|acknowledged|lgtm|sounds good|got it|noted|will do)\b", re.I)
_ACK_START = re.compile(r"\W*(agreed|ok|okay|ack|acknowledged|lgtm|sounds good|got it|noted|will do)\b", re.I)


def files_in(text: str) -> set[str]:
    out = set()
    for m in _FILE.finditer(text or ""):
        tok = m.group(1).strip(".,;:")
        if tok and not tok.startswith("http"):
            out.add(tok.lower())
    return out


def _overlap(a: set[str], b: set[str]) -> bool:
    if a & b:
        return True
    base = lambda s: {x.rsplit("/", 1)[-1] for x in s if x.rsplit("/", 1)[-1]}  # noqa: E731
    if base(a) & base(b):
        return True
    for x in a:
        for y in b:
            px, py = x.rstrip("*").rstrip("/"), y.rstrip("*").rstrip("/")
            if px and py and (px.startswith(py) or py.startswith(px)):
                return True
    return False


@dataclass
class Thread:
    kind: str  # pair | broadcast | you | ccdash
    pair: frozenset
    label_from: str
    label_to: str
    messages: list = field(default_factory=list)
    files: set = field(default_factory=set)
    recipients: list = field(default_factory=list)
    state: str = "open"

    @property
    def last_t(self) -> float:
        return self.messages[-1].t

    @property
    def first_t(self) -> float:
        return self.messages[0].t

    @property
    def pair_label(self) -> str:
        if self.kind == "pair":
            a, b = sorted(self.pair)
            return f"{a} ↔ {b}"
        return f"{self.label_from} → {self.label_to}"

    @property
    def topic(self) -> str:
        if self.files:
            return ", ".join(sorted(self.files)[:2])
        return self.messages[0].first_line[:40]


def _state(t: Thread, now: float) -> str:
    if t.kind in ("you", "ccdash"):
        return "delivered"
    if t.kind == "broadcast":
        repliers = {m.sender for m in t.messages[1:]}
        if t.recipients and set(t.recipients) <= repliers:
            return "acked"
    senders = {m.sender for m in t.messages}
    last = t.messages[-1]
    if len(senders) > 1 and _ACK.search(last.text or ""):
        return "agreed"
    if now - last.t < DISCUSSING:
        return "discussing"
    return "open"


def build_threads(messages, now: float | None = None) -> list[Thread]:
    now = now or time.time()
    threads: list[Thread] = []
    open_pair: dict[frozenset, Thread] = {}
    open_bcast: dict[str, Thread] = {}
    for m in sorted(messages, key=lambda x: x.t):
        mfiles = files_in(m.text)
        if m.kind == "ccdash":
            threads.append(Thread("ccdash", frozenset({"ccdash", m.to}), "ccdash", m.to, [m], mfiles))
            continue
        if m.kind == "you":
            pair = frozenset({"you", m.to})
            t = open_pair.get(pair)
            if t is None or m.t - t.last_t > SILENCE:
                t = Thread("you", pair, "you", m.to, [], set())
                threads.append(t)
                open_pair[pair] = t
            t.messages.append(m)
            t.files |= mfiles
            continue
        if m.to == "you":  # the lead's answer belongs to the conversation you started with it
            pair = frozenset({"you", m.sender})
            t = open_pair.get(pair)
            if t is None or m.t - t.last_t > SILENCE:
                t = Thread("you", pair, "you", m.sender, [], set())
                threads.append(t)
                open_pair[pair] = t
            t.messages.append(m)
            continue
        if m.to in ("all", "*"):
            t = Thread("broadcast", frozenset({m.sender, "all"}), m.sender, "all", [m], mfiles,
                       recipients=list(m.recipients))
            threads.append(t)
            open_bcast[m.sender] = t
            continue
        bc = open_bcast.get(m.to)
        if bc is not None and m.t - bc.last_t <= SILENCE and (not bc.recipients or m.sender in bc.recipients) \
                and m.sender not in {x.sender for x in bc.messages[1:]}:
            bc.messages.append(m)  # a recipient answering the broadcast
            continue
        pair = frozenset({m.sender, m.to})
        t = open_pair.get(pair)
        # "Ack. I only touch theme.ts" answers the thread, although it names another file
        reply = t is not None and t.messages[-1].sender != m.sender and bool(_ACK_START.match(m.text or ""))
        new = t is None or m.t - t.last_t > SILENCE or \
            (mfiles and t.files and not reply and not _overlap(mfiles, t.files))
        if new:
            t = Thread("pair", pair, m.sender, m.to, [], set())
            threads.append(t)
            open_pair[pair] = t
        t.messages.append(m)
        if not reply:
            t.files |= mfiles
    for t in threads:
        t.state = _state(t, now)
    threads.sort(key=lambda t: t.last_t, reverse=True)
    return threads
