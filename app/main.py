"""The web server: a family password, an events API, and the app's static files."""

import asyncio
import hashlib
import hmac
from datetime import date, datetime, timedelta, timezone
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

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
SESSION_COOKIE = "fc_session"
MAX_RANGE_DAYS = 62


class Login(BaseModel):
    password: str


def create_app(settings: Settings | None = None, fetch: Fetcher = http_fetch) -> FastAPI:
    settings = settings or load_settings()
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
        }

    @app.get("/api/events", dependencies=[Depends(require_session)])
    async def events(start: date, end: date):
        if end <= start or end - start > timedelta(days=MAX_RANGE_DAYS):
            raise HTTPException(status_code=400, detail="טווח תאריכים לא תקין")

        with_feed = [p for p in settings.people if p.feed_url]
        results = await asyncio.gather(*(cache.get(p.feed_url) for p in with_feed))
        all_events, errors = [], []
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

    @app.get("/api/health")
    def health():
        return {"ok": True}

    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
    return app
