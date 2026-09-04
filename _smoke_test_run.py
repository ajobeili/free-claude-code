from datetime import datetime, timezone
from tools.agent_orchestrator import get_event_context

cases = [
    ("25m AFTER NFP (the bug)", datetime(2026, 9, 4, 13, 55, tzinfo=timezone.utc)),
    ("5m BEFORE NFP",            datetime(2026, 9, 4, 13, 25, tzinfo=timezone.utc)),
    ("90m BEFORE NFP (caution)", datetime(2026, 9, 4, 12, 0,  tzinfo=timezone.utc)),
    ("CALM mid-month",           datetime(2026, 9, 10, 10, 0,  tzinfo=timezone.utc)),
    ("45m AFTER NFP (post-out)", datetime(2026, 9, 4, 14, 15, tzinfo=timezone.utc)),
    ("2h 5m AFTER NFP (caution tail)", datetime(2026, 9, 4, 15, 35, tzinfo=timezone.utc)),
]
for label, t in cases:
    ctx = get_event_context(t)
    print(f"--- {label} ---")
    for k in ("now_utc","next_event","minutes_until","active_event","minutes_to_active","in_pre_window","in_post_window","blackout_active","in_caution_window"):
        print(f"  {k}: {ctx.get(k)}")
    print()