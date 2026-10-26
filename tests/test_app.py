from datetime import date
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from app.config import load_calendars, load_settings
from app.feeds import FeedCache, FeedError
from app.main import create_app

TZ = ZoneInfo("Asia/Jerusalem")

# Outlook style: explicit VTIMEZONE-less TZID, a weekly meeting with one
# cancelled occurrence, and a private appointment.
OUTLOOK = b"""BEGIN:VCALENDAR
VERSION:2.0
PRODID:Microsoft Exchange Server 2010
BEGIN:VEVENT
UID:standup
SUMMARY:Standup
DTSTART;TZID=Asia/Jerusalem:20261026T090000
DTEND;TZID=Asia/Jerusalem:20261026T093000
RRULE:FREQ=WEEKLY;BYDAY=MO,WE
EXDATE;TZID=Asia/Jerusalem:20261028T090000
LOCATION:Zoom
END:VEVENT
BEGIN:VEVENT
UID:doctor
SUMMARY:Doctor
CLASS:PRIVATE
LOCATION:Clinic
DTSTART:20261027T120000Z
DTEND:20261027T130000Z
END:VEVENT
BEGIN:VEVENT
UID:cancelled
SUMMARY:Gone
STATUS:CANCELLED
DTSTART:20261029T120000Z
DTEND:20261029T130000Z
END:VEVENT
END:VCALENDAR
"""

# Google style: zone given only in X-WR-TIMEZONE, events in UTC, an all-day
# event spanning two days.
GOOGLE = b"""BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Google Inc//Google Calendar 70.9054//EN
X-WR-CALNAME:Kids
X-WR-TIMEZONE:Asia/Jerusalem
BEGIN:VEVENT
UID:football
SUMMARY:Football
DTSTART:20261020T140000Z
DTEND:20261020T150000Z
RRULE:FREQ=WEEKLY;BYDAY=TU
END:VEVENT
BEGIN:VEVENT
UID:trip
SUMMARY:School trip
DTSTART;VALUE=DATE:20261029
DTEND;VALUE=DATE:20261031
END:VEVENT
END:VCALENDAR
"""

ENV = {
    "FAMILY_PASSWORD": "secret",
    "CAL_1_NAME": "Dad",
    "CAL_1_URL": "webcal://outlook.example/cal.ics",
    "CAL_2_NAME": "Kids",
    "CAL_2_URL": "https://google.example/basic.ics",
    "CAL_2_COLOR": "#00ff00",
}

FEEDS = {
    "https://outlook.example/cal.ics": OUTLOOK,
    "https://google.example/basic.ics": GOOGLE,
}


def make_client(env=ENV, feeds=FEEDS):
    async def fetch(url):
        value = feeds[url]
        if isinstance(value, Exception):
            raise value
        return value

    return TestClient(create_app(load_settings(env), fetch=fetch))


def signed_in(client):
    assert client.post("/api/login", json={"password": "secret"}).status_code == 200
    return client


def week(client, start="2026-10-25", end="2026-11-01"):
    response = client.get(f"/api/events?start={start}&end={end}")
    assert response.status_code == 200, response.text
    return response.json()


def test_calendars_from_numbered_env_vars():
    cals = load_calendars({**ENV, "CAL_5_URL": "https://x.example/a.ics"})
    assert [c.id for c in cals] == ["cal1", "cal2", "cal5"]
    assert cals[0].url == "https://outlook.example/cal.ics"  # webcal:// rewritten
    assert cals[1].color == "#00ff00"
    assert cals[2].name == "יומן 5"


def test_everything_requires_the_family_password():
    client = make_client()
    assert client.get("/api/events?start=2026-10-25&end=2026-11-01").status_code == 401
    assert client.get("/api/calendars").status_code == 401
    assert client.post("/api/login", json={"password": "wrong"}).status_code == 401
    assert client.get("/api/session").json() == {"authenticated": False, "configured": True}


def test_no_password_configured_means_no_access():
    client = make_client(env={k: v for k, v in ENV.items() if k != "FAMILY_PASSWORD"})
    assert client.post("/api/login", json={"password": ""}).status_code == 503
    assert client.get("/api/calendars").status_code == 401


def test_feed_urls_are_never_sent_to_the_browser():
    client = signed_in(make_client())
    body = client.get("/api/calendars").text
    assert "example" not in body
    assert client.get("/api/calendars").json()["timezone"] == "Asia/Jerusalem"


def test_week_merges_both_feeds_and_expands_recurrences():
    data = week(signed_in(make_client()))
    assert data["errors"] == []
    got = [(e["calendar"], e["title"], e["start"]) for e in data["events"]]
    assert got == [
        ("cal1", "Standup", "2026-10-26T09:00:00+02:00"),
        ("cal1", "תפוס", "2026-10-27T14:00:00+02:00"),  # private: no title
        # Set up in summer time as 14:00 UTC = 17:00 in Israel. After the
        # clocks change (Oct 25 2026) it must stay 17:00 local, not slide to 16:00.
        ("cal2", "Football", "2026-10-27T17:00:00+02:00"),
        ("cal2", "School trip", "2026-10-29"),
        # Wednesday's standup is excluded and the cancelled event is gone.
    ]
    doctor = data["events"][1]
    assert doctor["location"] == ""
    trip = data["events"][3]
    assert trip["allDay"] and trip["end"] == "2026-10-31"


def test_private_calendar_hides_every_title():
    env = {**ENV, "CAL_1_PRIVATE": "1"}
    data = week(signed_in(make_client(env=env)))
    titles = {e["title"] for e in data["events"] if e["calendar"] == "cal1"}
    assert titles == {"תפוס"}


def test_a_broken_feed_is_reported_without_hiding_the_others():
    feeds = {**FEEDS, "https://outlook.example/cal.ics": b"<html>Sign in</html>"}
    data = week(signed_in(make_client(feeds=feeds)))
    assert [e["calendar"] for e in data["errors"]] == ["cal1"]
    assert data["errors"][0]["stale"] is False
    assert {e["calendar"] for e in data["events"]} == {"cal2"}


def test_invalid_range_rejected():
    client = signed_in(make_client())
    assert client.get("/api/events?start=2026-10-25&end=2026-10-25").status_code == 400
    assert client.get("/api/events?start=2026-01-01&end=2026-12-31").status_code == 400


@pytest.mark.anyio
async def test_cache_keeps_last_good_copy_when_refresh_fails():
    now = [0.0]
    responses = [OUTLOOK, FeedError("down")]
    calls = []

    async def fetch(url):
        calls.append(url)
        value = responses.pop(0)
        if isinstance(value, Exception):
            raise value
        return value

    cache = FeedCache(fetch=fetch, ttl=600, clock=lambda: now[0])
    first = await cache.get("u")
    assert first.error is None and first.calendar is not None

    now[0] = 10  # within TTL: no new request
    await cache.get("u")
    assert len(calls) == 1

    now[0] = 700  # expired, refresh fails: old copy kept and flagged stale
    second = await cache.get("u")
    assert second.calendar is first.calendar
    assert second.error == "down" and second.stale is True


def test_static_app_is_served():
    client = make_client()
    assert "יומן משפחתי" in client.get("/").text
    assert client.get("/manifest.webmanifest").status_code == 200
    assert client.get("/sw.js").status_code == 200
