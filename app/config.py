"""Settings read from environment variables.

Each calendar is a numbered group of variables, so they can be added one by one
in Render's dashboard without editing any file:

    CAL_1_NAME=אבא
    CAL_1_URL=https://outlook.office365.com/owa/calendar/.../calendar.ics
    CAL_1_COLOR=#2563eb      (optional)
    CAL_1_PRIVATE=1          (optional: show "תפוס" instead of titles)
"""

import hashlib
import os
from dataclasses import dataclass
from zoneinfo import ZoneInfo

MAX_CALENDARS = 20

DEFAULT_COLORS = [
    "#2563eb",  # blue
    "#db2777",  # pink
    "#16a34a",  # green
    "#ea580c",  # orange
    "#7c3aed",  # violet
    "#0891b2",  # cyan
    "#ca8a04",  # amber
    "#dc2626",  # red
]


@dataclass(frozen=True)
class CalendarSource:
    id: str
    name: str
    color: str
    url: str
    private: bool = False

    def public(self) -> dict:
        """What the browser may see: never the feed URL, which is a secret."""
        return {"id": self.id, "name": self.name, "color": self.color}


@dataclass(frozen=True)
class Settings:
    calendars: tuple[CalendarSource, ...]
    password: str | None
    secret_key: str
    timezone: ZoneInfo
    cache_seconds: int


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on", "כן"}


def normalize_url(url: str) -> str:
    url = url.strip()
    # Outlook and Google both hand out webcal:// links; they are plain HTTPS.
    if url.lower().startswith("webcal://"):
        url = "https://" + url[len("webcal://"):]
    return url


def load_calendars(env=os.environ) -> tuple[CalendarSource, ...]:
    calendars = []
    for i in range(1, MAX_CALENDARS + 1):
        url = (env.get(f"CAL_{i}_URL") or "").strip()
        if not url:
            continue
        calendars.append(
            CalendarSource(
                id=f"cal{i}",
                name=(env.get(f"CAL_{i}_NAME") or "").strip() or f"יומן {i}",
                color=(env.get(f"CAL_{i}_COLOR") or "").strip()
                or DEFAULT_COLORS[(i - 1) % len(DEFAULT_COLORS)],
                url=normalize_url(url),
                private=_truthy(env.get(f"CAL_{i}_PRIVATE")),
            )
        )
    return tuple(calendars)


def load_settings(env=os.environ) -> Settings:
    password = (env.get("FAMILY_PASSWORD") or "").strip() or None
    # Without an explicit key, sessions are tied to the password: changing the
    # password signs everyone out.
    secret_key = (env.get("SECRET_KEY") or "").strip() or hashlib.sha256(
        ("family-calendar:" + (password or "")).encode()
    ).hexdigest()
    return Settings(
        calendars=load_calendars(env),
        password=password,
        secret_key=secret_key,
        timezone=ZoneInfo((env.get("TIMEZONE") or "Asia/Jerusalem").strip()),
        cache_seconds=int(env.get("CACHE_SECONDS") or 600),
    )
