"""
SuperLive Discovery Module
اكتشاف وربط user_id مع profile_id والبث المباشر
"""

import asyncio
import json
from playwright.async_api import async_playwright
from typing import Dict, List, Optional, Any


class SuperLiveDiscovery:
    """
    فئة اكتشاف وربط المعرفات في SuperLive
    """
    
    BASE_URL = "https://superlivetv.com"
    
    def __init__(self):
        self.cache = {}  # تخزين مؤقت للـ mappings
    
    async def discover_profile_id(self, user_id: str) -> Optional[Dict[str, Any]]:
        """
        اكتشاف profile_id من user_id
        
        Returns:
            dict: {
                'profile_id': str,
                'username': str,
                'is_live': bool,
                'user_data': dict
            }
        """
        # التحقق من الكاش
        if user_id in self.cache:
            print(f"[Cache] Using cached profile_id for user_id: {user_id}")
            return self.cache[user_id]
        
        print(f"[Discovery] Discovering profile_id for user_id: {user_id}")
        
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(
                user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
            )
            page = await context.new_page()
            
            # اعتراض API responses
            api_responses = []
            
            async def capture_response(response):
                url = response.url
                try:
                    if 'json' in response.headers.get('content-type', ''):
                        if any(kw in url.lower() for kw in [
                            'api', 'search', 'user', 'profile', 'graphql'
                        ]):
                            body = await response.json()
                            api_responses.append({
                                'url': url,
                                'status': response.status,
                                'body': body
                            })
                except:
                    pass
            
            page.on('response', capture_response)
            
            # فتح صفحة البحث
            search_url = f"{self.BASE_URL}/fr/search"
            await page.goto(search_url, wait_until='networkidle')
            await page.wait_for_timeout(2000)
            
            # البحث عن user_id
            try:
                # محاولة 1: input[type="search"]
                search_input = page.locator('input[type="search"]')
                if await search_input.count() == 0:
                    # محاولة 2: input[placeholder*="بحث"]
                    search_input = page.locator('input[placeholder*="بحث"], input[placeholder*="search"], input[placeholder*="Search"]')
                
                if await search_input.count() > 0:
                    await search_input.first.fill(user_id)
                    await search_input.first.press('Enter')
                    await page.wait_for_timeout(3000)
                else:
                    print(f"[Discovery] Search input not found")
            except Exception as e:
                print(f"[Discovery] Error during search: {e}")
            
            # تحليل API responses
            result = None
            
            for resp in api_responses:
                body = resp['body']
                
                if isinstance(body, dict):
                    # البحث عن profile_id
                    profile_id = self._extract_value(body, 'profile_id')
                    
                    if not profile_id:
                        profile_id = self._extract_value(body, 'profileId')
                    
                    if not profile_id:
                        # البحث في data
                        if 'data' in body:
                            profile_id = self._extract_value(body['data'], 'profile_id')
                            if not profile_id:
                                profile_id = self._extract_value(body['data'], 'profileId')
                    
                    if not profile_id:
                        # البحث في results
                        if 'results' in body and isinstance(body['results'], list):
                            for item in body['results']:
                                if isinstance(item, dict):
                                    if str(item.get('id')) == user_id or str(item.get('user_id')) == user_id:
                                        profile_id = item.get('profile_id') or item.get('profileId')
                                        if profile_id:
                                            break
                    
                    if profile_id:
                        # استخراج بيانات إضافية
                        username = self._extract_value(body, 'username')
                        if not username:
                            username = self._extract_value(body, 'display_name')
                        if not username:
                            username = self._extract_value(body, 'displayName')
                        
                        is_live = self._extract_value(body, 'is_live')
                        if is_live is None:
                            is_live = self._extract_value(body, 'isLive')
                        if is_live is None:
                            is_live = False
                        
                        result = {
                            'profile_id': str(profile_id),
                            'username': username,
                            'is_live': bool(is_live),
                            'user_data': body,
                            'source': 'api'
                        }
                        break
            
            await browser.close()
        
        # تخزين في الكاش
        if result:
            self.cache[user_id] = result
            print(f"[Discovery] ✓ Profile ID discovered: {result['profile_id']}")
        else:
            print(f"[Discovery] ✗ Could not discover profile_id for user_id: {user_id}")
        
        return result
    
    async def check_live_status(self, profile_id: str) -> Optional[Dict[str, Any]]:
        """
        التحقق من حالة البث المباشر
        
        Returns:
            dict: {
                'is_live': bool,
                'stream_id': str,
                'stream_url': str,
                'stream_info': dict
            }
        """
        print(f"[LiveStatus] Checking live status for profile_id: {profile_id}")
        
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(
                user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
            )
            page = await context.new_page()
            
            api_responses = []
            
            async def capture_response(response):
                url = response.url
                try:
                    if 'json' in response.headers.get('content-type', ''):
                        if any(kw in url.lower() for kw in [
                            'live', 'stream', 'status', 'broadcast'
                        ]):
                            body = await response.json()
                            api_responses.append({
                                'url': url,
                                'body': body
                            })
                except:
                    pass
            
            page.on('response', capture_response)
            
            # فتح صفحة البروفايل
            profile_url = f"{self.BASE_URL}/profile/{profile_id}"
            await page.goto(profile_url, wait_until='networkidle')
            await page.wait_for_timeout(3000)
            
            # تحليل responses
            result = None
            
            for resp in api_responses:
                body = resp['body']
                
                if isinstance(body, dict):
                    is_live = self._extract_value(body, 'is_live')
                    if is_live is None:
                        is_live = self._extract_value(body, 'isLive')
                    if is_live is None:
                        is_live = self._extract_value(body, 'live')
                    
                    if is_live:
                        stream_id = self._extract_value(body, 'stream_id')
                        if not stream_id:
                            stream_id = self._extract_value(body, 'streamId')
                        
                        stream_url = self._extract_value(body, 'stream_url')
                        if not stream_url:
                            stream_url = self._extract_value(body, 'streamUrl')
                        if not stream_url:
                            stream_url = self._extract_value(body, 'hls_url')
                        if not stream_url:
                            stream_url = self._extract_value(body, 'hlsUrl')
                        
                        result = {
                            'is_live': True,
                            'stream_id': stream_id,
                            'stream_url': stream_url,
                            'stream_info': body,
                            'source': 'api'
                        }
                        break
            
            # Fallback: فحص DOM
            if not result:
                dom_result = await self._check_live_status_dom(page, profile_id)
                if dom_result:
                    result = dom_result
            
            await browser.close()
        
        if result:
            print(f"[LiveStatus] ✓ User is live: {result['is_live']}")
        else:
            print(f"[LiveStatus] ✗ User is not live or status unknown")
        
        return result
    
    async def _check_live_status_dom(self, page, profile_id: str) -> Optional[Dict[str, Any]]:
        """
        طريقة بديلة: فحص DOM
        """
        try:
            # البحث عن مؤشرات البث المباشر
            live_indicators = [
                '.live-badge',
                '.live-indicator',
                '[data-status="live"]',
                '.streaming-badge',
                'text="LIVE"',
                'text="En direct"',
                'text="مباشر"'
            ]
            
            is_live = False
            for selector in live_indicators:
                try:
                    count = await page.locator(selector).count()
                    if count > 0:
                        is_live = True
                        break
                except:
                    pass
            
            if not is_live:
                return None
            
            # البحث عن stream_url
            stream_url = None
            
            # في video elements
            videos = await page.locator('video').all()
            for video in videos:
                src = await video.get_attribute('src')
                if src and any(ext in src for ext in ['.m3u8', '.mpd', 'rtmp']):
                    stream_url = src
                    break
            
            # في source elements
            if not stream_url:
                sources = await page.locator('video source').all()
                for source in sources:
                    src = await source.get_attribute('src')
                    if src and any(ext in src for ext in ['.m3u8', '.mpd', 'rtmp']):
                        stream_url = src
                        break
            
            if stream_url:
                return {
                    'is_live': True,
                    'stream_url': stream_url,
                    'source': 'dom'
                }
            
            return None
        
        except Exception as e:
            print(f"[LiveStatus] DOM check error: {e}")
            return None
    
    async def validate_stream(self, user_id: str, profile_id: str, stream_url: str) -> Dict[str, Any]:
        """
        التحقق من أن البث مرتبط بالمستخدم الصحيح
        """
        print(f"[Validation] Validating stream for user_id: {user_id}, profile_id: {profile_id}")
        
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(
                user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
            )
            page = await context.new_page()
            
            api_responses = []
            
            async def capture_response(response):
                url = response.url
                try:
                    if 'json' in response.headers.get('content-type', ''):
                        body = await response.json()
                        api_responses.append(body)
                except:
                    pass
            
            page.on('response', capture_response)
            
            # فتح صفحة البث
            await page.goto(stream_url, wait_until='networkidle')
            await page.wait_for_timeout(5000)
            
            # التحقق من API metadata
            metadata_match = False
            for resp in api_responses:
                if isinstance(resp, dict):
                    resp_str = str(resp)
                    if user_id in resp_str or profile_id in resp_str:
                        metadata_match = True
                        break
            
            # التحقق من DOM
            page_text = await page.locator('body').inner_text()
            dom_has_user_id = user_id in page_text
            dom_has_profile_id = profile_id in page_text
            
            # استخراج stream URL الفعلي
            actual_stream_url = None
            videos = await page.locator('video').all()
            for video in videos:
                src = await video.get_attribute('src')
                if src and any(ext in src for ext in ['.m3u8', '.mpd', 'rtmp']):
                    actual_stream_url = src
                    break
            
            if not actual_stream_url:
                sources = await page.locator('video source').all()
                for source in sources:
                    src = await source.get_attribute('src')
                    if src and any(ext in src for ext in ['.m3u8', '.mpd', 'rtmp']):
                        actual_stream_url = src
                        break
            
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
            
            validation_passed = checks_passed >= 2
            
            return {
                'validation_passed': validation_passed,
                'checks_passed': checks_passed,
                'total_checks': total_checks,
                'metadata_match': metadata_match,
                'dom_has_user_id': dom_has_user_id,
                'dom_has_profile_id': dom_has_profile_id,
                'actual_stream_url': actual_stream_url
            }
    
    def _extract_value(self, obj: Any, key: str) -> Any:
        """
        استخراج قيمة من object متداخل
        """
        if isinstance(obj, dict):
            if key in obj:
                return obj[key]
            for v in obj.values():
                result = self._extract_value(v, key)
                if result is not None:
                    return result
        elif isinstance(obj, list):
            for item in obj:
                result = self._extract_value(item, key)
                if result is not None:
                    return result
        return None


# مثال على الاستخدام
async def main():
    discovery = SuperLiveDiscovery()
    
    # اختبار مع user_id من المثال
    user_id = "51527806"
    
    # Phase 1: Discovery
    profile_info = await discovery.discover_profile_id(user_id)
    if not profile_info:
        print("Failed to discover profile_id")
        return
    
    print(f"\n{'='*60}")
    print(f"Phase 1 Results:")
    print(f"  User ID: {user_id}")
    print(f"  Profile ID: {profile_info['profile_id']}")
    print(f"  Username: {profile_info.get('username')}")
    print(f"  Is Live: {profile_info.get('is_live')}")
    print(f"{'='*60}\n")
    
    # Phase 2: Live Status Check
    live_status = await discovery.check_live_status(profile_info['profile_id'])
    if not live_status or not live_status.get('is_live'):
        print("User is not live")
        return
    
    print(f"\n{'='*60}")
    print(f"Phase 2 Results:")
    print(f"  Is Live: {live_status['is_live']}")
    print(f"  Stream ID: {live_status.get('stream_id')}")
    print(f"  Stream URL: {live_status.get('stream_url')}")
    print(f"{'='*60}\n")
    
    # Phase 3: Validation
    stream_url = live_status.get('stream_url')
    if not stream_url:
        print("No stream URL found")
        return
    
    validation = await discovery.validate_stream(
        user_id,
        profile_info['profile_id'],
        stream_url
    )
    
    print(f"\n{'='*60}")
    print(f"Phase 3 Results:")
    print(f"  Validation Passed: {validation['validation_passed']}")
    print(f"  Checks Passed: {validation['checks_passed']}/{validation['total_checks']}")
    print(f"  Metadata Match: {validation['metadata_match']}")
    print(f"  DOM Has User ID: {validation['dom_has_user_id']}")
    print(f"  DOM Has Profile ID: {validation['dom_has_profile_id']}")
    print(f"  Actual Stream URL: {validation['actual_stream_url']}")
    print(f"{'='*60}\n")
    
    if validation['validation_passed']:
        print("✓ Stream validated successfully! Ready for recording.")
        # هنا يمكن بدء التسجيل
    else:
        print("✗ Stream validation failed. Do not record.")


if __name__ == "__main__":
    asyncio.run(main())
