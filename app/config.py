"""Who is in the family, and where each person's events come from.

The family itself (names, colors, kids' activities) lives in `family.yaml`.
Secret feed links never go in that file: a person with id `aba` reads theirs
from the environment variable FEED_ABA, set in Render's dashboard.
"""

import hashlib
import os
import re
from dataclasses import dataclass, field
from datetime import date, time
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_FAMILY_FILE = ROOT / "family.yaml"

DEFAULT_COLORS = ["#2563eb", "#db2777", "#16a34a", "#ea580c", "#7c3aed", "#0891b2", "#ca8a04"]

# Python's weekday(): Monday is 0.
HEBREW_DAYS = {"שני": 0, "שלישי": 1, "רביעי": 2, "חמישי": 3, "שישי": 4, "שבת": 5, "ראשון": 6}


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Activity:
    """A weekly recurring activity, e.g. football every Tuesday 16:30-17:30."""

    title: str
    weekdays: tuple[int, ...]
    start: time
    end: time
    location: str = ""
    first: date | None = None
    last: date | None = None
    skip: frozenset[date] = frozenset()


@dataclass(frozen=True)
class Lesson:
    """One lesson in a child's school timetable, e.g. 08:00-08:45 מתמטיקה."""

    start: time
    end: time
    subject: str

    def public(self) -> dict:
        return {"start": self.start.strftime("%H:%M"), "end": self.end.strftime("%H:%M"), "subject": self.subject}


@dataclass(frozen=True)
class Person:
    id: str
    name: str
    color: str
    feed_url: str | None = None
    private: bool = False
    activities: tuple[Activity, ...] = field(default_factory=tuple)
    # School timetable: day of the week (0 = Sunday, as in JavaScript) -> lessons.
    school: dict[int, tuple[Lesson, ...]] = field(default_factory=dict)

    def public(self) -> dict:
        """What the browser may see: never the feed URL, which is a secret."""
        return {"id": self.id, "name": self.name, "color": self.color}


@dataclass(frozen=True)
class Settings:
    people: tuple[Person, ...]
    password: str | None
    secret_key: str
    timezone: ZoneInfo
    cache_seconds: int
    database_url: str | None = None
    database_url_unpooled: str | None = None


def database_url(value: str | None) -> str | None:
    """A connection string as pasted from Neon's Connect window, which by default
    shows it inside a command: psql 'postgresql://...'. Keep just the URL."""
    value = (value or "").strip()
    if value.lower().startswith("psql "):
        value = value[5:].strip()
    return value.strip("'\"").strip() or None


def normalize_url(url: str) -> str:
    url = url.strip()
    # Outlook and Google both hand out webcal:// links; they are plain HTTPS.
    if url.lower().startswith("webcal://"):
        url = "https://" + url[len("webcal://"):]
    return url


def _date(value, where: str) -> date | None:
    if value is None or isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value).strip())
    except ValueError:
        raise ConfigError(f"{where}: תאריך לא תקין '{value}' (צריך להיות 2026-09-01)") from None


def _weekdays(value, where: str) -> tuple[int, ...]:
    names = value if isinstance(value, list) else re.split(r"[,\s]+|\bו", str(value or ""))
    days = []
    for name in names:
        name = str(name).strip().removeprefix("יום").strip()
        if not name:
            continue
        if name not in HEBREW_DAYS:
            raise ConfigError(f"{where}: יום לא מוכר '{name}' (ראשון, שני, … שבת)")
        days.append(HEBREW_DAYS[name])
    if not days:
        raise ConfigError(f"{where}: חסר יום (day)")
    return tuple(days)


def _hours(value, where: str) -> tuple[time, time]:
    # YAML reads an unquoted 16:30 as a number (990), so ask for quotes.
    match = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*[-–]\s*(\d{1,2}):(\d{2})\s*", str(value or ""))
    if not isinstance(value, str) or not match:
        raise ConfigError(f'{where}: שעות לא תקינות {value!r} (צריך להיות "16:30-17:30" עם מרכאות)')
    h1, m1, h2, m2 = map(int, match.groups())
    try:
        start, end = time(h1, m1), time(h2, m2)
    except ValueError:
        raise ConfigError(f"{where}: שעה לא קיימת {value!r}") from None
    if end <= start:
        raise ConfigError(f"{where}: שעת הסיום לפני שעת ההתחלה {value!r}")
    return start, end


def _activity(raw: dict, where: str) -> Activity:
    title = str(raw.get("title") or "").strip()
    if not title:
        raise ConfigError(f"{where}: חסר שם לחוג (title)")
    where = f"{where} ({title})"
    start, end = _hours(raw.get("time"), where)
    return Activity(
        title=title,
        weekdays=_weekdays(raw.get("day"), where),
        start=start,
        end=end,
        location=str(raw.get("location") or "").strip(),
        first=_date(raw.get("from"), where),
        last=_date(raw.get("until"), where),
        skip=frozenset(_date(d, where) for d in raw.get("skip") or []),
    )


def _school(raw, where: str) -> dict[int, tuple[Lesson, ...]]:
    """ראשון: ["08:00-08:45 מתמטיקה", ...] for each school day."""
    if not raw:
        return {}
    if not isinstance(raw, dict):
        raise ConfigError(f"{where}: מערכת השעות צריכה להיות רשימה לפי ימים (ראשון:, שני:, …)")
    days = {}
    for day_name, lessons in raw.items():
        name = str(day_name).strip().removeprefix("יום").strip()
        if name not in HEBREW_DAYS:
            raise ConfigError(f"{where}: יום לא מוכר '{day_name}' (ראשון, שני, … שבת)")
        day = (HEBREW_DAYS[name] + 1) % 7  # Python's Monday-first to Sunday-first
        parsed = []
        for lesson in lessons or []:
            match = re.fullmatch(r"\s*(\d{1,2}:\d{2}\s*[-–]\s*\d{1,2}:\d{2})\s+(.+?)\s*", str(lesson))
            if not match:
                raise ConfigError(
                    f'{where}, יום {name}: שיעור לא תקין {lesson!r} (צריך להיות "08:00-08:45 מתמטיקה")'
                )
            start, end = _hours(match[1], f"{where}, יום {name}")
            parsed.append(Lesson(start=start, end=end, subject=match[2]))
        days[day] = tuple(sorted(parsed, key=lambda l: l.start))
    return days


def load_people(path: Path = DEFAULT_FAMILY_FILE, env=os.environ) -> tuple[Person, ...]:
    try:
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    except (yaml.YAMLError, ValueError) as exc:  # ValueError: e.g. a 13th month
        raise ConfigError(f"family.yaml: הקובץ לא תקין: {exc}") from None
    people, seen = [], set()
    for i, raw in enumerate(data.get("people") or []):
        pid = str(raw.get("id") or "").strip().lower()
        if not re.fullmatch(r"[a-z][a-z0-9_]*", pid):
            raise ConfigError(f"family.yaml: id '{pid}' צריך להיות באותיות אנגליות קטנות (למשל leo)")
        if pid in seen:
            raise ConfigError(f"family.yaml: id '{pid}' מופיע פעמיים")
        seen.add(pid)
        name = str(raw.get("name") or "").strip() or pid
        people.append(
            Person(
                id=pid,
                name=name,
                color=str(raw.get("color") or "").strip() or DEFAULT_COLORS[i % len(DEFAULT_COLORS)],
                feed_url=normalize_url(env.get(f"FEED_{pid.upper()}") or "") or None,
                private=bool(raw.get("private")),
                activities=tuple(
                    _activity(a or {}, f"family.yaml, {name}")
                    for a in raw.get("activities") or []
                ),
                school=_school(raw.get("school"), f"family.yaml, {name}, מערכת שעות"),
            )
        )
    return tuple(people)


def load_settings(env=os.environ, family_file: Path | None = None) -> Settings:
    password = (env.get("FAMILY_PASSWORD") or "").strip() or None
    # Without an explicit key, sessions are tied to the password: changing the
    # password signs everyone out.
    secret_key = (env.get("SECRET_KEY") or "").strip() or hashlib.sha256(
        ("family-calendar:" + (password or "")).encode()
    ).hexdigest()
    return Settings(
        people=load_people(family_file or Path(env.get("FAMILY_FILE") or DEFAULT_FAMILY_FILE), env),
        password=password,
        secret_key=secret_key,
        timezone=ZoneInfo((env.get("TIMEZONE") or "Asia/Jerusalem").strip()),
        cache_seconds=int(env.get("CACHE_SECONDS") or 600),
        database_url=database_url(env.get("DATABASE_URL")),
        database_url_unpooled=database_url(env.get("DATABASE_URL_UNPOOLED")),
    )
