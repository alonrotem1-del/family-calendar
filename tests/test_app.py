from datetime import date
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from pathlib import Path

from app.config import ConfigError, load_people, load_settings
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

FAMILY = """
people:
  - id: aba
    name: Dad
  - id: kids
    name: Kids
    color: "#00ff00"
  - id: leo
    name: Leo
    activities:
      - title: Basketball
        day: שני ורביעי
        time: "17:00-18:00"
        location: Gym
        until: 2026-10-28
      - title: Piano
        day: [שישי]
        time: "10:00 - 10:45"
        skip: [2026-10-30]
"""

ENV = {
    "FAMILY_PASSWORD": "secret",
    "FEED_ABA": "webcal://outlook.example/cal.ics",
    "FEED_KIDS": "https://google.example/basic.ics",
}

FEEDS = {
    "https://outlook.example/cal.ics": OUTLOOK,
    "https://google.example/basic.ics": GOOGLE,
}


@pytest.fixture
def family_file(tmp_path):
    path = tmp_path / "family.yaml"
    path.write_text(FAMILY, encoding="utf-8")
    return path


def make_client(family_file, env=ENV, feeds=FEEDS):
    async def fetch(url):
        value = feeds[url]
        if isinstance(value, Exception):
            raise value
        return value

    return TestClient(create_app(load_settings(env, family_file), fetch=fetch))


def signed_in(client):
    assert client.post("/api/login", json={"password": "secret"}).status_code == 200
    return client


def week(client, start="2026-10-25", end="2026-11-01"):
    response = client.get(f"/api/events?start={start}&end={end}")
    assert response.status_code == 200, response.text
    return response.json()


def test_people_from_family_file(family_file):
    people = load_people(family_file, ENV)
    assert [p.id for p in people] == ["aba", "kids", "leo"]
    assert people[0].feed_url == "https://outlook.example/cal.ics"  # webcal:// rewritten
    assert people[1].color == "#00ff00"
    assert people[2].feed_url is None
    basketball, piano = people[2].activities
    assert basketball.weekdays == (0, 2)  # Monday, Wednesday
    assert piano.weekdays == (4,)  # Friday


def test_the_real_family_file_loads():
    people = load_people(Path(__file__).resolve().parent.parent / "family.yaml", {})
    assert [p.name for p in people][:3] == ["אמא", "אבא", "יולי"]


@pytest.mark.parametrize(
    "activity, message",
    [
        ('{title: X, day: Monday, time: "16:00-17:00"}', "יום לא מוכר"),
        ("{title: X, day: שני, time: 16:00-17:00}", None),  # string thanks to the dash
        ("{title: X, day: שני, time: 16:00}", "שעות לא תקינות"),  # YAML reads 960
        ('{title: X, day: שני, time: "17:00-16:00"}', "שעת הסיום"),
        ('{title: X, day: שני, time: "16:00-17:00", from: 2026-13-01}', "לא תקין"),
        ('{day: שני, time: "16:00-17:00"}', "חסר שם"),
    ],
)
def test_family_file_mistakes_are_explained(tmp_path, activity, message):
    path = tmp_path / "family.yaml"
    path.write_text(f"people:\n  - id: leo\n    activities: [{activity}]\n", encoding="utf-8")
    if message is None:
        load_people(path, {})
        return
    with pytest.raises(ConfigError, match=message):
        load_people(path, {})


def test_everything_requires_the_family_password(family_file):
    client = make_client(family_file)
    assert client.get("/api/events?start=2026-10-25&end=2026-11-01").status_code == 401
    assert client.get("/api/people").status_code == 401
    assert client.post("/api/login", json={"password": "wrong"}).status_code == 401
    assert client.get("/api/session").json() == {"authenticated": False, "configured": True}


def test_no_password_configured_means_no_access(family_file):
    client = make_client(family_file, env={k: v for k, v in ENV.items() if k != "FAMILY_PASSWORD"})
    assert client.post("/api/login", json={"password": ""}).status_code == 503
    assert client.get("/api/people").status_code == 401


def test_feed_urls_are_never_sent_to_the_browser(family_file):
    client = signed_in(make_client(family_file))
    body = client.get("/api/people").text
    assert "example" not in body
    assert client.get("/api/people").json()["timezone"] == "Asia/Jerusalem"


def test_week_merges_feeds_and_activities(family_file):
    data = week(signed_in(make_client(family_file)))
    assert data["errors"] == []
    got = [(e["person"], e["title"], e["start"]) for e in data["events"]]
    assert got == [
        ("aba", "Standup", "2026-10-26T09:00:00+02:00"),
        ("leo", "Basketball", "2026-10-26T17:00:00+02:00"),
        ("aba", "תפוס", "2026-10-27T14:00:00+02:00"),  # private: no title
        # Set up in summer time as 14:00 UTC = 17:00 in Israel. After the
        # clocks change (Oct 25 2026) it must stay 17:00 local, not slide to 16:00.
        ("kids", "Football", "2026-10-27T17:00:00+02:00"),
        ("leo", "Basketball", "2026-10-28T17:00:00+02:00"),  # last day included
        ("kids", "School trip", "2026-10-29"),
        # Wednesday's standup is excluded, the cancelled event is gone, and
        # Friday's piano is skipped.
    ]
    doctor = data["events"][2]
    assert doctor["location"] == ""
    trip = data["events"][5]
    assert trip["allDay"] and trip["end"] == "2026-10-31"
    assert data["events"][1]["location"] == "Gym"
    assert data["events"][1]["end"] == "2026-10-26T18:00:00+02:00"


def test_activity_ends_after_until_date(family_file):
    data = week(signed_in(make_client(family_file)), "2026-11-01", "2026-11-08")
    leo = [(e["title"], e["start"][:10]) for e in data["events"] if e["person"] == "leo"]
    assert leo == [("Piano", "2026-11-06")]


def test_private_person_hides_every_title(tmp_path):
    path = tmp_path / "family.yaml"
    path.write_text("people:\n  - id: aba\n    private: true\n", encoding="utf-8")
    data = week(signed_in(make_client(path)))
    assert {e["title"] for e in data["events"]} == {"תפוס"}


def test_a_broken_feed_is_reported_without_hiding_the_others(family_file):
    feeds = {**FEEDS, "https://outlook.example/cal.ics": b"<html>Sign in</html>"}
    data = week(signed_in(make_client(family_file, feeds=feeds)))
    assert [e["person"] for e in data["errors"]] == ["aba"]
    assert data["errors"][0]["stale"] is False
    assert {e["person"] for e in data["events"]} == {"kids", "leo"}


def test_invalid_range_rejected(family_file):
    client = signed_in(make_client(family_file))
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


def test_static_app_is_served(family_file):
    client = make_client(family_file)
    assert "יומן משפחתי" in client.get("/").text
    assert client.get("/manifest.webmanifest").status_code == 200
    assert client.get("/sw.js").status_code == 200
