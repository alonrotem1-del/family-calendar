"""Entry point for uvicorn: `uvicorn app.asgi:app`."""

from dotenv import load_dotenv

load_dotenv()
load_dotenv(".env.local")  # where `neon env pull` writes DATABASE_URL

from .main import create_app  # noqa: E402  (settings are read after .env loads)

app = create_app()
