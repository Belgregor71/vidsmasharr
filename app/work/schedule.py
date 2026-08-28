"""When may we work, and how hard?

Two independent questions, deliberately kept apart:

- **The window.** Overnight we get the box to ourselves and run at full width.
  During the day we keep going but stay out of the way, on one thread and
  niced. Pure clock arithmetic, so it is trivially testable.
- **Is anyone actually watching?** Nothing else matters as much. The J3455 has
  one video engine; an encode running while Plex transcodes a stream makes the
  stream stutter, and a stuttering stream is the fastest way for this project
  to be uninstalled. So: check first, and treat "cannot tell" as "yes, someone
  is watching".

Tautulli is asked before Plex. It is already running on :8181, its activity
endpoint is one small JSON call, and it does not need the Plex token.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time as clock_time

import httpx


@dataclass
class WorkWindow:
    working: bool
    threads: int
    nice: int
    reason: str


def _parse(value: str) -> clock_time:
    hour, _, minute = value.partition(":")
    return clock_time(int(hour), int(minute or 0))


def is_night(now: datetime, schedule) -> bool:
    """Is `now` inside the full-speed window? Handles wrapping past midnight."""
    start = _parse(schedule.night_start)
    end = _parse(schedule.night_end)
    current = now.time()

    if start <= end:
        return start <= current < end
    # 23:00 -> 07:00 wraps: night is "after start OR before end".
    return current >= start or current < end


def window_for(now: datetime, schedule) -> WorkWindow:
    if is_night(now, schedule):
        return WorkWindow(
            working=True, threads=schedule.night_threads, nice=0,
            reason=f"night window ({schedule.night_start}-{schedule.night_end})",
        )
    if not schedule.day_enabled:
        return WorkWindow(
            working=False, threads=0, nice=0,
            reason=f"outside the night window and daytime work is off",
        )
    return WorkWindow(
        working=True, threads=schedule.day_threads, nice=schedule.day_nice,
        reason="daytime, throttled",
    )


# ------------------------------------------------------------------ streaming


def tautulli_streams(config) -> int | None:
    """Active stream count from Tautulli, or None if it could not be asked."""
    tautulli = getattr(config, "tautulli", None)
    if not tautulli or not tautulli.enabled or not tautulli.url:
        return None
    try:
        response = httpx.get(
            f"{tautulli.url.rstrip('/')}/api/v2",
            params={"apikey": tautulli.api_key, "cmd": "get_activity"},
            timeout=tautulli.timeout,
        )
        response.raise_for_status()
        data = (response.json().get("response") or {}).get("data") or {}
        return int(data.get("stream_count") or 0)
    except (httpx.HTTPError, ValueError, TypeError, KeyError):
        return None


def plex_streams(config) -> int | None:
    """Active stream count straight from Plex, as a fallback."""
    plex = config.plex
    if not plex.enabled or not plex.token:
        return None
    try:
        response = httpx.get(
            f"{plex.url.rstrip('/')}/status/sessions",
            headers={"X-Plex-Token": plex.token, "Accept": "application/json"},
            timeout=15.0,
        )
        response.raise_for_status()
        container = (response.json() or {}).get("MediaContainer") or {}
        return int(container.get("size") or 0)
    except (httpx.HTTPError, ValueError, TypeError, KeyError):
        return None


def someone_is_watching(config) -> tuple[bool, str]:
    """(should we pause, why).

    "Cannot tell" pauses. If both Tautulli and Plex are unreachable something
    is wrong with the stack, and grinding the GPU through whatever that is has
    no upside -- the queue is months long either way, so an hour of caution
    costs nothing.
    """
    if not config.schedule.pause_when_streaming:
        return False, "stream pausing is disabled"

    count = tautulli_streams(config)
    source = "Tautulli"
    if count is None:
        count = plex_streams(config)
        source = "Plex"

    if count is None:
        configured = (
            getattr(getattr(config, "tautulli", None), "enabled", False)
            or config.plex.enabled
        )
        if not configured:
            return False, "no Tautulli or Plex configured to ask"
        return True, "could not reach Tautulli or Plex to ask; pausing to be safe"

    if count > 0:
        return True, f"{source} reports {count} active stream(s)"
    return False, f"{source} reports nobody watching"


def may_work_now(config, now: datetime | None = None) -> WorkWindow:
    """The full answer: window, then streaming."""
    window = window_for(now or datetime.now(), config.schedule)
    if not window.working:
        return window

    paused, reason = someone_is_watching(config)
    if paused:
        return WorkWindow(working=False, threads=0, nice=0, reason=reason)
    return window
