"""
Persistent local token usage storage.
Records per-request token usage with timestamps.
Supports aggregation by hour / day / week.
"""

import json
import time
from pathlib import Path

BASE_DIR = Path(__file__).parent
STORE_PATH = BASE_DIR / "data" / "usage_history.json"


def _load():
    try:
        return json.loads(STORE_PATH.read_text("utf-8"))
    except Exception:
        return {"entries": []}


def _save(data):
    STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STORE_PATH.write_text(json.dumps(data, ensure_ascii=False))


def record_usage(tokens, requests=1):
    """Record a single request's token usage."""
    data = _load()
    data["entries"].append({
        "ts": int(time.time()),
        "tokens": tokens,
        "requests": requests,
    })
    cutoff = int(time.time()) - 90 * 86400
    data["entries"] = [e for e in data["entries"] if e["ts"] >= cutoff]
    _save(data)


def get_hourly(hours=24):
    """Aggregate usage by hour for the last N hours."""
    data = _load()
    now = int(time.time())
    cutoff = now - hours * 3600
    buckets = {}
    for e in data["entries"]:
        if e["ts"] < cutoff:
            continue
        hour_key = (e["ts"] // 3600) * 3600
        b = buckets.setdefault(hour_key, {"tokens": 0, "requests": 0})
        b["tokens"] += e["tokens"]
        b["requests"] += e["requests"]
    result = []
    for hour_ts in sorted(buckets.keys()):
        result.append({
            "ts": hour_ts,
            "label": time.strftime("%m/%d %H:00", time.localtime(hour_ts)),
            "tokens": buckets[hour_ts]["tokens"],
            "requests": buckets[hour_ts]["requests"],
        })
    return result


def get_daily(days=30):
    """Aggregate usage by day for the last N days."""
    data = _load()
    now = int(time.time())
    cutoff = now - days * 86400
    buckets = {}
    for e in data["entries"]:
        if e["ts"] < cutoff:
            continue
        day_key = (e["ts"] // 86400) * 86400
        b = buckets.setdefault(day_key, {"tokens": 0, "requests": 0})
        b["tokens"] += e["tokens"]
        b["requests"] += e["requests"]
    result = []
    for day_ts in sorted(buckets.keys()):
        result.append({
            "ts": day_ts,
            "label": time.strftime("%m/%d", time.localtime(day_ts)),
            "tokens": buckets[day_ts]["tokens"],
            "requests": buckets[day_ts]["requests"],
        })
    return result


def get_weekly(weeks=12):
    """Aggregate usage by ISO week for the last N weeks."""
    data = _load()
    now = int(time.time())
    cutoff = now - weeks * 7 * 86400
    buckets = {}
    for e in data["entries"]:
        if e["ts"] < cutoff:
            continue
        t = time.localtime(e["ts"])
        # Monday midnight of the week
        week_start = e["ts"] - (t.tm_wday * 86400) - (
            t.tm_hour * 3600 + t.tm_min * 60 + t.tm_sec
        )
        b = buckets.setdefault(week_start, {"tokens": 0, "requests": 0})
        b["tokens"] += e["tokens"]
        b["requests"] += e["requests"]
    result = []
    for ws in sorted(buckets.keys()):
        result.append({
            "ts": ws,
            "label": time.strftime("%m/%d", time.localtime(ws)),
            "tokens": buckets[ws]["tokens"],
            "requests": buckets[ws]["requests"],
        })
    return result
