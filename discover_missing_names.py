#!/usr/bin/env python3
"""
Discover Missing Names - One-time script to update all watchlist names
Uses multiple sources to maximize name discovery:
1. Method A (Search API) - primary
2. Direct profile page title - secondary  
3. Livestream page - fallback
4. Local watchlist.json - last resort
"""

import asyncio
import json
import os
import re
import sys
import urllib.request
import urllib.error
from pathlib import Path

# Add support for both import styles
try:
    from superlive_discovery import SuperLiveDiscovery
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False
    print("⚠️ superlive_discovery not available")

# Also try to import playwright directly
try:
    from playwright.async_api import async_playwright
    DIRECT_PLAYWRIGHT = True
except ImportError:
    DIRECT_PLAYWRIGHT = False

# Configuration
WORKER_API_URL = os.environ.get("WORKER_API_URL", "").strip()
AUTO_API_TOKEN = os.environ.get("AUTO_API_TOKEN", "").strip()
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
BASE_URL = "https://superlivetv.com"

WATCHLIST_FILE = Path("data/watchlist.json")
LOCAL_FALLBACK_FILE = Path("watchlist.json")

print(f"[INIT] WORKER_API_URL: {'SET' if WORKER_API_URL else 'MISSING'}")
print(f"[INIT] AUTO_API_TOKEN: {'SET' if AUTO_API_TOKEN else 'MISSING'}")
print(f"[INIT] Playwright: {'✓' if DIRECT_PLAYWRIGHT else '✗'}")

if WORKER_API_URL and WORKER_API_URL.endswith('/'):
    WORKER_API_URL = WORKER_API_URL[:-1]


def get_watchlist_from_worker() -> list:
    """Get watchlist from Worker API (KV - authoritative source)"""
    if not WORKER_API_URL or not AUTO_API_TOKEN:
        return []
    url = f"{WORKER_API_URL}/api/watchlist"
    req = urllib.request.Request(url, headers={
        'X-Auto-Token': AUTO_API_TOKEN,
        'Content-Type': 'application/json',
        'User-Agent': 'SuperLive-DiscoverNames/1.0',
        'Accept': 'application/json'
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            if response.status == 200:
                data = json.loads(response.read().decode('utf-8'))
                return data.get("watchlist", [])
    except Exception as e:
        print(f"[WORKER] ✗ Error: {e}")
    return []


def get_local_watchlist() -> dict:
    """Get names from local watchlist.json files as fallback"""
    names = {}
    for path in [WATCHLIST_FILE, LOCAL_FALLBACK_FILE]:
        if path.exists():
            try:
                with open(path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    for entry in data.get("watchlist", []):
                        uid = str(entry.get("stream_id"))
                        name = entry.get("display_name")
                        if name and name != uid:
                            names[uid] = name
            except Exception as e:
                print(f"[LOCAL] ⚠️ Error reading {path}: {e}")
    return names


def update_display_name(user_id: str, display_name: str) -> bool:
    """Update display_name in Cloudflare KV"""
    if not WORKER_API_URL or not AUTO_API_TOKEN:
        return False
    if not display_name or display_name == user_id:
        return False
    
    url = f"{WORKER_API_URL}/api/update-display-name/{user_id}"
    payload = json.dumps({"display_name": display_name}).encode('utf-8')
    req = urllib.request.Request(url, data=payload, headers={
        'X-Auto-Token': AUTO_API_TOKEN,
        'Content-Type': 'application/json',
        'User-Agent': 'SuperLive-DiscoverNames/1.0',
        'Accept': 'application/json'
    })
    try:
        with urllib.request.urlopen(req, timeout=15) as response:
            if response.status == 200:
                data = json.loads(response.read().decode('utf-8'))
                return data.get("success", False)
    except urllib.error.HTTPError as e:
        if e.code != 400:
            print(f"[UPDATE] ✗ HTTP {e.code} for {user_id}")
    except Exception as e:
        print(f"[UPDATE] ✗ Error for {user_id}: {e}")
    return False


async def discover_name_method_a(user_id: str) -> str:
    """Method A: Use search API to find username"""
    if not DIRECT_PLAYWRIGHT:
        return None
    
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=['--no-sandbox', '--disable-setuid-sandbox', '--disable-dev-shm-usage']
            )
            context = await browser.new_context(
                user_agent=USER_AGENT,
                viewport={'width': 1280, 'height': 800},
                locale='fr-FR'
            )
            page = await context.new_page()
            
            # Capture API responses
            api_data = []
            async def on_response(response):
                try:
                    ct = (response.headers or {}).get("content-type", "")
                    if "json" in ct.lower():
                        try:
                            body = await response.json()
                            api_data.append({"url": response.url, "body": body})
                        except:
                            pass
                except:
                    pass
            
            page.on("response", on_response)
            
            search_url = f"{BASE_URL}/fr/search?q={user_id}"
            await page.goto(search_url, timeout=30000, wait_until="domcontentloaded")
            await page.wait_for_timeout(3000)
            
            # Search API responses for username
            username = None
            for resp in api_data:
                body = resp.get("body", {})
                username = _extract_username_from_json(body, user_id)
                if username:
                    break
            
            # Also try DOM extraction
            if not username:
                try:
                    username = await page.evaluate('''() => {
                        const results = document.querySelectorAll('[class*="result"], [class*="user"], [class*="item"]');
                        for (const r of results) {
                            const text = r.textContent || '';
                            if (text.includes("@" + document.querySelector('[name="q"]')?.value || '')) {
                                const nameEl = r.querySelector('[class*="name"], [class*="username"], h3, h4');
                                if (nameEl && nameEl.textContent.trim()) {
                                    return nameEl.textContent.trim();
                                }
                            }
                        }
                        return null;
                    }''')
                except:
                    pass
            
            await browser.close()
            return username
    except Exception as e:
        print(f"[METHOD-A] ✗ Error for {user_id}: {e}")
        return None


def _extract_username_from_json(obj, target_user_id, depth=0):
    """Recursively extract username from JSON"""
    if depth > 10:
        return None
    if isinstance(obj, dict):
        # Check if this is the right user
        user_match = False
        for k, v in obj.items():
            if str(k).lower() in ('user_id', 'userid', 'id', 'stream_id') and str(v) == str(target_user_id):
                user_match = True
                break
        
        # Look for username fields
        for k, v in obj.items():
            kl = str(k).lower()
            if kl in ('username', 'nickname', 'display_name', 'name', 'user_name'):
                if isinstance(v, str) and v.strip() and v != str(target_user_id):
                    if user_match or not any(str(val) == str(target_user_id) for val in obj.values() if isinstance(val, (str, int))):
                        return v.strip()
        
        # Recurse
        for v in obj.values():
            result = _extract_username_from_json(v, target_user_id, depth + 1)
            if result:
                return result
    elif isinstance(obj, list):
        for item in obj:
            result = _extract_username_from_json(item, target_user_id, depth + 1)
            if result:
                return result
    return None


async def discover_name_method_profile_title(user_id: str) -> str:
    """Method B: Visit direct profile page and extract title"""
    if not DIRECT_PLAYWRIGHT:
        return None
    
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=['--no-sandbox', '--disable-setuid-sandbox']
            )
            context = await browser.new_context(
                user_agent=USER_AGENT,
                viewport={'width': 1280, 'height': 800},
                locale='fr-FR'
            )
            page = await context.new_page()
            
            # Try different profile URL patterns
            urls_to_try = [
                f"{BASE_URL}/fr/profile/{user_id}",
                f"{BASE_URL}/fr/user/{user_id}",
                f"{BASE_URL}/fr/{user_id}",
            ]
            
            username = None
            for url in urls_to_try:
                try:
                    response = await page.goto(url, timeout=20000, wait_until="domcontentloaded")
                    if response and response.status == 200:
                        await page.wait_for_timeout(2000)
                        
                        # Extract from title tag
                        title = await page.title()
                        if title:
                            # Remove common suffixes like " - SuperLive"
                            cleaned = re.split(r'\s*[-|]\s*(?:SuperLive|Super Live|live).*$', title, flags=re.IGNORECASE)[0].strip()
                            if cleaned and len(cleaned) > 1 and cleaned != str(user_id):
                                username = cleaned
                                break
                        
                        # Try to extract from DOM
                        if not username:
                            try:
                                username = await page.evaluate('''() => {
                                    const selectors = [
                                        '[class*="username"]',
                                        '[class*="display-name"]',
                                        '[class*="user-name"]',
                                        'h1.profile-name',
                                        '.profile-header h1',
                                        '[data-testid="username"]'
                                    ];
                                    for (const sel of selectors) {
                                        const el = document.querySelector(sel);
                                        if (el && el.textContent && el.textContent.trim()) {
                                            return el.textContent.trim();
                                        }
                                    }
                                    return null;
                                }''')
                                if username and username != str(user_id):
                                    break
                                username = None
                            except:
                                pass
                except Exception:
                    continue
            
            await browser.close()
            return username
    except Exception as e:
        print(f"[METHOD-B] ✗ Error for {user_id}: {e}")
        return None


async def discover_name_method_livestream(user_id: str) -> str:
    """Method C: Check livestream page for username (works even if offline)"""
    if not DIRECT_PLAYWRIGHT:
        return None
    
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=['--no-sandbox', '--disable-setuid-sandbox']
            )
            context = await browser.new_context(
                user_agent=USER_AGENT,
                viewport={'width': 1280, 'height': 800},
                locale='fr-FR'
            )
            page = await context.new_page()
            
            # Capture API responses
            api_data = []
            async def on_response(response):
                try:
                    ct = (response.headers or {}).get("content-type", "")
                    if "json" in ct.lower():
                        try:
                            body = await response.json()
                            api_data.append(body)
                        except:
                            pass
                except:
                    pass
            
            page.on("response", on_response)
            
            url = f"{BASE_URL}/fr/livestream/{user_id}"
            await page.goto(url, timeout=20000, wait_until="domcontentloaded")
            await page.wait_for_timeout(3000)
            
            username = None
            
            # Try API responses
            for body in api_data:
                extracted = _extract_username_from_json(body, user_id)
                if extracted:
                    username = extracted
                    break
            
            # Try title tag
            if not username:
                title = await page.title()
                if title:
                    cleaned = re.split(r'\s*[-|]\s*(?:SuperLive|Super Live|live|BROADCAST).*$', title, flags=re.IGNORECASE)[0].strip()
                    if cleaned and cleaned != str(user_id) and len(cleaned) > 1:
                        username = cleaned
            
            # Try DOM
            if not username:
                try:
                    username = await page.evaluate('''() => {
                        const selectors = [
                            '[class*="username"]',
                            '[class*="display-name"]',
                            '[class*="host-name"]',
                            '[class*="streamer"]',
                            '.live-user-name',
                            'h1',
                            '.profile-name'
                        ];
                        for (const sel of selectors) {
                            const el = document.querySelector(sel);
                            if (el && el.textContent && el.textContent.trim()) {
                                const text = el.textContent.trim();
                                if (text.length > 1 && text.length < 100) {
                                    return text;
                                }
                            }
                        }
                        return null;
                    }''')
                except:
                    pass
            
            await browser.close()
            return username if username != str(user_id) else None
    except Exception as e:
        print(f"[METHOD-C] ✗ Error for {user_id}: {e}")
        return None


async def discover_all_names(users_to_check: list, local_names: dict) -> dict:
    """Discover names for all users using multiple methods"""
    results = {}
    total = len(users_to_check)
    
    for idx, user in enumerate(users_to_check, 1):
        user_id = str(user.get("stream_id"))
        current_name = user.get("display_name")
        
        print(f"\n[{idx}/{total}] Processing {user_id}...")
        
        # Skip if already has a valid name
        if current_name and current_name != user_id and len(current_name) > 1:
            print(f"  ✓ Already has name: {current_name}")
            results[user_id] = {"status": "skipped", "name": current_name}
            continue
        
        # Try local fallback first (fast)
        if user_id in local_names:
            local_name = local_names[user_id]
            print(f"  📁 Found in local file: {local_name}")
            if update_display_name(user_id, local_name):
                print(f"  ✓ Updated in KV")
                results[user_id] = {"status": "updated_local", "name": local_name}
                continue
        
        # Try Method A (search API)
        print(f"  🔍 Trying Method A (Search)...")
        name_a = await discover_name_method_a(user_id)
        if name_a:
            print(f"  ✓ Method A found: {name_a}")
            if update_display_name(user_id, name_a):
                results[user_id] = {"status": "updated_a", "name": name_a}
                continue
        
        # Try Method B (direct profile page)
        print(f"  🌐 Trying Method B (Profile page)...")
        name_b = await discover_name_method_profile_title(user_id)
        if name_b:
            print(f"  ✓ Method B found: {name_b}")
            if update_display_name(user_id, name_b):
                results[user_id] = {"status": "updated_b", "name": name_b}
                continue
        
        # Try Method C (livestream page)
        print(f"  📺 Trying Method C (Livestream page)...")
        name_c = await discover_name_method_livestream(user_id)
        if name_c:
            print(f"  ✓ Method C found: {name_c}")
            if update_display_name(user_id, name_c):
                results[user_id] = {"status": "updated_c", "name": name_c}
                continue
        
        # All methods failed
        print(f"  ✗ All methods failed for {user_id}")
        results[user_id] = {"status": "failed", "name": None}
        
        # Small delay between users
        await asyncio.sleep(1)
    
    return results


async def main():
    print("=" * 70)
    print("DISCOVER MISSING NAMES - One-time Update Script")
    print("=" * 70)
    
    # Get watchlist from Worker (authoritative)
    watchlist = get_watchlist_from_worker()
    if not watchlist:
        print("[ERROR] Could not load watchlist from Worker")
        return
    
    print(f"\n[WATCHLIST] Loaded {len(watchlist)} users from Worker KV")
    
    # Get local names as fallback
    local_names = get_local_watchlist()
    print(f"[LOCAL] Found {len(local_names)} names in local files")
    
    # Find users with missing names
    users_needing_update = []
    users_with_names = []
    
    for user in watchlist:
        user_id = str(user.get("stream_id"))
        name = user.get("display_name")
        
        if not name or name == user_id or len(name) <= 1:
            users_needing_update.append(user)
        else:
            users_with_names.append(user)
    
    print(f"\n[STATS] Users with names: {len(users_with_names)}")
    print(f"[STATS] Users needing update: {len(users_needing_update)}")
    
    if not users_needing_update:
        print("\n✓ All users already have names - nothing to do!")
        return
    
    print(f"\n[USERS TO UPDATE]:")
    for u in users_needing_update:
        print(f"  • {u.get('stream_id')}")
    
    # Run discovery
    print(f"\n{'=' * 70}")
    print("STARTING DISCOVERY")
    print("=" * 70)
    
    results = await discover_all_names(users_needing_update, local_names)
    
    # Summary
    print(f"\n{'=' * 70}")
    print("SUMMARY")
    print("=" * 70)
    
    updated = sum(1 for r in results.values() if r['status'].startswith('updated'))
    skipped = sum(1 for r in results.values() if r['status'] == 'skipped')
    failed = sum(1 for r in results.values() if r['status'] == 'failed')
    
    print(f"✓ Updated: {updated}")
    print(f"→ Skipped: {skipped}")
    print(f"✗ Failed: {failed}")
    
    if failed > 0:
        print(f"\n[FAILED USERS] (could not discover name):")
        for uid, r in results.items():
            if r['status'] == 'failed':
                print(f"  • {uid}")
        print("\n💡 These may be:")
        print("  - Deleted/suspended accounts")
        print("  - New accounts not yet indexed")
        print("  - Private accounts with no public name")
    
    print(f"\n{'=' * 70}")
    print("DONE")
    print("=" * 70)


if __name__ == "__main__":
    asyncio.run(main())
