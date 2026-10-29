"""The web server: a family password, an events API, and the app's static files."""

import asyncio
import hashlib
import hmac
import os
import re
from urllib.parse import urlsplit
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse
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
from .store import EventStore, StoreUnavailable

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
    notes: str = ""  # what to bring, what to wear, the teacher's message


def _clock(value: str | None, label: str) -> time:
    match = re.fullmatch(r"(\d{1,2}):(\d{2})", (value or "").strip())
    if not match or int(match[1]) > 23 or int(match[2]) > 59:
        raise HTTPException(status_code=400, detail=f"{label} לא תקינה")
    return time(int(match[1]), int(match[2]))


class SchoolNote(BaseModel):
    """A message from school for one day, for one or more children."""

    title: str
    people: list[str]
    date: date
    notes: str = ""


def note_to_stored(body: SchoolNote, settings: Settings) -> dict:
    title = body.title.strip()
    if not title:
        raise HTTPException(status_code=400, detail="צריך לכתוב כותרת להודעה")
    if len(title) > 120:
        raise HTTPException(status_code=400, detail="הכותרת ארוכה מדי")
    known = [p.id for p in settings.people]
    if set(body.people) - set(known):
        raise HTTPException(status_code=400, detail="בן משפחה לא מוכר")
    people = [p for p in known if p in set(body.people)]
    if not people:
        raise HTTPException(status_code=400, detail="צריך לבחור לפחות ילד אחד")
    return {"day": body.date.isoformat(), "people": people, "title": title, "notes": body.notes.strip()[:2000]}


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
        "notes": body.notes.strip()[:2000],
        "first_day": day,
        "last_day": day,
    }


def same_database(pooled: str | None, direct: str | None) -> bool | None:
    """Whether Neon's pooled and direct URLs lead to the same database. If not, the
    table is created in one database and events are saved in another."""
    if not pooled or not direct:
        return None
    a, b = urlsplit(pooled), urlsplit(direct)
    host = lambda u: (u.hostname or "").replace("-pooler", "")
    return host(a) == host(b) and a.path == b.path


def to_api(stored: dict) -> dict:
    return {
        key: stored[key] for key in ("id", "title", "people", "start", "end", "allDay", "location", "notes")
    } | {"editable": True}


def create_app(
    settings: Settings | None = None,
    fetch: Fetcher = http_fetch,
    store: EventStore | None = None,
) -> FastAPI:
    settings = settings or load_settings()
    store = store or EventStore(settings.database_url, direct_url=settings.database_url_unpooled)
    cache = FeedCache(fetch=fetch, ttl=settings.cache_seconds)
    session_token = hmac.new(
        settings.secret_key.encode(), b"family-calendar-session-v1", hashlib.sha256
    ).hexdigest()

    app = FastAPI(title="יומן משפחתי", docs_url=None, redoc_url=None)

    @app.middleware("http")
    async def always_fresh(request: Request, call_next):
        """Without this, a phone may keep running yesterday's app.js after an
        update. no-cache still lets it reuse a copy, but only after checking it
        (a quick 304 when nothing changed). Family data is never stored."""
        response = await call_next(request)
        api = request.url.path.startswith("/api/")
        response.headers.setdefault("Cache-Control", "no-store" if api else "no-cache")
        return response

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
            "storage": storage_kind(),
            "storageError": store.error,
        }

    @app.get("/api/school", dependencies=[Depends(require_session)])
    def school():
        """The children's school timetables, for the second tab."""
        return {
            "kids": [
                p.public() | {
                    "days": {str(day): [l.public() for l in lessons] for day, lessons in p.school.items()}
                }
                for p in settings.people
                if p.school
            ]
        }

    @app.get("/api/school/notes", dependencies=[Depends(require_session)])
    def school_notes(start: date, end: date):
        if end <= start or end - start > timedelta(days=MAX_RANGE_DAYS):
            raise HTTPException(status_code=400, detail="טווח תאריכים לא תקין")
        try:
            return {"notes": store.notes_between(start.isoformat(), end.isoformat()), "error": None}
        except StoreUnavailable as exc:
            return {"notes": [], "error": f"הודעות בית הספר לא זמינות כרגע: אין חיבור למסד הנתונים ({exc})"}

    @app.post("/api/school/notes", dependencies=[Depends(require_session)])
    def add_school_note(body: SchoolNote):
        return store.save_note(note_to_stored(body, settings))

    @app.put("/api/school/notes/{note_id}", dependencies=[Depends(require_session)])
    def edit_school_note(note_id: str, body: SchoolNote):
        saved = store.save_note(note_to_stored(body, settings) | {"replace": True}, note_id)
        if saved is None:
            raise HTTPException(status_code=404, detail="ההודעה כבר לא קיימת")
        return saved

    @app.delete("/api/school/notes/{note_id}", dependencies=[Depends(require_session)])
    def delete_school_note(note_id: str):
        if not store.delete_note(note_id):
            raise HTTPException(status_code=404, detail="ההודעה כבר לא קיימת")
        return {"deleted": True}

    @app.get("/api/events", dependencies=[Depends(require_session)])
    async def events(start: date, end: date):
        if end <= start or end - start > timedelta(days=MAX_RANGE_DAYS):
            raise HTTPException(status_code=400, detail="טווח תאריכים לא תקין")

        with_feed = [p for p in settings.people if p.feed_url]
        added, *results = await asyncio.gather(
            asyncio.to_thread(added_between, start.isoformat(), end.isoformat()),
            *(cache.get(p.feed_url) for p in with_feed),
        )
        added, store_error = added
        all_events = [to_api(e) for e in added]
        errors = []
        if store_error:
            errors.append({"person": None, "message": store_error, "stale": False})
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

    def added_between(first_day: str, end_day: str) -> tuple[list[dict], str | None]:
        try:
            return store.between(first_day, end_day), None
        except StoreUnavailable as exc:
            return [], f"אירועים שנוספו באפליקציה לא זמינים כרגע: אין חיבור למסד הנתונים ({exc})"

    @app.exception_handler(StoreUnavailable)
    async def store_unavailable(request: Request, exc: StoreUnavailable):
        return JSONResponse(
            status_code=503,
            content={"detail": f"אי אפשר לשמור כרגע: אין חיבור למסד הנתונים ({exc})"},
        )

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

    def storage_kind() -> str:
        return "temporary" if not store.durable else "unavailable" if store.error else "durable"

    @app.get("/api/health")
    def health(db: bool = False):
        """Public, and says nothing private: which version runs and whether saving works.
        /api/health?db=1 also tries the database (left out of Render's frequent checks,
        so Neon can still suspend when idle)."""
        if db and store.durable:
            try:
                store.between("1900-01-01", "1900-01-02")
            except StoreUnavailable:
                pass
        return {
            "ok": True,
            "version": (os.environ.get("RENDER_GIT_COMMIT") or "local")[:7],
            "storage": storage_kind(),
            "sameDatabase": same_database(settings.database_url, settings.database_url_unpooled),
        }

    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
    return app
