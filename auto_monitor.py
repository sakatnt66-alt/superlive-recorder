import asyncio
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

# ============================================================
# CONFIG
# ============================================================
WORKER_URL = os.environ.get("WORKER_URL", "").rstrip("/")
AUTO_API_TOKEN = os.environ.get("AUTO_API_TOKEN", "")

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
SEND_REPORT = os.environ.get("SEND_REPORT", "1") not in ("0", "false", "False")
UPDATE_WATCHLIST_NAMES = os.environ.get("UPDATE_WATCHLIST_NAMES", "1") not in ("0", "false", "False")

MAX_CONCURRENT = int(os.environ.get("MAX_CONCURRENT", "5"))
API_TIMEOUT = int(os.environ.get("API_TIMEOUT", "20"))

PLAYWRIGHT_CONCURRENCY = int(os.environ.get("PLAYWRIGHT_CONCURRENCY", "2"))
PLAYWRIGHT_WAIT_MS = int(os.environ.get("PLAYWRIGHT_WAIT_MS", "10000"))
PLAYWRIGHT_RETRY_WAIT_MS = int(os.environ.get("PLAYWRIGHT_RETRY_WAIT_MS", "5000"))
PAGE_TIMEOUT_MS = int(os.environ.get("PAGE_TIMEOUT_MS", "30000"))
PLAYWRIGHT_MAX_USERS_PER_CYCLE = int(os.environ.get("PLAYWRIGHT_MAX_USERS_PER_CYCLE", "12"))

BASE_LIVE_URL = "https://superlivetv.com/fr/livestream/{user_id}"

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

# ============================================================
# STATUS CONSTANTS
# ============================================================
LIVE_NORMAL = "LIVE_NORMAL"
LIVE_PREMIUM = "LIVE_PREMIUM"
OFFLINE = "OFFLINE"
UNKNOWN = "UNKNOWN"

# ============================================================
# LOGGING
# ============================================================
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

    video_info = result.get("video_info")
    if video_info:
        log(
            f"video_state="
            f"videos={video_info.get('videoCount', 0)},"
            f"srcObj={video_info.get('srcObjectCount', 0)},"
            f"live={video_info.get('liveVideoCandidate', False)},"
            f"premium_json={video_info.get('premiumFromJson', False)},"
            f"stream_ended={video_info.get('streamEnded', False)}"
        )

# ============================================================
# TELEGRAM REPORT
# ============================================================
def html_escape(text: str) -> str:
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


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

# ============================================================
# WORKER API CLIENT
# ============================================================
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
    recordings = data.get("recordings", [])
    for rec in recordings:
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
# JSON PREMIUM DETECTION
# ============================================================
def check_json_for_premium(json_bodies: List[str], user_id: str) -> bool:
    for body in json_bodies:
        if user_id not in body:
            continue
        if re.search(r'"(?:isPremium|is_premium|premium)"\s*:\s*true', body, re.IGNORECASE):
            return True
        if re.search(r'"(?:type|access|streamType|accessType|mode)"\s*:\s*"?premium', body, re.IGNORECASE):
            return True
        if re.search(r'"(?:isPaywall|paywall)"\s*:\s*true', body, re.IGNORECASE):
            return True
        try:
            obj = json.loads(body)
            if _check_obj_for_premium(obj, user_id):
                return True
        except Exception:
            pass
    return False


def _check_obj_for_premium(obj, user_id, depth=0):
    if depth > 6:
        return False
    if isinstance(obj, dict):
        is_user_obj = False
        for key, value in obj.items():
            if isinstance(value, (str, int, float)) and str(value) == str(user_id):
                is_user_obj = True
                break

        for key, value in obj.items():
            key_lower = str(key).lower()
            if key_lower in ('ispremium', 'is_premium', 'premium', 'ispaywall', 'paywall'):
                if value is True or str(value).lower() == 'true':
                    return True
            if key_lower in ('type', 'access', 'streamtype', 'accesstype', 'mode'):
                if str(value).lower() in ('premium', 'paid', 'private', 'paywall', 'subscription'):
                    return True
            if key_lower in ('locked', 'isprivate', 'is_private', 'private'):
                if (value is True or str(value).lower() == 'true') and is_user_obj:
                    return True

        for value in obj.values():
            if _check_obj_for_premium(value, user_id, depth + 1):
                return True

    elif isinstance(obj, list):
        for item in obj[:50]:
            if _check_obj_for_premium(item, user_id, depth + 1):
                return True

    return False

# ============================================================
# NAME EXTRACTION
# ============================================================
def normalize_display_name(value):
    if value is None:
        return None
    s = str(value).strip()
    s = s.replace("&amp;", "&")
    s = re.sub(r"<[^>]+>", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    if len(s) < 2 or len(s) > 60:
        return None
    if s.isdigit():
        return None
    if re.search(r"^\d", s):
        return None
    if re.search(r"\d+[.,\s]?\d*\s*[kKmM]\b", s, re.IGNORECASE):
        return None
    if re.search(r"^[0-9.,\s\-+%]+$", s):
        return None
    if not re.search(r"[\u0600-\u06FFa-zA-Z\U0001F300-\U0001FAFF\u2600-\u27BF]", s):
        return None
    lowered = s.lower()
    blocked = {
        "live", "offline", "premium", "superlive", "super live",
        "recording", "unknown", "none", "null", "video", "stream",
        "views", "viewers", "followers", "fans", "likes",
        "watching", "subscribe", "follow", "following",
        "k", "m", "b", "undefined",
    }
    if lowered in blocked:
        return None
    return s

# ============================================================
# CLASSIFICATION ENGINE
# ============================================================
def classify_final(video_info: Dict[str, Any], json_bodies: List[str], user_id: str) -> str:
    src_object_count = int(video_info.get("srcObjectCount", 0))
    live_video_candidate = bool(video_info.get("liveVideoCandidate", False))
    stream_ended = bool(video_info.get("streamEnded", False))

    has_active_video = src_object_count > 0 and live_video_candidate

    is_premium = check_json_for_premium(json_bodies, user_id)

    if has_active_video:
        if is_premium:
            return LIVE_PREMIUM
        return LIVE_NORMAL

    if stream_ended:
        return OFFLINE

    return UNKNOWN

# ============================================================
# PLAYWRIGHT DETECTION
# ============================================================
PAGE_INFO_JS = r"""
(userId) => {
    const videos = Array.from(document.querySelectorAll('video'));
    let srcObjectCount = 0;
    let visibleCount = 0;
    let liveVideoCandidate = false;
    let streamEnded = false;
    let streamerName = '';

    try {
        const ogTitle = document.querySelector('meta[property="og:title"]');
        if (ogTitle && ogTitle.content) {
            let title = ogTitle.content.trim();
            title = title.replace(/\s*[\|\-\u2013\u2014]\s*(SuperLive|superlivetv|Super Live).*$/i, '');
            title = title.replace(/\s*(en direct|live|direct).*$/i, '');
            if (title.length >= 2 && title.length <= 60) {
                streamerName = title.trim();
            }
        }
    } catch (e) {}

    try {
        const bodyText = document.body ? document.body.innerText : '';
        if (bodyText.includes("Le direct s'est termin\u00e9") || bodyText.includes("Stream ended")) {
            streamEnded = true;
        }
    } catch (e) {}

    for (const video of videos) {
        if (video.srcObject) {
            srcObjectCount++;
        }
        const rect = video.getBoundingClientRect();
        const visible = rect.width > 50 && rect.height > 50;
        if (visible) {
            visibleCount++;
        }
        if (video.srcObject && video.readyState >= 2 && visible) {
            liveVideoCandidate = true;
        }
    }

    if (streamEnded && srcObjectCount > 0 && liveVideoCandidate) {
        streamEnded = false;
    }

    return {
        videoCount: videos.length,
        srcObjectCount,
        visibleCount,
        liveVideoCandidate,
        streamEnded,
        streamerName
    };
}
"""


async def playwright_classify_user(context, user_id: str, semaphore: asyncio.Semaphore) -> Dict[str, Any]:
    async with semaphore:
        page = await context.new_page()
        json_bodies: List[str] = []
        json_count = 0
        url = BASE_LIVE_URL.format(user_id=user_id)
        video_info = {
            "videoCount": 0, "srcObjectCount": 0, "visibleCount": 0,
            "liveVideoCandidate": False, "streamEnded": False,
            "streamerName": "",
        }

        async def on_response(response):
            nonlocal json_count
            if json_count >= 30:
                return
            try:
                content_type = (response.headers or {}).get("content-type", "")
                if "json" not in content_type.lower():
                    return
                response_url = response.url or ""
                body = await response.text()
                if not body:
                    return
                if user_id in body or user_id in response_url:
                    json_count += 1
                    json_bodies.append(body[:200000])
            except Exception:
                pass

        page.on("response", on_response)

        try:
            await page.goto(url, timeout=PAGE_TIMEOUT_MS, wait_until="domcontentloaded")
            await page.wait_for_timeout(PLAYWRIGHT_WAIT_MS)

            try:
                video_info = await page.evaluate(PAGE_INFO_JS, user_id)
            except Exception:
                video_info = {
                    "videoCount": 0, "srcObjectCount": 0, "visibleCount": 0,
                    "liveVideoCandidate": False, "streamEnded": False,
                    "streamerName": "",
                }

            if not video_info.get("liveVideoCandidate") and video_info.get("srcObjectCount", 0) == 0:
                await page.wait_for_timeout(PLAYWRIGHT_RETRY_WAIT_MS)
                try:
                    video_info = await page.evaluate(PAGE_INFO_JS, user_id)
                except Exception:
                    pass

        except Exception as e:
            await page.close()
            return {
                "status": UNKNOWN,
                "reason": str(e)[:120],
                "stream_url": url,
                "display_name": None,
            }

        await page.close()

        final_status = classify_final(video_info, json_bodies, user_id)

        display_name = None
        streamer_name = video_info.get("streamerName", "")
        if streamer_name:
            normalized = normalize_display_name(streamer_name)
            if normalized:
                display_name = normalized

        if not display_name:
            for body in json_bodies[:10]:
                try:
                    obj = json.loads(body)
                    if isinstance(obj, dict):
                        for key in ("username", "nickname", "display_name", "name", "streamer_name", "broadcaster_name"):
                            val = obj.get(key)
                            if val:
                                normalized = normalize_display_name(val)
                                if normalized and user_id in body:
                                    display_name = normalized
                                    break
                        if display_name:
                            break
                except Exception:
                    continue

        video_info["premiumFromJson"] = check_json_for_premium(json_bodies, user_id)

        result = {
            "status": final_status,
            "reason": f"video={video_info.get('liveVideoCandidate')},premium_json={video_info.get('premiumFromJson')},ended={video_info.get('streamEnded')}",
            "stream_url": url,
            "display_name": display_name,
            "video_info": video_info,
        }

        return result


async def playwright_classify_many(user_ids: List[str]) -> Dict[str, Dict[str, Any]]:
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
                "--autoplay-policy=no-user-gesture-required",
            ])
            context = await browser.new_context(
                user_agent=USER_AGENT,
                viewport={"width": 1280, "height": 800},
                locale="fr-FR",
            )
            semaphore = asyncio.Semaphore(PLAYWRIGHT_CONCURRENCY)
            tasks = [playwright_classify_user(context, uid, semaphore) for uid in user_ids]
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

# ============================================================
# MAIN MONITOR
# ============================================================
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

        log(f"Running browser detection for {len(selected)} users")
        results = await playwright_classify_many(selected)

        for user_id, result in results.items():
            log_detection(user_id, result, "playwright")

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
