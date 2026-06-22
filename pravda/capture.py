import asyncio
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

# Timeout for each individual capture operation (MHTML, screenshot, etc.).
CAPTURE_TIMEOUT_MS = 15_000


@dataclass
class CaptureResult:
    """Pure evidence captured from a page — no persistence concerns."""

    http_status: int | None
    error: str | None
    condition_met: bool
    lifecycle_events: list[str]
    headers: dict[str, str]
    plaintext_hash: str | None
    rendered_html_hash: str | None
    screenshot_hash: str | None
    blob_hash: str | None
    blob_content_type: str | None


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
    lifecycle = await Lifecycle.start(page)

    navigation = await _navigate(
        page, lifecycle, url, condition_type, condition, condition_timeout_ms
    )
    logger.info("Lifecycle events for %s: %s", url, lifecycle.fired)

    artifacts = await _capture_artifacts(page, lifecycle, url)

    return CaptureResult(
        http_status=navigation.http_status,
        error=navigation.error,
        condition_met=navigation.condition_met,
        lifecycle_events=lifecycle.fired,
        headers=navigation.headers,
        plaintext_hash=artifacts.plaintext_hash,
        rendered_html_hash=artifacts.rendered_html_hash,
        screenshot_hash=artifacts.screenshot_hash,
        blob_hash=artifacts.blob_hash,
        blob_content_type=artifacts.blob_content_type,
    )


class Lifecycle:
    """Tracks the page's CDP ``Page.lifecycleEvent`` stream.

    One stream, two consumers: ``fired`` records every event name (capture
    gating reads it), and ``wait`` blocks until a name arrives. Because the
    single sync handler appends *before* it wakes a waiter, when ``wait``
    returns the name is guaranteed present in ``fired`` — no cross-source race.
    """

    def __init__(self, cdp: CDPSession):
        self.cdp = cdp
        self.fired: list[str] = []
        self._waiters: dict[str, asyncio.Event] = {}
        cdp.on("Page.lifecycleEvent", self._on_event)

    def _on_event(self, params) -> None:
        name = params["name"]
        self.fired.append(name)
        if name in self._waiters:
            self._waiters[name].set()

    async def wait(self, name: str, timeout_ms: int) -> None:
        """Block until lifecycle event *name* has fired, or time out.

        Mirrors how Playwright's own ``wait_for_load_state`` resolves — "return
        if already fired, else await its arrival" — but against our own stream,
        so completion guarantees *name* is present in ``fired`` for gating.
        """
        if name in self.fired:
            return
        waiter = self._waiters.setdefault(name, asyncio.Event())
        await asyncio.wait_for(waiter.wait(), timeout_ms / 1000)

    @classmethod
    async def start(cls, page: Page) -> "Lifecycle":
        """Enable CDP lifecycle events on *page* and return a ready tracker."""
        cdp = await page.context.new_cdp_session(page)
        await cdp.send("Page.enable", {})
        await cdp.send("Page.setLifecycleEventsEnabled", {"enabled": True})
        return cls(cdp)


@dataclass
class _Navigation:
    http_status: int | None
    headers: dict[str, str]
    condition_met: bool
    error: str | None


async def _navigate(
    page: Page,
    lifecycle: Lifecycle,
    url: str,
    condition_type: ConditionType,
    condition: str,
    condition_timeout_ms: int,
) -> _Navigation:
    """Navigate to *url*, then wait for the requested condition.

    Status and headers are read at "commit" (first response), *before* the
    condition wait — so a condition timeout still records the HTTP response.

    Lifecycle conditions wait on our own CDP event stream (``lifecycle.fired``);
    selector conditions use Playwright's ``wait_for_selector``.
    """
    http_status: int | None = None
    headers: dict[str, str] = {}
    try:
        response = await page.goto(url, wait_until="commit", timeout=NAV_TIMEOUT_MS)
        http_status = response.status
        headers = {
            key.lower(): value for key, value in (await response.all_headers()).items()
        }

        if condition_type is ConditionType.lifecycle:
            await lifecycle.wait(condition, condition_timeout_ms)
        else:
            await page.wait_for_selector(condition, timeout=condition_timeout_ms)

        return _Navigation(http_status, headers, condition_met=True, error=None)
    except (PlaywrightTimeout, asyncio.TimeoutError) as exception:
        error = str(exception) or (
            f"Timeout {condition_timeout_ms}ms exceeded waiting for '{condition}'"
        )
        logger.warning(
            "Timeout for %s (condition_type=%s, condition=%s): %s",
            url,
            condition_type.value,
            condition,
            error,
        )
        return _Navigation(http_status, headers, condition_met=False, error=error)


@dataclass
class CapturedArtifacts:
    """Hashes of the four captured artifacts, plus the blob's MIME type.

    Three are fixed-MIME (plaintext/rendered_html/screenshot); the blob is
    polymorphic (multipart/related today, application/pdf and others later),
    so its content type is recorded alongside.
    """

    plaintext_hash: str | None
    rendered_html_hash: str | None
    screenshot_hash: str | None
    blob_hash: str | None
    blob_content_type: str | None


# Each artifact is gated on a lifecycle event: we only attempt the capture
# if the page actually reached that point, otherwise it would error or hang.
async def _capture_artifacts(
    page: Page,
    lifecycle: Lifecycle,
    url: str,
) -> CapturedArtifacts:
    async def capture_mhtml() -> str:
        result = await asyncio.wait_for(
            lifecycle.cdp.send("Page.captureSnapshot", {"format": "mhtml"}),
            CAPTURE_TIMEOUT_MS / 1000,
        )
        return result["data"]

    plaintext_hash = await _capture_one(
        "plaintext",
        lambda: page.inner_text("body", timeout=CAPTURE_TIMEOUT_MS),
        "DOMContentLoaded",
        url,
        lifecycle.fired,
    )
    rendered_html_hash = await _capture_one(
        "rendered_html",
        lambda: page.content(),
        "DOMContentLoaded",
        url,
        lifecycle.fired,
    )
    screenshot_hash = await _capture_one(
        "screenshot",
        lambda: page.screenshot(full_page=True, timeout=CAPTURE_TIMEOUT_MS),
        "load",
        url,
        lifecycle.fired,
    )
    blob_hash = await _capture_one(
        "blob",
        capture_mhtml,
        "DOMContentLoaded",
        url,
        lifecycle.fired,
    )

    return CapturedArtifacts(
        plaintext_hash=plaintext_hash,
        rendered_html_hash=rendered_html_hash,
        screenshot_hash=screenshot_hash,
        blob_hash=blob_hash,
        blob_content_type="multipart/related" if blob_hash is not None else None,
    )


async def _capture_one(
    name: str,
    callback,
    gate: str,
    url: str,
    lifecycle_events: list[str],
) -> str | None:
    """Capture one artifact via *callback* and store the blob, gated on *gate*."""
    if gate not in lifecycle_events:
        logger.warning("Skipping %s for %s — never reached %s", name, url, gate)
        return None
    try:
        data = await callback()
        if isinstance(data, str):
            data = data.encode()
        return await put_blob(data)
    except (asyncio.TimeoutError, PlaywrightTimeout):
        logger.warning("Timeout capturing %s for %s", name, url)
        return None
    except Exception as exception:
        logger.warning("Failed to capture %s for %s: %s", name, url, exception)
        return None
