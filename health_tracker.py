"""
In-memory health/availability tracker for proxy requests.
Tracks success/failure per 10-minute slot over a 24-hour window.
"""

import time
import threading

SLOT_MINUTES = 10
SLOT_SECONDS = SLOT_MINUTES * 60
NUM_SLOTS = 24 * 60 // SLOT_MINUTES  # 144

_lock = threading.Lock()
_slots = {}


def _slot_key(t=None):
    if t is None:
        t = int(time.time())
    return t // SLOT_SECONDS


def record_request(success=True):
    with _lock:
        key = _slot_key()
        if key not in _slots:
            _slots[key] = {"total": 0, "success": 0}
        _slots[key]["total"] += 1
        if success:
            _slots[key]["success"] += 1
        cutoff = _slot_key() - NUM_SLOTS
        for k in list(_slots):
            if k < cutoff:
                del _slots[k]


def get_availability():
    """Return list of 144 slots for the last 24h.

    Each slot::
        slot_start_ts   int     unix timestamp of slot beginning
        slot_end_ts     int     unix timestamp of slot end
        total           int     total requests in this slot
        success         int     successful requests
        availability    float   None if no data, else 0-100
    """
    now_key = _slot_key()
    result = []
    with _lock:
        for i in range(NUM_SLOTS):
            key = now_key - (NUM_SLOTS - 1 - i)
            slot_start = key * SLOT_SECONDS
            slot_end = slot_start + SLOT_SECONDS
            data = _slots.get(key, {"total": 0, "success": 0})
            avail = (data["success"] / data["total"] * 100) if data["total"] > 0 else None
            result.append({
                "slot_start_ts": slot_start,
                "slot_end_ts": slot_end,
                "total": data["total"],
                "success": data["success"],
                "availability": avail,
            })
    return result


def get_summary():
    """Return aggregated availability for the last 1h / 6h / 24h."""
    now_key = _slot_key()
    ranges = {
        "1h": NUM_SLOTS // 24,
        "6h": NUM_SLOTS // 4,
        "24h": NUM_SLOTS,
    }
    summary = {}
    for label, count in ranges.items():
        total = success = 0
        with _lock:
            for i in range(count):
                key = now_key - (count - 1 - i)
                data = _slots.get(key, {"total": 0, "success": 0})
                total += data["total"]
                success += data["success"]
        summary[label] = {
            "total": total,
            "success": success,
            "availability": (success / total * 100) if total > 0 else None,
        }
    return summary
