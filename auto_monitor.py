"""
auto_monitor.py - Auto Monitoring System with Discovery Layer
دمج SuperLiveDiscovery لاكتشاف الـ Profile والبث الصحيح بدقة عالية

Architecture:
  Phase 1: Discovery (user_id → profile_id)
  Phase 2: Live Status Check (profile_id → stream info)
  Phase 3: Stream Validation (user_id + profile_id verification)
  Phase 4: Trigger Recording (if validation passed)
"""

import asyncio
import json
import os
import sys
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

# Import the Discovery Layer
try:
    from superlive_discovery import SuperLiveDiscovery
    DISCOVERY_AVAILABLE = True
except ImportError as e:
    print(f"[FATAL] superlive_discovery.py not found: {e}")
    DISCOVERY_AVAILABLE = False

WORKER_URL = os.environ.get("WORKER_URL", "").rstrip("/")
AUTO_API_TOKEN = os.environ.get("AUTO_API_TOKEN", "")

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
SEND_REPORT = os.environ.get("SEND_REPORT", "1") not in ("0", "false", "False")
# Disabled by default to save KV writes (free plan: 1000 writes/day)
UPDATE_WATCHLIST_NAMES = os.environ.get("UPDATE_WATCHLIST_NAMES", "0") not in ("0", "false", "False")

MAX_CONCURRENT = int(os.environ.get("MAX_CONCURRENT", "5"))
API_TIMEOUT = int(os.environ.get("API_TIMEOUT", "20"))

# Discovery settings
DISCOVERY_CONCURRENCY = int(os.environ.get("DISCOVERY_CONCURRENCY", "2"))
DISCOVERY_MAX_USERS_PER_CYCLE = int(os.environ.get("DISCOVERY_MAX_USERS_PER_CYCLE", "12"))
VALIDATION_MIN_CHECKS = int(os.environ.get("VALIDATION_MIN_CHECKS", "2"))

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
DISCOVERY_FAILED = "DISCOVERY_FAILED"
VALIDATION_FAILED = "VALIDATION_FAILED"


def log(message: str) -> None:
    print(f"[AUTO] {message}", flush=True)


def log_discovery(user_id: str, result: Dict[str, Any]) -> None:
    """Log detailed discovery results"""
    log(f"username={user_id}")
    log(f"source=discovery_layer")

    profile_id = result.get('profile_id')
    if profile_id:
        log(f"profile_id={profile_id}")

    username = result.get('username')
    if username:
        log(f"username_discovered={username}")

    phase = result.get('phase', 'unknown')
    log(f"phase={phase}")

    status = result.get('status', UNKNOWN)
    log(f"status={status}")

    action = result.get('action', 'SKIP')
    log(f"action={action}")

    reason = result.get('reason', 'unknown')
    log(f"reason={reason}")

    validation = result.get('validation', {})
    if validation:
        log(
            f"validation="
            f"passed={validation.get('validation_passed', False)},"
            f"checks={validation.get('checks_passed', 0)}/{validation.get('total_checks', 0)},"
            f"metadata_match={validation.get('metadata_match', False)},"
            f"dom_user={validation.get('dom_has_user_id', False)},"
            f"dom_profile={validation.get('dom_has_profile_id', False)}"
        )

    stream_url = result.get('stream_url')
    if stream_url:
        log(f"stream_url={stream_url}")


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
    lines.append("📈 نتائج الفحص (عبر Discovery Layer):")
    lines.append(f"• 🟢 بث عادي: {stats['live_normal']}")
    lines.append(f"• 🟡 بث مدفوع (تم التجاهل): {stats['live_premium']}")
    lines.append(f"• ⚪ غير متصل: {stats['offline']}")
    if stats.get("discovery_failed", 0) > 0:
        lines.append(f"• ⚠️ فشل الاكتشاف: {stats['discovery_failed']}")
    if stats.get("validation_failed", 0) > 0:
        lines.append(f"• ❌ فشل التحقق: {stats['validation_failed']}")
    if stats.get("unknown", 0) > 0:
        lines.append(f"• ❓ غير مؤكد: {stats['unknown']}")

    names = stats.get("names", {})
    profile_map = stats.get("profile_map", {})
    if names:
        lines.append("")
        lines.append("👤 الأسماء والمعرفات:")
        for user_id, display_name in names.items():
            profile_id = profile_map.get(user_id, "N/A")
            lines.append(f"• {user_id} [P:{profile_id}] → {html_escape(display_name)}")

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


async def trigger_auto_recording(
    user_id: str,
    stream_url: str,
    stream_name: str = "",
    profile_id: str = "",
):
    """
    Trigger the recording workflow with extended payload.
    Now includes profile_id for proper tracking.
    """
    payload = {
        "stream_url": stream_url,
        "source": "auto",
        "stream_name": stream_name,
        "profile_id": profile_id,
        "user_id": user_id,
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
    """Update name in KV. Only called when UPDATE_WATCHLIST_NAMES=1."""
    try:
        payload = {"display_name": display_name}
        status, data = await api_request(
            f"/api/watchlist/name/{user_id}", "POST", payload
        )
        if status == 200:
            log(f"Saved display name for {user_id}: {display_name}")
    except Exception as e:
        log(f"Name save error for {user_id}: {e}")


# ============================================================
# DISCOVERY LAYER INTEGRATION
# ============================================================
async def process_user_with_discovery(
    discovery: "SuperLiveDiscovery",
    user_id: str,
    stored_names: Dict[str, str],
    semaphore: asyncio.Semaphore,
) -> Dict[str, Any]:
    """
    Process a single user through the complete Discovery pipeline:
    
    Phase 1: Discover profile_id from user_id
    Phase 2: Check live status using profile_id
    Phase 3: Validate stream ownership
    Phase 4: Return classification result
    
    Returns a dict with:
      - status: LIVE_NORMAL | LIVE_PREMIUM | OFFLINE | UNKNOWN | DISCOVERY_FAILED | VALIDATION_FAILED
      - profile_id: discovered profile ID
      - stream_url: validated stream URL (if live)
      - display_name: discovered username
      - reason: explanation of the result
      - validation: validation details
    """
    async with semaphore:
        result = {
            "user_id": user_id,
            "profile_id": None,
            "stream_url": None,
            "display_name": stored_names.get(user_id),
            "username": None,
            "status": UNKNOWN,
            "reason": "",
            "action": "SKIP",
            "phase": "none",
            "validation": {},
        }

        if not DISCOVERY_AVAILABLE:
            result["status"] = DISCOVERY_FAILED
            result["reason"] = "discovery_module_not_available"
            result["action"] = "SKIP_DISCOVERY_FAILED"
            result["phase"] = "init"
            return result

        # ============================================================
        # PHASE 1: Discovery (user_id → profile_id)
        # ============================================================
        try:
            log(f"[Phase 1] Discovering profile for user_id: {user_id}")
            profile_info = await discovery.discover_profile_id(user_id)

            if not profile_info or not profile_info.get("profile_id"):
                result["status"] = DISCOVERY_FAILED
                result["reason"] = "profile_id_not_discovered"
                result["action"] = "SKIP_DISCOVERY_FAILED"
                result["phase"] = "phase_1"
                log(f"[Phase 1] ✗ Failed for user_id: {user_id}")
                return result

            profile_id = str(profile_info["profile_id"])
            result["profile_id"] = profile_id
            result["phase"] = "phase_1_done"

            # Update display_name if discovered
            discovered_username = profile_info.get("username")
            if discovered_username:
                result["username"] = discovered_username
                if not result["display_name"]:
                    result["display_name"] = discovered_username

            # Check if API already indicates not live
            if not profile_info.get("is_live"):
                result["status"] = OFFLINE
                result["reason"] = "api_reports_offline"
                result["action"] = "SKIP_OFFLINE"
                result["phase"] = "phase_1_offline"
                log(f"[Phase 1] ✓ User offline (profile_id: {profile_id})")
                return result

            log(f"[Phase 1] ✓ Profile discovered: {profile_id}")

        except Exception as e:
            result["status"] = DISCOVERY_FAILED
            result["reason"] = f"phase_1_error:{str(e)[:80]}"
            result["action"] = "SKIP_DISCOVERY_FAILED"
            result["phase"] = "phase_1_error"
            log(f"[Phase 1] ✗ Error for {user_id}: {e}")
            return result

        # ============================================================
        # PHASE 2: Live Status Check (profile_id → stream info)
        # ============================================================
        try:
            log(f"[Phase 2] Checking live status for profile_id: {profile_id}")
            live_status = await discovery.check_live_status(profile_id)

            if not live_status or not live_status.get("is_live"):
                result["status"] = OFFLINE
                result["reason"] = "live_status_check_failed"
                result["action"] = "SKIP_OFFLINE"
                result["phase"] = "phase_2_offline"
                log(f"[Phase 2] ✗ User not live (profile_id: {profile_id})")
                return result

            stream_url = live_status.get("stream_url")
            stream_id = live_status.get("stream_id")

            if not stream_url:
                result["status"] = UNKNOWN
                result["reason"] = "stream_url_not_found"
                result["action"] = "SKIP_UNCERTAIN"
                result["phase"] = "phase_2_no_url"
                log(f"[Phase 2] ✗ Stream URL not found for profile_id: {profile_id}")
                return result

            result["stream_url"] = stream_url
            result["phase"] = "phase_2_done"

            # Check for premium status
            stream_info = live_status.get("stream_info", {})
            if isinstance(stream_info, dict):
                is_premium = False
                for key in ["is_premium", "isPremium", "premium", "paywall"]:
                    if stream_info.get(key):
                        is_premium = True
                        break
                if is_premium:
                    result["status"] = LIVE_PREMIUM
                    result["reason"] = "premium_stream"
                    result["action"] = "SKIP_PREMIUM"
                    result["phase"] = "phase_2_premium"
                    log(f"[Phase 2] ✓ Premium stream detected for {profile_id}")
                    return result

            log(f"[Phase 2] ✓ Live stream confirmed: {stream_url}")

        except Exception as e:
            result["status"] = UNKNOWN
            result["reason"] = f"phase_2_error:{str(e)[:80]}"
            result["action"] = "SKIP_UNCERTAIN"
            result["phase"] = "phase_2_error"
            log(f"[Phase 2] ✗ Error for {user_id}: {e}")
            return result

        # ============================================================
        # PHASE 3: Stream Validation (ownership verification)
        # ============================================================
        try:
            log(f"[Phase 3] Validating stream ownership for user_id: {user_id}")
            validation_result = await discovery.validate_stream(
                user_id, profile_id, stream_url
            )

            result["validation"] = validation_result
            result["phase"] = "phase_3_done"

            if not validation_result.get("validation_passed"):
                result["status"] = VALIDATION_FAILED
                result["reason"] = (
                    f"validation_failed:"
                    f"{validation_result.get('checks_passed', 0)}/"
                    f"{validation_result.get('total_checks', 0)}_checks_passed"
                )
                result["action"] = "SKIP_VALIDATION_FAILED"
                log(
                    f"[Phase 3] ✗ Validation failed for {user_id} "
                    f"(profile_id: {profile_id})"
                )
                return result

            # Check if the actual stream URL from validation differs
            actual_stream_url = validation_result.get("actual_stream_url")
            if actual_stream_url and actual_stream_url != stream_url:
                result["stream_url"] = actual_stream_url
                log(f"[Phase 3] ✓ Stream URL updated to actual URL")

            log(
                f"[Phase 3] ✓ Stream validated: "
                f"{validation_result.get('checks_passed', 0)}/"
                f"{validation_result.get('total_checks', 0)} checks passed"
            )

        except Exception as e:
            result["status"] = VALIDATION_FAILED
            result["reason"] = f"phase_3_error:{str(e)[:80]}"
            result["action"] = "SKIP_VALIDATION_FAILED"
            result["phase"] = "phase_3_error"
            log(f"[Phase 3] ✗ Error for {user_id}: {e}")
            return result

        # ============================================================
        # PHASE 4: Final classification - LIVE_NORMAL
        # ============================================================
        result["status"] = LIVE_NORMAL
        result["reason"] = "all_phases_passed"
        result["action"] = "CANDIDATE"
        result["phase"] = "phase_4_complete"
        log(f"[Phase 4] ✓ User {user_id} is ready for recording")

        return result


async def process_many_users_with_discovery(
    discovery: "SuperLiveDiscovery",
    user_ids: List[str],
    stored_names: Dict[str, str],
) -> Dict[str, Dict[str, Any]]:
    """
    Process multiple users through the Discovery pipeline.
    Uses a semaphore to control concurrency.
    """
    results = {}
    if not user_ids:
        return results

    if not DISCOVERY_AVAILABLE:
        log("Discovery module not available. Skipping all users.")
        for user_id in user_ids:
            results[user_id] = {
                "user_id": user_id,
                "status": DISCOVERY_FAILED,
                "reason": "discovery_module_not_available",
                "action": "SKIP_DISCOVERY_FAILED",
                "phase": "init",
                "display_name": stored_names.get(user_id),
            }
        return results

    semaphore = asyncio.Semaphore(DISCOVERY_CONCURRENCY)
    tasks = [
        process_user_with_discovery(discovery, uid, stored_names, semaphore)
        for uid in user_ids
    ]

    gathered = await asyncio.gather(*tasks, return_exceptions=True)

    for user_id, result in zip(user_ids, gathered):
        if isinstance(result, Exception):
            results[user_id] = {
                "user_id": user_id,
                "status": DISCOVERY_FAILED,
                "reason": f"exception:{str(result)[:100]}",
                "action": "SKIP_DISCOVERY_FAILED",
                "phase": "exception",
                "display_name": stored_names.get(user_id),
            }
        else:
            # Fallback to stored name if discovery didn't find one
            if not result.get("display_name"):
                result["display_name"] = stored_names.get(user_id)
            results[user_id] = result

    return results


# ============================================================
# MAIN MONITOR LOOP
# ============================================================
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
        "discovery_failed": 0,
        "validation_failed": 0,
        "started_recordings": 0,
        "names": {},
        "profile_map": {},
    }

    log("Starting monitor (with Discovery Layer)")

    if not WORKER_URL:
        log("FATAL: WORKER_URL is not set")
        return 1
    if not AUTO_API_TOKEN:
        log("FATAL: AUTO_API_TOKEN is not set")
        return 1

    if not DISCOVERY_AVAILABLE:
        log("FATAL: superlive_discovery.py not found in project")
        return 1

    # Initialize Discovery Layer (single instance with cache)
    discovery = SuperLiveDiscovery()
    log("Discovery Layer initialized")

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

        selected = users_to_check[:DISCOVERY_MAX_USERS_PER_CYCLE]
        stats["checked_now"] = len(selected)

        log(
            f"Running Discovery Layer for {len(selected)} users "
            f"(concurrency: {DISCOVERY_CONCURRENCY})"
        )

        results = await process_many_users_with_discovery(
            discovery, selected, stored_names
        )

        # Log all results
        for user_id, result in results.items():
            log_discovery(user_id, result)

        # Aggregate stats
        for user_id, result in results.items():
            status = result.get("status", UNKNOWN)
            if status == LIVE_NORMAL:
                stats["live_normal"] += 1
            elif status == LIVE_PREMIUM:
                stats["live_premium"] += 1
            elif status == OFFLINE:
                stats["offline"] += 1
            elif status == DISCOVERY_FAILED:
                stats["discovery_failed"] += 1
            elif status == VALIDATION_FAILED:
                stats["validation_failed"] += 1
            else:
                stats["unknown"] += 1

            # Track profile mapping
            if result.get("profile_id"):
                stats["profile_map"][user_id] = result["profile_id"]

            # Track names
            discovered_name = result.get("display_name") or result.get("username")
            final_name = discovered_name or stored_names.get(user_id)
            if final_name:
                stats["names"][user_id] = final_name
            if UPDATE_WATCHLIST_NAMES and discovered_name:
                await update_watchlist_name(user_id, discovered_name)

        # Refresh active recordings before triggering
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

        # Trigger recordings for validated streams
        for user_id in watchlist:
            result = results.get(user_id)
            if not result:
                continue

            # Only record LIVE_NORMAL (validation passed)
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
                result.get("display_name")
                or result.get("username")
                or stats["names"].get(user_id, "")
            )
            profile_id = result.get("profile_id", "")

            log(f"username={user_id}")
            log(f"profile_id={profile_id}")
            log("status=LIVE_NORMAL")
            log("validation=PASSED")
            log("Recording state: NOT_RECORDING")
            log(
                f"Slots: {active['active_count']}/{active['max_concurrent']}"
            )
            if stream_name:
                log(f"display_name={stream_name}")
            log(f"stream_url={stream_url}")
            log("Action: START_RECORDING")

            ok, reason = await trigger_auto_recording(
                user_id, stream_url, stream_name, profile_id
            )

            if ok:
                log(f"username={user_id}")
                log(f"profile_id={profile_id}")
                log("trigger=SUCCESS")
                log("workflow=record.yml")
                log("recording_engine=existing_record_once.py")
                log("validation=PASSED")
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
        # No lock to release - concurrency handled by GitHub Actions
        pass


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
