"""Phase 0 spike: summarize the hooklog JSONL.   python events.py <events.jsonl> [--full LABEL]"""

import json
import sys

path = sys.argv[1]
full = sys.argv[3] if len(sys.argv) > 3 and sys.argv[2] == "--full" else None
sys.stdout.reconfigure(encoding="utf-8")
for line in open(path, encoding="utf-8"):
    r = json.loads(line)
    i = r["input"] if isinstance(r["input"], dict) else {}
    if full:
        if r["label"] == full or full == "*":
            print(json.dumps(r, indent=1, ensure_ascii=False))
        continue
    detail = i.get("source") or i.get("notification_type") or i.get("tool_name") or i.get("reason") or ""
    text = i.get("prompt") or i.get("last_assistant_message") or i.get("message") or ""
    agent = r["env"].get("CCDASH_AGENT", "-")
    print(
        f"{r['ts'][11:]} {agent:9} {r['label']:22} {detail:18} "
        f"sid={str(i.get('session_id'))[:8]} pm={i.get('permission_mode')} "
        f"{text[:50]!r} ms={r['ms_total']} chain={r['chain'][1:3]}"
    )
