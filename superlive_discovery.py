"""SuperLiveDiscovery 11.4 - async-first defensive public-page discovery.
Designed for callers that use ``await discovery.discover_profile_id(user_id)``.
Requires: pip install playwright && playwright install chromium
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
except ImportError as exc:
    raise ImportError(
        "SuperLiveDiscovery requires Playwright. Install with: pip install playwright; "
        "then run: playwright install chromium"
    ) from exc

VERSION = "11.4"
DEFAULT_BASE_URL = "https://superlivetv.com"
DEFAULT_LOCALE = "fr"
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
    LIVE_TERMS = ("live now", "is live", "live stream", "watch live", "currently live", "مباشر", "بث مباشر", "en direct", "direct", "en vivo", "vivo")
    NON_LIVE_TERMS = ("replay", "recorded", "offline", "not live", "ended", "past stream", "إعادة", "مسجل", "غير متصل", "انتهى البث", "hors ligne", "terminé", "rediffusion", "passé")
    PREMIUM_TERMS = ("premium", "paid stream", "subscriber only", "subscribers only", "members only", "exclusive stream", "vip", "مدفوع", "للمشتركين", "حصري", "privé", "exclusive", "abonnés", "réservé")
    ERROR_TERMS = ("page not found", "user not found", "profile not found", "404 not found", "account does not exist")

    def __init__(self, base_url: str = DEFAULT_BASE_URL, headless: bool = True,
                 timeout_ms: int = 6000, navigation_timeout_ms: int = 9000,
                 user_agent: Optional[str] = None, logger: Optional[logging.Logger] = None,
                 retries: int = 0, **kwargs: Any) -> None:
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
        self._operation_lock = asyncio.Lock()

    async def open(self) -> "SuperLiveDiscovery":
        async with self._start_lock:
            if self.page is not None and not self.page.is_closed():
                return self
            self._playwright = await async_playwright().start()
            try:
                self.browser = await self._playwright.chromium.launch(headless=self.headless)
                opts: dict[str, Any] = {
                    "java_script_enabled": True,
                    "ignore_https_errors": False,
                    "viewport": {"width": 430, "height": 932},
                }
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
                if response is not None and response.status >= 400:
                    self.log.debug("HTTP %s while opening %s", response.status, url)
                    return False
                try:
                    await page.wait_for_load_state("networkidle", timeout=min(1500, self.timeout_ms))
                except PlaywrightTimeoutError:
                    pass
                return True
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
        parsed = urlparse(url)
        path = parsed.path.lower().rstrip("/")
        user = str(username or "").lower()
        segments = [segment for segment in path.split("/") if segment]
        route_match = bool(
            len(segments) >= 3
            and segments[-2] == "profile"
            and segments[-1] == user
        )
        legacy_match = bool(
            path == user or path.endswith("/" + user) or path.endswith("/@" + user)
            or f"/user/{user}" in path or f"/users/{user}" in path
        )
        excerpt = (title + " " + text[:3500]).lower()
        error = any(term in excerpt for term in self.ERROR_TERMS)
        has_content = bool(title.strip() or text.strip())
        return bool((route_match or legacy_match) and not error and has_content)

    @staticmethod
    def _is_public_profile_route(url: str) -> bool:
        path = urlparse(url).path.lower().rstrip("/")
        segments = [segment for segment in path.split("/") if segment]
        if len(segments) >= 3 and segments[-2] == "profile":
            return True
        reserved = {"discover", "search", "livestream", "login", "signup", "register", "settings", "notifications"}
        return len(segments) == 2 and segments[0] in {"fr", "en", "ar"} and segments[1] not in reserved

    async def discover_profile_id(self, user_id: Any) -> dict[str, Any]:
        async with self._operation_lock:
            return await self._discover_profile_id_unlocked(user_id)

    async def _discover_profile_id_unlocked(self, user_id: Any) -> dict[str, Any]:
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

        if raw.startswith(("https://", "http://")):
            if not self._same_site(raw):
                result["error"] = "external_url_rejected"
                return result
            candidates = [raw]
        else:
            encoded = quote(username, safe="")
            if username.isdigit():
                candidates = []
            else:
                candidates = [f"{self.base_url}/fr/{encoded}"]

        page = await self._page()
        seen: set[str] = set()
        for candidate in candidates:
            candidate = self._norm_url(candidate)
            if candidate in seen or not self._same_site(candidate):
                continue
            seen.add(candidate)
            if not await self._goto(candidate):
                continue
            current = self._norm_url(page.url)
            if not self._same_site(current):
                continue
            current_path = urlparse(current).path.lower().rstrip("/")
            requested_path = urlparse(candidate).path.lower().rstrip("/")
            text = await self._text(page)
            title = await self._title(page)
            if current_path != requested_path and not await self._looks_profile(username, current, title, text):
                continue
            if not await self._looks_profile(username, current, title, text):
                continue
            profile_id = await self._extract_profile_id(page, username)
            result.update({
                "success": True, "ok": True, "profile_id": profile_id,
                "username": username, "profile_url": current,
                "profile_id_source": "page_metadata" if profile_id != username else "url_or_input",
                "error": None,
            })
            return result

        search_urls = [
            f"{self.base_url}/fr/search",
            f"{self.base_url}/fr/search?q={quote(username, safe='')}",
            f"{self.base_url}/en/search",
        ]
        inspected: set[str] = set()
        search_control_found = False
        search_action_attempted = False
        verified_results_seen = False

        for search_index, search_url in enumerate(search_urls, start=1):
            self.log.info("Search attempt %d/%d for key %s: %s", search_index, len(search_urls), raw, search_url)
            if not await self._goto(search_url):
                continue
            if urlparse(search_url).path.rstrip("/").endswith("/search") and not urlparse(search_url).query:
                selectors = (
                    "input[type=search]", "input[name=search]", "input[name=query]",
                    "input[name=q]", "input[name=keyword]", "input[name=term]",
                    "input[placeholder*='search' i]", "input[placeholder*='recherch' i]",
                    "input[placeholder*='utilisateur' i]", "input[placeholder*='ID' i]",
                    "input[aria-label*='search' i]", "input[aria-label*='recherch' i]",
                    "input[type=text]",
                )
                for selector in selectors:
                    try:
                        fields = page.locator(selector)
                        count = min(await fields.count(), 8)
                        chosen = None
                        visible_count = 0
                        for field_index in range(count):
                            field = fields.nth(field_index)
                            try:
                                visible = await field.is_visible()
                                enabled = await field.is_enabled()
                                input_type = (await field.get_attribute("type") or "text").lower()
                            except PlaywrightError:
                                continue
                            if visible:
                                visible_count += 1
                                if visible and enabled and input_type not in {"hidden", "checkbox", "radio", "password", "email"}:
                                    chosen = field
                                    break
                        if chosen is None:
                            continue
                        search_control_found = True
                        await chosen.fill(raw)
                        search_action_attempted = True
                        try:
                            await chosen.press("Enter", timeout=1200)
                        except PlaywrightError:
                            pass
                        try:
                            await page.wait_for_load_state("domcontentloaded", timeout=1200)
                        except PlaywrightTimeoutError:
                            pass
                        await page.wait_for_timeout(450)
                        if urlparse(page.url).path.rstrip("/").endswith("/search") and not urlparse(page.url).query:
                            try:
                                form = chosen.locator("xpath=ancestor::form[1]")
                                submit = form.locator("button[type=submit], input[type=submit], button:not([type])").first
                                if await submit.count() and await submit.is_visible() and await submit.is_enabled():
                                    await submit.click(timeout=1200)
                                    try:
                                        await page.wait_for_load_state("domcontentloaded", timeout=1200)
                                    except PlaywrightTimeoutError:
                                        pass
                                    await page.wait_for_timeout(450)
                            except PlaywrightError:
                                pass
                        self.log.info("Search form action attempted for key %s using selector %s; url=%s", raw, selector, page.url)
                        break
                    except PlaywrightError:
                        continue

            try:
                links = await page.locator("a[href]").evaluate_all("""els => els.slice(0,2000).map(a => {
                    let n = a, card = '';
                    for (let i=0; i<5 && n; i++, n=n.parentElement) {
                        const t = (n.innerText || n.textContent || '').trim();
                        if (t.length >= 2 && t.length <= 1200) { card = t; if (i >= 1) break; }
                    }
                    return {href:a.href, text:(a.innerText||a.textContent||'').trim(), card};
                })""")
                profile_link_count = sum(1 for link in links if self._same_site(str(link.get("href", ""))) and self._is_public_profile_route(str(link.get("href", ""))))
                page_text = (await self._text(page))[:12000]
                normalized_page_text = self._clean(page_text).casefold()
                normalized_query = self._clean(raw).casefold()
                results_heading = bool(
                    normalized_query
                    and normalized_query in normalized_page_text
                    and re.search(r"(?:résultats?\s+pour|resultats?\s+pour|results?\s+for|search\s+results?)", normalized_page_text, re.I)
                )
                verified_results_seen = verified_results_seen or results_heading
                self.log.info(
                    "Search results for key %s: control_found=%s action_attempted=%s results_heading=%s anchors=%d profile_links=%d url=%s",
                    raw, search_control_found, search_action_attempted, results_heading,
                    len(links), profile_link_count, page.url,
                )
            except PlaywrightError as exc:
                self.log.warning("Could not read search results for key %s: %s", raw, type(exc).__name__)
                continue

            if not results_heading:
                self.log.warning(
                    "Search results not verified for key %s (control_found=%s action_attempted=%s); refusing candidate links",
                    raw, search_control_found, search_action_attempted,
                )
                continue

            for item in links:
                href = str(item.get("href", "")).strip()
                label = self._clean(str(item.get("text", ""))).lower()
                card_text = self._clean(str(item.get("card", "")))
                if not href or not self._same_site(href):
                    continue
                href = self._norm_url(href)
                if href in inspected or not self._is_public_profile_route(href):
                    continue
                inspected.add(href)
                candidate_path = urlparse(href).path.lower().rstrip("/")
                candidate_slug = candidate_path.rsplit("/", 1)[-1]
                if candidate_slug in {"profile", "search", "discover", "login", "signup"}:
                    continue
                card_lower = card_text.lower()
                result_card_evidence = bool(
                    card_text and len(card_text) > len(label)
                    and (username.lower() in card_lower or username.lower() in label or results_heading)
                )
                unique_verified_result = bool(results_heading and profile_link_count == 1)
                direct_identity_match = (
                    candidate_slug == username.lower()
                    or username.lower() in label
                    or username.lower() in card_lower
                    or (results_heading and result_card_evidence)
                    or unique_verified_result
                )
                if not direct_identity_match:
                    continue
                if not await self._goto(href):
                    continue
                current = self._norm_url(page.url)
                text = await self._text(page)
                title = await self._title(page)
                excerpt = (title + " " + text[:3500]).lower()
                if any(term in excerpt for term in self.ERROR_TERMS):
                    continue
                if not self._is_public_profile_route(current) or not (title or text):
                    continue
                profile_id = await self._extract_profile_id(page, "")
                match = re.search(r"/profile/([^/]+)$", urlparse(current).path, re.I)
                route_profile_id = match.group(1) if match else candidate_slug
                if not profile_id:
                    profile_id = route_profile_id
                self.log.info(
                    "Resolved watchlist key %s to profile %s via site search (%s)",
                    raw, profile_id, current,
                )
                result.update({
                    "success": True, "ok": True,
                    "profile_id": profile_id,
                    "username": candidate_slug,
                    "profile_url": current,
                    "profile_id_source": "site_search_result",
                    "input_user_id": raw,
                    "error": None,
                })
                return result

        if not verified_results_seen and not search_control_found:
            result["error"] = "SEARCH_CONTROL_NOT_FOUND"
        elif not verified_results_seen and not search_action_attempted:
            result["error"] = "SEARCH_NOT_SUBMITTED"
        elif not verified_results_seen:
            result["error"] = "SEARCH_RESULTS_NOT_VERIFIED"
        else:
            result["error"] = "NO_MATCHING_PROFILE"
        self.log.warning(
            "Profile resolution failed for key %s after %d search routes: %s",
            raw, len(search_urls), result["error"],
        )
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
        explicit = [x for x in positive if x != "video_element_present"]
        if negative and not any(x in combined for x in ("live now", "currently live", "بث مباشر", "en direct", "direct")):
            return False, ["negative_status:" + x for x in negative]
        return bool(explicit), explicit or negative

    async def detect_premium(self, page: Optional[Page] = None) -> tuple[bool, list[str]]:
        page = page or await self._page()
        text = (await self._text(page)).lower()
        evidence = [term for term in self.PREMIUM_TERMS if term in text]
        return bool(evidence), evidence

    async def _extract_display_name(self, page: Page, fallback: str, intercepted_data: list) -> str:
        """Extract real display name from HTML or intercepted JSON."""
        name = ""
        # 1. Meta tags
        try:
            og_title = await page.locator("meta[property='og:title']").get_attribute("content")
            if og_title and len(og_title) < 100:
                name = og_title.split("-")[0].split("|")[0].strip()
                if name and name.lower() not in ["superlive", "superlivetv", "profile"]:
                    return name
        except: pass
        
        # 2. Title tag
        try:
            title = await page.title()
            if title:
                name = title.split("-")[0].split("|")[0].strip()
                if name and name.lower() not in ["superlive", "superlivetv", "profile"]:
                    return name
        except: pass

        # 3. H1 or specific classes
        try:
            for sel in ["h1", ".username", ".profile-name", ".nickname", "[class*='name' i]"]:
                el = page.locator(sel).first
                if await el.count():
                    text = await el.inner_text()
                    text = text.strip()
                    if text and len(text) < 50 and text.lower() not in ["superlive", "follow", "subscribe"]:
                        return text
        except: pass
        
        # 4. Intercepted JSON
        for body in intercepted_data:
            m = re.search(r'["\'](?:nickname|displayName|display_name|name|username)["\']\s*:\s*["\']([^"\']{2,50})["\']', body)
            if m:
                val = m.group(1).strip()
                if val and val.lower() not in ["superlive", "null", "undefined", "user", fallback.lower()]:
                    return val
                    
        return fallback

    async def check_live_status(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        profile_url = str(kwargs.get("profile_url") or kwargs.get("url") or kwargs.get("profile") or "").strip()
        user_id = str(kwargs.get("user_id") or kwargs.get("username") or kwargs.get("profile_id") or "").strip()
        profile_id = str(kwargs.get("profile_id") or "").strip()
        
        for value in args:
            if not isinstance(value, (str, int)):
                continue
            text = str(value).strip()
            if text.startswith(("https://", "http://")) and not profile_url:
                profile_url = text
            elif text and not user_id:
                user_id = text

        if not profile_url and user_id:
            resolved = await self.discover_profile_id(user_id)
            profile_url = str(resolved.get("profile_url") or "")
            if not profile_url:
                return {
                    "success": False, "ok": False, "user_id": user_id,
                    "profile_url": None, "is_live": False, "is_premium": False,
                    "stream_url": None, "stream_id": None, "ownership_verified": False,
                    "error": resolved.get("error") or "profile_not_found",
                    "live_evidence": [], "premium_evidence": [],
                }

        if not profile_url or not self._same_site(profile_url):
            return {
                "success": False, "ok": False, "user_id": user_id or None,
                "profile_url": profile_url or None, "is_live": False,
                "is_premium": False, "stream_url": None, "stream_id": None, "ownership_verified": False,
                "error": "missing_or_external_profile_url",
                "live_evidence": [], "premium_evidence": [],
            }

        async with self._operation_lock:
            intercepted_data = []
            
            async def on_response(response):
                try:
                    if response.status == 200:
                        ct = response.headers.get("content-type", "")
                        if any(t in ct for t in ["json", "javascript", "text/plain", "html"]):
                            cl = response.headers.get("content-length", "0")
                            if cl.isdigit() and int(cl) < 2000000:
                                body = await response.text()
                                intercepted_data.append(body)
                except Exception:
                    pass

            try:
                page = await self._page()
                page.on("response", on_response)
                
                if not await self._goto(profile_url):
                    page.remove_listener("response", on_response)
                    return {
                        "success": False, "ok": False, "user_id": user_id or None,
                        "profile_url": profile_url, "is_live": False,
                        "is_premium": False, "stream_url": None, "stream_id": None, "ownership_verified": False,
                        "error": "profile_navigation_failed",
                        "live_evidence": [], "premium_evidence": [],
                    }
                
                # Wait for XHRs to fire
                await page.wait_for_timeout(1500)
                
                current_url = self._norm_url(page.url)
                title = await self._title(page)
                text = await self._text(page)
                
                live, live_evidence = await self.is_live_page(page)
                premium, premium_evidence = await self.detect_premium(page)
                display_name = await self._extract_display_name(page, user_id, intercepted_data)
                
                stream_url = None
                stream_id = None
                ownership_verified = False
                
                html_content = await page.content()
                
                # 1. Find /livestream/ID in HTML (Profile Page)
                # Matches 5 to 15 digit numbers
                html_matches = re.findall(r'/livestream/(\d{5,15})', html_content)
                valid_html_ids = [m for m in html_matches if m != str(user_id) and m != str(profile_id)]
                
                if valid_html_ids:
                    stream_id = valid_html_ids[0]
                    stream_url = f"https://superlivetv.com/fr/livestream/{stream_id}"
                    ownership_verified = True  # Found on their profile page
                    live_evidence.append("html_livestream_link")
                else:
                    # 2. Check intercepted JSON (Network API)
                    for body in intercepted_data:
                        m = re.search(r'["\'](?:streamId|stream_id|roomId|room_id|liveId|live_id|broadcastId|liveStreamId)["\']\s*:\s*["\']?(\d{5,15})["\']?', body)
                        if m:
                            val = m.group(1)
                            if val != str(user_id) and val != str(profile_id):
                                # CRITICAL: Verify ownership via JSON payload
                                if str(user_id) in body or str(profile_id) in body:
                                    stream_id = val
                                    stream_url = f"https://superlivetv.com/fr/livestream/{stream_id}"
                                    ownership_verified = True
                                    live_evidence.append("json_api_verified")
                                    break
                                    
                # 3. Fallback to <a> tags if still not found (legacy support)
                if not stream_id:
                    try:
                        links = await page.locator("a[href]").evaluate_all(
                            "els => els.slice(0,1500).map(a => a.href)"
                        )
                        for link in links:
                            absolute = self._norm_url(str(link or ""))
                            if not self._same_site(absolute):
                                continue
                            path = urlparse(absolute).path
                            match = re.search(r"/livestream/(\d{5,15})(?:/|$)", path, re.I)
                            if match:
                                val = match.group(1)
                                if val != str(user_id) and val != str(profile_id):
                                    stream_id = val
                                    stream_url = absolute
                                    ownership_verified = True # Found in DOM
                                    break
                    except PlaywrightError:
                        pass

                page.remove_listener("response", on_response)
                
                # If the profile itself does not prove a live stream, inspect the
                # discovered livestream page too.
                stream_title = ""
                if stream_url and not live:
                    try:
                        # Re-register listener for stream page
                        page.on("response", on_response)
                        if await self._goto(stream_url):
                            stream_page = await self._page()
                            await stream_page.wait_for_timeout(1000)
                            stream_title = await self._title(stream_page)
                            stream_live, stream_evidence = await self.is_live_page(stream_page)
                            stream_premium, stream_premium_evidence = await self.detect_premium(stream_page)
                            if stream_live:
                                live = True
                                live_evidence = ["livestream_page"] + stream_evidence
                            if stream_premium:
                                premium = True
                                premium_evidence = stream_premium_evidence
                    except Exception as stream_exc:
                        self.log.debug("Livestream page inspection failed for %s: %s", stream_url, stream_exc)
                    finally:
                        page.remove_listener("response", on_response)

                has_page = bool(current_url and (title or text))
                return {
                    "success": has_page, "ok": has_page,
                    "user_id": user_id or self._slug(urlparse(current_url).path.rsplit("/", 1)[-1]),
                    "profile_url": current_url, "is_live": bool(live),
                    "live": bool(live), "status": "live" if live else "not_live_or_unconfirmed",
                    "is_premium": bool(premium), "premium": bool(premium),
                    "stream_url": stream_url, "stream_id": stream_id,
                    "ownership_verified": ownership_verified,
                    "display_name": display_name,
                    "title": title, "stream_title": stream_title,
                    "live_evidence": live_evidence,
                    "premium_evidence": premium_evidence,
                    "error": None if has_page else "empty_profile_page",
                }
            except Exception as exc:
                self.log.warning("Live-status check failed for %s: %s", profile_url, exc)
                try:
                    page = await self._page()
                    page.remove_listener("response", on_response)
                except: pass
                return {
                    "success": False, "ok": False, "user_id": user_id or None,
                    "profile_url": profile_url, "is_live": False,
                    "is_premium": False, "stream_url": None, "stream_id": None, "ownership_verified": False,
                    "error": f"inspection_error:{type(exc).__name__}",
                    "live_evidence": [], "premium_evidence": [],
                }

    async def inspect_url(self, url: str, fallback_username: str = "") -> dict[str, Any]:
        async with self._operation_lock:
            return await self._inspect_url_unlocked(url, fallback_username)

    async def _inspect_url_unlocked(self, url: str, fallback_username: str = "") -> dict[str, Any]:
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
            if not await self._goto(f"{self.base_url}/fr/discover"):
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

def __enter__(self):
    raise RuntimeError("SuperLiveDiscovery v11 is async. Use 'async with SuperLiveDiscovery() as discovery:'")

def __exit__(self, *args):
    return None
