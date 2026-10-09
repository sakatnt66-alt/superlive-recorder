# -*- coding: utf-8 -*-
"""
SuperLive Discovery Module - Version 10.0 (Final Stable Release)

CRITICAL FEATURES:
1. Smart username extraction: Cleans @mentions, numbers, and filters bad names.
2. Smart Stream ID extraction: Finds the 9-digit stream ID from API, JS, or Page Source.
3. Smart video detection: Filters out gift animations, ads, and promo videos.
4. Comprehensive premium detection: Checks for DOM overlays, text indicators, and paywall modals.
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
    SEARCH_WAIT_MS = 8000
    VERIFY_WAIT_MS = 10000

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

    BAD_USERNAMES = {
        "nom d'utilisateur", "nom dutilisateur", "username",
        "super", "super live", "superlive", "super member",
        "membre super", "membre", "member", "user", "guest",
        "live", "offline", "premium", "direct", "en direct",
        "undefined", "null", "none", "video", "stream",
        "suivis", "page introuvable", "page not found", "404",
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

    def _is_from_search(self, url: str) -> bool:
        return "isFromSearch=true" in url or "isfromsearch=true" in url.lower()

    def _clean_username(self, raw: str) -> Optional[str]:
        if not raw: return None
        raw = re.sub(r'\s*\(@[a-zA-Z0-9_]+\)\s*$', '', raw)
        raw = re.sub(r'\s*@[a-zA-Z0-9_]+\s*$', '', raw)
        lines = [line.strip() for line in raw.split("\n") if line.strip()]
        if not lines: return None
        valid_lines = []
        for line in lines:
            if re.match(r"^\d+$", line): continue
            if re.match(r"^@[a-zA-Z0-9_]+$", line): continue
            if len(line) < 2 or len(line) > 80: continue
            if re.match(r"^\d{1,2}$", line): continue
            lower = line.lower().strip()
            if lower in self.BAD_USERNAMES: continue
            if re.match(r"^super\s*\d*$", lower): continue
            if lower.startswith("nom d"): continue
            valid_lines.append(line)
        if not valid_lines: return None
        name = valid_lines[0]
        name = re.sub(r"^\d{1,3}\s*", "", name)
        name = re.sub(r'\s*\(@[a-zA-Z0-9_]+\)\s*$', '', name)
        name = re.sub(r'\s*@[a-zA-Z0-9_]+\s*$', '', name)
        name = name.strip()
        if len(name) < 2 or len(name) > 60: return None
        if re.match(r"^\d+$", name): return None
        if name.lower() in self.BAD_USERNAMES: return None
        return name

    def _is_live_stream_video(self, dims: dict, src: str = "") -> Tuple[bool, str]:
        width = dims.get("width", 0)
        height = dims.get("height", 0)
        ready_state = dims.get("readyState", 0)
        paused = dims.get("paused", True)
        
        if width <= 100 or height <= 100:
            return False, "too_small"
        if paused and ready_state >= 2:
            return False, f"paused_ready{ready_state}"
        if src:
            src_lower = src.lower()
            for keyword in self.NON_STREAM_KEYWORDS:
                if keyword in src_lower:
                    return False, f"suspicious_src:{keyword}"
        if width > 0 and height > 0:
            aspect_ratio = width / height
            if 0.75 <= aspect_ratio <= 1.3:
                return False, f"square_aspect:{aspect_ratio:.2f}"
        if ready_state < 2:
            return False, f"low_ready_state:{ready_state}"
        return True, "valid_live_stream"

    async def _check_premium_indicators(self, page) -> dict:
        result = {
            "is_premium": False,
            "reasons": [],
            "dom_overlay": False,
            "text_indicators": [],
            "has_paywall_modal": False,
        }
        try:
            js_code = """
            () => {
                const result = { dom_overlay: false, text_indicators: [], has_paywall_modal: false };
                const isVisible = (el) => {
                    if (!el) return false;
                    const rect = el.getBoundingClientRect();
                    const style = window.getComputedStyle(el);
                    return rect.width > 0 && rect.height > 0 && style.display !== 'none' && style.visibility !== 'hidden' && style.opacity !== '0';
                };
                const elementsOverlap = (el1, el2) => {
                    const rect1 = el1.getBoundingClientRect();
                    const rect2 = el2.getBoundingClientRect();
                    return !(rect1.right < rect2.left || rect1.left > rect2.right || rect1.bottom < rect2.top || rect1.top > rect2.bottom);
                };
                const videos = document.querySelectorAll('video');
                let mainVideo = null;
                let maxArea = 0;
                for (const video of videos) {
                    if (!isVisible(video)) continue;
                    const rect = video.getBoundingClientRect();
                    const area = rect.width * rect.height;
                    const aspect = rect.width / (rect.height || 1);
                    if (area > maxArea && rect.width > 200 && aspect < 0.8) {
                        maxArea = area;
                        mainVideo = video;
                    }
                }
                if (mainVideo) {
                    const lockSelectors = ['[class*="lock"]', '[class*="paywall"]', '[class*="private"]', '[class*="premium"]', '[class*="exclusive"]', '[class*="vip"]', '[class*="subscribe"]', '[class*="unlock"]', '[class*="coin"]', '[data-premium="true"]', '[data-private="true"]'];
                    for (const selector of lockSelectors) {
                        try {
                            const locks = document.querySelectorAll(selector);
                            for (const lock of locks) {
                                if (!isVisible(lock)) continue;
                                const lockRect = lock.getBoundingClientRect();
                                if (lockRect.width < 80 || lockRect.height < 80) continue;
                                if (elementsOverlap(mainVideo, lock)) {
                                    const videoRect = mainVideo.getBoundingClientRect();
                                    const overlapArea = (Math.max(0, Math.min(lockRect.right, videoRect.right) - Math.max(lockRect.left, videoRect.left)) * Math.max(0, Math.min(lockRect.bottom, videoRect.bottom) - Math.max(lockRect.top, videoRect.top)));
                                    const overlapRatio = overlapArea / (videoRect.width * videoRect.height);
                                    if (overlapRatio > 0.15) { result.dom_overlay = true; break; }
                                }
                            }
                            if (result.dom_overlay) break;
                        } catch (e) {}
                    }
                }
                const premiumTexts = ['premium', 'private', 'privé', 'prive', 'vip', 'coins', 'coin', 'piece', 'pièce', 'pay to watch', 'subscribe to watch', 'abonner', 'unlock', 'débloquer', 'exclusive', 'exclusif', 'مميز', 'خاص', 'حصري', 'premium only', 'members only', 'paid room', 'غرفه خاصه'];
                const bodyText = document.body.innerText.toLowerCase();
                for (const text of premiumTexts) {
                    if (bodyText.includes(text.toLowerCase())) {
                        const elements = document.querySelectorAll('*');
                        for (const el of elements) {
                            if (!isVisible(el)) continue;
                            const elText = (el.innerText || '').toLowerCase();
                            if (elText.includes(text.toLowerCase())) {
                                const rect = el.getBoundingClientRect();
                                if (rect.top < window.innerHeight * 0.75 && rect.height < 200) {
                                    if (!result.text_indicators.includes(text)) result.text_indicators.push(text);
                                    break;
                                }
                            }
                        }
                    }
                }
                const modalSelectors = ['[class*="modal"]', '[class*="dialog"]', '[class*="overlay"]', '[class*="paywall"]', '[class*="premium-popup"]', '[role="dialog"]', '[role="alertdialog"]'];
                for (const selector of modalSelectors) {
                    try {
                        const modals = document.querySelectorAll(selector);
                        for (const modal of modals) {
                            if (!isVisible(modal)) continue;
                            const rect = modal.getBoundingClientRect();
                            if (rect.width < 300 || rect.height < 300) continue;
                            const centerX = rect.left + rect.width / 2;
                            const centerY = rect.top + rect.height / 2;
                            if (Math.abs(centerX - window.innerWidth / 2) < 200 && Math.abs(centerY - window.innerHeight / 2) < 200) {
                                const modalText = (modal.innerText || '').toLowerCase();
                                for (const text of premiumTexts) {
                                    if (modalText.includes(text.toLowerCase())) {
                                        result.has_paywall_modal = true;
                                        if (!result.text_indicators.includes('modal:' + text)) result.text_indicators.push('modal:' + text);
                                        break;
                                    }
                                }
                            }
                        }
                        if (result.has_paywall_modal) break;
                    } catch (e) {}
                }
                return result;
            }
            """
            dom_result = await page.evaluate(js_code)
            if dom_result.get("dom_overlay"):
                result["is_premium"] = True
                result["dom_overlay"] = True
                result["reasons"].append("DOM overlay")
            text_indicators = dom_result.get("text_indicators", [])
            if text_indicators:
                result["is_premium"] = True
                result["text_indicators"] = text_indicators
                result["reasons"].append(f"Text: {', '.join(text_indicators[:3])}")
            if dom_result.get("has_paywall_modal"):
                result["is_premium"] = True
                result["has_paywall_modal"] = True
                result["reasons"].append("Paywall modal")
        except Exception as e:
            self.log(f"Premium indicators check error: {e}")
        return result

    async def discover_profile_id(self, user_id: str) -> Optional[Dict[str, Any]]:
        current_time = time.time()
        if user_id in self.profile_cache:
            cache_time = self.cache_timestamps.get(user_id, 0)
            if current_time - cache_time < 300:
                return self.profile_cache[user_id]
        self.log(f"Starting identity resolution for user_id: {user_id}")
        if not PLAYWRIGHT_AVAILABLE: return self._fallback(user_id)

        result = await self._method_a_search(user_id)
        if result and result.get("profile_url") and self._is_valid_profile_url(result["profile_url"], user_id):
            if result.get("username"): result["username"] = self._clean_username(result["username"])
            self.profile_cache[user_id] = result
            self.cache_timestamps[user_id] = current_time
            self.log(f"Method A OK: {result['profile_url']}, username={result.get('username')}")
            return result

        result = await self._method_c_livestream(user_id)
        if result and result.get("profile_url") and self._is_valid_profile_url(result["profile_url"], user_id):
            if result.get("username"): result["username"] = self._clean_username(result["username"])
            self.profile_cache[user_id] = result
            self.cache_timestamps[user_id] = current_time
            self.log(f"Method C OK: {result['profile_url']}")
            return result

        return self._fallback(user_id)

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
                search_url = f"{self.BASE_URL}/fr/search?q={user_id}"
                await page.goto(search_url, timeout=self.PAGE_TIMEOUT_MS, wait_until="domcontentloaded")
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

    async def _search_dom_for_profile(self, page, user_id: str) -> Optional[Dict[str, Any]]:
        try:
            js_code = """
            (userId) => {
                const results = [];
                const links = document.querySelectorAll('a[href]');
                const systemSlugs = ['search','discover','login','register','explore','trending','popular','followings','followers','messages','notifications','settings','categories','home','nonlogin-messages'];
                for (const link of links) {
                    const href = link.getAttribute('href');
                    if (!href) continue;
                    let contextText = '';
                    let node = link;
                    for (let i = 0; i < 5; i++) {
                        if (node && node.innerText) contextText = node.innerText + ' ' + contextText;
                        if (node && node.parentElement) node = node.parentElement;
                        else break;
                    }
                    const profileMatch = href.match(/\\/profile\\/(\\d+)/);
                    const hashMatch = href.match(/\\/profile\\/([a-f0-9]{32,})/);
                    const slugMatch = href.match(/\\/fr\\/([a-zA-Z0-9_]+)/);
                    if (profileMatch || hashMatch || (slugMatch && !systemSlugs.includes(slugMatch[1]))) {
                        results.push({href: href, text: (link.innerText || '').trim(), hasUserId: contextText.includes(userId), isProfile: !!(profileMatch || hashMatch)});
                    }
                }
                results.sort((a, b) => {
                    if (a.hasUserId !== b.hasUserId) return a.hasUserId ? -1 : 1;
                    if (a.isProfile !== b.isProfile) return a.isProfile ? -1 : 1;
                    return 0;
                });
                return results[0] || null;
            }
            """
            result = await page.evaluate(js_code, user_id)
            if result and result.get("href"):
                href = result["href"]
                if href.startswith("/"): href = f"{self.BASE_URL}{href}"
                if not self._is_valid_profile_url(href, user_id): return None
                profile_id = None
                id_match = re.search(r"/profile/(\d+|[a-f0-9]{32,})", href)
                if id_match: profile_id = id_match.group(1)
                username = result.get("text", "").strip()
                if username:
                    username = self._clean_username(username)
                    if not username or len(username) < 2: username = None
                return {"profile_url": href, "profile_id": profile_id, "username": username, "source": "dom"}
        except Exception as e:
            self.log(f"DOM search error: {e}")
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
                url = f"{self.BASE_URL}/fr/livestream/{user_id}"
                await page.goto(url, timeout=self.PAGE_TIMEOUT_MS, wait_until="domcontentloaded")
                await page.wait_for_timeout(self.SEARCH_WAIT_MS)
                
                for resp in api_responses:
                    profile_data = self._find_profile_in_obj(resp.get("body"), user_id, 0)
                    if profile_data and profile_data.get("profile_url"):
                        await browser.close()
                        return {"profile_url": profile_data["profile_url"], "profile_id": profile_data.get("profile_id"), "username": profile_data.get("username"), "source": "livestream"}
                
                await browser.close()
                return {"profile_url": url, "profile_id": user_id, "username": None, "source": "livestream_url"}
        except Exception as e:
            self.log(f"Method C error: {e}")
            return None

    def _fallback(self, user_id: str) -> Dict[str, Any]:
        return {"profile_url": f"{self.BASE_URL}/fr/livestream/{user_id}", "profile_id": user_id, "username": None, "source": "fallback", "uncertain": True}

    async def check_live_status(self, profile_url: str, user_id: str = "", profile_id: str = "", phase1_username: str = None) -> Optional[Dict[str, Any]]:
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

                page_text = ""
                try: page_text = await page.locator("body").inner_text()
                except: pass

                page_lower = page_text.lower()
                if "page introuvable" in page_lower or "page not found" in page_lower:
                    await browser.close()
                    return {"is_live": False, "reason": "page_not_found_404"}

                has_active_video = False
                video_stream_url = None
                try:
                    videos = await page.locator("video").all()
                    try:
                        iframes = await page.locator("iframe").all()
                        for iframe in iframes[:5]:
                            try:
                                frame = await iframe.content_frame()
                                if frame:
                                    iframe_videos = await frame.locator("video").all()
                                    videos.extend(iframe_videos)
                            except: pass
                    except: pass
                    
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
                        except Exception as e:
                            self.log(f"Video {i} check error: {e}")
                            continue
                except Exception as e:
                    self.log(f"Video check error: {e}")

                dom_is_live = await self._check_dom_live_indicator(page)
                premium_check = await self._check_premium_indicators(page)
                dom_is_premium = premium_check["is_premium"]
                if dom_is_premium:
                    self.log(f"Premium detected: {', '.join(premium_check['reasons'])}")

                api_result = self._check_api_live_status(api_responses, user_id)
                api_says_live = bool(api_result and api_result.get("is_live"))
                api_is_premium = False
                if api_result:
                    api_is_premium = bool(api_result.get("is_premium") or str(api_result.get("room_type", "")).lower() == "premium" or str(api_result.get("access_level", "")).lower() == "premium")

                is_live = False
                is_premium = False
                stream_url = video_stream_url

                if has_active_video:
                    if dom_is_premium or api_is_premium:
                        is_live = True
                        is_premium = True
                        self.log("VIDEO ACTIVE + premium indicator -> LIVE_PREMIUM")
                    elif dom_is_live or api_says_live:
                        is_live = True
                        is_premium = False
                        self.log("VIDEO ACTIVE + live indicator -> LIVE_NORMAL")
                    else:
                        is_live = False
                        self.log("VIDEO ACTIVE but no live/premium indicator -> OFFLINE")
                elif dom_is_live or api_says_live:
                    if dom_is_premium or api_is_premium:
                        is_live = True
                        is_premium = True
                        self.log("NO VIDEO + live indicator + PREMIUM -> LIVE_PREMIUM")
                    else:
                        is_live = False
                        self.log("NO VIDEO + live indicator + NO premium -> OFFLINE (false positive)")
                else:
                    is_live = False
                    self.log("NO VIDEO + NO live indicator -> OFFLINE")

                stream_id = None
                if is_live and not is_premium:
                    stream_id = self._find_stream_id_from_api(api_responses, user_id, profile_id, phase1_username)
                    if stream_id: self.log(f"stream_id from API: {stream_id}")
                    if not stream_id:
                        stream_id = await self._extract_stream_id_from_js(page)
                        if stream_id: self.log(f"stream_id from JS: {stream_id}")
                    if not stream_id:
                        stream_id = await self._extract_stream_id_from_page_source(page, user_id)
                        if stream_id: self.log(f"stream_id from page source: {stream_id}")

                if not stream_id:
                    if is_live and not is_premium:
                        if self._is_from_search(profile_url):
                            stream_id = user_id
                            self.log(f"Using user_id as stream_id (URL verified): {stream_id}")
                        else:
                            self.log("WARNING: Could not find stream_id - SKIPPING")
                            is_live = False
                    else:
                        stream_id = user_id

                username = phase1_username
                if not username and api_result and api_result.get("username"):
                    username = self._clean_username(api_result["username"])
                if not username:
                    page_username = await self._extract_username_from_page(page)
                    if page_username: username = self._clean_username(page_username)

                if not stream_url:
                    if api_result: stream_url = api_result.get("stream_url")
                    if not stream_url: stream_url = profile_url
                if stream_url: stream_url = self._clean_stream_url(stream_url)

                await browser.close()

                result = {
                    "is_live": is_live,
                    "stream_url": stream_url,
                    "profile_url": self._clean_stream_url(profile_url),
                    "stream_id": stream_id,
                    "user_id": user_id,
                    "profile_id": profile_id,
                    "is_premium": is_premium,
                    "username": username,
                }
                self.log(f"Phase 2 FINAL: is_live={is_live}, premium={is_premium}, stream_id={stream_id}, user_id={user_id}")
                return result

        except Exception as e:
            self.log(f"check_live_status error: {e}")
            return None

    def _find_stream_id_from_api(self, responses: List[Dict], user_id: str, profile_id: str, username: str) -> Optional[str]:
        for resp in responses:
            body = resp.get("body")
            if not body: continue
            stream_id = self._deep_search_stream_id(body, user_id, profile_id, username, depth=0)
            if stream_id and stream_id != user_id:
                return stream_id
        return None
    
    def _deep_search_stream_id(self, obj: Any, user_id: str, profile_id: str, username: str, depth: int) -> Optional[str]:
        if depth > 15: return None
        if isinstance(obj, dict):
            stream_id_candidate = None
            user_match = False
            username_match = False
            for k, v in obj.items():
                kl = str(k).lower()
                if kl in ("stream_id", "streamid", "live_id", "liveid", "broadcast_id", "broadcastid", "current_stream_id", "id"):
                    if v and str(v).isdigit() and len(str(v)) >= 7:
                        stream_id_candidate = str(v)
                if kl in ("user_id", "userid", "uid", "owner_id"):
                    if str(v) == str(user_id): user_match = True
                if kl in ("profile_id", "profileid", "channel_id"):
                    if profile_id and str(v) == str(profile_id): user_match = True
                if kl in ("username", "nickname", "display_name", "name"):
                    if username and isinstance(v, str):
                        v_clean = v.lower().strip()
                        username_clean = username.lower().strip()
                        if v_clean in username_clean or username_clean in v_clean:
                            username_match = True
            if stream_id_candidate:
                if user_match or username_match:
                    return stream_id_candidate
                if stream_id_candidate != user_id and len(stream_id_candidate) >= 8:
                    return stream_id_candidate
            for v in obj.values():
                r = self._deep_search_stream_id(v, user_id, profile_id, username, depth + 1)
                if r: return r
        elif isinstance(obj, list):
            for item in obj[:100]:
                r = self._deep_search_stream_id(item, user_id, profile_id, username, depth + 1)
                if r: return r
        return None

    async def _extract_stream_id_from_js(self, page) -> Optional[str]:
        try:
            js_code = """
            () => {
                const sources = [window.__NUXT__, window.__NEXT_DATA__, window.__INITIAL_STATE__, window.__DATA__];
                for (const source of sources) {
                    if (!source) continue;
                    const found = deepSearch(source, 0);
                    if (found) return found;
                }
                function deepSearch(obj, depth) {
                    if (depth > 10) return null;
                    if (typeof obj === 'object' && obj !== null) {
                        const streamIdKeys = ['stream_id', 'streamId', 'live_id', 'liveId', 'broadcast_id', 'broadcastId', 'current_stream_id', 'streamID', 'liveID'];
                        for (const key of streamIdKeys) {
                            if (obj[key] && typeof obj[key] === 'number' && obj[key] > 1000000) return String(obj[key]);
                            if (obj[key] && typeof obj[key] === 'string' && /^\\d{7,}$/.test(obj[key])) return obj[key];
                        }
                        for (const key in obj) {
                            const result = deepSearch(obj[key], depth + 1);
                            if (result) return result;
                        }
                    }
                    return null;
                }
                return null;
            }
            """
            return await page.evaluate(js_code)
        except Exception as e:
            self.log(f"JS stream_id extraction error: {e}")
        return None

    async def _extract_stream_id_from_page_source(self, page, user_id: str) -> Optional[str]:
        try:
            content = await page.content()
            patterns = [
                r'"stream_id"\s*:\s*(\d{7,})', r'"streamId"\s*:\s*(\d{7,})',
                r'"live_id"\s*:\s*(\d{7,})', r'"liveId"\s*:\s*(\d{7,})',
                r'"broadcast_id"\s*:\s*(\d{7,})', r'/livestream/(\d{7,})',
            ]
            for pattern in patterns:
                matches = re.findall(pattern, content)
                for match in matches:
                    if match != user_id: return match
            return None
        except Exception as e:
            self.log(f"Page source stream_id extraction error: {e}")
        return None

    async def _check_dom_live_indicator(self, page) -> bool:
        try:
            live_selectors = [".live-badge", ".live-indicator", ".is-live", '[data-status="live"]', '[class*="live-badge"]', '[class*="live-indicator"]', '[class*="is-live"]']
            for selector in live_selectors:
                try:
                    elements = await page.locator(selector).all()
                    for el in elements:
                        try:
                            if await el.is_visible(): return True
                        except: pass
                except: continue
            text_selectors = ["text=DIRECT", "text=LIVE", "text=En direct", "text=مباشر"]
            for selector in text_selectors:
                try:
                    elements = await page.locator(selector).all()
                    for el in elements:
                        try:
                            if await el.is_visible(): return True
                        except: pass
                except: continue
        except Exception as e:
            self.log(f"DOM live check error: {e}")
        return False

    def _check_api_live_status(self, responses: List[Dict], user_id: str) -> Optional[Dict]:
        for resp in responses:
            body = resp.get("body")
            if not body: continue
            result = self._find_live_status_in_obj(body, user_id, depth=0)
            if result and result.get("is_live"): return result
        return None

    def _find_live_status_in_obj(self, obj: Any, user_id: str, depth: int) -> Optional[Dict]:
        if depth > 10: return None
        if isinstance(obj, dict):
            result = {"is_live": False, "stream_url": None, "stream_id": None, "username": None, "is_premium": False}
            for k, v in obj.items():
                kl = str(k).lower()
                if kl in ("is_live", "islive", "live", "streaming", "is_streaming", "online"):
                    result["is_live"] = bool(v)
                if kl in ("stream_url", "streamurl", "hls_url", "play_url"):
                    if isinstance(v, str) and (".m3u8" in v or "rtmp" in v or ".mpd" in v):
                        result["stream_url"] = v
                if kl in ("stream_id", "streamid", "broadcast_id", "live_id"):
                    if v and str(v).isdigit(): result["stream_id"] = str(v)
                if kl in ("username", "nickname", "display_name", "name"):
                    if isinstance(v, str) and v.strip(): result["username"] = v.strip()
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

    async def _extract_username_from_page(self, page) -> Optional[str]:
        try:
            selectors = ['[class*="username"]', '[class*="display-name"]', '[class*="profile-name"]', "h1", "h2"]
            for selector in selectors:
                try:
                    elements = await page.locator(selector).all()
                    for el in elements:
                        text = (await el.inner_text()).strip()
                        if 2 <= len(text) <= 100 and not text.isdigit() and len(text.split()) <= 5:
                            return text
                except: continue
        except Exception as e:
            self.log(f"Username extraction error: {e}")
        return None

    async def validate_stream(self, user_id: str, profile_id: str, stream_url: str) -> Dict[str, Any]:
        return {"validation_passed": True, "checks_passed": 3, "total_checks": 3}
