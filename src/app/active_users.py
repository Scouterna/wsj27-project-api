"""Track recently-authenticated users in memory and expose the count as a Prometheus gauge.

A user is "active" (logged in) if their token was validated within a sliding
window (``ACTIVE_USER_TIMEOUT_SECONDS``, default 10 minutes). We keep an in-memory
table of ``user_id -> last_seen`` timestamps, refreshed on every authentication,
and a single async timer sweeps out expired entries. The gauge always reflects
``len(_last_seen)``.

This is intentionally in-memory (per-pod, no persistence) — it's a real-time
operational metric, not an audit log.
"""

import asyncio
import logging
import time

from prometheus_client import Gauge

from .config import get_settings

logger = logging.getLogger(__name__)

# user_id -> wall-clock time of last authentication (time.time()).
_last_seen: dict[str, float] = {}
# Guards _last_seen and _sweep_task. Both auth dependencies are async and run on
# the event loop, so an asyncio lock is sufficient (no threading involved).
_lock = asyncio.Lock()
# The single pending sweep timer, or None when the table is empty.
_sweep_task: asyncio.Task | None = None

ACTIVE_USERS = Gauge(
    "wsj27_project_active_users",
    "Number of users seen within the active-user timeout window",
)


def _timeout() -> int:
    return get_settings().ACTIVE_USER_TIMEOUT_SECONDS


def _arm_sweep(delay: float) -> None:
    """Schedule the sweep to run in ``delay`` seconds. Must hold ``_lock``."""
    global _sweep_task
    _sweep_task = asyncio.create_task(_sweep_after(max(delay, 0)))


async def _sweep_after(delay: float) -> None:
    """Wait ``delay`` seconds, then remove expired entries and re-arm as needed."""
    global _sweep_task
    try:
        await asyncio.sleep(delay)
    except asyncio.CancelledError:
        return

    async with _lock:
        timeout = _timeout()
        now = time.time()
        expired = [uid for uid, seen in _last_seen.items() if seen + timeout <= now]
        for uid in expired:
            del _last_seen[uid]
        ACTIVE_USERS.set(len(_last_seen))

        if not _last_seen:
            # No running timer when the table is empty (per requirement).
            _sweep_task = None
            return

        oldest = min(_last_seen.values())
        _arm_sweep((oldest + timeout) - now)


async def track_user(user_id: str) -> None:
    """Record that ``user_id`` just authenticated; update the gauge and timer."""
    async with _lock:
        was_empty = not _last_seen
        _last_seen[user_id] = time.time()
        ACTIVE_USERS.set(len(_last_seen))
        # Only arm on the empty -> non-empty transition. If a timer is already
        # pending it re-arms itself on its next sweep; refreshing an existing
        # entry (even the oldest) at worst causes one harmless early no-op sweep.
        if was_empty:
            _arm_sweep(_timeout())


async def shutdown() -> None:
    """Cancel the pending sweep timer, e.g. on application shutdown."""
    global _sweep_task
    async with _lock:
        if _sweep_task is not None:
            _sweep_task.cancel()
            _sweep_task = None
