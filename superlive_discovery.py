# -*- coding: utf-8 -*-
"""
SuperLive Discovery Module - Version 10.4 (Final Robust Fix)

CRITICAL FIXES in v10.4:
1. Accept paused videos if they have a valid CDN/stream src (readyState >= 2).
2. Force stream_url to be /fr/livestream/{stream_id} if stream_id is found.
"""

import asyncio
import json
import re
import time
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
    SEARCH_WAIT_MS = 5000
    VERIFY_WAIT_MS = 5000

    USER_AGENT = (
        "Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    )

    SYSTEM_PAGES_EXACT = {
        "discover", "explore", "trending", "popular", "categories",
        "search", "login", "register", "signup", "signin", "logout",
        "about", "contact", "terms", "privacy", "help", "support",
        "faq", "blog", "news", "home",
        "followings", "followers", "messages", "notifications",
        "settings", "favorites", "history", "downloads", "uploads",
        "wallet", "coins", "recharge", "payment", "subscription",
        "profile-edit", "edit-profile", "account",
        "nonlogin-messages", "nonlogin-notifications",
    }

    NON_STREAM_KEYWORDS = [
        "gifts", "gift", "misc", "ads", "ad/", "promo",
        "banner", "animation", "effect", "sticker", "emote",
        "reward", "bonus", "intro", "outro", "thumbnail"
    ]

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
        if re.search(r"/profile/\d+", url_path): return True
        if re.search(r"/profile/[a-f0-9]{32,}", url_path): return True
        if re.search(r"/livestream/\d+", url_path): return True
        slug_match = re.match(r".*/fr/([^/]+)/?$", url_path)
        if slug_match:
            slug = slug_match.group(1).lower()
            if slug in self.SYSTEM_PAGES_EXACT: return False
            if re.match(r"^[a-zA-Z0-9_]{2,50}$", slug): return True
        last_slug_match = re.match(r".*/([^/]+)/?$", url_path)
        if last_slug_match:
            slug = last_slug_match.group(1).lower()
            if re.match(r"^\d{5,}$", slug): return True
            if re.match(r"^[a-f0-9]{32,}$", slug): return True
            if slug in self.SYSTEM_PAGES_EXACT: return False
            if re.match(r"^[a-zA-Z0-9_]{2,50}$", slug): return True
        if user_id and user_id in url: return True
        return False

    def _is_live_stream_video(self, dims: dict, src: str = "") -> Tuple[bool, str]:
        width = dims.get("width", 0)
        height = dims.get("height", 0)
        ready_state = dims.get("readyState", 0)
        paused = dims.get("paused", True)
        
        if width <= 100 or height <= 100:
            return False, "too_small"
        
        if ready_state < 2:
            return False, f"low_ready_state:{ready_state}"
        
        # CRITICAL FIX: If it has a valid stream src, accept it even if paused.
        # Browsers often pause videos until interaction, but the stream is valid.
        if src:
            src_lower = src.lower()
            for keyword in self.NON_STREAM_KEYWORDS:
                if keyword in src_lower:
                    return False, f"suspicious_src:{keyword}"
            
            if any(ext in src_lower for ext in [".m3u8", ".mpd", "rtmp", "cdn-", "spl-live", "blob:"]):
                return True, "valid_stream_src"
        
        # If no src (e.g., WebRTC), it must not be paused to be considered actively streaming
        if paused:
            return False, "paused_no_src"
        
        if width > 0 and height > 0:
            aspect_ratio = width / height
            if 0.75 <= aspect_ratio <= 1.3:
                return False, f"square_aspect:{aspect_ratio:.2f}"
        
        return True, "valid_live_stream"

    async def discover_profile_id(self, user_id: str) -> Optional[Dict[str, Any]]:
        current_time = time.time()
        if user_id in self.profile_cache:
            cache_time = self.cache_timestamps.get(user_id, 0)
            if current_time - cache_time < 300:
                return self.profile_cache[user_id]
        self.log(f"Starting identity resolution for user_id: {user_id}")
        if not PLAYWRIGHT_AVAILABLE: 
            return {"profile_url": f"{self.BASE_URL}/fr/livestream/{user_id}", "profile_id": user_id, "username": None, "source": "fallback"}

        for method in [self._method_a_search, self._method_c_livestream]:
            result = await method(user_id)
            if result and result.get("profile_url") and self._is_valid_profile_url(result["profile_url"], user_id):
                self.profile_cache[user_id] = result
                self.cache_timestamps[user_id] = current_time
                self.log(f"Method OK: {result['profile_url']}")
                return result

        return {"profile_url": f"{self.BASE_URL}/fr/livestream/{user_id}", "profile_id": user_id, "username": None, "source": "fallback"}

    async def _method_a_search(self, user_id: str) -> Optional[Dict[str, Any]]:
        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"])
                context = await browser.new_context(user_agent=self.USER_AGENT, viewport={"width": 1280, "height": 800}, locale="fr-FR")
                page = await context.new_page()
                api_responses = []
                async def on_response(response):
                    try:
                        ct = (response.headers or {}).get("content-type", "")
                        if "json" in ct.lower():
                            try:
                                body = await response.json()
                                api_responses.append({"url": response.url, "body": body})
                            except: pass
                    except: pass
                page.on("response", on_response)
                await page.goto(f"{self.BASE_URL}/fr/search?q={user_id}", timeout=self.PAGE_TIMEOUT_MS, wait_until="domcontentloaded")
                await page.wait_for_timeout(self.SEARCH_WAIT_MS)
                
                for resp in api_responses:
                    profile_data = self._find_profile_in_obj(resp.get("body"), user_id, 0)
                    if profile_data:
                        profile_url = profile_data.get("profile_url") or (f"{self.BASE_URL}/fr/profile/{profile_data.get('profile_id')}" if profile_data.get("profile_id") else None)
                        if profile_url and self._is_valid_profile_url(profile_url, user_id):
                            await browser.close()
                            return {"profile_url": profile_url, "profile_id": profile_data.get("profile_id"), "username": profile_data.get("username"), "source": "api"}
                
                await browser.close()
                return None
        except Exception as e:
            self.log(f"Method A error: {e}")
            return None

    def _find_profile_in_obj(self, obj: Any, user_id: str, depth: int) -> Optional[Dict]:
        if depth > 10: return None
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
                r = self._find_profile_in_obj(v, user_id, depth + 1)
                if r: return r
        elif isinstance(obj, list):
            for item in obj[:100]:
                r = self._find_profile_in_obj(item, user_id, depth + 1)
                if r: return r
        return None

    async def _method_c_livestream(self, user_id: str) -> Optional[Dict[str, Any]]:
        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
                context = await browser.new_context(user_agent=self.USER_AGENT, viewport={"width": 1280, "height": 800}, locale="fr-FR")
                page = await context.new_page()
                api_responses = []
                async def on_response(response):
                    try:
                        ct = (response.headers or {}).get("content-type", "")
                        if "json" in ct.lower():
                            try:
                                body = await response.json()
                                api_responses.append({"url": response.url, "body": body})
                            except: pass
                    except: pass
                page.on("response", on_response)
                await page.goto(f"{self.BASE_URL}/fr/livestream/{user_id}", timeout=self.PAGE_TIMEOUT_MS, wait_until="domcontentloaded")
                await page.wait_for_timeout(self.SEARCH_WAIT_MS)
                
                for resp in api_responses:
                    profile_data = self._find_profile_in_obj(resp.get("body"), user_id, 0)
                    if profile_data and profile_data.get("profile_url"):
                        await browser.close()
                        return {"profile_url": profile_data["profile_url"], "profile_id": profile_data.get("profile_id"), "username": profile_data.get("username"), "source": "livestream"}
                
                await browser.close()
                return {"profile_url": f"{self.BASE_URL}/fr/livestream/{user_id}", "profile_id": user_id, "username": None, "source": "livestream_url"}
        except Exception as e:
            self.log(f"Method C error: {e}")
            return None

    async def check_live_status(self, profile_url: str, user_id: str = "", profile_id: str = "") -> Optional[Dict[str, Any]]:
        self.log(f"Phase 2: Checking live at {profile_url}")
        if not PLAYWRIGHT_AVAILABLE: return None

        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"])
                context = await browser.new_context(user_agent=self.USER_AGENT, viewport={"width": 1280, "height": 800}, locale="fr-FR")
                page = await context.new_page()
                api_responses = []
                async def on_response(response):
                    try:
                        ct = (response.headers or {}).get("content-type", "")
                        if "json" in ct.lower():
                            try:
                                body = await response.json()
                                api_responses.append({"url": response.url, "body": body})
                            except: pass
                    except: pass
                page.on("response", on_response)
                await page.goto(profile_url, timeout=self.PAGE_TIMEOUT_MS, wait_until="domcontentloaded")
                await page.wait_for_timeout(self.VERIFY_WAIT_MS)

                has_active_video = False
                video_stream_url = None
                
                for attempt in range(3):
                    videos = await page.locator("video").all()
                    try:
                        iframes = await page.locator("iframe").all()
                        for iframe in iframes[:3]:
                            try:
                                frame = await iframe.content_frame()
                                if frame:
                                    iframe_videos = await frame.locator("video").all()
                                    videos.extend(iframe_videos)
                            except: pass
                    except: pass
                    
                    if len(videos) > 0:
                        break
                    self.log(f"No videos found, waiting 5s (attempt {attempt+1}/3)...")
                    await asyncio.sleep(5)

                self.log(f"Found {len(videos)} total video element(s) after retries")
                
                for i, video in enumerate(videos):
                    try:
                        dims = await video.evaluate("""el => ({ width: el.videoWidth || el.clientWidth, height: el.videoHeight || el.clientHeight, readyState: el.readyState, paused: el.paused, src: el.src || el.currentSrc || '' })""")
                        src = dims.get("src", "")
                        is_real, reason = self._is_live_stream_video(dims, src)
                        if is_real:
                            has_active_video = True
                            video_stream_url = src if src else None
                            self.log(f"Video {i}: ✓ REAL LIVE STREAM {dims}")
                            break
                        else:
                            self.log(f"Video {i}: ✗ REJECTED ({reason}) {dims}")
                    except Exception as e:
                        self.log(f"Video {i} check error: {e}")

                if not has_active_video:
                    self.log("No valid video elements found on page after retries.")

                api_result = next((self._find_live_status_in_obj(r.get("body"), user_id, 0) for r in api_responses if self._find_live_status_in_obj(r.get("body"), user_id, 0)), None)
                api_says_live = bool(api_result and api_result.get("is_live"))

                is_live = False
                is_premium = False
                stream_url = video_stream_url

                if has_active_video:
                    is_live = True
                    is_premium = False
                    self.log("VIDEO ACTIVE + valid stream -> LIVE_NORMAL")
                elif api_says_live:
                    is_live = True
                    is_premium = bool(api_result.get("is_premium") or str(api_result.get("room_type", "")).lower() == "premium")
                    self.log(f"NO VIDEO + API says live -> {'LIVE_PREMIUM' if is_premium else 'LIVE_NORMAL'}")
                else:
                    is_live = False
                    self.log("NO VIDEO + NO live indicator -> OFFLINE")

                stream_id = None
                if is_live and not is_premium:
                    for resp in api_responses:
                        sid = self._find_stream_id_in_obj(resp.get("body"), user_id, 0)
                        if sid:
                            stream_id = sid
                            self.log(f"stream_id from API: {stream_id}")
                            break
                    
                    if not stream_id:
                        content = await page.content()
                        match = re.search(r'/livestream/(\d{7,})', content)
                        if match:
                            stream_id = match.group(1)
                            self.log(f"stream_id from page source: {stream_id}")

                # CRITICAL FIX: Force stream_url to be /fr/livestream/{stream_id} if found!
                if stream_id and len(str(stream_id)) >= 7:
                    stream_url = f"{self.BASE_URL}/fr/livestream/{stream_id}"
                else:
                    if not stream_url:
                        if api_result: stream_url = api_result.get("stream_url")
                        if not stream_url: stream_url = profile_url
                
                if stream_url: stream_url = self._clean_stream_url(stream_url)

                await browser.close()

                return {
                    "is_live": is_live,
                    "stream_url": stream_url,
                    "profile_url": self._clean_stream_url(profile_url),
                    "stream_id": stream_id,
                    "user_id": user_id,
                    "profile_id": profile_id,
                    "is_premium": is_premium,
                }

        except Exception as e:
            self.log(f"check_live_status error: {e}")
            return None

    def _find_live_status_in_obj(self, obj: Any, user_id: str, depth: int) -> Optional[Dict]:
        if depth > 10: return None
        if isinstance(obj, dict):
            result = {"is_live": False, "stream_url": None, "stream_id": None, "is_premium": False}
            for k, v in obj.items():
                kl = str(k).lower()
                if kl in ("is_live", "islive", "live", "streaming", "is_streaming", "online"):
                    result["is_live"] = bool(v)
                if kl in ("stream_url", "streamurl", "hls_url", "play_url"):
                    if isinstance(v, str) and (".m3u8" in v or "rtmp" in v or ".mpd" in v):
                        result["stream_url"] = v
                if kl in ("stream_id", "streamid", "broadcast_id", "live_id"):
                    if v and str(v).isdigit(): result["stream_id"] = str(v)
                if kl in ("is_premium", "ispaid", "room_type", "access_level"):
                    if isinstance(v, str) and v.lower() in ("premium", "private", "paid"):
                        result["is_premium"] = True
                    elif isinstance(v, bool) and v:
                        result["is_premium"] = True
            if result["is_live"]: return result
            for v in obj.values():
                r = self._find_live_status_in_obj(v, user_id, depth + 1)
                if r and r.get("is_live"): return r
        elif isinstance(obj, list):
            for item in obj[:50]:
                r = self._find_live_status_in_obj(item, user_id, depth + 1)
                if r and r.get("is_live"): return r
        return None

    def _find_stream_id_in_obj(self, obj: Any, user_id: str, depth: int) -> Optional[str]:
        if depth > 10: return None
        if isinstance(obj, dict):
            for k, v in obj.items():
                kl = str(k).lower()
                if kl in ("stream_id", "streamid", "live_id", "liveid", "broadcast_id", "broadcastid"):
                    if v and str(v).isdigit() and len(str(v)) >= 7:
                        return str(v)
            for v in obj.values():
                r = self._find_stream_id_in_obj(v, user_id, depth + 1)
                if r: return r
        elif isinstance(obj, list):
            for item in obj[:50]:
                r = self._find_stream_id_in_obj(item, user_id, depth + 1)
                if r: return r
        return None
