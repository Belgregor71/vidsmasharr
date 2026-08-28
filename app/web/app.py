"""FastAPI application factory.

Server-rendered Jinja2. Every action is a plain form POST that redirects, so
the UI works with no JavaScript at all; HTMX is layered on top for the parts
where a full page reload would be annoying. That matters here because this box
is on a home LAN and the CDN may or may not be reachable.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.config import Config, load_config
from app.db import Database

WEB_DIR = Path(__file__).parent
TEMPLATES = Jinja2Templates(directory=str(WEB_DIR / "templates"))


def human_bytes(value: float | int | None) -> str:
    if not value:
        return "0 B"
    size = float(value)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(size) < 1024 or unit == "TB":
            return f"{size:.0f} {unit}" if unit in ("B", "KB") else f"{size:.2f} {unit}"
        size /= 1024
    return f"{size:.2f} TB"


def human_duration(seconds: float | None) -> str:
    if not seconds:
        return "-"
    minutes = int(seconds // 60)
    return f"{minutes // 60}h {minutes % 60:02d}m" if minutes >= 60 else f"{minutes}m"


TEMPLATES.env.filters["bytes"] = human_bytes
TEMPLATES.env.filters["duration"] = human_duration


def create_app(config: Config | None = None, db: Database | None = None) -> FastAPI:
    config = config or load_config()
    db = db or Database(config.db_path)

    app = FastAPI(title="vidsmasharr", docs_url=None, redoc_url=None)
    app.state.config = config
    app.state.db = db
    app.state.templates = TEMPLATES

    static_dir = WEB_DIR / "static"
    static_dir.mkdir(parents=True, exist_ok=True)
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    from app.web.routes import ui

    app.include_router(ui.router)
    return app
