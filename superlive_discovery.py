# -*- coding: utf-8 -*-
import asyncio, json, re, time
from typing import Dict, List, Optional, Any, Tuple
from urllib.parse import urlparse

try:
    from playwright.async_api import async_playwright
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False

class SuperLiveDiscovery:
    BASE_URL = "https://superlivetv.com"
    PAGE_TIMEOUT_MS = 30000
    SEARCH_WAIT_MS = 5000   # تم التسريع من 8000 إلى 5000
    VERIFY_WAIT_MS = 5000   # تم التسريع من 15000 إلى 5000 (هذا هو السبب الرئيسي في بطء 417 ثانية!)

    USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    SYSTEM_PAGES_EXACT = {"discover", "explore", "trending", "popular", "categories", "search", "login", "register", "signup", "signin", "logout", "about", "contact", "terms", "privacy", "help", "support", "faq", "blog", "news", "home", "followings", "followers", "messages", "notifications", "settings", "favorites", "history", "downloads", "uploads", "wallet", "coins", "recharge", "payment", "subscription", "profile-edit", "edit-profile", "account", "nonlogin-messages", "nonlogin-notifications"}
    BAD_USERNAMES = {"nom d'utilisateur", "nom dutilisateur", "username", "super", "super live", "superlive", "super member", "membre super", "membre", "member", "user", "guest", "live", "offline", "premium", "direct", "en direct", "undefined", "null", "none", "video", "stream", "suivis", "page introuvable", "page not found", "404"}

    def __init__(self):
        self.profile_cache = {}
        self.cache_timestamps = {}

    def log(self, message: str) -> None:
        print(f"[Discovery] {message}", flush=True)

    def _clean_stream_url(self, url: str) -> str:
        if not url: return url
        parsed = urlparse(url)
        clean_url = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
        if parsed.query: clean_url += f"?{parsed.query}"
        if parsed.fragment: clean_url += f"#{parsed.fragment}"
        return clean_url

    def _is_valid_profile_url(self, url: str, user_id: str = "") -> bool:
        if not url: return False
        url_path = url.split("?")[0].split("#")[0]
        if re.search(r"/profile/\d+", url_path) or re.search(r"/livestream/\d+", url_path): return True
        slug_match = re.match(r".*/fr/([^/]+)/?$", url_path)
        if slug_match:
            slug = slug_match.group(1).lower()
            if slug in self.SYSTEM_PAGES_EXACT: return False
            if re.match(r"^[a-zA-Z0-9_]{2,50}$", slug): return True
        last_slug_match = re.match(r".*/([^/]+)/?$", url_path)
        if last_slug_match:
            slug = last_slug_match.group(1).lower()
            if re.match(r"^\d{5,}$", slug) or re.match(r"^[a-zA-Z0-9_]{2,50}$", slug): return True
        return bool(user_id and user_id in url)

    def _clean_username(self, raw: str) -> Optional[str]:
        if not raw: return None
        lines = [line.strip() for line in re.sub(r'\s*\(@[a-zA-Z0-9_]+\)\s*$', '', raw).split("\n") if line.strip()]
        if not lines: return None
        for line in lines:
            if re.match(r"^\d+$", line) or re.match(r"^@[a-zA-Z0-9_]+$", line) or len(line) < 2 or len(line) > 80: continue
            lower = line.lower()
            if lower in self.BAD_USERNAMES or re.match(r"^super\s*\d*$", lower) or lower.startswith("nom d"): continue
            name = re.sub(r"^\d{1,3}\s*", "", line).strip()
            if 2 <= len(name) <= 60 and not re.match(r"^\d+$", name) and name.lower() not in self.BAD_USERNAMES:
                return name
        return None

    async def discover_profile_id(self, user_id: str) -> Optional[Dict[str, Any]]:
        current_time = time.time()
        if user_id in self.profile_cache and current_time - self.cache_timestamps.get(user_id, 0) < 300:
            return self.profile_cache[user_id]
        self.log(f"Starting identity resolution for user_id: {user_id}")
        if not PLAYWRIGHT_AVAILABLE: return {"profile_url": f"{self.BASE_URL}/fr/livestream/{user_id}", "profile_id": user_id, "username": None, "source": "fallback", "uncertain": True}

        for method in [self._method_a_search, self._method_c_livestream]:
            result = await method(user_id)
            if result and result.get("profile_url") and self._is_valid_profile_url(result["profile_url"], user_id):
                if result.get("username"): result["username"] = self._clean_username(result["username"])
                self.profile_cache[user_id] = result
                self.cache_timestamps[user_id] = current_time
                self.log(f"Method OK: {result['profile_url']}")
                return result
        return {"profile_url": f"{self.BASE_URL}/fr/livestream/{user_id}", "profile_id": user_id, "username": None, "source": "fallback", "uncertain": True}

    async def _method_a_search(self, user_id: str) -> Optional[Dict[str, Any]]:
        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"])
                context = await browser.new_context(user_agent=self.USER_AGENT, viewport={"width": 1280, "height": 800}, locale="fr-FR")
                page = await context.new_page()
                api_responses = []
                page.on("response", lambda r: api_responses.append({"url": r.url, "body": r.json()}) if "json" in (r.headers or {}).get("content-type", "").lower() else None)
                await page.goto(f"{self.BASE_URL}/fr/search?q={user_id}", timeout=self.PAGE_TIMEOUT_MS, wait_until="domcontentloaded")
                await page.wait_for_timeout(self.SEARCH_WAIT_MS)
                
                for resp in api_responses:
                    profile_data = self._find_in_obj(resp.get("body"), user_id, 0)
                    if profile_data:
                        profile_url = profile_data.get("profile_url") or (f"{self.BASE_URL}/fr/profile/{profile_data.get('profile_id')}" if profile_data.get("profile_id") else None)
                        if profile_url and self._is_valid_profile_url(profile_url, user_id):
                            await browser.close()
                            return {"profile_url": profile_url, "profile_id": profile_data.get("profile_id"), "username": self._clean_username(profile_data.get("username")), "source": "api"}
                
                await browser.close()
                return None
        except Exception as e:
            self.log(f"Method A error: {e}")
            return None

    def _find_in_obj(self, obj: Any, user_id: str, depth: int) -> Optional[Dict]:
        if depth > 10 or not isinstance(obj, (dict, list)): return None
        if isinstance(obj, dict):
            has_uid = any(isinstance(v, (str, int)) and str(v) == str(user_id) and any(t in str(k).lower() for t in ("id", "user", "uid")) for k, v in obj.items())
            if has_uid:
                res = {"profile_id": None, "profile_url": None, "username": None}
                for k, v in obj.items():
                    kl = str(k).lower()
                    if kl in ("profile_id", "profileid", "channel_id"): res["profile_id"] = str(v)
                    elif kl in ("profile_url", "url", "link", "href") and isinstance(v, str) and v.startswith("http"): res["profile_url"] = v
                    elif kl in ("username", "nickname", "display_name", "name") and isinstance(v, str) and v.strip(): res["username"] = v.strip()
                if res["profile_id"] or res["profile_url"]: return res
            for v in obj.values():
                r = self._find_in_obj(v, user_id, depth + 1)
                if r: return r
        elif isinstance(obj, list):
            for item in obj[:100]:
                r = self._find_in_obj(item, user_id, depth + 1)
                if r: return r
        return None

    async def _method_c_livestream(self, user_id: str) -> Optional[Dict[str, Any]]:
        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
                context = await browser.new_context(user_agent=self.USER_AGENT, viewport={"width": 1280, "height": 800}, locale="fr-FR")
                page = await context.new_page()
                api_responses = []
                page.on("response", lambda r: api_responses.append({"url": r.url, "body": r.json()}) if "json" in (r.headers or {}).get("content-type", "").lower() else None)
                await page.goto(f"{self.BASE_URL}/fr/livestream/{user_id}", timeout=self.PAGE_TIMEOUT_MS, wait_until="domcontentloaded")
                await page.wait_for_timeout(self.SEARCH_WAIT_MS)
                await browser.close()
                
                profile_data = self._find_in_obj(api_responses[-1].get("body") if api_responses else None, user_id, 0)
                if profile_data and profile_data.get("profile_url"):
                    return {"profile_url": profile_data["profile_url"], "profile_id": profile_data.get("profile_id"), "username": self._clean_username(profile_data.get("username")), "source": "livestream"}
                return {"profile_url": f"{self.BASE_URL}/fr/livestream/{user_id}", "profile_id": user_id, "username": None, "source": "livestream_fallback"}
        except Exception as e:
            self.log(f"Method C error: {e}")
            return None

    async def check_live_status(self, profile_url: str, user_id: str = "", profile_id: str = "", phase1_username: str = None) -> Optional[Dict[str, Any]]:
        self.log(f"Phase 2: Checking live at {profile_url}")
        if not PLAYWRIGHT_AVAILABLE: return None
        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"])
                context = await browser.new_context(user_agent=self.USER_AGENT, viewport={"width": 1280, "height": 800}, locale="fr-FR")
                page = await context.new_page()
                api_responses = []
                page.on("response", lambda r: api_responses.append({"url": r.url, "body": r.json()}) if "json" in (r.headers or {}).get("content-type", "").lower() else None)
                await page.goto(profile_url, timeout=self.PAGE_TIMEOUT_MS, wait_until="domcontentloaded")
                await page.wait_for_timeout(self.VERIFY_WAIT_MS)

                page_text = ""
                try: page_text = await page.locator("body").inner_text()
                except: pass

                if "page introuvable" in page_text.lower() or "page not found" in page_text.lower() or len(page_text.strip()) < 50:
                    await browser.close()
                    return {"is_live": False, "reason": "page_blank_or_404"}

                has_active_video = False
                video_stream_url = None
                try:
                    videos = await page.locator("video").all()
                    for video in videos[:3]:
                        try:
                            dims = await video.evaluate("""el => ({ width: el.videoWidth || el.clientWidth, height: el.videoHeight || el.clientHeight, readyState: el.readyState, src: el.src || el.currentSrc || '' })""")
                            if dims.get("width", 0) > 100 and dims.get("height", 0) > 100 and dims.get("readyState", 0) >= 1:
                                has_active_video = True
                                video_stream_url = dims.get("src")
                                break
                        except: continue
                except: pass

                api_result = next((self._find_in_obj(r.get("body"), user_id, 0) for r in api_responses if self._find_in_obj(r.get("body"), user_id, 0)), None)
                api_says_live = bool(api_result and api_result.get("is_live"))
                
                is_live = False
                is_premium = False
                
                if has_active_video:
                    is_live = True
                elif api_says_live and api_result.get("stream_url"):
                    is_live = True
                    video_stream_url = api_result.get("stream_url")

                if is_live and api_result:
                    is_premium = bool(api_result.get("is_premium") or str(api_result.get("room_type", "")).lower() == "premium")

                username = phase1_username or (self._clean_username(api_result.get("username")) if api_result else None)
                if not username:
                    try:
                        text = await page.locator("h1, h2, [class*='username'], [class*='display-name']").first.inner_text()
                        username = self._clean_username(text)
                    except: pass

                await browser.close()
                return {
                    "is_live": is_live, "stream_url": self._clean_stream_url(video_stream_url or profile_url),
                    "profile_url": self._clean_stream_url(profile_url), "stream_id": api_result.get("stream_id") if api_result else None,
                    "user_id": user_id, "profile_id": profile_id, "is_premium": is_premium, "username": username
                }
        except Exception as e:
            self.log(f"check_live_status error: {e}")
            return None
