"""
SuperLiveDiscovery — reconstructed robust implementation
Version: 10.1

Purpose:
  - Discover publicly visible SuperLiveTV profile/stream pages using Playwright.
  - Detect likely live streams using multiple independent signals.
  - Detect Premium/paid indicators only when they are contextually relevant.
  - Validate stream records instead of returning unconditional success.

Notes:
  - This is a standalone reconstruction, not a line-by-line patch of the expired
    Version 10.0 attachment. Site markup can change; selectors are intentionally
    defensive and should be adjusted if SuperLiveTV changes its public UI.
  - Uses public pages only. It does not bypass logins, access controls, paywalls,
    or DRM, and it does not extract protected media URLs.
  - Install: pip install playwright
  - Browser setup: playwright install chromium
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable, Optional
from urllib.parse import urljoin, urlparse, urldefrag

from playwright.sync_api import (
    Browser,
    BrowserContext,
    Error as PlaywrightError,
    Page,
    TimeoutError as PlaywrightTimeoutError,
    sync_playwright,
)


VERSION = "10.1"
DEFAULT_BASE_URL = "https://superlivetv.com"
LOG = logging.getLogger("SuperLiveDiscovery")


@dataclass
class StreamRecord:
    """Normalized result for a publicly discoverable profile/stream."""

    username: str = ""
    profile_url: str = ""
    stream_url: str = ""
    stream_id: str = ""
    is_live: bool = False
    is_premium: bool = False
    premium_evidence: list[str] = field(default_factory=list)
    live_evidence: list[str] = field(default_factory=list)
    title: str = ""
    validation_ok: bool = False
    validation_reason: str = ""
    discovered_at: float = field(default_factory=time.time)
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class SuperLiveDiscovery:
    """
    Public-page discovery helper for SuperLiveTV.

    Main methods:
      - open()
      - find_profile()
      - discover_streams()
      - is_live_page()
      - detect_premium()
      - validate_stream()
      - close()

    The class uses synchronous Playwright so it can be dropped into ordinary
    synchronous scripts.
    """

    LIVE_TERMS = (
        "live now",
        "is live",
        "live stream",
        "watch live",
        "currently live",
        "live",
        "مباشر",
        "بث مباشر",
    )
    NON_LIVE_TERMS = (
        "replay",
        "recorded",
        "recording",
        "offline",
        "not live",
        "ended",
        "past stream",
        "إعادة",
        "مسجل",
        "غير متصل",
        "انتهى البث",
    )
    PREMIUM_TERMS = (
        "premium",
        "paid stream",
        "paid live",
        "subscriber only",
        "subscribers only",
        "members only",
        "exclusive stream",
        "vip",
        "مدفوع",
        "للمشتركين",
        "حصري",
    )
    AD_TERMS = (
        "advertisement",
        "advertising",
        "sponsored",
        "promoted",
        "ad choices",
        "إعلان",
        "إعلانات",
    )
    GIFT_TERMS = (
        "gift",
        "send a gift",
        "gift animation",
        "virtual gift",
        "هدية",
        "الهدايا",
    )

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        headless: bool = True,
        timeout_ms: int = 15000,
        navigation_timeout_ms: int = 30000,
        user_agent: Optional[str] = None,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.headless = headless
        self.timeout_ms = timeout_ms
        self.navigation_timeout_ms = navigation_timeout_ms
        self.user_agent = user_agent
        self.log = logger or LOG

        self._playwright = None
        self.browser: Optional[Browser] = None
        self.context: Optional[BrowserContext] = None
        self.page: Optional[Page] = None

    # --------------------------- Lifecycle ---------------------------

    def open(self) -> "SuperLiveDiscovery":
        """Start Chromium and open a reusable page."""
        if self.page is not None:
            return self

        self._playwright = sync_playwright().start()
        self.browser = self._playwright.chromium.launch(headless=self.headless)

        context_args: dict[str, Any] = {
            "ignore_https_errors": False,
            "java_script_enabled": True,
        }
        if self.user_agent:
            context_args["user_agent"] = self.user_agent

        self.context = self.browser.new_context(**context_args)
        self.page = self.context.new_page()
        self.page.set_default_timeout(self.timeout_ms)
        self.page.set_default_navigation_timeout(self.navigation_timeout_ms)
        return self

    def close(self) -> None:
        """Close Playwright resources safely."""
        for obj_name in ("page", "context", "browser"):
            obj = getattr(self, obj_name, None)
            if obj is not None:
                try:
                    obj.close()
                except Exception as exc:
                    self.log.debug("Could not close %s cleanly: %s", obj_name, exc)
                setattr(self, obj_name, None)

        if self._playwright is not None:
            try:
                self._playwright.stop()
            except Exception as exc:
                self.log.debug("Could not stop Playwright cleanly: %s", exc)
            self._playwright = None

    def __enter__(self) -> "SuperLiveDiscovery":
        return self.open()

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def _require_page(self) -> Page:
        if self.page is None:
            self.open()
        assert self.page is not None
        return self.page

    # --------------------------- Helpers -----------------------------

    @staticmethod
    def _clean_text(value: Optional[str]) -> str:
        if not value:
            return ""
        return re.sub(r"\s+", " ", value).strip()

    @staticmethod
    def _normalize_url(url: str) -> str:
        url, _fragment = urldefrag(url.strip())
        return url.rstrip("/")

    @staticmethod
    def _slug_username(value: str) -> str:
        value = value.strip().lstrip("@")
        return re.sub(r"[^A-Za-z0-9_.-]", "", value)

    def _safe_goto(self, url: str) -> bool:
        page = self._require_page()
        try:
            response = page.goto(url, wait_until="domcontentloaded")
            try:
                page.wait_for_load_state("networkidle", timeout=5000)
            except PlaywrightTimeoutError:
                # Streaming pages may never reach networkidle; DOM is still usable.
                pass

            if response is not None and response.status >= 400:
                self.log.warning("Navigation returned HTTP %s: %s", response.status, url)
                return False
            return True
        except (PlaywrightError, PlaywrightTimeoutError) as exc:
            self.log.warning("Navigation failed for %s: %s", url, exc)
            return False

    def _page_text(self) -> str:
        page = self._require_page()
        try:
            return self._clean_text(page.locator("body").inner_text(timeout=4000)).lower()
        except PlaywrightError:
            return ""

    def _meta_content(self, selectors: Iterable[str]) -> str:
        page = self._require_page()
        for selector in selectors:
            try:
                locator = page.locator(selector).first
                if locator.count() and locator.is_visible():
                    value = locator.get_attribute("content")
                    if value:
                        return self._clean_text(value)
            except PlaywrightError:
                continue
        return ""

    def _document_title(self) -> str:
        try:
            return self._clean_text(self._require_page().title())
        except PlaywrightError:
            return ""

    def _is_same_site(self, url: str) -> bool:
        try:
            target = urlparse(url)
            base = urlparse(self.base_url)
            return target.netloc.lower() == base.netloc.lower() or target.netloc.lower().endswith(
                "." + base.netloc.lower()
            )
        except Exception:
            return False

    # ----------------------- Profile discovery ----------------------

    def find_profile(self, username: str) -> Optional[dict[str, str]]:
        """
        Try common public profile URL patterns and then search the site's links.

        Returns {"username": ..., "profile_url": ...} or None.
        """
        name = self._slug_username(username)
        if not name:
            self.log.warning("find_profile received an empty/invalid username")
            return None

        candidates = [
            f"{self.base_url}/{name}",
            f"{self.base_url}/user/{name}",
            f"{self.base_url}/users/{name}",
            f"{self.base_url}/profile/{name}",
            f"{self.base_url}/@{name}",
        ]

        for candidate in candidates:
            if not self._safe_goto(candidate):
                continue
            page = self._require_page()
            current_url = self._normalize_url(page.url)
            text = self._page_text()
            title = self._document_title().lower()

            if self._looks_like_profile(name, current_url, title, text):
                return {"username": name, "profile_url": current_url}

        # Fallback: search visible same-site links for the requested username.
        search_urls = [
            f"{self.base_url}/search?q={name}",
            f"{self.base_url}/search/{name}",
        ]
        for search_url in search_urls:
            if not self._safe_goto(search_url):
                continue
            page = self._require_page()
            try:
                anchors = page.locator("a[href]")
                count = min(anchors.count(), 500)
                for i in range(count):
                    anchor = anchors.nth(i)
                    href = anchor.get_attribute("href") or ""
                    label = self._clean_text(anchor.inner_text(timeout=500)).lower()
                    absolute = urljoin(self.base_url + "/", href)
                    if not self._is_same_site(absolute):
                        continue
                    if name.lower() in label or name.lower() in urlparse(absolute).path.lower():
                        if self._normalize_url(absolute) != self._normalize_url(search_url):
                            return {"username": name, "profile_url": self._normalize_url(absolute)}
            except PlaywrightError as exc:
                self.log.debug("Link fallback failed: %s", exc)

        self.log.info("No public profile found for username=%s", name)
        return None

    def _looks_like_profile(
        self, username: str, url: str, title: str, text: str
    ) -> bool:
        path = urlparse(url).path.lower().strip("/")
        user = username.lower()
        path_match = (
            path == user
            or path.endswith("/" + user)
            or path.endswith("/@" + user)
            or f"/user/{user}" in path
            or f"/profile/{user}" in path
        )
        identity_match = user in title or user in text[:1500]
        error_page = any(
            phrase in text[:1500]
            for phrase in ("page not found", "user not found", "profile not found", "404 not found")
        )
        return bool(path_match and identity_match and not error_page)

    # ---------------------- Live detection --------------------------

    def is_live_page(self, page: Optional[Page] = None) -> tuple[bool, list[str]]:
        """
        Decide whether the current page appears to represent a live stream.

        A playing video alone is NOT sufficient. The decision requires at least
        one live-specific signal and no strong offline/replay signal.
        """
        page = page or self._require_page()
        evidence: list[str] = []

        try:
            body_text = self._clean_text(page.locator("body").inner_text(timeout=4000)).lower()
        except PlaywrightError:
            body_text = ""

        title = ""
        try:
            title = self._clean_text(page.title()).lower()
        except PlaywrightError:
            pass

        url = page.url.lower()
        combined = f"{title} {body_text[:12000]}"

        # Strong negative evidence takes priority over generic "live" matches.
        negative_hits = [term for term in self.NON_LIVE_TERMS if term in combined]
        if negative_hits:
            evidence.append("non-live indicators: " + ", ".join(negative_hits[:5]))
            # "not live" / "offline" / "ended" are especially strong.
            strong_negative = ("not live", "offline", "ended", "غير متصل", "انتهى البث")
            if any(term in combined for term in strong_negative):
                return False, evidence

        # URL hints are supportive, not conclusive by themselves.
        if any(token in url for token in ("/live/", "/stream/", "live_id=", "stream_id=")):
            evidence.append("stream-like URL")

        # Check semantically meaningful visible labels, avoiding arbitrary video presence.
        try:
            selectors = [
                '[aria-label*="live" i]',
                '[title*="live" i]',
                '[data-status*="live" i]',
                '[class*="live-badge" i]',
                '[class*="live-status" i]',
                '[class*="is-live" i]',
                '[data-testid*="live" i]',
            ]
            for selector in selectors:
                locator = page.locator(selector)
                count = min(locator.count(), 30)
                for idx in range(count):
                    item = locator.nth(idx)
                    try:
                        if not item.is_visible():
                            continue
                        label = self._clean_text(
                            item.get_attribute("aria-label")
                            or item.get_attribute("title")
                            or item.get_attribute("data-status")
                            or item.inner_text(timeout=400)
                        ).lower()
                        if label and any(term in label for term in self.LIVE_TERMS):
                            evidence.append(f"visible live indicator: {label[:100]}")
                            break
                    except PlaywrightError:
                        continue
                if any(x.startswith("visible live indicator:") for x in evidence):
                    break
        except PlaywrightError:
            pass

        live_text_hits = [term for term in self.LIVE_TERMS if term in combined]
        if live_text_hits:
            evidence.append("live text: " + ", ".join(live_text_hits[:5]))

        # Video presence is recorded only as supporting evidence.
        try:
            videos = page.locator("video")
            video_count = min(videos.count(), 10)
            for i in range(video_count):
                video = videos.nth(i)
                try:
                    if not video.is_visible():
                        continue
                    duration = video.evaluate(
                        "(v) => ({duration: v.duration, readyState: v.readyState, paused: v.paused})"
                    )
                    evidence.append(
                        "visible video element "
                        f"(readyState={duration.get('readyState')}, "
                        f"paused={duration.get('paused')}, duration={duration.get('duration')})"
                    )
                    break
                except PlaywrightError:
                    continue
        except PlaywrightError:
            pass

        # Require a live-specific phrase/indicator. Video or URL alone is insufficient.
        has_live_signal = any(
            item.startswith("visible live indicator:") or item.startswith("live text:")
            for item in evidence
        )
        has_non_live_replay = any(
            term in combined for term in ("replay", "recorded", "past stream", "إعادة", "مسجل")
        )

        if has_non_live_replay and not any(
            phrase in combined for phrase in ("live now", "currently live", "بث مباشر")
        ):
            evidence.append("replay/recorded content takes precedence over generic live wording")
            return False, evidence

        return bool(has_live_signal), evidence

    # ----------------------- Premium detection ----------------------

    def detect_premium(self, page: Optional[Page] = None) -> tuple[bool, list[str]]:
        """
        Detect premium/paid status from contextual page evidence.

        A generic mention of Premium in navigation, ads, or site-wide text is not
        enough. Strong evidence is a visible paywall/subscriber-only message or
        a paid label near the stream/player/profile content.
        """
        page = page or self._require_page()
        evidence: list[str] = []

        try:
            body = self._clean_text(page.locator("body").inner_text(timeout=4000)).lower()
        except PlaywrightError:
            body = ""

        # Inspect visible, semantically relevant elements first.
        selectors = [
            '[class*="premium" i]',
            '[class*="paid" i]',
            '[class*="subscriber" i]',
            '[class*="vip" i]',
            '[data-testid*="premium" i]',
            '[aria-label*="premium" i]',
            '[title*="premium" i]',
        ]
        for selector in selectors:
            try:
                locator = page.locator(selector)
                count = min(locator.count(), 40)
                for i in range(count):
                    item = locator.nth(i)
                    try:
                        if not item.is_visible():
                            continue
                        text = self._clean_text(
                            item.get_attribute("aria-label")
                            or item.get_attribute("title")
                            or item.inner_text(timeout=500)
                        ).lower()
                        if not text:
                            continue
                        if any(term in text for term in self.PREMIUM_TERMS):
                            if not any(ad in text for ad in self.AD_TERMS):
                                evidence.append(f"relevant visible premium element: {text[:140]}")
                    except PlaywrightError:
                        continue
            except PlaywrightError:
                continue

        # Explicit access-denial/payment phrases are strong contextual evidence.
        strong_phrases = (
            "subscribe to watch",
            "subscription required",
            "paid stream",
            "unlock this stream",
            "members only",
            "subscribers only",
            "premium members",
            "pay to watch",
            "اشترك للمشاهدة",
            "المشتركين فقط",
            "بث مدفوع",
        )
        for phrase in strong_phrases:
            if phrase in body:
                evidence.append(f"explicit access/payment message: {phrase}")

        # Avoid false positives from generic site navigation mentioning Premium.
        strong = any(
            item.startswith("explicit access/payment message:")
            or item.startswith("relevant visible premium element:")
            for item in evidence
        )

        # If the only hits are generic terms in the page text, do not label premium.
        if not strong:
            return False, []

        return True, evidence

    # ---------------------- Stream extraction -----------------------

    def _extract_stream_id(self, url: str, page: Optional[Page] = None) -> str:
        page = page or self._require_page()
        patterns = (
            r"[?&](?:stream_id|streamId|live_id|liveId|broadcast_id|broadcastId)=([A-Za-z0-9_-]+)",
            r"/(?:live|stream|broadcast)/([A-Za-z0-9_-]+)",
        )
        for pattern in patterns:
            match = re.search(pattern, url, flags=re.IGNORECASE)
            if match:
                return match.group(1)

        try:
            candidates = page.locator(
                '[data-stream-id], [data-live-id], [data-broadcast-id], '
                '[data-testid*="stream-id" i]'
            )
            for i in range(min(candidates.count(), 20)):
                item = candidates.nth(i)
                for attr in ("data-stream-id", "data-live-id", "data-broadcast-id"):
                    value = item.get_attribute(attr)
                    if value and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value):
                        return value
        except PlaywrightError:
            pass
        return ""

    def _extract_username(self, page: Optional[Page] = None, fallback: str = "") -> str:
        page = page or self._require_page()
        candidates = [
            'meta[property="profile:username"]',
            'meta[name="author"]',
            '[data-username]',
            '[class*="username" i]',
            '[class*="user-name" i]',
            '[data-testid*="username" i]',
        ]
        for selector in candidates:
            try:
                locator = page.locator(selector).first
                if not locator.count():
                    continue
                value = (
                    locator.get_attribute("content")
                    or locator.get_attribute("data-username")
                    or self._clean_text(locator.inner_text(timeout=500))
                )
                value = self._slug_username(value or "")
                if value and len(value) <= 80:
                    return value
            except PlaywrightError:
                continue

        # Last-resort URL-path heuristic; do not claim certainty.
        path_parts = [p for p in urlparse(page.url).path.split("/") if p]
        for part in reversed(path_parts):
            part = part.lstrip("@")
            if part and part.lower() not in {
                "live", "stream", "profile", "user", "users", "watch", "video"
            }:
                candidate = self._slug_username(part)
                if candidate:
                    return candidate
        return self._slug_username(fallback)

    def _extract_title(self, page: Optional[Page] = None) -> str:
        page = page or self._require_page()
        for selector in (
            'meta[property="og:title"]',
            'meta[name="twitter:title"]',
            "h1",
            '[class*="stream-title" i]',
            '[class*="live-title" i]',
        ):
            try:
                locator = page.locator(selector).first
                if not locator.count():
                    continue
                value = locator.get_attribute("content") or self._clean_text(
                    locator.inner_text(timeout=500)
                )
                if value:
                    return self._clean_text(value)[:300]
            except PlaywrightError:
                continue
        return self._document_title()[:300]

    def discover_streams(
        self,
        urls: Optional[Iterable[str]] = None,
        max_links: int = 100,
        same_site_only: bool = True,
    ) -> list[dict[str, Any]]:
        """
        Inspect supplied URLs, or discover stream-like links from the base page.

        Results are normalized dictionaries. A page is marked live only when
        the live detector finds a meaningful live-specific signal.
        """
        page = self._require_page()
        targets: list[str] = []

        if urls:
            for url in urls:
                if not url:
                    continue
                absolute = urljoin(self.base_url + "/", url.strip())
                if same_site_only and not self._is_same_site(absolute):
                    continue
                normalized = self._normalize_url(absolute)
                if normalized not in targets:
                    targets.append(normalized)
        else:
            if not self._safe_goto(self.base_url):
                return []
            selectors = (
                "a[href*='live' i]",
                "a[href*='stream' i]",
                "a[href*='watch' i]",
                "a[href*='profile' i]",
                "a[href*='user' i]",
            )
            seen: set[str] = set()
            for selector in selectors:
                try:
                    anchors = page.locator(selector)
                    for i in range(min(anchors.count(), max_links)):
                        href = anchors.nth(i).get_attribute("href") or ""
                        absolute = self._normalize_url(urljoin(self.base_url + "/", href))
                        if not absolute or absolute in seen:
                            continue
                        if same_site_only and not self._is_same_site(absolute):
                            continue
                        if urlparse(absolute).path in ("", "/"):
                            continue
                        seen.add(absolute)
                        targets.append(absolute)
                        if len(targets) >= max_links:
                            break
                except PlaywrightError:
                    continue
                if len(targets) >= max_links:
                    break

        results: list[dict[str, Any]] = []
        for target in targets[:max_links]:
            if not self._safe_goto(target):
                continue

            is_live, live_evidence = self.is_live_page(page)
            is_premium, premium_evidence = self.detect_premium(page)
            title = self._extract_title(page)
            username = self._extract_username(page)
            stream_id = self._extract_stream_id(page.url, page)

            record = StreamRecord(
                username=username,
                profile_url=page.url if not is_live else "",
                stream_url=page.url,
                stream_id=stream_id,
                is_live=is_live,
                is_premium=is_premium,
                premium_evidence=premium_evidence,
                live_evidence=live_evidence,
                title=title,
            )
            validation = self.validate_stream(record)
            record.validation_ok = validation["ok"]
            record.validation_reason = validation["reason"]
            results.append(record.to_dict())

        return results

    # ----------------------- Stream validation ----------------------

    def validate_stream(self, stream: Any) -> dict[str, Any]:
        """
        Validate a StreamRecord or dictionary.

        This method does not return success unconditionally. It checks required
        fields, URL format/site scope, stream ID format, and live evidence.
        Validation of a record's structure does not guarantee that a stream will
        remain live or that playback is accessible.
        """
        if isinstance(stream, StreamRecord):
            data = stream.to_dict()
        elif isinstance(stream, dict):
            data = stream
        else:
            return {
                "ok": False,
                "reason": "Unsupported stream object; expected StreamRecord or dict.",
                "checks": {},
            }

        checks: dict[str, bool] = {}
        stream_url = str(data.get("stream_url") or "").strip()
        username = self._slug_username(str(data.get("username") or ""))
        stream_id = str(data.get("stream_id") or "").strip()
        is_live = data.get("is_live") is True
        live_evidence = data.get("live_evidence") or []

        parsed = urlparse(stream_url)
        checks["has_stream_url"] = bool(stream_url)
        checks["valid_http_scheme"] = parsed.scheme in ("http", "https")
        checks["has_hostname"] = bool(parsed.hostname)
        checks["same_site"] = self._is_same_site(stream_url) if stream_url else False
        checks["username_present"] = bool(username)
        checks["stream_id_format"] = bool(
            stream_id and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", stream_id)
        )
        checks["live_flag_true"] = is_live
        checks["live_evidence_present"] = bool(live_evidence)

        # URL + username are required for a useful normalized record.
        core_ok = (
            checks["has_stream_url"]
            and checks["valid_http_scheme"]
            and checks["has_hostname"]
            and checks["same_site"]
            and checks["username_present"]
        )

        if not core_ok:
            failed = [name for name, passed in checks.items() if not passed]
            return {
                "ok": False,
                "reason": "Failed required validation checks: " + ", ".join(failed),
                "checks": checks,
            }

        if is_live and not checks["live_evidence_present"]:
            return {
                "ok": False,
                "reason": "Record claims live status but contains no supporting live evidence.",
                "checks": checks,
            }

        if not is_live:
            return {
                "ok": False,
                "reason": "Page/record did not provide sufficient evidence that the stream is live.",
                "checks": checks,
            }

        return {
            "ok": True,
            "reason": "Required fields and live evidence passed validation.",
            "checks": checks,
        }

    # ---------------------- Convenience API -------------------------

    def inspect_url(self, url: str, fallback_username: str = "") -> dict[str, Any]:
        """Inspect one URL and return a normalized record plus validation result."""
        absolute = urljoin(self.base_url + "/", url.strip())
        if not self._is_same_site(absolute):
            return {
                "ok": False,
                "reason": "URL is outside the configured site.",
                "url": absolute,
            }

        if not self._safe_goto(absolute):
            return {
                "ok": False,
                "reason": "Could not load the public page.",
                "url": absolute,
            }

        page = self._require_page()
        is_live, live_evidence = self.is_live_page(page)
        is_premium, premium_evidence = self.detect_premium(page)
        record = StreamRecord(
            username=self._extract_username(page, fallback_username),
            profile_url=page.url if not is_live else "",
            stream_url=page.url,
            stream_id=self._extract_stream_id(page.url, page),
            is_live=is_live,
            is_premium=is_premium,
            premium_evidence=premium_evidence,
            live_evidence=live_evidence,
            title=self._extract_title(page),
        )
        validation = self.validate_stream(record)
        record.validation_ok = validation["ok"]
        record.validation_reason = validation["reason"]
        return {
            "ok": validation["ok"],
            "reason": validation["reason"],
            "checks": validation["checks"],
            "stream": record.to_dict(),
        }

    def find_live_streams_for_user(self, username: str) -> list[dict[str, Any]]:
        """Find a public profile and inspect its stream-like links."""
        profile = self.find_profile(username)
        if not profile:
            return []

        page = self._require_page()
        if not self._safe_goto(profile["profile_url"]):
            return []

        urls: list[str] = []
        try:
            anchors = page.locator("a[href]")
            for i in range(min(anchors.count(), 300)):
                href = anchors.nth(i).get_attribute("href") or ""
                absolute = self._normalize_url(urljoin(page.url, href))
                path = urlparse(absolute).path.lower()
                if self._is_same_site(absolute) and any(
                    token in path for token in ("/live", "/stream", "/watch", "/broadcast")
                ):
                    if absolute not in urls:
                        urls.append(absolute)
        except PlaywrightError as exc:
            self.log.debug("Could not inspect profile links: %s", exc)

        # Inspect the profile itself as well; it may be the live page.
        urls.insert(0, profile["profile_url"])
        return self.discover_streams(urls=urls, max_links=100)


def _configure_logging(verbose: bool = False) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )


def main() -> int:
    """
    Minimal CLI example:
      python super_live_discovery.py username
      python super_live_discovery.py --url https://superlivetv.com/some-page
    """
    import argparse

    parser = argparse.ArgumentParser(description="Discover publicly visible SuperLiveTV streams.")
    parser.add_argument("username", nargs="?", help="Public username to inspect")
    parser.add_argument("--url", help="Inspect one public page URL")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help="Site base URL")
    parser.add_argument("--headed", action="store_true", help="Show the browser window")
    parser.add_argument("--verbose", action="store_true", help="Enable debug logging")
    args = parser.parse_args()
    _configure_logging(args.verbose)

    if not args.username and not args.url:
        parser.error("Provide a username or --url.")

    try:
        with SuperLiveDiscovery(base_url=args.base_url, headless=not args.headed) as discovery:
            if args.url:
                output = discovery.inspect_url(args.url, args.username or "")
            else:
                output = discovery.find_live_streams_for_user(args.username)
        print(json.dumps(output, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        LOG.exception("Unexpected failure: %s", exc)
        print(json.dumps({"ok": False, "reason": str(exc)}, ensure_ascii=False, indent=2))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
