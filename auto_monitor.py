import asyncio
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

WORKER_URL = os.environ.get("WORKER_URL", "").rstrip("/")
AUTO_API_TOKEN = os.environ.get("AUTO_API_TOKEN", "")

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
SEND_REPORT = os.environ.get("SEND_REPORT", "1") not in ("0", "false", "False")
UPDATE_WATCHLIST_NAMES = os.environ.get("UPDATE_WATCHLIST_NAMES", "1") not in ("0", "false", "False")

MAX_CONCURRENT = int(os.environ.get("MAX_CONCURRENT", "5"))
API_TIMEOUT = int(os.environ.get("API_TIMEOUT", "20"))

PLAYWRIGHT_CONCURRENCY = int(os.environ.get("PLAYWRIGHT_CONCURRENCY", "2"))
PLAYWRIGHT_WAIT_MS = int(os.environ.get("PLAYWRIGHT_WAIT_MS", "8000"))
PLAYWRIGHT_RETRY_WAIT_MS = int(os.environ.get("PLAYWRIGHT_RETRY_WAIT_MS", "4000"))
PAGE_TIMEOUT_MS = int(os.environ.get("PAGE_TIMEOUT_MS", "30000"))
PLAYWRIGHT_MAX_USERS_PER_CYCLE = int(os.environ.get("PLAYWRIGHT_MAX_USERS_PER_CYCLE", "12"))

SEARCH_URL = "https://superlivetv.com/fr/search"
BASE_LIVE_URL = "https://superlivetv.com/fr/livestream/{user_id}"

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

LIVE_NORMAL = "LIVE_NORMAL"
LIVE_PREMIUM = "LIVE_PREMIUM"
OFFLINE = "OFFLINE"
UNKNOWN = "UNKNOWN"

def log(message: str) -> None:
    print(f"[AUTO] {message}", flush=True)

def log_detection(user_id: str, result: Dict[str, Any], source: str) -> None:
    status = result.get("status", UNKNOWN)
    if status == LIVE_PREMIUM:
        action = "SKIP_PREMIUM"
    elif status == OFFLINE:
        action = "SKIP_OFFLINE"
    elif status == LIVE_NORMAL:
        action = "CANDIDATE"
    else:
        action = "SKIP_UNCERTAIN"

    log(f"username={user_id}")
    log(f"source={source}")
    log(f"status={status}")
    log(f"action={action}")
    log(f"reason={result.get('reason', 'unknown')}")
    if result.get("display_name"):
        log(f"display_name={result['display_name']}")
    if result.get("is_live"):
        log(f"is_live=True")
    if result.get("is_premium"):
        log(f"is_premium=True")

def html_escape(text: str) -> str:
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

def split_telegram_text(text: str, limit: int = 3900) -> List[str]:
    lines = text.split("\n")
    chunks = []
    current = ""
    for line in lines:
        if len(line) > limit:
            if current:
                chunks.append(current)
                current = ""
            for i in range(0, len(line), limit):
                chunks.append(line[i:i + limit])
            continue
        if len(current) + len(line) + 1 > limit:
            chunks.append(current)
            current = line
        else:
            current = f"{current}\n{line}" if current else line
    if current:
        chunks.append(current)
    return chunks or [""]

def send_telegram_report(text: str) -> bool:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log("Telegram report skipped: credentials missing")
        return False
    chunks = split_telegram_text(text)
    ok = True
    for chunk in chunks:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {"chat_id": TELEGRAM_CHAT_ID, "text": chunk, "parse_mode": "HTML"}
        try:
            data = json.dumps(payload).encode("utf-8")
            req = urllib.request.Request(url, data=data, method="POST",
                headers={"Content-Type": "application/json", "User-Agent": "AutoMonitor/1.0"})
            with urllib.request.urlopen(req, timeout=20) as response:
                response.read()
        except Exception as e:
            log(f"Telegram report error: {e}")
            ok = False
    if ok:
        log("Telegram report sent successfully")
    return ok

def build_report(elapsed_seconds: float, stats: Dict[str, Any]) -> str:
    lines = []
    lines.append(f"✅ انتهى الفحص في {elapsed_seconds:.1f} ثانية.")
    lines.append("")
    lines.append(f"📊 إحصائيات القائمة ({stats['total_watchlist']} مستخدم):")
    lines.append(f"• قيد التسجيل مسبقاً: {stats['already_recording']}")
    lines.append(f"• تم فحصه الآن: {stats['checked_now']}")
    lines.append("")
    lines.append("📈 نتائج الفحص:")
    lines.append(f"• 🟢 بث عادي: {stats['live_normal']}")
    lines.append(f"• 🟡 بث مدفوع (تم التجاهل): {stats['live_premium']}")
    lines.append(f"• ⚪ غير متصل: {stats['offline']}")
    if stats.get("unknown", 0) > 0:
        lines.append(f"• ❓ غير مؤكد: {stats['unknown']}")
    names = stats.get("names", {})
    if names:
        lines.append("")
        lines.append("👤 الأسماء:")
        for user_id, display_name in names.items():
            lines.append(f"• {user_id} → {html_escape(display_name)}")
    lines.append("")
    lines.append(f"🔴 تم تشغيل {stats['started_recordings']} تسجيل(ات) جديد(ة).")
    return "\n".join(lines)

def api_request_sync(path: str, method: str = "GET", payload: Optional[dict] = None):
    url = f"{WORKER_URL}{path}"
    headers = {
        "User-Agent": "SuperLive-AutoMonitor/1.0",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    if AUTO_API_TOKEN:
        headers["X-Auto-Token"] = AUTO_API_TOKEN
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=API_TIMEOUT) as response:
            body = response.read().decode("utf-8", errors="replace")
            try:
                return response.status, json.loads(body) if body else {}
            except Exception:
                return response.status, {"raw": body[:500]}
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode("utf-8", errors="replace")
        except Exception:
            body = str(e)
        try:
            return e.code, json.loads(body)
        except Exception:
            return e.code, {"error": body[:500]}
    except Exception as e:
        return 0, {"error": str(e)}

async def api_request(path: str, method: str = "GET", payload: Optional[dict] = None):
    return await asyncio.to_thread(api_request_sync, path, method, payload)

async def load_watchlist():
    status, data = await api_request("/api/watchlist", "GET")
    if status != 200:
        raise RuntimeError(f"Failed to load watchlist: HTTP {status} - {data}")
    items = data.get("watchlist", [])
    users = []
    stored_names = {}
    for item in items:
        username = str(item.get("stream_id") or item.get("username") or item.get("id") or "").strip()
        if not username or username in ("undefined", "null", ""):
            continue
        if not username.isdigit():
            continue
        users.append(username)
        display_name = item.get("display_name")
        if display_name:
            stored_names[username] = str(display_name).strip()
    return users, stored_names

async def load_active_recordings() -> Dict[str, Any]:
    status, data = await api_request("/api/active-recordings", "GET")
    if status != 200:
        raise RuntimeError(f"Failed to load active recordings: HTTP {status} - {data}")
    active_ids = set()
    for rec in data.get("recordings", []):
        if rec.get("status") == "recording":
            stream_id = str(rec.get("stream_id", "")).strip()
            if stream_id:
                active_ids.add(stream_id)
    return {
        "active_count": int(data.get("active_count", len(active_ids))),
        "max_concurrent": int(data.get("max_concurrent", MAX_CONCURRENT)),
        "active_ids": active_ids,
    }

async def acquire_monitor_lock() -> bool:
    try:
        status, data = await api_request("/api/monitor-lock/acquire", "POST")
        return status == 200 and data.get("acquired", False)
    except Exception as e:
        log(f"Monitor lock acquire error: {e}")
        return True

async def release_monitor_lock():
    try:
        await api_request("/api/monitor-lock/release", "POST")
    except Exception as e:
        log(f"Monitor lock release error: {e}")

async def trigger_auto_recording(user_id: str, stream_url: str, stream_name: str = ""):
    payload = {"stream_url": stream_url, "source": "auto", "stream_name": stream_name}
    status, data = await api_request(f"/api/auto-trigger/{user_id}", "POST", payload)
    if status == 200 and data.get("started"):
        return True, "started"
    if status == 409:
        return False, "already_recording"
    if status == 429:
        return False, "concurrency_limit"
    if status == 404:
        return False, "not_in_watchlist"
    return False, data.get("error", f"http_{status}")

async def update_watchlist_name(user_id: str, display_name: str):
    try:
        payload = {"display_name": display_name}
        status, data = await api_request(f"/api/watchlist/name/{user_id}", "POST", payload)
        if status == 200:
            log(f"Saved display name for {user_id}: {display_name}")
    except Exception as e:
        log(f"Name save error for {user_id}: {e}")

# ============================================================
# SEARCH PAGE STRATEGY
# ============================================================
# This is the KEY FIX: we use the search page to:
# 1. Get the CORRECT username for each user_id
# 2. Check if the user is LIVE (has a DIRECT button)
# 3. Navigate to the real broadcast via the DIRECT link
#
# This avoids ALL the problems with recommended streams
# and wrong names on the livestream page.

SEARCH_PAGE_JS = """
(userId) => {
    const results = [];
    // Look for search result containers
    const cards = document.querySelectorAll(
        '[class*="result"], [class*="card"], [class*="user"], [class*="profile"], article, .search-result'
    );

    for (const card of cards) {
        const text = card.innerText || '';
        // Check if this card contains our user_id
        if (!text.includes(userId)) continue;

        // Extract name: look for elements that look like usernames
        let displayName = '';

        // Strategy 1: Look for specific selectors
        const nameSelectors = [
            '[class*="username"]',
            '[class*="display-name"]',
            '[class*="name"]',
            'h1', 'h2', 'h3', 'h4',
            '.title',
            'strong', 'b',
        ];

        for (const sel of nameSelectors) {
            try {
                const el = card.querySelector(sel);
                if (el && el.innerText) {
                    const candidate = el.innerText.trim();
                    // Reject if it's just the user_id or too short
                    if (candidate.length >= 2 &&
                        candidate.length <= 60 &&
                        candidate !== userId &&
                        !candidate.match(/^\\d+$/) &&
                        !candidate.includes('Rechercher') &&
                        !candidate.includes('Résultats')
                    ) {
                        displayName = candidate;
                        break;
                    }
                }
            } catch (e) {}
        }

        // Strategy 2: Look for the line containing the user_id
        // The format is usually: "Name @handle" near the ID
        if (!displayName) {
            const lines = text.split('\\n').map(l => l.trim()).filter(l => l.length > 0);
            for (const line of lines) {
                // Skip lines that are just the ID or common labels
                if (line === userId) continue;
                if (line.match(/^\\d+$/)) continue;
                if (line.includes('Rechercher')) continue;
                if (line.includes('Résultats')) continue;
                if (line.includes('ProfilePicture')) continue;
                if (line.startsWith('@')) continue;

                // This might be the name
                if (line.length >= 2 && line.length <= 60) {
                    displayName = line;
                    break;
                }
            }
        }

        // Check for DIRECT button/link
        let isLive = false;
        let directLink = '';

        // Look for DIRECT button or link
        const directSelectors = [
            'a', 'button', '[role="button"]',
            '[class*="direct"]', '[class*="live"]', '[class*="badge"]'
        ];

        for (const sel of directSelectors) {
            try {
                const elements = card.querySelectorAll(sel);
                for (const el of elements) {
                    const elText = (el.innerText || el.textContent || '').trim().toUpperCase();
                    if (elText === 'DIRECT' || elText === 'LIVE' || elText === 'EN DIRECT') {
                        isLive = true;
                        if (el.tagName === 'A' && el.href) {
                            directLink = el.href;
                        }
                        break;
                    }
                }
                if (isLive) break;
            } catch (e) {}
        }

        // Also check the card text for DIRECT/LIVE indicators
        const upperText = text.toUpperCase();
        if (!isLive && (upperText.includes('DIRECT') || upperText.includes('EN DIRECT'))) {
            // Verify it's a status indicator, not just the word in some other context
            if (upperText.includes('DIRECT') && !upperText.includes('RECHERCHER')) {
                isLive = true;
            }
        }

        // Check for premium indicators
        let isPremium = false;
        const premiumIndicators = ['premium', 'payant', 'privé', 'private', 'exclusive'];
        const lowerText = text.toLowerCase();
        for (const indicator of premiumIndicators) {
            if (lowerText.includes(indicator)) {
                isPremium = true;
                break;
            }
        }

        if (displayName) {
            results.push({
                displayName,
                isLive,
                isPremium,
                directLink,
                cardText: text.substring(0, 300)
            });
        }
    }

    return {
        found: results.length > 0,
        results,
        pageText: (document.body ? document.body.innerText : '').substring(0, 1000),
        pageTitle: document.title || ''
    };
}
"""

def normalize_display_name(value):
    if value is None:
        return None
    s = str(value).strip()
    s = s.replace("&amp;", "&")
    s = re.sub(r"<[^>]+>", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    if len(s) < 2 or len(s) > 50:
        return None
    if s.isdigit():
        return None
    if re.search(r"^\d+$", s):
        return None
    if re.search(r"^\d+[.,\s]?\d*\s*[kKmM]\b", s, re.IGNORECASE):
        return None
    if re.search(r"^[0-9.,\s\-+%]+$", s):
        return None
    # Must contain at least one letter (Arabic, Latin, emoji)
    if not re.search(r"[\u0600-\u06FFa-zA-Z\U0001F300-\U0001FAFF\u2600-\u27BF]", s):
        return None
    word_count = len(s.split())
    if word_count > 5:
        return None
    lowered = s.lower()
    blocked = {
        "live", "offline", "premium", "superlive", "super live",
        "recording", "unknown", "none", "null", "video", "stream",
        "views", "viewers", "followers", "fans", "likes",
        "watching", "subscribe", "follow", "following",
        "k", "m", "b", "undefined", "super",
        "membre", "member", "user", "guest",
        "membre super", "super membre",
        "direct", "en direct",
    }
    if lowered in blocked:
        return None
    if "rencontrez" in lowered or "diffusions" in lowered or "regardez" in lowered:
        return None
    if "nouvelles personnes" in lowered:
        return None
    if "rechercher" in lowered or "résultats" in lowered:
        return None
    return s


async def search_user(context, user_id: str, semaphore: asyncio.Semaphore) -> Dict[str, Any]:
    """
    Search for a user on the search page and extract their name + live status.
    This is the reliable way to get the correct name and live status.
    """
    async with semaphore:
        page = await context.new_page()
        result = {
            "user_id": user_id,
            "display_name": None,
            "is_live": False,
            "is_premium": False,
            "direct_link": None,
            "error": None,
        }

        try:
            # Go to search page
            await page.goto(SEARCH_URL, timeout=PAGE_TIMEOUT_MS, wait_until="domcontentloaded")
            await page.wait_for_timeout(2000)

            # Find the search input and type the user_id
            search_input = None
            search_selectors = [
                'input[type="search"]',
                'input[type="text"]',
                'input[placeholder*="Rechercher"]',
                'input[placeholder*="nom"]',
                'input[placeholder*="utilisateur"]',
                'input[placeholder*="ID"]',
                'input[name="search"]',
                'input[name="q"]',
                '#search',
                '.search-input',
            ]

            for sel in search_selectors:
                try:
                    el = await page.query_selector(sel)
                    if el:
                        search_input = el
                        break
                except Exception:
                    continue

            if not search_input:
                # Try to find ANY visible text input
                try:
                    inputs = await page.query_selector_all('input[type="text"], input[type="search"]')
                    for inp in inputs:
                        if await inp.is_visible():
                            search_input = inp
                            break
                except Exception:
                    pass

            if not search_input:
                result["error"] = "search_input_not_found"
                await page.close()
                return result

            # Type the user_id and press Enter
            await search_input.click()
            await search_input.fill("")
            await search_input.type(user_id, delay=50)
            await page.wait_for_timeout(500)
            await page.keyboard.press("Enter")

            # Wait for results to load
            await page.wait_for_timeout(PLAYWRIGHT_WAIT_MS)

            # Extract results
            try:
                search_result = await page.evaluate(SEARCH_PAGE_JS, user_id)
            except Exception as e:
                result["error"] = f"evaluate_error: {str(e)[:100]}"
                await page.close()
                return result

            if search_result.get("found") and search_result.get("results"):
                # Take the first matching result
                first_result = search_result["results"][0]
                raw_name = first_result.get("displayName", "")
                normalized = normalize_display_name(raw_name)
                if normalized:
                    result["display_name"] = normalized
                else:
                    # Keep raw name if normalization fails but it looks valid
                    if raw_name and len(raw_name) >= 2 and len(raw_name) <= 50:
                        result["display_name"] = raw_name

                result["is_live"] = bool(first_result.get("isLive", False))
                result["is_premium"] = bool(first_result.get("isPremium", False))
                result["direct_link"] = first_result.get("directLink") or None

                log(f"Search result for {user_id}: "
                    f"name={result['display_name']}, "
                    f"is_live={result['is_live']}, "
                    f"is_premium={result['is_premium']}")
            else:
                # User not found in search results
                log(f"Search: user {user_id} not found in results")
                page_text = search_result.get("pageText", "")
                if user_id not in page_text:
                    result["error"] = "user_not_found_in_page"
                else:
                    result["error"] = "no_name_extracted"

        except Exception as e:
            result["error"] = str(e)[:200]
            log(f"Search error for {user_id}: {result['error']}")

        await page.close()
        return result


async def verify_live_on_stream_page(page, user_id: str) -> Dict[str, Any]:
    """
    After navigating to the livestream page (via DIRECT link),
    verify that the video is actually playing and belongs to this user.
    """
    result = {
        "has_active_video": False,
        "video_belongs_to_user": False,
        "is_premium": False,
        "error": None,
    }

    VERIFY_JS = r"""
    (userId) => {
        const videos = Array.from(document.querySelectorAll('video'));
        let srcObjectCount = 0;
        let belongsToUser = false;
        let isPremium = false;
        let mainVideoActive = false;

        const domContainsTargetId = (element) => {
            if (!userId || !element) return false;
            let node = element;
            let depth = 0;
            while (node && depth < 8) {
                try {
                    const values = [
                        node.id,
                        node.className,
                        node.getAttribute && node.getAttribute("data-stream-id"),
                        node.getAttribute && node.getAttribute("data-id"),
                        node.getAttribute && node.getAttribute("data-livestream-id"),
                        node.getAttribute && node.getAttribute("data-channel-id"),
                        node.getAttribute && node.getAttribute("data-video-id"),
                        node.getAttribute && node.getAttribute("href"),
                    ];
                    if (values.some(value => value != null && String(value).includes(userId))) {
                        return true;
                    }
                    if (node.querySelector) {
                        const descendants = node.querySelectorAll(
                            '[href], [data-stream-id], [data-livestream-id], [data-video-id]'
                        );
                        for (const descendant of descendants) {
                            const descendantValues = [
                                descendant.getAttribute && descendant.getAttribute("href"),
                                descendant.getAttribute && descendant.getAttribute("data-stream-id"),
                                descendant.getAttribute && descendant.getAttribute("data-livestream-id"),
                                descendant.getAttribute && descendant.getAttribute("data-video-id"),
                            ];
                            if (descendantValues.some(value => value != null && String(value).includes(userId))) {
                                return true;
                            }
                        }
                    }
                } catch (e) {}
                node = node.parentElement;
                depth++;
            }
            return false;
        };

        // Find the main (largest) video
        let mainVideo = null;
        let mainVideoArea = 0;
        for (const video of videos) {
            const rect = video.getBoundingClientRect();
            const area = rect.width * rect.height;
            if (area > mainVideoArea) {
                mainVideoArea = area;
                mainVideo = video;
            }
        }
        if (!mainVideo && videos.length > 0) {
            mainVideo = videos[0];
        }

        for (const video of videos) {
            if (video.srcObject) {
                srcObjectCount++;
            }
            const rect = video.getBoundingClientRect();
            const visible = rect.width > 50 && rect.height > 50;
            if (video.srcObject && video.readyState >= 2 && visible) {
                if (domContainsTargetId(video)) {
                    belongsToUser = true;
                }
            }
        }

        if (mainVideo) {
            const mainHasSrcObject = !!mainVideo.srcObject;
            const mainRect = mainVideo.getBoundingClientRect();
            const mainIsVisible = mainRect.width > 100 && mainRect.height > 100;
            const mainIsReady = mainVideo.readyState >= 2;
            const mainBelongs = domContainsTargetId(mainVideo);

            if (mainHasSrcObject && mainIsVisible && mainIsReady && mainBelongs) {
                mainVideoActive = true;
            }
        }

        // Check for premium indicators on the page
        try {
            const bodyText = (document.body ? document.body.innerText : '').toLowerCase();
            if (bodyText.includes('premium') || bodyText.includes('payant') || bodyText.includes('exclusive')) {
                // Only count if it's near the video area
                if (mainVideo) {
                    let container = mainVideo.parentElement;
                    let depth = 0;
                    while (container && depth < 5) {
                        const containerText = (container.innerText || '').toLowerCase();
                        if (containerText.includes('premium') || containerText.includes('payant')) {
                            isPremium = true;
                            break;
                        }
                        container = container.parentElement;
                        depth++;
                    }
                }
            }
        } catch (e) {}

        return {
            videoCount: videos.length,
            srcObjectCount,
            belongsToUser,
            mainVideoActive,
            isPremium
        };
    }
    """

    try:
        await page.wait_for_timeout(3000)
        verify_result = await page.evaluate(VERIFY_JS, user_id)

        result["has_active_video"] = bool(verify_result.get("mainVideoActive", False))
        result["video_belongs_to_user"] = bool(verify_result.get("belongsToUser", False))
        result["is_premium"] = bool(verify_result.get("isPremium", False))

    except Exception as e:
        result["error"] = str(e)[:200]

    return result


async def classify_user_via_search(context, user_id: str, semaphore: asyncio.Semaphore) -> Dict[str, Any]:
    """
    New strategy: use search page to get name + live status,
    then verify on the stream page if needed.
    """
    # Step 1: Search for the user
    search_result = await search_user(context, user_id, semaphore)

    if search_result.get("error"):
        return {
            "status": UNKNOWN,
            "reason": f"search_error:{search_result['error']}",
            "stream_url": BASE_LIVE_URL.format(user_id=user_id),
            "display_name": None,
            "video_info": {},
        }

    display_name = search_result.get("display_name")
    is_live = search_result.get("is_live", False)
    is_premium = search_result.get("is_premium", False)

    # Step 2: Determine status based on search results
    if not is_live:
        # User is not broadcasting
        return {
            "status": OFFLINE,
            "reason": "search:no_direct_button",
            "stream_url": BASE_LIVE_URL.format(user_id=user_id),
            "display_name": display_name,
            "video_info": {
                "search_live": False,
                "search_premium": is_premium,
            },
        }

    if is_premium:
        # User is broadcasting but it's premium
        return {
            "status": LIVE_PREMIUM,
            "reason": "search:premium_detected",
            "stream_url": BASE_LIVE_URL.format(user_id=user_id),
            "display_name": display_name,
            "video_info": {
                "search_live": True,
                "search_premium": True,
            },
        }

    # Step 3: User is live and not premium — verify on stream page
    # Open the livestream page to verify the video
    verify_page = await context.new_page()
    try:
        stream_url = BASE_LIVE_URL.format(user_id=user_id)

        # Navigate to the stream page
        if search_result.get("direct_link"):
            await verify_page.goto(search_result["direct_link"], timeout=PAGE_TIMEOUT_MS, wait_until="domcontentloaded")
        else:
            await verify_page.goto(stream_url, timeout=PAGE_TIMEOUT_MS, wait_until="domcontentloaded")

        # Verify the video is actually playing
        verify_result = await verify_live_on_stream_page(verify_page, user_id)

        await verify_page.close()

        if verify_result.get("has_active_video") and verify_result.get("video_belongs_to_user"):
            if verify_result.get("is_premium"):
                return {
                    "status": LIVE_PREMIUM,
                    "reason": "verified:premium_on_stream_page",
                    "stream_url": stream_url,
                    "display_name": display_name,
                    "video_info": verify_result,
                }
            return {
                "status": LIVE_NORMAL,
                "reason": "verified:video_active_and_belongs",
                "stream_url": stream_url,
                "display_name": display_name,
                "video_info": verify_result,
            }
        else:
            # Search said live but verification failed — trust verification
            return {
                "status": OFFLINE,
                "reason": "verified:no_active_video",
                "stream_url": stream_url,
                "display_name": display_name,
                "video_info": verify_result,
            }

    except Exception as e:
        await verify_page.close()
        # If stream page fails but search said live, trust search
        return {
            "status": LIVE_NORMAL if is_live else UNKNOWN,
            "reason": f"stream_page_error:{str(e)[:100]}",
            "stream_url": BASE_LIVE_URL.format(user_id=user_id),
            "display_name": display_name,
            "video_info": {"error": str(e)[:100]},
        }


async def classify_many_users(user_ids: List[str]) -> Dict[str, Dict[str, Any]]:
    results = {}
    if not user_ids:
        return results
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        log("Playwright is not installed. Skipping browser detection.")
        for user_id in user_ids:
            results[user_id] = {
                "status": UNKNOWN, "reason": "playwright_not_installed",
                "stream_url": BASE_LIVE_URL.format(user_id=user_id),
                "display_name": None,
            }
        return results
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True, args=[
                "--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu",
                "--disable-background-timer-throttling", "--disable-renderer-backgrounding",
            ])
            context = await browser.new_context(
                user_agent=USER_AGENT,
                viewport={"width": 1280, "height": 800},
                locale="fr-FR",
            )
            semaphore = asyncio.Semaphore(PLAYWRIGHT_CONCURRENCY)
            tasks = [classify_user_via_search(context, uid, semaphore) for uid in user_ids]
            gathered = await asyncio.gather(*tasks, return_exceptions=True)
            for user_id, result in zip(user_ids, gathered):
                if isinstance(result, Exception):
                    results[user_id] = {
                        "status": UNKNOWN, "reason": str(result)[:120],
                        "stream_url": BASE_LIVE_URL.format(user_id=user_id),
                        "display_name": None,
                    }
                else:
                    results[user_id] = result
            await context.close()
            await browser.close()
    except Exception as e:
        log(f"Playwright global error: {e}")
        for user_id in user_ids:
            if user_id not in results:
                results[user_id] = {
                    "status": UNKNOWN, "reason": "playwright_global_error",
                    "stream_url": BASE_LIVE_URL.format(user_id=user_id),
                    "display_name": None,
                }
    return results


async def async_main() -> int:
    monitor_start_time = time.monotonic()
    stats = {
        "total_watchlist": 0, "already_recording": 0, "checked_now": 0,
        "live_normal": 0, "live_premium": 0, "offline": 0, "unknown": 0,
        "started_recordings": 0, "names": {},
    }

    log("Starting monitor")

    if not WORKER_URL:
        log("FATAL: WORKER_URL is not set")
        return 1
    if not AUTO_API_TOKEN:
        log("FATAL: AUTO_API_TOKEN is not set")
        return 1

    lock_acquired = await acquire_monitor_lock()
    if not lock_acquired:
        log("Monitor lock not acquired. Another instance is running. Exiting.")
        return 0

    try:
        try:
            watchlist, stored_names = await load_watchlist()
        except Exception as e:
            log(f"FATAL: {e}")
            return 1

        log(f"Watchlist loaded: {len(watchlist)} users")
        stats["total_watchlist"] = len(watchlist)

        if not watchlist:
            log("Watchlist is empty. Nothing to do.")
            log("Monitor completed")
            return 0

        try:
            active = await load_active_recordings()
        except Exception as e:
            log(f"FATAL: {e}")
            return 1

        log(f"Active recordings: {active['active_count']}/{active['max_concurrent']}")

        if active["active_count"] >= active["max_concurrent"]:
            log("Concurrency limit already reached.")
            elapsed = time.monotonic() - monitor_start_time
            if SEND_REPORT:
                send_telegram_report(build_report(elapsed, stats))
            log("Monitor completed")
            return 0

        users_to_check = []
        for user_id in watchlist:
            if user_id in active["active_ids"]:
                stats["already_recording"] += 1
                if stored_names.get(user_id):
                    stats["names"][user_id] = stored_names[user_id]
                log(f"username={user_id}")
                log("status=ALREADY_RECORDING")
                log("action=SKIP")
                log("reason=already_recording")
            else:
                users_to_check.append(user_id)

        if not users_to_check:
            log("All watchlist users are already recording.")
            elapsed = time.monotonic() - monitor_start_time
            if SEND_REPORT:
                send_telegram_report(build_report(elapsed, stats))
            log("Monitor completed")
            return 0

        selected = users_to_check[:PLAYWRIGHT_MAX_USERS_PER_CYCLE]
        stats["checked_now"] = len(selected)

        log(f"Running search-based detection for {len(selected)} users")
        results = await classify_many_users(selected)

        for user_id, result in results.items():
            log_detection(user_id, result, "search_page")

        for user_id in users_to_check:
            result = results.get(user_id, {})
            status = result.get("status", UNKNOWN)
            if status == LIVE_NORMAL:
                stats["live_normal"] += 1
            elif status == LIVE_PREMIUM:
                stats["live_premium"] += 1
            elif status == OFFLINE:
                stats["offline"] += 1
            else:
                stats["unknown"] += 1

            discovered_name = result.get("display_name")
            final_name = discovered_name or stored_names.get(user_id)
            if final_name:
                stats["names"][user_id] = final_name
            if UPDATE_WATCHLIST_NAMES and discovered_name:
                await update_watchlist_name(user_id, discovered_name)

        try:
            active = await load_active_recordings()
        except Exception as e:
            log(f"Failed to refresh active recordings: {e}")
            elapsed = time.monotonic() - monitor_start_time
            if SEND_REPORT:
                send_telegram_report(build_report(elapsed, stats))
            log("Monitor completed")
            return 0

        slots_available = active["max_concurrent"] - active["active_count"]
        log(f"Slots: {active['active_count']}/{active['max_concurrent']}")

        if slots_available <= 0:
            log("No recording slots available.")
            elapsed = time.monotonic() - monitor_start_time
            if SEND_REPORT:
                send_telegram_report(build_report(elapsed, stats))
            log("Monitor completed")
            return 0

        for user_id in watchlist:
            result = results.get(user_id)
            if not result:
                continue
            status = result.get("status", UNKNOWN)
            if status != LIVE_NORMAL:
                continue
            if user_id in active["active_ids"]:
                log(f"username={user_id}")
                log("status=LIVE_NORMAL")
                log("action=SKIP")
                log("reason=already_recording")
                continue
            if slots_available <= 0:
                log(f"username={user_id}")
                log("status=LIVE_NORMAL")
                log("action=WAIT_NEXT_CYCLE")
                log("reason=no_slot_available")
                break
            stream_url = result.get("stream_url") or BASE_LIVE_URL.format(user_id=user_id)
            stream_name = result.get("display_name") or stats["names"].get(user_id, "")
            log(f"username={user_id}")
            log("status=LIVE_NORMAL")
            log("Recording state: NOT_RECORDING")
            log(f"Slots: {active['active_count']}/{active['max_concurrent']}")
            if stream_name:
                log(f"display_name={stream_name}")
            log("Action: START_RECORDING")
            ok, reason = await trigger_auto_recording(user_id, stream_url, stream_name)
            if ok:
                log(f"username={user_id}")
                log("trigger=SUCCESS")
                log("workflow=record.yml")
                log("recording_engine=existing_record_once.py")
                stats["started_recordings"] += 1
                slots_available -= 1
                active["active_ids"].add(user_id)
                active["active_count"] += 1
            elif reason == "concurrency_limit":
                log(f"username={user_id}")
                log("trigger=SKIPPED")
                log("reason=concurrency_limit")
                break
            elif reason == "already_recording":
                active["active_ids"].add(user_id)
                log(f"username={user_id}")
                log("trigger=SKIPPED")
                log("reason=already_recording")
            else:
                log(f"username={user_id}")
                log("trigger=FAILED")
                log(f"reason={reason}")

        elapsed = time.monotonic() - monitor_start_time
        if SEND_REPORT:
            send_telegram_report(build_report(elapsed, stats))
        log("Monitor completed")
        return 0

    finally:
        await release_monitor_lock()


def main() -> int:
    try:
        return asyncio.run(async_main())
    except KeyboardInterrupt:
        log("Interrupted")
        return 1
    except Exception as e:
        log(f"FATAL monitor error: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
