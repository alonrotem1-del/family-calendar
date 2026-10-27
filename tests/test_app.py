import os
from datetime import date
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from pathlib import Path

from app.config import ConfigError, load_people, load_settings
from app.feeds import FeedCache, FeedError
from app.main import create_app
from app.store import EventStore

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

    store = EventStore(sqlite_path=Path(family_file).parent / "events.db")
    return TestClient(create_app(load_settings(env, family_file), fetch=fetch, store=store))


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
    got = [(e["people"][0], e["title"], e["start"]) for e in data["events"]]
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
    leo = [(e["title"], e["start"][:10]) for e in data["events"] if e["people"] == ["leo"]]
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
    assert {e["people"][0] for e in data["events"]} == {"kids", "leo"}


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


# ---- Events added in the app ----------------------------------------------

PARENTS_EVENING = {
    "title": "  יום הורים  ",
    "people": ["leo", "aba"],  # stored in family order: aba first
    "date": "2026-10-28",
    "start": "18:30",
    "end": "19:15",
    "location": "בית הספר",
}


def test_add_an_event_for_two_people(family_file):
    client = signed_in(make_client(family_file))
    added = client.post("/api/events", json=PARENTS_EVENING)
    assert added.status_code == 200, added.text
    event = added.json()
    assert event["title"] == "יום הורים"
    assert event["people"] == ["aba", "leo"]
    assert event["start"] == "2026-10-28T18:30:00+02:00"
    assert event["editable"] is True

    data = week(client)
    shown = [e for e in data["events"] if e.get("editable")]
    assert [e["id"] for e in shown] == [event["id"]]
    # Feed and activity events are read-only.
    assert not any(e.get("editable") for e in data["events"] if e["id"] != event["id"])
    # Sorted in with everything else on Wednesday.
    wednesday = [e["title"] for e in data["events"] if e["start"].startswith("2026-10-28")]
    assert wednesday == ["Basketball", "יום הורים"]
    # Not in other weeks.
    assert not any(e.get("editable") for e in week(client, "2026-11-01", "2026-11-08")["events"])


def test_all_day_event(family_file):
    client = signed_in(make_client(family_file))
    event = client.post("/api/events", json={
        "title": "טיול", "people": ["leo"], "date": "2026-10-30", "allDay": True,
    }).json()
    assert (event["start"], event["end"], event["allDay"]) == ("2026-10-30", "2026-10-31", True)


def test_edit_and_delete(family_file):
    client = signed_in(make_client(family_file))
    event = client.post("/api/events", json=PARENTS_EVENING).json()

    edited = client.put(f"/api/events/{event['id']}", json={**PARENTS_EVENING, "start": "19:00", "end": "20:00", "people": ["kids"]})
    assert edited.status_code == 200
    assert edited.json()["people"] == ["kids"] and edited.json()["id"] == event["id"]
    assert [e["start"] for e in week(client)["events"] if e.get("editable")] == ["2026-10-28T19:00:00+02:00"]

    assert client.delete(f"/api/events/{event['id']}").status_code == 200
    assert not any(e.get("editable") for e in week(client)["events"])
    assert client.delete(f"/api/events/{event['id']}").status_code == 404
    assert client.put(f"/api/events/{event['id']}", json=PARENTS_EVENING).status_code == 404


@pytest.mark.parametrize(
    "change, message",
    [
        ({"title": "  "}, "שם"),
        ({"people": []}, "לפחות"),
        ({"people": ["stranger"]}, "לא מוכר"),
        ({"start": "25:00"}, "שעת ההתחלה"),
        ({"end": "18:00"}, "אחרי"),
        ({"end": None}, "שעת הסיום"),
    ],
)
def test_bad_events_are_refused_in_hebrew(family_file, change, message):
    client = signed_in(make_client(family_file))
    response = client.post("/api/events", json={**PARENTS_EVENING, **change})
    assert response.status_code == 400
    assert message in response.json()["detail"]


def test_adding_requires_the_password(family_file):
    client = make_client(family_file)
    assert client.post("/api/events", json=PARENTS_EVENING).status_code == 401
    assert client.delete("/api/events/x").status_code == 401


def test_storage_kind_is_reported(family_file):
    client = signed_in(make_client(family_file))
    assert client.get("/api/people").json()["storage"] == "temporary"


@pytest.fixture
def postgres_url(tmp_path):
    """A throwaway local Postgres, when the machine has one."""
    import shutil
    import subprocess
    import tempfile

    bindir = next(Path("/usr/lib/postgresql").glob("*/bin"), None) if Path("/usr/lib/postgresql").exists() else None
    initdb = shutil.which("initdb") or (bindir and str(bindir / "initdb"))
    if not initdb:
        pytest.skip("no local Postgres")
    pg_ctl = str(Path(initdb).parent / "pg_ctl")
    # Postgres refuses to run as root; then it runs as nobody, in a folder nobody can reach.
    run_as = ["runuser", "-u", "nobody", "--"] if hasattr(os, "geteuid") and os.geteuid() == 0 else []
    base = Path(tempfile.mkdtemp(prefix="family-calendar-pg-")) if run_as else tmp_path
    base.chmod(0o777)
    data, sock = base / "pg", base / "sock"
    sock.mkdir()
    sock.chmod(0o777)
    subprocess.run(run_as + [initdb, "-D", str(data), "-A", "trust", "-U", "test"], check=True, capture_output=True)
    # The server's output goes to a log file: a pipe it holds open would hang run().
    subprocess.run(
        run_as + [pg_ctl, "-D", str(data), "-l", str(base / "log"), "-o", f"-k {sock} -c listen_addresses=''", "-w", "start"],
        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60,
    )
    try:
        yield f"postgresql://test@/postgres?host={sock}"
    finally:
        subprocess.run(run_as + [pg_ctl, "-D", str(data), "-m", "fast", "stop"], capture_output=True, timeout=60)
        if run_as:
            shutil.rmtree(base, ignore_errors=True)


def test_events_survive_in_postgres(family_file, postgres_url):
    settings = load_settings({**ENV, "DATABASE_URL": postgres_url}, family_file)
    client = signed_in(TestClient(create_app(settings, fetch=None, store=EventStore(postgres_url))))
    assert client.get("/api/people").json()["storage"] == "durable"
    event = client.post("/api/events", json=PARENTS_EVENING).json()
    client.put(f"/api/events/{event['id']}", json={**PARENTS_EVENING, "title": "אסיפה"})

    # A new app on the same database, as after a restart.
    again = EventStore(postgres_url)
    assert [(e["title"], e["people"]) for e in again.between("2026-10-25", "2026-11-01")] == [("אסיפה", ["aba", "leo"])]
    assert again.delete(event["id"]) and again.between("2026-10-25", "2026-11-01") == []


# ---- When the database is missing or misconfigured --------------------------

def test_database_url_pasted_as_psql_command_is_cleaned():
    from app.config import database_url

    url = "postgresql://u:pw@ep-x-pooler.eu-central-1.aws.neon.tech/neondb?sslmode=require"
    assert database_url(f"psql '{url}'") == url
    assert database_url(f'  "{url}"\n') == url
    assert database_url(url) == url
    assert database_url("  ") is None


@pytest.mark.parametrize("bad_url", [
    "postgresql://family:secretpw@127.0.0.1:1/neondb?connect_timeout=2",  # nothing listening
    "this is not a url secretpw",
])
def test_calendar_keeps_working_without_the_database(family_file, bad_url):
    settings = load_settings({**ENV, "DATABASE_URL": bad_url}, family_file)

    async def fetch(url):
        return FEEDS[url]

    client = signed_in(TestClient(create_app(settings, fetch=fetch)))
    people = client.get("/api/people").json()
    assert people["storage"] == "unavailable" and people["storageError"]

    data = week(client)
    assert {e["people"][0] for e in data["events"]} == {"aba", "kids", "leo"}  # the rest still shows
    assert [e["person"] for e in data["errors"]] == [None]
    assert "מסד הנתונים" in data["errors"][0]["message"]

    refused = client.post("/api/events", json=PARENTS_EVENING)
    assert refused.status_code == 503 and "מסד הנתונים" in refused.json()["detail"]
    for text in (str(people), str(data), refused.text):
        assert "secretpw" not in text


def test_saving_recovers_once_the_database_is_back(tmp_path):
    folder = tmp_path / "not-yet"
    store = EventStore(sqlite_path=folder / "events.db")  # the folder doesn't exist: fails
    assert store.error
    folder.mkdir()
    assert store.between("2026-10-25", "2026-11-01") == []  # retried and recovered
    assert store.error is None
