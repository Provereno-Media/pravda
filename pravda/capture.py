import logging
from dataclasses import dataclass

from playwright.async_api import CDPSession, Page
from playwright.async_api import TimeoutError as PlaywrightTimeout

from pravda.db import ConditionType
from pravda.storage import put_blob

logger = logging.getLogger(__name__)

# Timeout for navigation (reaching "commit" — first HTTP response received).
NAV_TIMEOUT_MS = 10_000

# Timeout for waiting on the page condition after navigation.
CONDITION_TIMEOUT_MS = 30_000


@dataclass
class CapturedContent:
    """A single stored artifact: its MIME type and content hash."""

    content_type: str
    hash: str


@dataclass
class CaptureResult:
    """Pure evidence captured from a page — no persistence concerns."""

    http_status: int | None
    error: str | None
    condition_met: bool
    lifecycle_events: list[str]
    headers: dict[str, str]
    contents: list[CapturedContent]


async def capture_page(
    page: Page,
    url: str,
    condition_type: ConditionType = ConditionType.lifecycle,
    condition: str = "load",
    condition_timeout_ms: int = CONDITION_TIMEOUT_MS,
) -> CaptureResult:
    """Navigate to *url* and capture evidence: HTTP response, lifecycle
    events, and MHTML/screenshot/HTML/text blobs.

    Returns the evidence as a ``CaptureResult``. Storing it is the caller's
    job — this function never touches the database.
    """
    cdp, lifecycle_events = await _track_lifecycle(page)

    nav = await _navigate(page, url, condition_type, condition, condition_timeout_ms)
    logger.info("Lifecycle events for %s: %s", url, lifecycle_events)

    contents = await _capture_contents(page, cdp, url, lifecycle_events)

    return CaptureResult(
        http_status=nav.http_status,
        error=nav.error,
        condition_met=nav.condition_met,
        lifecycle_events=lifecycle_events,
        headers=nav.headers,
        contents=contents,
    )


async def _track_lifecycle(page: Page) -> tuple[CDPSession, list[str]]:
    """Enable CDP lifecycle events and return the session plus a list that
    accumulates event names (init, commit, DOMContentLoaded, load, …) as
    they fire during navigation."""
    cdp = await page.context.new_cdp_session(page)
    await cdp.send("Page.enable", {})
    await cdp.send("Page.setLifecycleEventsEnabled", {"enabled": True})

    events: list[str] = []
    cdp.on("Page.lifecycleEvent", lambda params: events.append(params["name"]))
    return cdp, events


@dataclass
class _Navigation:
    http_status: int | None
    headers: dict[str, str]
    condition_met: bool
    error: str | None


async def _navigate(
    page: Page,
    url: str,
    condition_type: ConditionType,
    condition: str,
    condition_timeout_ms: int,
) -> _Navigation:
    """Navigate to *url*, then wait for the requested condition.

    Status and headers are read at "commit" (first response), *before* the
    condition wait — so a condition timeout still records the HTTP response.
    """
    http_status: int | None = None
    headers: dict[str, str] = {}
    try:
        response = await page.goto(url, wait_until="commit", timeout=NAV_TIMEOUT_MS)
        http_status = response.status
        headers = {k.lower(): v for k, v in (await response.all_headers()).items()}

        if condition_type is ConditionType.lifecycle:
            await page.wait_for_load_state(condition, timeout=condition_timeout_ms)
        else:
            await page.wait_for_selector(condition, timeout=condition_timeout_ms)

        return _Navigation(http_status, headers, condition_met=True, error=None)
    except PlaywrightTimeout as exc:
        logger.warning(
            "Timeout for %s (condition_type=%s, condition=%s): %s",
            url,
            condition_type.value,
            condition,
            exc,
        )
        return _Navigation(http_status, headers, condition_met=False, error=str(exc))


# Each artifact is gated on a lifecycle event: we only attempt the capture
# if the page actually reached that point, otherwise it would error or hang.
async def _capture_contents(
    page: Page,
    cdp: CDPSession,
    url: str,
    lifecycle_events: list[str],
) -> list[CapturedContent]:
    async def mhtml() -> str:
        result = await cdp.send("Page.captureSnapshot", {"format": "mhtml"})
        return result["data"]

    specs = [
        ("multipart/related", "DOMContentLoaded", mhtml),
        ("image/png", "load", lambda: page.screenshot(full_page=True)),
        ("text/html", "DOMContentLoaded", page.content),
        ("text/plain", "DOMContentLoaded", lambda: page.inner_text("body")),
    ]

    contents = []
    for content_type, gate, fn in specs:
        content = await _capture_one(content_type, gate, fn, url, lifecycle_events)
        if content is not None:
            contents.append(content)
    return contents


async def _capture_one(
    content_type: str,
    gate: str,
    fn,
    url: str,
    lifecycle_events: list[str],
) -> CapturedContent | None:
    """Capture one artifact via *fn* and store the blob, gated on *gate*."""
    if gate not in lifecycle_events:
        logger.warning("Skipping %s for %s — never reached %s", content_type, url, gate)
        return None
    try:
        data = await fn()
        if isinstance(data, str):
            data = data.encode()
        return CapturedContent(content_type=content_type, hash=await put_blob(data))
    except Exception as exc:
        logger.warning("Failed to capture %s for %s: %s", content_type, url, exc)
        return None
