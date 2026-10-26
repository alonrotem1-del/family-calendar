"""The web server: a family password, an events API, and the app's static files."""

import asyncio
import hashlib
import hmac
import re
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .config import Settings, load_settings
from .feeds import (
    FeedCache,
    Fetcher,
    activities_between,
    events_between,
    http_fetch,
    sort_key,
)
from .store import EventStore

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
SESSION_COOKIE = "fc_session"
MAX_RANGE_DAYS = 62


class Login(BaseModel):
    password: str


class FamilyEvent(BaseModel):
    """An event added in the app. One or more family members can be in it."""

    title: str
    people: list[str]
    date: date
    allDay: bool = False
    start: str | None = None  # "17:00"
    end: str | None = None  # "18:30"
    location: str = ""


def _clock(value: str | None, label: str) -> time:
    match = re.fullmatch(r"(\d{1,2}):(\d{2})", (value or "").strip())
    if not match or int(match[1]) > 23 or int(match[2]) > 59:
        raise HTTPException(status_code=400, detail=f"{label} לא תקינה")
    return time(int(match[1]), int(match[2]))


def to_stored(body: FamilyEvent, settings: Settings) -> dict:
    """Checks a new or edited event and turns it into what the store keeps."""
    title = body.title.strip()
    if not title:
        raise HTTPException(status_code=400, detail="צריך לכתוב שם לאירוע")
    if len(title) > 120:
        raise HTTPException(status_code=400, detail="השם ארוך מדי")
    known = [p.id for p in settings.people]
    if set(body.people) - set(known):
        raise HTTPException(status_code=400, detail="בן משפחה לא מוכר")
    people = [p for p in known if p in set(body.people)]  # family order, no repeats
    if not people:
        raise HTTPException(status_code=400, detail="צריך לבחור לפחות בן משפחה אחד")
    day = body.date.isoformat()
    if body.allDay:
        start, end = day, (body.date + timedelta(days=1)).isoformat()
    else:
        begins, ends = _clock(body.start, "שעת ההתחלה"), _clock(body.end, "שעת הסיום")
        if ends <= begins:
            raise HTTPException(status_code=400, detail="שעת הסיום צריכה להיות אחרי שעת ההתחלה")
        start = datetime.combine(body.date, begins, tzinfo=settings.timezone).isoformat()
        end = datetime.combine(body.date, ends, tzinfo=settings.timezone).isoformat()
    return {
        "title": title,
        "people": people,
        "start": start,
        "end": end,
        "allDay": body.allDay,
        "location": body.location.strip()[:200],
        "first_day": day,
        "last_day": day,
    }


def to_api(stored: dict) -> dict:
    return {
        key: stored[key] for key in ("id", "title", "people", "start", "end", "allDay", "location")
    } | {"editable": True}


def create_app(
    settings: Settings | None = None,
    fetch: Fetcher = http_fetch,
    store: EventStore | None = None,
) -> FastAPI:
    settings = settings or load_settings()
    store = store or EventStore(settings.database_url)
    cache = FeedCache(fetch=fetch, ttl=settings.cache_seconds)
    session_token = hmac.new(
        settings.secret_key.encode(), b"family-calendar-session-v1", hashlib.sha256
    ).hexdigest()

    app = FastAPI(title="יומן משפחתי", docs_url=None, redoc_url=None)

    def is_signed_in(request: Request) -> bool:
        cookie = request.cookies.get(SESSION_COOKIE, "")
        return settings.password is not None and hmac.compare_digest(cookie, session_token)

    def require_session(request: Request) -> None:
        if not is_signed_in(request):
            raise HTTPException(status_code=401, detail="צריך להתחבר")

    @app.get("/api/session")
    def session(request: Request):
        return {
            "authenticated": is_signed_in(request),
            "configured": settings.password is not None,
        }

    @app.post("/api/login")
    async def login(body: Login, request: Request, response: Response):
        if settings.password is None:
            raise HTTPException(
                status_code=503, detail="לא הוגדרה סיסמה לאפליקציה (FAMILY_PASSWORD)"
            )
        if not hmac.compare_digest(body.password.strip().encode(), settings.password.encode()):
            await asyncio.sleep(1)  # slows down guessing
            raise HTTPException(status_code=401, detail="סיסמה שגויה")
        response.set_cookie(
            SESSION_COOKIE,
            session_token,
            max_age=60 * 60 * 24 * 365,
            httponly=True,
            samesite="lax",
            secure=request.url.scheme == "https",
        )
        return {"authenticated": True}

    @app.post("/api/logout")
    def logout(response: Response):
        response.delete_cookie(SESSION_COOKIE)
        return {"authenticated": False}

    @app.get("/api/people", dependencies=[Depends(require_session)])
    def people():
        return {
            "timezone": settings.timezone.key,
            "people": [p.public() for p in settings.people],
            # Without a database, added events vanish when Render restarts the app.
            "storage": "durable" if store.durable else "temporary",
        }

    @app.get("/api/events", dependencies=[Depends(require_session)])
    async def events(start: date, end: date):
        if end <= start or end - start > timedelta(days=MAX_RANGE_DAYS):
            raise HTTPException(status_code=400, detail="טווח תאריכים לא תקין")

        with_feed = [p for p in settings.people if p.feed_url]
        added, *results = await asyncio.gather(
            asyncio.to_thread(store.between, start.isoformat(), end.isoformat()),
            *(cache.get(p.feed_url) for p in with_feed),
        )
        all_events = [to_api(e) for e in added]
        errors = []
        for person in settings.people:
            all_events.extend(activities_between(person, start, end, settings.timezone))
        for person, result in zip(with_feed, results):
            if result.error:
                errors.append({"person": person.id, "message": result.error, "stale": result.stale})
            if result.calendar is None:
                continue
            try:
                all_events.extend(
                    events_between(result.calendar, person, start, end, settings.timezone)
                )
            except Exception as exc:  # one malformed event must not hide the others
                errors.append(
                    {
                        "person": person.id,
                        "message": f"שגיאה בקריאת היומן ({type(exc).__name__})",
                        "stale": False,
                    }
                )
        all_events.sort(key=sort_key)
        return {
            "events": all_events,
            "errors": errors,
            "generatedAt": datetime.now(timezone.utc).isoformat(),
        }

    @app.post("/api/events", dependencies=[Depends(require_session)])
    def add_event(body: FamilyEvent):
        return to_api(store.save(to_stored(body, settings)))

    @app.put("/api/events/{event_id}", dependencies=[Depends(require_session)])
    def edit_event(event_id: str, body: FamilyEvent):
        saved = store.save(to_stored(body, settings), event_id)
        if saved is None:
            raise HTTPException(status_code=404, detail="האירוע כבר לא קיים")
        return to_api(saved)

    @app.delete("/api/events/{event_id}", dependencies=[Depends(require_session)])
    def delete_event(event_id: str):
        if not store.delete(event_id):
            raise HTTPException(status_code=404, detail="האירוע כבר לא קיים")
        return {"deleted": True}

    @app.get("/api/health")
    def health():
        return {"ok": True}

    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
    return app
