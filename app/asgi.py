"""Entry point for uvicorn: `uvicorn app.asgi:app`."""

from dotenv import load_dotenv

load_dotenv()

from .main import create_app  # noqa: E402  (settings are read after .env loads)

app = create_app()
