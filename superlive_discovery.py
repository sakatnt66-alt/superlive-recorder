"""SuperLiveDiscovery 11.0 - async-first defensive public-page discovery.

Designed for callers that use ``await discovery.discover_profile_id(user_id)``.
Requires: pip install playwright && playwright install chromium

This module only inspects publicly accessible pages. It does not bypass login,
paywalls, access controls, or DRM. Website URL patterns and markup may change.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Optional, Iterable
from urllib.parse import urljoin, urlparse, urldefrag, quote

try:
    from playwright.async_api import (
        async_playwright, Browser, BrowserContext, Page,
        Error as PlaywrightError, TimeoutError as PlaywrightTimeoutError,
    )
except ImportError as exc:  # clear actionable message rather than obscure import failure
    raise ImportError(
        "SuperLiveDiscovery requires Playwright. Install with: pip install playwright; "
        "then run: playwright install chromium"
    ) from exc

VERSION = "11.0"
DEFAULT_BASE_URL = "https://superlivetv.com"
LOG = logging.getLogger("SuperLiveDiscovery")


@dataclass
class StreamRecord:
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
    validation_reason: str = "not validated"
    discovered_at: float = field(default_factory=time.time)
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class SuperLiveDiscovery:
    """Async Playwright discovery class. Public methods are awaitable."""

    LIVE_TERMS = ("live now", "is live", "live stream", "watch live", "currently live", "مباشر", "بث مباشر")
    NON_LIVE_TERMS = ("replay", "recorded", "offline", "not live", "ended", "past stream", "إعادة", "مسجل", "غير متصل", "انتهى البث")
    PREMIUM_TERMS = ("premium", "paid stream", "subscriber only", "subscribers only", "members only", "exclusive stream", "vip", "مدفوع", "للمشتركين", "حصري")
    ERROR_TERMS = ("page not found", "user not found", "profile not found", "404 not found", "account does not exist")

    def __init__(self, base_url: str = DEFAULT_BASE_URL, headless: bool = True,
                 timeout_ms: int = 15000, navigation_timeout_ms: int = 30000,
                 user_agent: Optional[str] = None, logger: Optional[logging.Logger] = None,
                 retries: int = 2, **kwargs: Any) -> None:
        # Accept benign legacy keyword arguments without crashing; keep behavior explicit.
        self.base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
        self.headless = headless
        self.timeout_ms = max(1000, int(timeout_ms))
        self.navigation_timeout_ms = max(self.timeout_ms, int(navigation_timeout_ms))
        self.user_agent = user_agent
        self.log = logger or LOG
        self.retries = max(0, min(int(retries), 5))
        self._playwright = None
        self.browser: Optional[Browser] = None
        self.context: Optional[BrowserContext] = None
        self.page: Optional[Page] = None
        self._start_lock = asyncio.Lock()

    async def open(self) -> "SuperLiveDiscovery":
        async with self._start_lock:
            if self.page is not None and not self.page.is_closed():
                return self
            self._playwright = await async_playwright().start()
            try:
                self.browser = await self._playwright.chromium.launch(headless=self.headless)
                opts: dict[str, Any] = {"java_script_enabled": True, "ignore_https_errors": False}
                if self.user_agent:
                    opts["user_agent"] = self.user_agent
                self.context = await self.browser.new_context(**opts)
                self.page = await self.context.new_page()
                self.page.set_default_timeout(self.timeout_ms)
                self.page.set_default_navigation_timeout(self.navigation_timeout_ms)
            except Exception:
                await self.close()
                raise
        return self

    async def close(self) -> None:
        for attr in ("page", "context", "browser"):
            obj = getattr(self, attr, None)
            if obj is not None:
                try:
                    await obj.close()
                except Exception as exc:
                    self.log.debug("Cleanup %s: %s", attr, exc)
                setattr(self, attr, None)
        if self._playwright is not None:
            try:
                await self._playwright.stop()
            except Exception as exc:
                self.log.debug("Cleanup Playwright: %s", exc)
            self._playwright = None

    async def __aenter__(self) -> "SuperLiveDiscovery":
        return await self.open()

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()

    async def _page(self) -> Page:
        if self.page is None or self.page.is_closed():
            await self.open()
        assert self.page is not None
        return self.page

    @staticmethod
    def _clean(value: Optional[str]) -> str:
        return re.sub(r"\s+", " ", value or "").strip()

    @staticmethod
    def _slug(value: Any) -> str:
        raw = str(value or "").strip().lstrip("@").strip()
        return re.sub(r"[^A-Za-z0-9_.-]", "", raw)[:128]

    @staticmethod
    def _norm_url(url: str) -> str:
        clean, _ = urldefrag((url or "").strip())
        return clean.rstrip("/")

    def _same_site(self, url: str) -> bool:
        try:
            a, b = urlparse(self.base_url), urlparse(url)
            return bool(a.hostname and b.hostname and (b.hostname.lower() == a.hostname.lower() or b.hostname.lower().endswith("." + a.hostname.lower())))
        except Exception:
            return False

    async def _goto(self, url: str) -> bool:
        page = await self._page()
        for attempt in range(self.retries + 1):
            try:
                response = await page.goto(url, wait_until="domcontentloaded", timeout=self.navigation_timeout_ms)
                # HTTP errors are not automatically fatal: some sites render useful challenge/error pages.
                if response is not None and response.status >= 500:
                    self.log.warning("HTTP %s while opening %s", response.status, url)
                try:
                    await page.wait_for_load_state("networkidle", timeout=min(2500, self.timeout_ms))
                except PlaywrightTimeoutError:
                    pass
                return response is None or response.status < 500
            except PlaywrightTimeoutError:
                self.log.debug("Navigation timeout (%d/%d): %s", attempt + 1, self.retries + 1, url)
            except PlaywrightError as exc:
                self.log.debug("Navigation error (%d/%d) %s: %s", attempt + 1, self.retries + 1, url, exc)
            if attempt < self.retries:
                await asyncio.sleep(min(0.4 * (attempt + 1), 1.2))
        return False

    async def _text(self, page: Optional[Page] = None) -> str:
        page = page or await self._page()
        try:
            return self._clean(await page.locator("body").inner_text(timeout=2000))[:50000]
        except PlaywrightError:
            return ""

    async def _title(self, page: Optional[Page] = None) -> str:
        page = page or await self._page()
        try:
            return self._clean(await page.title())[:300]
        except PlaywrightError:
            return ""

    async def _looks_profile(self, username: str, url: str, title: str, text: str) -> bool:
        path = urlparse(url).path.lower().strip("/")
        user = username.lower()
        path_match = (path == user or path.endswith("/" + user) or path.endswith("/@" + user)
                      or f"/user/{user}" in path or f"/users/{user}" in path or f"/profile/{user}" in path)
        excerpt = (title + " " + text[:2500]).lower()
        identity_match = user in excerpt
        error = any(term in text[:3000].lower() for term in self.ERROR_TERMS)
        return bool(path_match and identity_match and not error)

    async def discover_profile_id(self, user_id: Any) -> dict[str, Any]:
        """Resolve a username/user identifier to a public profile.

        Returns a stable dictionary with success, user_id, profile_id, username,
        profile_url, and error. profile_id is extracted from public page metadata
        when possible; otherwise the normalized username is used only as an
        identifier fallback and is marked with profile_id_source='username'.
        """
        raw = str(user_id or "").strip()
        username = self._slug(raw)
        result: dict[str, Any] = {
            "success": False, "ok": False, "user_id": raw, "profile_id": None,
            "username": username, "profile_url": None, "profile_id_source": None,
            "error": None,
        }
        if not username:
            result["error"] = "empty_or_invalid_user_id"
            return result

        candidates = [
            f"{self.base_url}/{quote(username)}",
            f"{self.base_url}/user/{quote(username)}",
            f"{self.base_url}/users/{quote(username)}",
            f"{self.base_url}/profile/{quote(username)}",
            f"{self.base_url}/@{quote(username)}",
        ]
        # Also allow callers to pass a URL, but only visit same-site URLs.
        if raw.startswith(("https://", "http://")):
            if self._same_site(raw):
                candidates.insert(0, raw)
            else:
                result["error"] = "external_url_rejected"
                return result

        page = await self._page()
        seen: set[str] = set()
        for candidate in candidates:
            candidate = self._norm_url(candidate)
            if candidate in seen:
                continue
            seen.add(candidate)
            if not await self._goto(candidate):
                continue
            current = self._norm_url(page.url)
            text = await self._text(page)
            title = await self._title(page)
            if not await self._looks_profile(username, current, title, text):
                continue
            profile_id = await self._extract_profile_id(page, username)
            result.update({"success": True, "ok": True, "profile_id": profile_id,
                           "username": username, "profile_url": current,
                           "profile_id_source": "page_metadata" if profile_id != username else "username",
                           "error": None})
            return result

        # Last-chance public search pages; never accept arbitrary external links.
        for search_url in (f"{self.base_url}/search?q={quote(username)}", f"{self.base_url}/search/{quote(username)}"):
            if not await self._goto(search_url):
                continue
            try:
                links = await page.locator("a[href]").evaluate_all("els => els.slice(0,500).map(a => ({href:a.href, text:(a.innerText||a.textContent||'').trim()}))")
            except PlaywrightError:
                continue
            for item in links:
                href = self._norm_url(str(item.get("href", "")))
                label = str(item.get("text", "")).lower()
                if href and self._same_site(href) and username.lower() in (label + " " + urlparse(href).path.lower()):
                    if await self._goto(href):
                        current = self._norm_url(page.url)
                        text = await self._text(page)
                        title = await self._title(page)
                        if await self._looks_profile(username, current, title, text):
                            profile_id = await self._extract_profile_id(page, username)
                            result.update({"success": True, "ok": True, "profile_id": profile_id,
                                           "profile_url": current, "profile_id_source": "page_metadata" if profile_id != username else "username", "error": None})
                            return result
        result["error"] = "public_profile_not_found_or_site_structure_changed"
        return result

    async def _extract_profile_id(self, page: Page, fallback: str) -> str:
        selectors = ["meta[name='profile:id']", "meta[property='profile:id']", "meta[name='user:id']", "[data-profile-id]", "[data-user-id]"]
        for selector in selectors:
            try:
                loc = page.locator(selector).first
                if await loc.count():
                    val = await loc.get_attribute("content") or await loc.get_attribute("data-profile-id") or await loc.get_attribute("data-user-id")
                    if val and re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", val.strip()):
                        return val.strip()
            except PlaywrightError:
                continue
        # Common public embedded JSON patterns; deliberately avoid parsing arbitrary scripts as executable code.
        try:
            html = (await page.content())[:1_500_000]
            patterns = [r'"profileId"\s*:\s*"?([A-Za-z0-9_.-]{1,128})', r'"userId"\s*:\s*"?([A-Za-z0-9_.-]{1,128})', r'"profile_id"\s*:\s*"?([A-Za-z0-9_.-]{1,128})']
            for pattern in patterns:
                match = re.search(pattern, html, re.I)
                if match and match.group(1).lower() not in {"null", "undefined"}:
                    return match.group(1)
        except PlaywrightError:
            pass
        return fallback

    # Compatibility alias for code using the older method name.
    async def find_profile(self, username: Any) -> Optional[dict[str, Any]]:
        result = await self.discover_profile_id(username)
        if not result["success"]:
            return None
        return {"username": result["username"], "profile_url": result["profile_url"],
                "profile_id": result["profile_id"], "success": True, "ok": True}

    async def is_live_page(self, page: Optional[Page] = None) -> tuple[bool, list[str]]:
        page = page or await self._page()
        text = (await self._text(page)).lower()
        title = (await self._title(page)).lower()
        combined = title + " " + text
        negative = [x for x in self.NON_LIVE_TERMS if x in combined]
        positive = [x for x in self.LIVE_TERMS if x in combined]
        try:
            video_count = await page.locator("video").count()
            if video_count:
                positive.append("video_element_present")
        except PlaywrightError:
            pass
        # Require explicit live evidence; a video element alone is not proof of live status.
        explicit = [x for x in positive if x != "video_element_present"]
        if negative and not any(x in combined for x in ("live now", "currently live", "بث مباشر")):
            return False, ["negative_status:" + x for x in negative]
        return bool(explicit), explicit or negative

    async def detect_premium(self, page: Optional[Page] = None) -> tuple[bool, list[str]]:
        page = page or await self._page()
        text = (await self._text(page)).lower()
        evidence = [term for term in self.PREMIUM_TERMS if term in text]
        return bool(evidence), evidence

    async def inspect_url(self, url: str, fallback_username: str = "") -> dict[str, Any]:
        if not self._same_site(url):
            return {"success": False, "ok": False, "error": "external_url_rejected", "stream_url": url}
        if not await self._goto(url):
            return {"success": False, "ok": False, "error": "navigation_failed", "stream_url": url}
        page = await self._page()
        live, live_evidence = await self.is_live_page(page)
        premium, premium_evidence = await self.detect_premium(page)
        title = await self._title(page)
        text = await self._text(page)
        username = fallback_username or self._slug(title.split("|")[0].split("-")[0])
        path = urlparse(page.url).path.strip("/").split("/")
        stream_id = next((p for p in reversed(path) if p and re.fullmatch(r"[A-Za-z0-9_-]{3,128}", p)), "")
        record = StreamRecord(username=username, profile_url=page.url, stream_url=page.url,
                              stream_id=stream_id, is_live=live, is_premium=premium,
                              premium_evidence=premium_evidence, live_evidence=live_evidence,
                              title=title, validation_ok=bool(page.url and self._same_site(page.url) and (title or text)),
                              validation_reason="public page loaded" if title or text else "page has no readable content")
        data = record.to_dict()
        data.update({"success": record.validation_ok, "ok": record.validation_ok, "error": None if record.validation_ok else "empty_page"})
        return data

    async def discover_streams(self, urls: Optional[Iterable[str]] = None, max_links: int = 100,
                               same_site_only: bool = True) -> list[dict[str, Any]]:
        page = await self._page()
        targets: list[str] = []
        if urls:
            for item in urls:
                absolute = self._norm_url(urljoin(self.base_url + "/", str(item or "").strip()))
                if absolute and (not same_site_only or self._same_site(absolute)) and absolute not in targets:
                    targets.append(absolute)
        else:
            if not await self._goto(self.base_url):
                return []
            try:
                links = await page.locator("a[href]").evaluate_all("els => els.slice(0,1000).map(a => a.href)")
            except PlaywrightError:
                links = []
            for href in links:
                absolute = self._norm_url(urljoin(self.base_url + "/", str(href)))
                path = urlparse(absolute).path.lower()
                if (absolute and (not same_site_only or self._same_site(absolute)) and path not in ("", "/")
                    and any(term in path for term in ("live", "stream", "watch", "profile", "user")) and absolute not in targets):
                    targets.append(absolute)
                if len(targets) >= max(1, min(int(max_links), 500)):
                    break
        results = []
        for target in targets[:max(1, min(int(max_links), 500))]:
            try:
                result = await self.inspect_url(target)
                if result.get("success"):
                    results.append(result)
            except Exception as exc:
                self.log.warning("Could not inspect %s: %s", target, exc)
        return results

    async def find_live_streams_for_user(self, username: str) -> list[dict[str, Any]]:
        profile = await self.discover_profile_id(username)
        if not profile.get("success"):
            return []
        inspected = await self.inspect_url(profile["profile_url"], fallback_username=profile["username"])
        return [inspected] if inspected.get("success") and inspected.get("is_live") else []

    async def validate_stream(self, stream: Any) -> dict[str, Any]:
        if isinstance(stream, StreamRecord):
            data = stream.to_dict()
        elif isinstance(stream, dict):
            data = dict(stream)
        else:
            return {"ok": False, "success": False, "reason": "unsupported_stream_record_type"}
        url = str(data.get("stream_url") or data.get("profile_url") or "").strip()
        if not url:
            return {"ok": False, "success": False, "reason": "missing_stream_url"}
        if not self._same_site(url):
            return {"ok": False, "success": False, "reason": "external_url_rejected"}
        return {"ok": bool(data.get("title") or data.get("username") or data.get("stream_id")),
                "success": bool(data.get("title") or data.get("username") or data.get("stream_id")),
                "reason": "record_has_identifying_fields" if (data.get("title") or data.get("username") or data.get("stream_id")) else "missing_identifying_fields"}

    # Explicitly unsupported sync context managers help surface migration errors clearly.
    def __enter__(self):
        raise RuntimeError("SuperLiveDiscovery v11 is async. Use 'async with SuperLiveDiscovery() as discovery:'")

    def __exit__(self, *args):
        return None
