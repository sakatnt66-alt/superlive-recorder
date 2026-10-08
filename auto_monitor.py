"""
auto_monitor.py - Version 4.0 (Final Corrected)

CRITICAL CHANGES:
1. No user limit - checks ALL users in watchlist
2. Phase 1 ONLY resolves identity (does NOT determine live status)
3. Phase 2 determines live status by visiting profile page
4. Recording only starts after Phase 2 confirms LIVE
"""

import asyncio
import json
import os
import sys
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

try:
    from superlive_discovery import SuperLiveDiscovery
    DISCOVERY_AVAILABLE = True
except ImportError:
    DISCOVERY_AVAILABLE = False

WORKER_URL = os.environ.get("WORKER_URL", "").rstrip("/")
AUTO_API_TOKEN = os.environ.get("AUTO_API_TOKEN", "")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
SEND_REPORT = os.environ.get("SEND_REPORT", "1") not in ("0", "false", "False")
UPDATE_WATCHLIST_NAMES = os.environ.get("UPDATE_WATCHLIST_NAMES", "0") not in ("0", "false", "False")

MAX_CONCURRENT = int(os.environ.get("MAX_CONCURRENT", "5"))
API_TIMEOUT = int(os.environ.get("API_TIMEOUT", "20"))
DISCOVERY_CONCURRENCY = int(os.environ.get("DISCOVERY_CONCURRENCY", "2"))
# REMOVED: DISCOVERY_MAX_USERS_PER_CYCLE - now checks ALL users

BASE_LIVE_URL = "https://superlivetv.com/fr/livestream/{user_id}"

LIVE_NORMAL = "LIVE_NORMAL"
LIVE_PREMIUM = "LIVE_PREMIUM"
OFFLINE = "OFFLINE"
UNKNOWN = "UNKNOWN"
DISCOVERY_FAILED = "DISCOVERY_FAILED"


def log(message: str) -> None:
    print(f"[AUTO] {message}", flush=True)


def log_result(user_id: str, result: Dict[str, Any]) -> None:
    log(f"username={user_id}")
    log(f"source=discovery_layer")
    if result.get("profile_url"):
        log(f"profile_url={result['profile_url']}")
    if result.get("profile_id"):
        log(f"profile_id={result['profile_id']}")
    if result.get("display_name"):
        log(f"display_name={result['display_name']}")
    log(f"phase={result.get('phase', 'unknown')}")
    log(f"status={result.get('status', UNKNOWN)}")
    log(f"action={result.get('action', 'SKIP')}")
    log(f"reason={result.get('reason', 'unknown')}")
    if result.get("stream_url"):
        log(f"stream_url={result['stream_url']}")


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
                headers={"Content-Type": "application/json", "User-Agent": "AutoMonitor/1.0"}
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
    lines = [f"✅ انتهى الفحص في {elapsed_seconds:.1f} ثانية.", ""]
    lines.append(f"📊 إحصائيات ({stats['total_watchlist']} مستخدم):")
    lines.append(f"• قيد التسجيل: {stats['already_recording']}")
    lines.append(f"• تم فحصه: {stats['checked_now']}")
    lines.append("")
    lines.append("📈 النتائج:")
    lines.append(f"• 🟢 بث عادي: {stats['live_normal']}")
    lines.append(f"• 🟡 بث مدفوع: {stats['live_premium']}")
    lines.append(f"• ⚪ غير متصل: {stats['offline']}")
    if stats.get("discovery_failed", 0) > 0:
        lines.append(f"• ⚠️ فشل اكتشاف: {stats['discovery_failed']}")
    if stats.get("unknown", 0) > 0:
        lines.append(f"• ❓ غير مؤكد: {stats['unknown']}")
    names = stats.get("names", {})
    profile_map = stats.get("profile_map", {})
    if names:
        lines.append("")
        lines.append("👤 الأسماء:")
        for uid, name in names.items():
            pid = profile_map.get(uid, "?")
            lines.append(f"• {uid} [P:{pid}] → {html_escape(name)}")
    lines.append("")
    lines.append(f"🔴 تم تشغيل {stats['started_recordings']} تسجيل.")
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
    data = json.dumps(payload).encode("utf-8") if payload else None
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
        uid = str(item.get("stream_id") or item.get("username") or item.get("id") or "").strip()
        if not uid or not uid.isdigit():
            continue
        users.append(uid)
        dn = item.get("display_name")
        if dn:
            stored_names[uid] = str(dn).strip()
    return users, stored_names


async def load_active_recordings():
    status, data = await api_request("/api/active-recordings", "GET")
    if status != 200:
        return {"active_count": 0, "max_concurrent": MAX_CONCURRENT, "active_ids": set()}
    active_ids = set()
    for rec in data.get("recordings", []):
        if rec.get("status") == "recording":
            sid = str(rec.get("stream_id", "")).strip()
            if sid:
                active_ids.add(sid)
    return {
        "active_count": len(active_ids),
        "max_concurrent": int(data.get("max_concurrent", MAX_CONCURRENT)),
        "active_ids": active_ids,
    }


async def trigger_auto_recording(user_id: str, stream_url: str, stream_name: str = "", profile_id: str = ""):
    payload = {
        "stream_url": stream_url,
        "source": "auto",
        "stream_name": stream_name,
        "profile_id": profile_id,
        "user_id": user_id,
    }
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
            log(f"Saved name for {user_id}: {display_name}")
    except Exception as e:
        log(f"Name save error for {user_id}: {e}")


# ============================================================
# PROCESS USER THROUGH ALL PHASES
# ============================================================
async def process_user(discovery, user_id: str, stored_names: Dict, semaphore):
    """
    Process one user through all phases:
    
    Phase 1: Resolve identity (user_id → profile_url)
    Phase 2: Check live status (visit profile_url, check for DIRECT)
    Phase 3: Validate stream (if live)
    Phase 4: Return result for recording decision
    """
    async with semaphore:
        result = {
            "user_id": user_id,
            "profile_url": None,
            "profile_id": None,
            "display_name": stored_names.get(user_id),
            "stream_url": None,
            "status": UNKNOWN,
            "reason": "",
            "action": "SKIP",
            "phase": "none",
        }

        if not DISCOVERY_AVAILABLE:
            result.update({"status": DISCOVERY_FAILED, "reason": "no_discovery", "phase": "init"})
            return result

        # =============================================
        # PHASE 1: IDENTITY RESOLUTION
        # =============================================
        try:
            log(f"[Phase 1] Resolving identity for {user_id}")
            profile_info = await discovery.discover_profile_id(user_id)

            if not profile_info or not profile_info.get("profile_url"):
                result.update({
                    "status": DISCOVERY_FAILED,
                    "reason": "profile_url_not_found",
                    "action": "SKIP_DISCOVERY_FAILED",
                    "phase": "phase_1_failed"
                })
                log(f"[Phase 1] ✗ Failed for {user_id}")
                return result

            result["profile_url"] = profile_info["profile_url"]
            result["profile_id"] = profile_info.get("profile_id")

            # Update display name if discovered
            if profile_info.get("username"):
                result["display_name"] = profile_info["username"]

            result["phase"] = "phase_1_done"
            log(f"[Phase 1] ✓ URL: {result['profile_url']}")

        except Exception as e:
            result.update({
                "status": DISCOVERY_FAILED,
                "reason": f"phase1_error:{str(e)[:80]}",
                "action": "SKIP_DISCOVERY_FAILED",
                "phase": "phase_1_error"
            })
            return result

        # =============================================
        # PHASE 2: LIVE STATUS DETECTION
        # Visit the profile URL and check for "DIRECT"
        # =============================================
        try:
            log(f"[Phase 2] Checking live at {result['profile_url']}")
            live_status = await discovery.check_live_status(
                result["profile_url"], user_id
            )

            if not live_status:
                result.update({
                    "status": UNKNOWN,
                    "reason": "live_check_failed",
                    "action": "SKIP_UNCERTAIN",
                    "phase": "phase_2_failed"
                })
                log(f"[Phase 2] ✗ Check failed for {user_id}")
                return result

            # Update username if found during live check
            if live_status.get("username") and not result.get("display_name"):
                result["display_name"] = live_status["username"]

            if not live_status.get("is_live"):
                result.update({
                    "status": OFFLINE,
                    "reason": "no_live_indicator",
                    "action": "SKIP_OFFLINE",
                    "phase": "phase_2_offline"
                })
                log(f"[Phase 2] ✗ User OFFLINE")
                return result

            # User is LIVE!
            is_premium = live_status.get("is_premium", False)
            stream_url = live_status.get("stream_url")

            if is_premium:
                result.update({
                    "status": LIVE_PREMIUM,
                    "reason": "premium_stream",
                    "action": "SKIP_PREMIUM",
                    "phase": "phase_2_premium"
                })
                log(f"[Phase 2] ✓ PREMIUM stream")
                return result

            # Use stream_url if found, otherwise use default
            if not stream_url:
                stream_url = result["profile_url"]  # Use profile URL as stream URL

            result.update({
                "status": LIVE_NORMAL,
                "reason": "live_confirmed",
                "action": "CANDIDATE",
                "stream_url": stream_url,
                "phase": "phase_2_live"
            })
            log(f"[Phase 2] ✓ User LIVE - stream: {stream_url}")

        except Exception as e:
            result.update({
                "status": UNKNOWN,
                "reason": f"phase2_error:{str(e)[:80]}",
                "action": "SKIP_UNCERTAIN",
                "phase": "phase_2_error"
            })
            return result

        # =============================================
        # PHASE 3: VALIDATION (simplified - trust Phase 2)
        # =============================================
        try:
            log(f"[Phase 3] Validating stream for {user_id}")
            validation = await discovery.validate_stream(
                user_id,
                result.get("profile_id", user_id),
                result.get("stream_url", "")
            )

            if not validation.get("validation_passed"):
                result.update({
                    "status": UNKNOWN,
                    "reason": "validation_failed",
                    "action": "SKIP_VALIDATION_FAILED",
                    "phase": "phase_3_failed"
                })
                log(f"[Phase 3] ✗ Validation failed")
                return result

            # Update stream URL if validation found a better one
            actual_url = validation.get("actual_stream_url")
            if actual_url:
                result["stream_url"] = actual_url

            result["phase"] = "phase_3_done"
            log(f"[Phase 3] ✓ Validated")

        except Exception as e:
            # Validation error but we still have Phase 2 result
            log(f"[Phase 3] ⚠ Error (proceeding): {e}")
            result["phase"] = "phase_3_error"

        return result


async def process_all_users(discovery, user_ids, stored_names):
    """Process ALL users (no limit)"""
    results = {}
    if not user_ids:
        return results

    semaphore = asyncio.Semaphore(DISCOVERY_CONCURRENCY)
    tasks = [process_user(discovery, uid, stored_names, semaphore) for uid in user_ids]
    gathered = await asyncio.gather(*tasks, return_exceptions=True)

    for uid, res in zip(user_ids, gathered):
        if isinstance(res, Exception):
            results[uid] = {
                "user_id": uid,
                "status": DISCOVERY_FAILED,
                "reason": f"exception:{str(res)[:100]}",
                "action": "SKIP_EXCEPTION",
                "phase": "exception",
                "display_name": stored_names.get(uid),
            }
        else:
            if not res.get("display_name"):
                res["display_name"] = stored_names.get(uid)
            results[uid] = res

    return results


# ============================================================
# MAIN
# ============================================================
async def async_main():
    start_time = time.monotonic()
    stats = {
        "total_watchlist": 0,
        "already_recording": 0,
        "checked_now": 0,
        "live_normal": 0,
        "live_premium": 0,
        "offline": 0,
        "unknown": 0,
        "discovery_failed": 0,
        "started_recordings": 0,
        "names": {},
        "profile_map": {},
    }

    log("Starting Auto Monitor v4.0 (ALL users, 4-phase pipeline)")

    if not WORKER_URL:
        log("FATAL: WORKER_URL not set")
        return 1
    if not AUTO_API_TOKEN:
        log("FATAL: AUTO_API_TOKEN not set")
        return 1
    if not DISCOVERY_AVAILABLE:
        log("FATAL: superlive_discovery.py not found")
        return 1

    discovery = SuperLiveDiscovery()
    log("Discovery Layer initialized")

    try:
        # Load watchlist
        try:
            watchlist, stored_names = await load_watchlist()
        except Exception as e:
            log(f"FATAL: {e}")
            return 1

        stats["total_watchlist"] = len(watchlist)
        log(f"Watchlist loaded: {len(watchlist)} users")

        if not watchlist:
            log("Watchlist empty.")
            return 0

        # Load active recordings
        try:
            active = await load_active_recordings()
        except Exception as e:
            log(f"FATAL: {e}")
            return 1

        log(f"Active recordings: {active['active_count']}/{active['max_concurrent']}")

        if active["active_count"] >= active["max_concurrent"]:
            log("Max concurrency reached.")
            elapsed = time.monotonic() - start_time
            if SEND_REPORT:
                send_telegram_report(build_report(elapsed, stats))
            return 0

        # Filter users not already recording
        users_to_check = []
        for uid in watchlist:
            if uid in active["active_ids"]:
                stats["already_recording"] += 1
                if stored_names.get(uid):
                    stats["names"][uid] = stored_names[uid]
                log(f"username={uid}")
                log("status=ALREADY_RECORDING")
                log("action=SKIP")
            else:
                users_to_check.append(uid)

        if not users_to_check:
            log("All users already recording.")
            elapsed = time.monotonic() - start_time
            if SEND_REPORT:
                send_telegram_report(build_report(elapsed, stats))
            return 0

        # CHECK ALL USERS - NO LIMIT
        stats["checked_now"] = len(users_to_check)
        log(f"Running Discovery Layer for {len(users_to_check)} users (concurrency: {DISCOVERY_CONCURRENCY})")

        results = await process_all_users(discovery, users_to_check, stored_names)

        # Log and aggregate results
        for uid, res in results.items():
            log_result(uid, res)

            status = res.get("status", UNKNOWN)
            if status == LIVE_NORMAL:
                stats["live_normal"] += 1
            elif status == LIVE_PREMIUM:
                stats["live_premium"] += 1
            elif status == OFFLINE:
                stats["offline"] += 1
            elif status == DISCOVERY_FAILED:
                stats["discovery_failed"] += 1
            else:
                stats["unknown"] += 1

            if res.get("profile_id"):
                stats["profile_map"][uid] = res["profile_id"]

            name = res.get("display_name")
            if name:
                stats["names"][uid] = name
            if UPDATE_WATCHLIST_NAMES and name:
                await update_watchlist_name(uid, name)

        # Refresh active recordings
        try:
            active = await load_active_recordings()
        except Exception as e:
            log(f"Failed to refresh: {e}")

        slots = active["max_concurrent"] - active["active_count"]
        log(f"Slots: {active['active_count']}/{active['max_concurrent']}")

        if slots <= 0:
            log("No slots available.")
            elapsed = time.monotonic() - start_time
            if SEND_REPORT:
                send_telegram_report(build_report(elapsed, stats))
            return 0

        # Trigger recordings for LIVE_NORMAL users
        for uid in watchlist:
            res = results.get(uid)
            if not res or res.get("status") != LIVE_NORMAL:
                continue
            if uid in active["active_ids"]:
                continue
            if slots <= 0:
                log(f"username={uid}")
                log("status=LIVE_NORMAL")
                log("action=WAIT_NEXT_CYCLE")
                log("reason=no_slot")
                break

            stream_url = res.get("stream_url") or BASE_LIVE_URL.format(user_id=uid)
            name = res.get("display_name") or stats["names"].get(uid, "")
            pid = res.get("profile_id", "")

            log(f"username={uid}")
            log(f"profile_id={pid}")
            log("status=LIVE_NORMAL")
            log("Action: START_RECORDING")

            ok, reason = await trigger_auto_recording(uid, stream_url, name, pid)

            if ok:
                log(f"username={uid}")
                log("trigger=SUCCESS")
                log("workflow=record.yml")
                stats["started_recordings"] += 1
                slots -= 1
                active["active_ids"].add(uid)
            elif reason == "concurrency_limit":
                log(f"username={uid}")
                log("trigger=SKIPPED")
                log("reason=concurrency_limit")
                break
            elif reason == "already_recording":
                active["active_ids"].add(uid)
                log(f"username={uid}")
                log("trigger=SKIPPED")
                log("reason=already_recording")
            else:
                log(f"username={uid}")
                log("trigger=FAILED")
                log(f"reason={reason}")

        elapsed = time.monotonic() - start_time
        if SEND_REPORT:
            send_telegram_report(build_report(elapsed, stats))
        log("Monitor completed")
        return 0

    finally:
        pass


def main():
    try:
        return asyncio.run(async_main())
    except KeyboardInterrupt:
        log("Interrupted")
        return 1
    except Exception as e:
        log(f"FATAL: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
