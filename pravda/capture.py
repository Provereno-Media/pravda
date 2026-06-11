import logging

from playwright.async_api import Page
from playwright.async_api import TimeoutError as PlaywrightTimeout
from sqlalchemy.ext.asyncio import AsyncSession

from pravda.db import Content, Header, Snapshot
from pravda.storage import put_blob

logger = logging.getLogger(__name__)


async def capture_page(
    page: Page, url: str, session: AsyncSession, wait_until: str = "load"
) -> Snapshot:
    """Navigate to *url*, capture evidence, store blobs, persist to *session*.

    Returns the ``Snapshot`` row (flushed, not committed — caller decides).
    """
    condition_met = True
    http_status: int | None = None
    resp_headers: dict[str, str] = {}

    try:
        response = await page.goto(url, wait_until=wait_until)
        if response:
            http_status = response.status
            raw = await response.all_headers()
            resp_headers = {k.lower(): v for k, v in raw.items()}
    except PlaywrightTimeout:
        logger.warning("Timeout waiting for %s (condition=%s)", url, wait_until)
        condition_met = False

    cdp = await page.context.new_cdp_session(page)
    mhtml_response = await cdp.send("Page.captureSnapshot", {"format": "mhtml"})
    mhtml_bytes = mhtml_response["data"].encode("utf-8")
    await cdp.detach()

    screenshot_bytes = await page.screenshot(full_page=True)
    rendered_html = (await page.content()).encode("utf-8")
    inner_text = (await page.inner_text("body")).encode("utf-8")

    # Store blobs
    mhtml_hash = await put_blob(mhtml_bytes)
    screenshot_hash = await put_blob(screenshot_bytes)
    rendered_html_hash = await put_blob(rendered_html)
    inner_text_hash = await put_blob(inner_text)

    # Persist snapshot row
    snapshot = Snapshot(
        url=url,
        http_status=http_status,
        condition=wait_until,
        condition_met=condition_met,
    )
    snapshot.contents = [
        Content(content_type="multipart/related", hash=mhtml_hash),
        Content(content_type="image/png", hash=screenshot_hash),
        Content(content_type="text/html", hash=rendered_html_hash),
        Content(content_type="text/plain", hash=inner_text_hash),
    ]
    snapshot.headers = [
        Header(name=name, value=value) for name, value in resp_headers.items()
    ]
    session.add(snapshot)
    await session.flush()
    logger.info("Saved snapshot %s for %s", snapshot.id, url)

    return snapshot
