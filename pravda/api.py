import json
import logging
import os
import uuid

from fastapi import Depends, FastAPI
from playwright.async_api import async_playwright
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from pravda.capture import capture_page
from pravda.db import get_session, init_db
from pravda.storage import content_path

BROWSER_CHANNEL = "chrome"
BROWSER_WS_URL = os.environ["BROWSER_WS_URL"]

logger = logging.getLogger(__name__)

app = FastAPI(title="Pravda", description="Evidence layer for web pages")


@app.on_event("startup")
async def startup() -> None:
    await init_db()
    logger.info("Database initialized")


# --- Request / response models ---


class SnapshotCreate(BaseModel):
    url: str


class ContentOut(BaseModel):
    content_type: str
    path: str


class HeaderOut(BaseModel):
    name: str
    value: str


class SnapshotOut(BaseModel):
    id: uuid.UUID
    url: str
    captured_at: str
    http_status: int
    contents: list[ContentOut]
    headers: list[HeaderOut]


class HealthOut(BaseModel):
    status: str


# --- Endpoints ---


@app.get("/health")
async def health() -> HealthOut:
    return HealthOut(status="ok")


@app.post("/snapshots", response_model=SnapshotOut)
async def create_snapshot(
    body: SnapshotCreate,
    session: AsyncSession = Depends(get_session),
) -> SnapshotOut:
    async with async_playwright() as p:
        browser = await p.chromium.connect(
            BROWSER_WS_URL,
            headers={
                "x-playwright-launch-options": json.dumps(
                    {"channel": BROWSER_CHANNEL, "headless": False}
                ),
            },
        )
        context = await browser.new_context()
        page = await context.new_page()

        snapshot = await capture_page(page, body.url, session)

        await context.close()

    await session.commit()
    return SnapshotOut(
        id=snapshot.id,
        url=snapshot.url,
        captured_at=snapshot.captured_at.isoformat(),
        http_status=snapshot.http_status,
        contents=[
            ContentOut(content_type=c.content_type, path=content_path(c.hash))
            for c in snapshot.contents
        ],
        headers=[HeaderOut(name=h.name, value=h.value) for h in snapshot.headers],
    )
