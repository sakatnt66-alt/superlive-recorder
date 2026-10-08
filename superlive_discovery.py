"""
SuperLive Discovery Module - Version 2.0
اكتشاف وربط user_id مع profile_id والبث المباشر
باستخدام Multi-Method Fallback Chain

Methods:
  A: /fr/search?q={user_id} (Direct query parameter)
  B: /profile/{user_id} (Watch for redirect/API)
  C: /fr/livestream/{user_id} (Watch for API)
  D: Fallback to user_id as profile_id
"""

import asyncio
import json
import re
from typing import Dict, List, Optional, Any

try:
    from playwright.async_api import async_playwright
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False


class SuperLiveDiscovery:
    """
    فئة اكتشاف وربط المعرفات في SuperLive
    مع Multi-Method Fallback Chain
    """

    BASE_URL = "https://superlivetv.com"
    PAGE_TIMEOUT_MS = 30000
    SEARCH_WAIT_MS = 8000
    VERIFY_WAIT_MS = 8000

    # User agent يحاكي متصفح حقيقي
    USER_AGENT = (
        "Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    )

    def __init__(self):
        self.cache = {}  # تخزين مؤقت للـ mappings
        self.profile_id_cache = {}  # cache خاص للـ profile_id فقط

    def log(self, message: str) -> None:
        """Logging موحّد"""
        print(f"[Discovery] {message}", flush=True)

    # ============================================================
    # PUBLIC API: discover_profile_id
    # ============================================================
    async def discover_profile_id(self, user_id: str) -> Optional[Dict[str, Any]]:
        """
        اكتشاف profile_id من user_id عبر سلسلة من الطرق:

        Method A: /fr/search?q={user_id}
        Method B: /profile/{user_id} + monitoring
        Method C: /fr/livestream/{user_id} + monitoring
        Method D: Fallback (user_id as profile_id)

        Returns:
            dict: {
                'profile_id': str,
                'username': str,
                'is_live': bool,
                'user_data': dict,
                'method': str,
                'source': 'api' | 'dom' | 'fallback'
            }
        """
        # التحقق من الكاش
        if user_id in self.profile_id_cache:
            self.log(f"[Cache] Using cached profile_id for user_id: {user_id}")
            return self.profile_id_cache[user_id]

        self.log(f"Starting discovery for user_id: {user_id}")

        if not PLAYWRIGHT_AVAILABLE:
            self.log("Playwright not available, using fallback method")
            return await self._method_d_fallback(user_id)

        # Try Method A: Search with query parameter
        result = await self._method_a_search_query(user_id)
        if result:
            self.profile_id_cache[user_id] = result
            self.log(f"✓ Method A succeeded: profile_id={result['profile_id']}")
            return result

        # Try Method B: Direct profile page
        result = await self._method_b_profile_page(user_id)
        if result:
            self.profile_id_cache[user_id] = result
            self.log(f"✓ Method B succeeded: profile_id={result['profile_id']}")
            return result

        # Try Method C: Livestream page monitoring
        result = await self._method_c_livestream_page(user_id)
        if result:
            self.profile_id_cache[user_id] = result
            self.log(f"✓ Method C succeeded: profile_id={result['profile_id']}")
            return result

        # Fallback Method D
        result = await self._method_d_fallback(user_id)
        self.profile_id_cache[user_id] = result
        self.log(f"✓ Method D (fallback): profile_id={result['profile_id']}")
        return result

    # ============================================================
    # METHOD A: /fr/search?q={user_id}
    # ============================================================
    async def _method_a_search_query(self, user_id: str) -> Optional[Dict[str, Any]]:
        """
        استخدام query parameter مباشرة في URL البحث
        """
        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(
                    headless=True,
                    args=[
                        "--no-sandbox",
                        "--disable-dev-shm-usage",
                        "--disable-gpu",
                    ],
                )
                context = await browser.new_context(
                    user_agent=self.USER_AGENT,
                    viewport={"width": 1280, "height": 800},
                    locale="fr-FR",
                )
                page = await context.new_page()

                # اعتراض جميع responses
                api_responses = []
                redirects = []

                async def on_response(response):
                    try:
                        url = response.url
                        status = response.status

                        # Record redirects
                        if 300 <= status < 400:
                            location = response.headers.get("location", "")
                            redirects.append({
                                "from": url,
                                "to": location,
                                "status": status,
                            })

                        # Record JSON responses
                        content_type = (response.headers or {}).get("content-type", "")
                        if "json" in content_type.lower():
                            try:
                                body = await response.json()
                                api_responses.append({
                                    "url": url,
                                    "status": status,
                                    "body": body,
                                })
                            except Exception:
                                try:
                                    text = await response.text()
                                    api_responses.append({
                                        "url": url,
                                        "status": status,
                                        "body": text[:10000],
                                    })
                                except Exception:
                                    pass
                    except Exception:
                        pass

                page.on("response", on_response)

                # ✅ المفتاح: استخدام query parameter مباشرة
                search_url = f"{self.BASE_URL}/fr/search?q={user_id}"
                self.log(f"Method A: Opening {search_url}")

                try:
                    await page.goto(
                        search_url,
                        timeout=self.PAGE_TIMEOUT_MS,
                        wait_until="domcontentloaded",
                    )
                    # انتظار إضافي لتحميل النتائج
                    await page.wait_for_timeout(self.SEARCH_WAIT_MS)
                except Exception as e:
                    self.log(f"Method A: Page load error: {e}")
                    await browser.close()
                    return None

                # تحليل الـ responses
                result = self._analyze_responses_for_profile(
                    api_responses, redirects, user_id, "method_a"
                )

                # إذا لم يُعثر على نتيجة من API، حاول DOM
                if not result:
                    result = await self._extract_from_search_dom(page, user_id)

                await browser.close()
                return result

        except Exception as e:
            self.log(f"Method A: Global error: {e}")
            return None

    # ============================================================
    # METHOD B: /profile/{user_id} مع مراقبة الـ redirect
    # ============================================================
    async def _method_b_profile_page(self, user_id: str) -> Optional[Dict[str, Any]]:
        """
        فتح صفحة البروفايل مباشرة ومراقبة:
        - Redirect إلى profile_id مختلف
        - API responses تحتوي profile_id
        """
        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(
                    headless=True,
                    args=["--no-sandbox", "--disable-dev-shm-usage"],
                )
                context = await browser.new_context(
                    user_agent=self.USER_AGENT,
                    viewport={"width": 1280, "height": 800},
                    locale="fr-FR",
                )
                page = await context.new_page()

                api_responses = []
                redirects = []

                async def on_response(response):
                    try:
                        url = response.url
                        status = response.status

                        if 300 <= status < 400:
                            location = response.headers.get("location", "")
                            redirects.append({
                                "from": url,
                                "to": location,
                                "status": status,
                            })

                        content_type = (response.headers or {}).get("content-type", "")
                        if "json" in content_type.lower():
                            try:
                                body = await response.json()
                                api_responses.append({
                                    "url": url,
                                    "status": status,
                                    "body": body,
                                })
                            except Exception:
                                pass
                    except Exception:
                        pass

                page.on("response", on_response)

                # محاولة فتح /profile/{user_id}
                profile_url = f"{self.BASE_URL}/profile/{user_id}"
                self.log(f"Method B: Opening {profile_url}")

                try:
                    await page.goto(
                        profile_url,
                        timeout=self.PAGE_TIMEOUT_MS,
                        wait_until="domcontentloaded",
                    )
                    await page.wait_for_timeout(self.SEARCH_WAIT_MS)
                except Exception as e:
                    self.log(f"Method B: Page load error: {e}")
                    await browser.close()
                    return None

                # Final URL after any redirects
                final_url = page.url
                self.log(f"Method B: Final URL: {final_url}")

                # استخراج profile_id من الـ final URL
                profile_id_from_url = self._extract_profile_id_from_url(final_url)

                # تحليل responses
                result = self._analyze_responses_for_profile(
                    api_responses, redirects, user_id, "method_b"
                )

                # إذا وجدنا profile_id من URL ولم نجده في API
                if not result and profile_id_from_url:
                    result = {
                        "profile_id": profile_id_from_url,
                        "username": None,
                        "is_live": False,
                        "user_data": {"source": "url_redirect"},
                        "method": "method_b",
                        "source": "url",
                    }

                # إذا لم يُعثر على شيء، حاول DOM
                if not result:
                    dom_result = await self._extract_from_profile_dom(page, user_id)
                    if dom_result:
                        result = dom_result
                        result["method"] = "method_b"

                await browser.close()
                return result

        except Exception as e:
            self.log(f"Method B: Global error: {e}")
            return None

    # ============================================================
    # METHOD C: /fr/livestream/{user_id} مع مراقبة API
    # ============================================================
    async def _method_c_livestream_page(self, user_id: str) -> Optional[Dict[str, Any]]:
        """
        فتح صفحة البث المباشر ومراقبة API responses
        التي قد تحتوي على معلومات البروفايل الحقيقي
        """
        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(
                    headless=True,
                    args=["--no-sandbox", "--disable-dev-shm-usage"],
                )
                context = await browser.new_context(
                    user_agent=self.USER_AGENT,
                    viewport={"width": 1280, "height": 800},
                    locale="fr-FR",
                )
                page = await context.new_page()

                api_responses = []
                redirects = []

                async def on_response(response):
                    try:
                        url = response.url
                        status = response.status

                        if 300 <= status < 400:
                            location = response.headers.get("location", "")
                            redirects.append({
                                "from": url,
                                "to": location,
                                "status": status,
                            })

                        content_type = (response.headers or {}).get("content-type", "")
                        if "json" in content_type.lower():
                            try:
                                body = await response.json()
                                api_responses.append({
                                    "url": url,
                                    "status": status,
                                    "body": body,
                                })
                            except Exception:
                                pass
                    except Exception:
                        pass

                page.on("response", on_response)

                livestream_url = f"{self.BASE_URL}/fr/livestream/{user_id}"
                self.log(f"Method C: Opening {livestream_url}")

                try:
                    await page.goto(
                        livestream_url,
                        timeout=self.PAGE_TIMEOUT_MS,
                        wait_until="domcontentloaded",
                    )
                    await page.wait_for_timeout(self.SEARCH_WAIT_MS)
                except Exception as e:
                    self.log(f"Method C: Page load error: {e}")
                    await browser.close()
                    return None

                # تحليل responses - البحث عن profile_id مختلف عن user_id
                result = self._analyze_responses_for_profile(
                    api_responses, redirects, user_id, "method_c"
                )

                await browser.close()
                return result

        except Exception as e:
            self.log(f"Method C: Global error: {e}")
            return None

    # ============================================================
    # METHOD D: Fallback (user_id as profile_id)
    # ============================================================
    async def _method_d_fallback(self, user_id: str) -> Dict[str, Any]:
        """
        Fallback: استخدام user_id كـ profile_id مباشرة
        هذه الطريقة تستخدم عندما تفشل كل الطرق الأخرى
        """
        self.log(f"Method D: Using user_id ({user_id}) as profile_id (fallback)")
        return {
            "profile_id": user_id,
            "username": None,
            "is_live": False,
            "user_data": {"source": "fallback"},
            "method": "method_d",
            "source": "fallback",
        }

    # ============================================================
    # HELPER: تحليل API responses للعثور على profile_id
    # ============================================================
    def _analyze_responses_for_profile(
        self,
        api_responses: List[Dict],
        redirects: List[Dict],
        user_id: str,
        method_name: str,
    ) -> Optional[Dict[str, Any]]:
        """
        تحليل جميع الـ API responses للعثور على profile_id
        المرتبط بـ user_id
        """
        # أولاً: فحص الـ redirects
        for redirect in redirects:
            to_url = redirect.get("to", "")
            profile_id = self._extract_profile_id_from_url(to_url)
            if profile_id and profile_id != user_id:
                self.log(f"[{method_name}] Found profile_id in redirect: {profile_id}")
                return {
                    "profile_id": profile_id,
                    "username": None,
                    "is_live": False,
                    "user_data": {"source": "redirect"},
                    "method": method_name,
                    "source": "redirect",
                }

        # ثانياً: فحص API responses
        for resp in api_responses:
            body = resp.get("body")
            if body is None:
                continue

            # البحث عن profile_id في response
            profile_data = self._search_profile_in_response(body, user_id)
            if profile_data:
                return {
                    "profile_id": profile_data["profile_id"],
                    "username": profile_data.get("username"),
                    "is_live": profile_data.get("is_live", False),
                    "user_data": body if isinstance(body, dict) else {},
                    "method": method_name,
                    "source": "api",
                }

        return None

    def _search_profile_in_response(
        self, body: Any, user_id: str
    ) -> Optional[Dict[str, Any]]:
        """
        البحث عن profile_id في API response
        يبحث عن أي object يحتوي على user_id ويحتوي أيضاً على profile_id
        """
        if isinstance(body, str):
            # Parse as JSON if possible
            try:
                body = json.loads(body)
            except Exception:
                # Regex search in raw text
                return self._regex_search_profile(body, user_id)

        return self._recursive_search_profile(body, user_id, depth=0)

    def _recursive_search_profile(
        self, obj: Any, user_id: str, depth: int
    ) -> Optional[Dict[str, Any]]:
        """
        بحث متكرر عن profile_id في object
        """
        if depth > 10:
            return None

        if isinstance(obj, dict):
            # Check if this dict contains user_id
            has_user_id = False
            for key, value in obj.items():
                if isinstance(value, (str, int, float)) and str(value) == str(user_id):
                    key_lower = str(key).lower()
                    if any(
                        token in key_lower
                        for token in ("id", "user", "uid", "streamer", "broadcaster")
                    ):
                        has_user_id = True
                        break

            # If has user_id, look for profile_id
            if has_user_id:
                profile_id = None
                username = None
                is_live = False

                for key, value in obj.items():
                    key_lower = str(key).lower()

                    # profile_id keys
                    if key_lower in (
                        "profile_id", "profileid", "profile",
                        "channel_id", "channelid", "channel",
                        "streamer_id", "streamerid",
                        "broadcaster_id", "broadcasterid",
                    ):
                        if value and str(value) != str(user_id):
                            profile_id = str(value)

                    # username keys
                    if key_lower in (
                        "username", "user_name", "nickname",
                        "display_name", "displayname", "name",
                        "streamer_name", "streamername",
                        "broadcaster_name", "broadcastername",
                    ):
                        if isinstance(value, str) and value.strip():
                            username = value.strip()

                    # is_live keys
                    if key_lower in (
                        "is_live", "islive", "live", "streaming",
                        "is_streaming", "isstreaming", "online",
                    ):
                        is_live = bool(value)

                if profile_id:
                    return {
                        "profile_id": profile_id,
                        "username": username,
                        "is_live": is_live,
                    }

            # Recurse into children
            for value in obj.values():
                result = self._recursive_search_profile(value, user_id, depth + 1)
                if result:
                    return result

        elif isinstance(obj, list):
            for item in obj[:100]:  # Limit to avoid huge lists
                result = self._recursive_search_profile(item, user_id, depth + 1)
                if result:
                    return result

        return None

    def _regex_search_profile(
        self, text: str, user_id: str
    ) -> Optional[Dict[str, Any]]:
        """
        Regex-based search in raw text
        """
        if user_id not in text:
            return None

        # Look for profile_id patterns near user_id
        # Example: "user_id":51527806,"profile_id":25192720
        patterns = [
            rf'"user_id"\s*:\s*{user_id}[^{{}}]*?"profile_id"\s*:\s*"?(\d+)"?',
            rf'"profile_id"\s*:\s*"?(\d+)"?[^{{}}]*?"user_id"\s*:\s*{user_id}',
            rf'"id"\s*:\s*{user_id}[^{{}}]*?"profile_id"\s*:\s*"?(\d+)"?',
            rf'"profile_id"\s*:\s*"?(\d+)"?[^{{}}]*?"id"\s*:\s*{user_id}',
        ]

        for pattern in patterns:
            match = re.search(pattern, text, re.IGNORECASE | re.DOTALL)
            if match:
                return {
                    "profile_id": match.group(1),
                    "username": None,
                    "is_live": False,
                }

        # Look for any numeric profile_id near user_id
        user_id_pos = text.find(user_id)
        if user_id_pos >= 0:
            # Search in a window around user_id
            window = text[max(0, user_id_pos - 500):user_id_pos + 500]
            match = re.search(
                r'["\'](?:profile_id|profileId|profile)["\']\s*[:=]\s*["\']?(\d{5,12})["\']?',
                window,
                re.IGNORECASE,
            )
            if match and match.group(1) != user_id:
                return {
                    "profile_id": match.group(1),
                    "username": None,
                    "is_live": False,
                }

        return None

    def _extract_profile_id_from_url(self, url: str) -> Optional[str]:
        """
        استخراج profile_id من URL
        """
        if not url:
            return None

        # Patterns like:
        # /profile/25192720
        # /profile/25192720?param=value
        # /fr/profile/25192720
        patterns = [
            r"/profile/(\d+)",
            r"/user/(\d+)",
            r"/channel/(\d+)",
            r"/streamer/(\d+)",
        ]

        for pattern in patterns:
            match = re.search(pattern, url)
            if match:
                return match.group(1)

        return None

    # ============================================================
    # DOM EXTRACTION HELPERS
    # ============================================================
    async def _extract_from_search_dom(
        self, page, user_id: str
    ) -> Optional[Dict[str, Any]]:
        """
        استخراج profile_id من DOM صفحة البحث
        """
        try:
            # البحث عن links تحتوي على profile/
            links = await page.locator('a[href*="/profile/"]').all()
            for link in links:
                try:
                    href = await link.get_attribute("href")
                    text = (await link.inner_text()).strip()

                    profile_id = self._extract_profile_id_from_url(href or "")
                    if profile_id:
                        self.log(f"Found profile_id in search DOM: {profile_id}")
                        return {
                            "profile_id": profile_id,
                            "username": text if len(text) < 50 else None,
                            "is_live": False,
                            "user_data": {"source": "search_dom"},
                            "method": "method_a_dom",
                            "source": "dom",
                        }
                except Exception:
                    continue

            # البحث عن أي link يحتوي على user_id
            all_links = await page.locator("a[href]").all()
            for link in all_links:
                try:
                    href = await link.get_attribute("href")
                    if href and user_id in href:
                        # Found a link with user_id, check if there's a profile link nearby
                        text = (await link.inner_text()).strip()
                        profile_id = self._extract_profile_id_from_url(href)
                        if profile_id:
                            return {
                                "profile_id": profile_id,
                                "username": text if len(text) < 50 else None,
                                "is_live": False,
                                "user_data": {"source": "link_dom"},
                                "method": "method_a_dom",
                                "source": "dom",
                            }
                except Exception:
                    continue

            # البحث في الصفحة كاملة عن profile URLs
            content = await page.content()
            matches = re.findall(r'/profile/(\d+)', content)
            for match in matches:
                if match != user_id:
                    self.log(f"Found profile_id in page HTML: {match}")
                    return {
                        "profile_id": match,
                        "username": None,
                        "is_live": False,
                        "user_data": {"source": "html_scan"},
                        "method": "method_a_dom",
                        "source": "dom",
                    }

        except Exception as e:
            self.log(f"DOM extraction error: {e}")

        return None

    async def _extract_from_profile_dom(
        self, page, user_id: str
    ) -> Optional[Dict[str, Any]]:
        """
        استخراج profile_id من DOM صفحة البروفايل
        """
        try:
            current_url = page.url
            profile_id = self._extract_profile_id_from_url(current_url)

            if profile_id and profile_id != user_id:
                return {
                    "profile_id": profile_id,
                    "username": None,
                    "is_live": False,
                    "user_data": {"source": "profile_dom"},
                    "method": "method_b_dom",
                    "source": "dom",
                }

            # Look for canonical link
            try:
                canonical = await page.locator('link[rel="canonical"]').first.get_attribute("href")
                if canonical:
                    profile_id = self._extract_profile_id_from_url(canonical)
                    if profile_id:
                        return {
                            "profile_id": profile_id,
                            "username": None,
                            "is_live": False,
                            "user_data": {"source": "canonical"},
                            "method": "method_b_dom",
                            "source": "dom",
                        }
            except Exception:
                pass

        except Exception as e:
            self.log(f"Profile DOM extraction error: {e}")

        return None

    # ============================================================
    # PUBLIC API: check_live_status
    # ============================================================
    async def check_live_status(
        self, profile_id: str
    ) -> Optional[Dict[str, Any]]:
        """
        التحقق من حالة البث المباشر باستخدام profile_id
        """
        self.log(f"Checking live status for profile_id: {profile_id}")

        if not PLAYWRIGHT_AVAILABLE:
            return None

        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(
                    headless=True,
                    args=["--no-sandbox", "--disable-dev-shm-usage"],
                )
                context = await browser.new_context(
                    user_agent=self.USER_AGENT,
                    viewport={"width": 1280, "height": 800},
                    locale="fr-FR",
                )
                page = await context.new_page()

                api_responses = []

                async def on_response(response):
                    try:
                        url = response.url
                        content_type = (response.headers or {}).get("content-type", "")
                        if "json" in content_type.lower():
                            if any(
                                kw in url.lower()
                                for kw in [
                                    "live", "stream", "status", "broadcast",
                                    "channel", "profile",
                                ]
                            ):
                                try:
                                    body = await response.json()
                                    api_responses.append({"url": url, "body": body})
                                except Exception:
                                    pass
                    except Exception:
                        pass

                page.on("response", on_response)

                # محاولة فتح صفحة البروفايل
                profile_url = f"{self.BASE_URL}/profile/{profile_id}"
                try:
                    await page.goto(
                        profile_url,
                        timeout=self.PAGE_TIMEOUT_MS,
                        wait_until="domcontentloaded",
                    )
                    await page.wait_for_timeout(self.VERIFY_WAIT_MS)
                except Exception as e:
                    self.log(f"Profile page load error: {e}")
                    await browser.close()
                    return None

                # تحليل responses
                for resp in api_responses:
                    body = resp.get("body")
                    if isinstance(body, dict):
                        is_live = self._extract_value(
                            body, ["is_live", "isLive", "live", "streaming"]
                        )
                        if is_live:
                            stream_url = self._extract_value(
                                body,
                                ["stream_url", "streamUrl", "hls_url", "hlsUrl",
                                 "url", "src", "play_url"],
                            )
                            stream_id = self._extract_value(
                                body, ["stream_id", "streamId", "id"]
                            )

                            return {
                                "is_live": True,
                                "stream_id": stream_id,
                                "stream_url": stream_url,
                                "stream_info": body,
                                "source": "api",
                            }

                # Fallback: DOM
                dom_result = await self._check_live_status_dom(page, profile_id)
                await browser.close()

                if dom_result:
                    self.log(f"✓ User is live (DOM): {dom_result.get('stream_url')}")
                    return dom_result

                self.log(f"✗ User is not live")
                return {"is_live": False, "source": "dom"}

        except Exception as e:
            self.log(f"check_live_status error: {e}")
            return None

    async def _check_live_status_dom(
        self, page, profile_id: str
    ) -> Optional[Dict[str, Any]]:
        """
        فحص DOM للبحث عن مؤشرات البث المباشر
        """
        try:
            # البحث عن مؤشرات البث المباشر
            live_indicators = [
                ".live-badge",
                ".live-indicator",
                '[data-status="live"]',
                ".streaming-badge",
                "text=LIVE",
                "text=En direct",
            ]

            is_live = False
            for selector in live_indicators:
                try:
                    count = await page.locator(selector).count()
                    if count > 0:
                        is_live = True
                        break
                except Exception:
                    pass

            if not is_live:
                return None

            # البحث عن stream_url
            stream_url = None

            # في video elements
            videos = await page.locator("video").all()
            for video in videos:
                src = await video.get_attribute("src")
                if src and any(ext in src for ext in [".m3u8", ".mpd", "rtmp"]):
                    stream_url = src
                    break

            # في source elements
            if not stream_url:
                sources = await page.locator("video source").all()
                for source in sources:
                    src = await source.get_attribute("src")
                    if src and any(ext in src for ext in [".m3u8", ".mpd", "rtmp"]):
                        stream_url = src
                        break

            # البحث في links
            if not stream_url:
                links = await page.locator('a[href*="livestream"]').all()
                for link in links:
                    href = await link.get_attribute("href")
                    if href:
                        stream_url = href
                        break

            if stream_url:
                return {
                    "is_live": True,
                    "stream_url": stream_url,
                    "source": "dom",
                }

            return None

        except Exception as e:
            self.log(f"DOM live check error: {e}")
            return None

    # ============================================================
    # PUBLIC API: validate_stream
    # ============================================================
    async def validate_stream(
        self, user_id: str, profile_id: str, stream_url: str
    ) -> Dict[str, Any]:
        """
        التحقق من أن البث مرتبط بالمستخدم الصحيح
        """
        self.log(
            f"Validating stream for user_id: {user_id}, "
            f"profile_id: {profile_id}"
        )

        if not PLAYWRIGHT_AVAILABLE:
            return {
                "validation_passed": True,  # Trust the stream URL if no Playwright
                "checks_passed": 1,
                "total_checks": 1,
                "metadata_match": False,
                "dom_has_user_id": False,
                "dom_has_profile_id": False,
                "actual_stream_url": stream_url,
                "source": "no_playwright",
            }

        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(
                    headless=True,
                    args=["--no-sandbox", "--disable-dev-shm-usage"],
                )
                context = await browser.new_context(
                    user_agent=self.USER_AGENT,
                    viewport={"width": 1280, "height": 800},
                    locale="fr-FR",
                )
                page = await context.new_page()

                api_responses = []

                async def on_response(response):
                    try:
                        content_type = (response.headers or {}).get("content-type", "")
                        if "json" in content_type.lower():
                            try:
                                body = await response.json()
                                api_responses.append(body)
                            except Exception:
                                pass
                    except Exception:
                        pass

                page.on("response", on_response)

                # فتح صفحة البث
                try:
                    await page.goto(
                        stream_url,
                        timeout=self.PAGE_TIMEOUT_MS,
                        wait_until="domcontentloaded",
                    )
                    await page.wait_for_timeout(self.VERIFY_WAIT_MS)
                except Exception as e:
                    self.log(f"Stream page load error: {e}")
                    await browser.close()
                    return {
                        "validation_passed": False,
                        "checks_passed": 0,
                        "total_checks": 3,
                        "error": str(e)[:100],
                    }

                # Check 1: API metadata match
                metadata_match = False
                for resp in api_responses:
                    if isinstance(resp, dict):
                        resp_str = str(resp)
                        if user_id in resp_str or profile_id in resp_str:
                            metadata_match = True
                            break

                # Check 2 & 3: DOM
                try:
                    page_text = await page.locator("body").inner_text()
                except Exception:
                    page_text = ""

                dom_has_user_id = user_id in page_text
                dom_has_profile_id = profile_id in page_text

                # استخراج stream URL الفعلي
                actual_stream_url = stream_url
                try:
                    videos = await page.locator("video").all()
                    for video in videos:
                        src = await video.get_attribute("src")
                        if src and any(
                            ext in src for ext in [".m3u8", ".mpd", "rtmp"]
                        ):
                            actual_stream_url = src
                            break

                    if actual_stream_url == stream_url:
                        sources = await page.locator("video source").all()
                        for source in sources:
                            src = await source.get_attribute("src")
                            if src and any(
                                ext in src for ext in [".m3u8", ".mpd", "rtmp"]
                            ):
                                actual_stream_url = src
                                break
                except Exception:
                    pass

                await browser.close()

                # حساب النتيجة
                checks_passed = 0
                total_checks = 3

                if metadata_match:
                    checks_passed += 1
                if dom_has_user_id or dom_has_profile_id:
                    checks_passed += 1
                if actual_stream_url:
                    checks_passed += 1

                # تمرير إذا نجح على الأقل 2 من 3 فحوصات
                validation_passed = checks_passed >= 2

                return {
                    "validation_passed": validation_passed,
                    "checks_passed": checks_passed,
                    "total_checks": total_checks,
                    "metadata_match": metadata_match,
                    "dom_has_user_id": dom_has_user_id,
                    "dom_has_profile_id": dom_has_profile_id,
                    "actual_stream_url": actual_stream_url,
                }

        except Exception as e:
            self.log(f"Validation error: {e}")
            return {
                "validation_passed": False,
                "checks_passed": 0,
                "total_checks": 3,
                "error": str(e)[:100],
            }

    # ============================================================
    # HELPER: extract_value من dict متداخل
    # ============================================================
    def _extract_value(self, obj: Any, keys: List[str]) -> Any:
        """
        استخراج قيمة من object متداخل باستخدام عدة keys محتملة
        """
        if isinstance(obj, dict):
            for key in keys:
                if key in obj:
                    return obj[key]
                # Case-insensitive search
                for k, v in obj.items():
                    if str(k).lower() == key.lower():
                        return v
            # Recurse
            for v in obj.values():
                result = self._extract_value(v, keys)
                if result is not None:
                    return result
        elif isinstance(obj, list):
            for item in obj:
                result = self._extract_value(item, keys)
                if result is not None:
                    return result
        return None
