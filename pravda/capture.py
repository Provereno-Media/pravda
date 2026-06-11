import logging

from playwright.async_api import Page
from playwright.async_api import TimeoutError as PlaywrightTimeout
from sqlalchemy.ext.asyncio import AsyncSession

from pravda.db import ConditionType, Content, Header, Snapshot
from pravda.storage import put_blob

logger = logging.getLogger(__name__)

# Timeout for navigation (reaching "commit" — first HTTP response received).
NAV_TIMEOUT_MS = 10_000

# Timeout for waiting on the page condition after navigation.
CONDITION_TIMEOUT_MS = 30_000


async def capture_page(
    page: Page,
    url: str,
    session: AsyncSession,
    condition_type: ConditionType = ConditionType.lifecycle,
    condition: str = "load",
    condition_timeout_ms: int = CONDITION_TIMEOUT_MS,
) -> Snapshot:
    """Navigate to *url*, capture evidence, store blobs, persist to *session*.

    Returns the ``Snapshot`` row (flushed, not committed — caller decides).
    """
    http_status: int | None = None
    resp_headers: dict[str, str] = {}
    error: str | None = None
    lifecycle_events: list[str] = []

    # --- CDP session: lifecycle tracking + MHTML capture ----------------
    cdp = await page.context.new_cdp_session(page)
    await cdp.send("Page.enable", {})
    await cdp.send("Page.setLifecycleEventsEnabled", {"enabled": True})
    cdp.on(
        "Page.lifecycleEvent",
        lambda params: lifecycle_events.append(params["name"]),
    )

    # --- Navigation: reach "commit" to grab the HTTP response, then wait
    #     for the requested condition. Headers/status are captured between
    #     the two steps, so a condition timeout still records the response.
    condition_met = False
    try:
        response = await page.goto(url, wait_until="commit", timeout=NAV_TIMEOUT_MS)
        http_status = response.status
        raw = await response.all_headers()
        resp_headers = {k.lower(): v for k, v in raw.items()}

        if condition_type is ConditionType.lifecycle:
            await page.wait_for_load_state(condition, timeout=condition_timeout_ms)
        else:
            await page.wait_for_selector(condition, timeout=condition_timeout_ms)
        condition_met = True
    except PlaywrightTimeout as exc:
        logger.warning(
            "Timeout for %s (condition_type=%s, condition=%s): %s",
            url,
            condition_type.value,
            condition,
            exc,
        )
        error = str(exc)

    logger.info("Lifecycle events for %s: %s", url, lifecycle_events)

    # --- Capture page content -------------------------------------------
    contents: list[Content] = []

    async def capture(content_type: str, gate: str, fn) -> None:
        """Capture via `fn` and store the blob, gated on a lifecycle event."""
        if gate not in lifecycle_events:
            logger.warning(
                "Skipping %s for %s — never reached %s", content_type, url, gate
            )
            return
        try:
            data = await fn()
            if isinstance(data, str):
                data = data.encode()
            contents.append(
                Content(content_type=content_type, hash=await put_blob(data))
            )
        except Exception as exc:
            logger.warning("Failed to capture %s for %s: %s", content_type, url, exc)

    async def mhtml() -> str:
        result = await cdp.send("Page.captureSnapshot", {"format": "mhtml"})
        return result["data"]

    await capture("multipart/related", "DOMContentLoaded", mhtml)
    await capture("image/png", "load", lambda: page.screenshot(full_page=True))
    await capture("text/html", "DOMContentLoaded", page.content)
    await capture("text/plain", "DOMContentLoaded", lambda: page.inner_text("body"))

    # --- Persist snapshot row -------------------------------------------
    snapshot = Snapshot(
        url=url,
        http_status=http_status,
        error=error,
        condition_type=condition_type,
        condition=condition,
        condition_met=condition_met,
        lifecycle_events=lifecycle_events,
    )
    snapshot.contents = contents
    snapshot.headers = [
        Header(name=name, value=value) for name, value in resp_headers.items()
    ]
    session.add(snapshot)
    await session.flush()
    logger.info("Saved snapshot %s for %s", snapshot.id, url)

    return snapshot
