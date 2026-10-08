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
PAGE_WAIT_MS = int(os.environ.get("PAGE_WAIT_MS", "10000"))
PAGE_TIMEOUT_MS = int(os.environ.get("PAGE_TIMEOUT_MS", "30000"))
PLAYWRIGHT_MAX_USERS_PER_CYCLE = int(os.environ.get("PLAYWRIGHT_MAX_USERS_PER_CYCLE", "12"))

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
    vi = result.get("video_info") or {}
    if vi:
        log(
            f"video_state="
            f"sameTargetDom={vi.get('sameTargetDom', False)},"
            f"activePackets={vi.get('activePackets', False)},"
            f"candidates={vi.get('candidates_count', 0)},"
            f"streamEnded={vi.get('streamEnded', False)},"
            f"premium={vi.get('premium', False)}"
        )


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
            req = urllib.request.Request(
                url, data=data, method="POST",
                headers={
                    "Content-Type": "application/json",
                    "User-Agent": "AutoMonitor/1.0",
                },
            )
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
        username = str(
            item.get("stream_id") or item.get("username") or item.get("id") or ""
        ).strip()
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
        raise RuntimeError(
            f"Failed to load active recordings: HTTP {status} - {data}"
        )
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


async def trigger_auto_recording(
    user_id: str, stream_url: str, stream_name: str = ""
):
    payload = {
        "stream_url": stream_url,
        "source": "auto",
        "stream_name": stream_name,
    }
    status, data = await api_request(
        f"/api/auto-trigger/{user_id}", "POST", payload
    )
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
        status, data = await api_request(
            f"/api/watchlist/name/{user_id}", "POST", payload
        )
        if status == 200:
            log(f"Saved display name for {user_id}: {display_name}")
    except Exception as e:
        log(f"Name save error for {user_id}: {e}")


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
    if re.search(r"\d+[.,\s]?\d*\s*[kKmM]\b", s, re.IGNORECASE):
        return None
    if re.search(r"^[0-9.,\s\-+%]+$", s):
        return None
    if not re.search(
        r"[\u0600-\u06FFa-zA-Z\U0001F300-\U0001FAFF\u2600-\u27BF]", s
    ):
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


# ============================================================
# PAGE CHECK JS - Uses the SAME domContainsTargetId logic
# from record_once.py WEBRTC_HOOK to verify the video is
# actually associated with the target user_id.
# ============================================================
PAGE_CHECK_JS = r"""
(userId) => {
    const result = {
        streamEnded: false,
        sameTargetDom: false,
        activePackets: false,
        candidates_count: 0,
        premium: false,
        videoWidth: 0,
        videoHeight: 0,
        displayName: null,
    };

    try {
        // Check if stream has ended
        const bodyText = document.body ? document.body.innerText : '';
        if (
            bodyText.includes("Le direct s'est termin") ||
            bodyText.includes("Stream ended") ||
            bodyText.includes("diffusion est terminée")
        ) {
            result.streamEnded = true;
        }

        // Check for premium indicators
        const premiumIndicators = ['premium', 'payant', 'privé', 'exclusive', 'abonnement'];
        const lowerBody = bodyText.toLowerCase();
        for (const indicator of premiumIndicators) {
            if (lowerBody.includes(indicator)) {
                // Only mark as premium if it appears near video-related content
                // and not just in navigation/footer
                const premiumSections = document.querySelectorAll(
                    '[class*="premium"], [class*="paywall"], [class*="lock"], [class*="subscribe"], [class*="unlock"]'
                );
                if (premiumSections.length > 0) {
                    result.premium = true;
                    break;
                }
            }
        }

        // Get all videos with srcObject (same logic as record_once.py)
        const videos = Array.from(document.querySelectorAll("video"));
        const viewportWidth = window.innerWidth || document.documentElement.clientWidth || 0;
        const viewportHeight = window.innerHeight || document.documentElement.clientHeight || 0;

        // domContainsTargetId - EXACT same logic as record_once.py WEBRTC_HOOK
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

        // Find videos with srcObject and check target association
        // Uses same scoring logic as record_once.py __superliveSelectTargetVideo
        const candidates = [];
        for (let index = 0; index < videos.length; index++) {
            const video = videos[index];
            try {
                const stream = video.srcObject;
                if (!stream) continue;

                const videoTrack = stream.getVideoTracks().find(t => t.readyState === "live");
                if (!videoTrack) continue;
                if (video.videoWidth <= 0 || video.videoHeight <= 0) continue;
                if (video.readyState < 2) continue;

                const style = getComputedStyle(video);
                if (style.display === "none" || style.visibility === "hidden" || style.opacity === "0") {
                    continue;
                }

                const rect = video.getBoundingClientRect();
                const left = Math.max(0, rect.left);
                const top = Math.max(0, rect.top);
                const right = Math.min(viewportWidth, rect.right);
                const bottom = Math.min(viewportHeight, rect.bottom);
                const visibleWidth = Math.max(0, right - left);
                const visibleHeight = Math.max(0, bottom - top);
                const visibleArea = visibleWidth * visibleHeight;
                const layoutArea = Math.max(0, rect.width) * Math.max(0, rect.height);

                const sameTargetDom = domContainsTargetId(video);

                // Identity score - same as record_once.py
                let identityScore = 0;
                if (sameTargetDom) identityScore += 1000000;

                const score = identityScore
                    + Math.min(visibleArea, 1000000) / 100
                    + Math.min(layoutArea, 1000000) / 10000;

                candidates.push({
                    index,
                    video,
                    videoTrack,
                    rect,
                    visibleArea,
                    layoutArea,
                    sameTargetDom,
                    score,
                });
            } catch (e) {}
        }

        result.candidates_count = candidates.length;

        if (!candidates.length) {
            // No live video found at all
            return result;
        }

        // Sort by score (same as record_once.py)
        candidates.sort((a, b) => b.score - a.score);
        const best = candidates[0];

        result.sameTargetDom = best.sameTargetDom;
        result.videoWidth = best.video.videoWidth;
        result.videoHeight = best.video.videoHeight;

        // Extract display name from the area around the best video
        // Look for text elements near the video that might contain the name
        try {
            let container = best.video.parentElement;
            let nameSearchDepth = 0;
            while (container && nameSearchDepth < 5) {
                const nameElements = container.querySelectorAll(
                    '[class*="name"], [class*="username"], [class*="nickname"], [class*="profile"], [class*="title"], h1, h2, h3, h4'
                );
                for (const el of nameElements) {
                    const text = (el.innerText || '').trim();
                    if (text.length >= 2 && text.length <= 50) {
                        const wordCount = text.split(/\s+/).length;
                        if (wordCount <= 5) {
                            result.displayName = text;
                            break;
                        }
                    }
                }
                if (result.displayName) break;
                container = container.parentElement;
                nameSearchDepth++;
            }

            // Also try document.title and og:title
            if (!result.displayName) {
                const ogTitle = document.querySelector('meta[property="og:title"]');
                if (ogTitle && ogTitle.content) {
                    result.displayName = ogTitle.content;
                } else if (document.title) {
                    result.displayName = document.title;
                }
            }
        } catch (e) {}

        // Check WebRTC inbound stats for active packets
        try {
            const peers = window.__superlivePeerConnections || [];
            for (const pc of peers) {
                try {
                    const stats = await pc.getStats();
                    stats.forEach(report => {
                        if (report.type !== "inbound-rtp") return;
                        const kind = report.kind || report.mediaType || null;
                        if (kind !== "video") return;
                        const packetsReceived = Number(report.packetsReceived || 0);
                        if (packetsReceived > 0) {
                            result.activePackets = true;
                        }
                    });
                } catch (e) {}
            }
        } catch (e) {}

    } catch (e) {
        result.error = String(e);
    }

    return result;
}
"""


async def check_user_on_stream_page(
    context, user_id: str, semaphore: asyncio.Semaphore
) -> Dict[str, Any]:
    """
    Open the livestream page for the user and check:
    1. Is there a video with srcObject associated with this user_id?
    2. Has the stream ended?
    3. Is it a premium stream?
    4. What is the display name?

    Uses the SAME domContainsTargetId logic as record_once.py
    to ensure the video is actually associated with the target user.
    """
    async with semaphore:
        page = await context.new_page()
        url = BASE_LIVE_URL.format(user_id=user_id)

        result = {
            "sameTargetDom": False,
            "activePackets": False,
            "candidates_count": 0,
            "streamEnded": False,
            "premium": False,
            "displayName": None,
            "videoWidth": 0,
            "videoHeight": 0,
            "error": None,
        }

        try:
            await page.goto(url, timeout=PAGE_TIMEOUT_MS, wait_until="domcontentloaded")
            await page.wait_for_timeout(PAGE_WAIT_MS)

            try:
                check_result = await page.evaluate(PAGE_CHECK_JS, user_id)
                if isinstance(check_result, dict):
                    result.update(check_result)
            except Exception as e:
                result["error"] = f"check_evaluate: {str(e)[:100]}"

        except Exception as e:
            result["error"] = f"page_goto: {str(e)[:100]}"

        await page.close()
        return result


async def classify_user(context, user_id: str, semaphore: asyncio.Semaphore) -> Dict[str, Any]:
    """
    Classify a user based on the stream page check.
    Uses the same verification logic as record_once.py.
    """
    check_result = await check_user_on_stream_page(context, user_id, semaphore)

    video_info = {
        "sameTargetDom": check_result.get("sameTargetDom", False),
        "activePackets": check_result.get("activePackets", False),
        "candidates_count": check_result.get("candidates_count", 0),
        "streamEnded": check_result.get("streamEnded", False),
        "premium": check_result.get("premium", False),
        "videoWidth": check_result.get("videoWidth", 0),
        "videoHeight": check_result.get("videoHeight", 0),
    }

    # Extract display name
    display_name = None
    raw_name = check_result.get("displayName")
    if raw_name:
        normalized = normalize_display_name(raw_name)
        if normalized:
            display_name = normalized

    # Decision logic
    if check_result.get("error"):
        return {
            "status": UNKNOWN,
            "reason": f"error:{check_result['error'][:80]}",
            "stream_url": BASE_LIVE_URL.format(user_id=user_id),
            "display_name": display_name,
            "video_info": video_info,
        }

    if check_result.get("streamEnded"):
        return {
            "status": OFFLINE,
            "reason": "stream_ended",
            "stream_url": BASE_LIVE_URL.format(user_id=user_id),
            "display_name": display_name,
            "video_info": video_info,
        }

    if not check_result.get("sameTargetDom"):
        # No video associated with this user_id found
        # This means the user is either offline or only has recommended streams
        return {
            "status": OFFLINE,
            "reason": "no_target_video_found",
            "stream_url": BASE_LIVE_URL.format(user_id=user_id),
            "display_name": display_name,
            "video_info": video_info,
        }

    # Video is associated with the user
    if check_result.get("premium"):
        return {
            "status": LIVE_PREMIUM,
            "reason": "premium_stream",
            "stream_url": BASE_LIVE_URL.format(user_id=user_id),
            "display_name": display_name,
            "video_info": video_info,
        }

    # Live normal stream
    return {
        "status": LIVE_NORMAL,
        "reason": "target_video_active",
        "stream_url": BASE_LIVE_URL.format(user_id=user_id),
        "display_name": display_name,
        "video_info": video_info,
    }


async def classify_many_users(
    user_ids: List[str], stored_names: Dict[str, str] = None
) -> Dict[str, Dict[str, Any]]:
    results = {}
    if not user_ids:
        return results
    if stored_names is None:
        stored_names = {}
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        log("Playwright is not installed. Skipping browser detection.")
        for user_id in user_ids:
            results[user_id] = {
                "status": UNKNOWN,
                "reason": "playwright_not_installed",
                "stream_url": BASE_LIVE_URL.format(user_id=user_id),
                "display_name": stored_names.get(user_id),
                "video_info": {},
            }
        return results
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                    "--disable-background-timer-throttling",
                    "--disable-renderer-backgrounding",
                ],
            )
            context = await browser.new_context(
                user_agent=USER_AGENT,
                viewport={"width": 1280, "height": 800},
                locale="fr-FR",
            )
            semaphore = asyncio.Semaphore(PLAYWRIGHT_CONCURRENCY)
            tasks = [classify_user(context, uid, semaphore) for uid in user_ids]
            gathered = await asyncio.gather(*tasks, return_exceptions=True)
            for user_id, result in zip(user_ids, gathered):
                if isinstance(result, Exception):
                    results[user_id] = {
                        "status": UNKNOWN,
                        "reason": str(result)[:120],
                        "stream_url": BASE_LIVE_URL.format(user_id=user_id),
                        "display_name": stored_names.get(user_id),
                        "video_info": {},
                    }
                else:
                    if not result.get("display_name") and stored_names.get(user_id):
                        result["display_name"] = stored_names[user_id]
                    results[user_id] = result
            await context.close()
            await browser.close()
    except Exception as e:
        log(f"Playwright global error: {e}")
        for user_id in user_ids:
            if user_id not in results:
                results[user_id] = {
                    "status": UNKNOWN,
                    "reason": "playwright_global_error",
                    "stream_url": BASE_LIVE_URL.format(user_id=user_id),
                    "display_name": stored_names.get(user_id),
                    "video_info": {},
                }
    return results


async def async_main() -> int:
    monitor_start_time = time.monotonic()
    stats = {
        "total_watchlist": 0,
        "already_recording": 0,
        "checked_now": 0,
        "live_normal": 0,
        "live_premium": 0,
        "offline": 0,
        "unknown": 0,
        "started_recordings": 0,
        "names": {},
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

        log(
            f"Active recordings: "
            f"{active['active_count']}/{active['max_concurrent']}"
        )

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

        log(f"Running stream-page detection for {len(selected)} users")
        results = await classify_many_users(selected, stored_names)

        for user_id, result in results.items():
            log_detection(user_id, result, "stream_page")

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
        log(
            f"Slots: {active['active_count']}/{active['max_concurrent']}"
        )

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
            stream_url = result.get("stream_url") or BASE_LIVE_URL.format(
                user_id=user_id
            )
            stream_name = (
                result.get("display_name") or stats["names"].get(user_id, "")
            )
            log(f"username={user_id}")
            log("status=LIVE_NORMAL")
            log("Recording state: NOT_RECORDING")
            log(
                f"Slots: {active['active_count']}/{active['max_concurrent']}"
            )
            if stream_name:
                log(f"display_name={stream_name}")
            log("Action: START_RECORDING")
            ok, reason = await trigger_auto_recording(
                user_id, stream_url, stream_name
            )
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
