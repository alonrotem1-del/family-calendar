"""Fetching iCal feeds and turning them into a flat list of events."""

import asyncio
import time
from dataclasses import dataclass
from datetime import date, datetime, time as dtime, timedelta
from typing import Awaitable, Callable
from zoneinfo import ZoneInfo

import httpx
import icalendar
import recurring_ical_events
import x_wr_timezone

from .config import CalendarSource

Fetcher = Callable[[str], Awaitable[bytes]]


class FeedError(Exception):
    """A feed could not be loaded; the message is shown to the family as is."""


async def http_fetch(url: str) -> bytes:
    try:
        async with httpx.AsyncClient(
            timeout=20,
            follow_redirects=True,
            headers={"User-Agent": "family-calendar/1.0"},
        ) as client:
            response = await client.get(url)
    except httpx.HTTPError as exc:
        raise FeedError(f"אין חיבור לשרת היומן ({type(exc).__name__})") from exc
    if response.status_code != 200:
        raise FeedError(f"שרת היומן החזיר שגיאה {response.status_code}")
    return response.content


def parse_feed(data: bytes) -> icalendar.Calendar:
    # An unpublished Outlook calendar often answers with an HTML login page.
    if b"BEGIN:VCALENDAR" not in data[:2048]:
        raise FeedError("הקישור לא מחזיר יומן (אולי היומן לא פורסם או שהקישור שגוי)")
    try:
        calendar = icalendar.Calendar.from_ical(data)
    except ValueError as exc:
        raise FeedError("קובץ היומן פגום") from exc
    # Google feeds state their zone in X-WR-TIMEZONE instead of on each event.
    return x_wr_timezone.to_standard(calendar)


@dataclass
class _Entry:
    calendar: icalendar.Calendar | None = None
    fetched_at: float | None = None
    error: str | None = None


@dataclass
class FeedResult:
    calendar: icalendar.Calendar | None
    error: str | None
    # True when the error is covered by an older copy that is still shown.
    stale: bool


class FeedCache:
    """Keeps each feed for `ttl` seconds, and the last good copy after that
    if a refresh fails, so a flaky server leaves old events on screen instead
    of an empty week."""

    def __init__(self, fetch: Fetcher = http_fetch, ttl: float = 600, clock=time.monotonic):
        self._fetch = fetch
        self._ttl = ttl
        self._clock = clock
        self._entries: dict[str, _Entry] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    async def get(self, url: str) -> FeedResult:
        entry = self._entries.setdefault(url, _Entry())
        lock = self._locks.setdefault(url, asyncio.Lock())
        async with lock:
            now = self._clock()
            fresh = entry.fetched_at is not None and now - entry.fetched_at < self._ttl
            if not fresh:
                try:
                    entry.calendar = parse_feed(await self._fetch(url))
                    entry.error = None
                except FeedError as exc:
                    entry.error = str(exc)
                # A failure also waits out the TTL, so a broken feed is not
                # hammered on every page load.
                entry.fetched_at = now
        return FeedResult(
            calendar=entry.calendar,
            error=entry.error,
            stale=entry.error is not None and entry.calendar is not None,
        )


def _as_local(value: date | datetime, tz: ZoneInfo) -> datetime:
    if not isinstance(value, datetime):
        return datetime.combine(value, dtime.min, tzinfo=tz)
    if value.tzinfo is None:  # "floating" time: means local time
        return value.replace(tzinfo=tz)
    return value.astimezone(tz)


def _end_of(component, start: date | datetime) -> date | datetime:
    if component.get("DTEND") is not None:
        return component["DTEND"].dt
    if component.get("DURATION") is not None:
        return start + component["DURATION"].dt
    # RFC 5545: no end means one day for an all-day event, an instant otherwise.
    return start + timedelta(days=1) if not isinstance(start, datetime) else start


def events_between(
    calendar: icalendar.Calendar,
    source: CalendarSource,
    start: date,
    end: date,
    tz: ZoneInfo,
) -> list[dict]:
    """Every occurrence overlapping [start, end), recurring ones expanded."""
    window_start = datetime.combine(start, dtime.min, tzinfo=tz)
    window_end = datetime.combine(end, dtime.min, tzinfo=tz)
    events = []
    for component in recurring_ical_events.of(calendar).between(window_start, window_end):
        if str(component.get("STATUS", "")).upper() == "CANCELLED":
            continue
        dtstart = component["DTSTART"].dt
        dtend = _end_of(component, dtstart)
        all_day = not isinstance(dtstart, datetime)

        hidden = source.private or str(component.get("CLASS", "")).upper() in {
            "PRIVATE",
            "CONFIDENTIAL",
        }
        title = str(component.get("SUMMARY", "")).strip()
        if hidden:
            title = "תפוס"
        location = "" if hidden else str(component.get("LOCATION", "")).strip()

        if all_day:
            start_value = dtstart.isoformat()
            end_value = (dtend if not isinstance(dtend, datetime) else dtend.date()).isoformat()
        else:
            start_value = _as_local(dtstart, tz).isoformat()
            end_value = _as_local(dtend, tz).isoformat()

        events.append(
            {
                "id": f"{source.id}:{component.get('UID', '')}:{start_value}",
                "calendar": source.id,
                "title": title or "(ללא כותרת)",
                "start": start_value,
                "end": end_value,
                "allDay": all_day,
                "location": location,
            }
        )
    return events


def sort_key(event: dict) -> tuple:
    # All-day events first within a day, then by start time.
    return (event["start"][:10], not event["allDay"], event["start"], event["title"])
