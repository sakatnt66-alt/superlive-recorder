#!/usr/bin/env python3
"""SuperLive Auto Monitor: discover confirmed live streams and trigger recording."""
import asyncio
import html
import json
import os
import re
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path
from superlive_discovery import SuperLiveDiscovery

GITHUB_REPOSITORY = os.environ.get("GITHUB_REPOSITORY", "")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
WORKER_API_URL = os.environ.get("WORKER_API_URL", "").strip().rstrip("/")
AUTO_API_TOKEN = os.environ.get("AUTO_API_TOKEN", "").strip()
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
USER_AGENT = "SuperLive-AutoMonitor/1.1 (GitHub Actions; +https://github.com/sakatnt66-alt/superlive-recorder)"
WATCHLIST_FILE = Path("data/watchlist.json")

MAX_CONCURRENT = 1
MAX_RECORDINGS = 5
MONITOR_BUDGET_SECONDS = 185
PER_USER_TIMEOUT_SECONDS = 14  # Increased to allow for deeper DOM/Network inspection

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(line_buffering=True)

import logging
logging.basicConfig(level=logging.INFO, format="[DISCOVERY] %(levelname)s %(message)s", stream=sys.stdout)

print(f"[DEBUG] WORKER_API_URL: {'SET' if WORKER_API_URL else 'MISSING'} (length={len(WORKER_API_URL)})", flush=True)
print(f"[DEBUG] AUTO_API_TOKEN: {'SET' if AUTO_API_TOKEN else 'MISSING'} (length={len(AUTO_API_TOKEN)})", flush=True)


def is_valid_stream_id(stream_id, user_id, profile_id) -> bool:
    """Strict validation: Stream ID must be numeric, valid length, and MUST NOT equal user_id or profile_id."""
    sid = str(stream_id or "").strip()
    uid = str(user_id or "").strip()
    pid = str(profile_id or "").strip()
    
    if not sid.isdigit():
        return False
    if not (5 <= len(sid) <= 15):
        return False
    # CRITICAL FIX: Stream ID must be independent of User/Profile IDs
    if sid == uid or sid == pid:
        return False
        
    return True


def is_valid_profile_id(profile_id) -> bool:
    value = str(profile_id or "").strip()
    return value.isdigit() and 5 <= len(value) <= 15


def read_error_body(error):
    try:
        body = error.read().decode("utf-8", errors="replace")
        return body[:500] if body else "(empty)"
    except Exception:
        return "(unreadable)"


def _request_json(url, *, headers=None, payload=None, timeout=8):
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request_headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    if headers:
        request_headers.update(headers)
    req = urllib.request.Request(url, data=data, headers=request_headers)
    with urllib.request.urlopen(req, timeout=timeout) as response:
        raw = response.read().decode("utf-8", errors="replace")
        return response.status, (json.loads(raw) if raw else {})


def send_telegram_message(text: str, parse_mode: str = "HTML"):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        _request_json(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            payload={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": parse_mode},
        )
    except Exception as exc:
        print(f"[TELEGRAM] Error sending message: {exc}")


def get_watchlist() -> list:
    headers = {"X-Auto-Token": AUTO_API_TOKEN}
    if WORKER_API_URL and AUTO_API_TOKEN:
        try:
            _, data = _request_json(f"{WORKER_API_URL}/api/watchlist", headers=headers)
            watchlist = data.get("watchlist", [])
            print(f"[WATCHLIST] Loaded {len(watchlist)} users from Worker API (KV)")
            return watchlist if isinstance(watchlist, list) else []
        except urllib.error.HTTPError as exc:
            print(f"[WATCHLIST] HTTP {exc.code}: {read_error_body(exc)}")
        except Exception as exc:
            print(f"[WATCHLIST] Worker API error: {exc}")
            
    print("[WATCHLIST] Falling back to local watchlist.json")
    try:
        with WATCHLIST_FILE.open("r", encoding="utf-8") as file:
            data = json.load(file)
            watchlist = data.get("watchlist", [])
            return watchlist if isinstance(watchlist, list) else []
    except FileNotFoundError:
        print("[WATCHLIST] Local watchlist.json not found")
    except Exception as exc:
        print(f"[WATCHLIST] Error loading local watchlist: {exc}")
    return []


def get_active_recordings() -> list:
    if not WORKER_API_URL or not AUTO_API_TOKEN:
        return []
    try:
        _, data = _request_json(
            f"{WORKER_API_URL}/api/active-recordings",
            headers={"X-Auto-Token": AUTO_API_TOKEN},
        )
        recordings = data.get("recordings", [])
        return recordings if isinstance(recordings, list) else []
    except urllib.error.HTTPError as exc:
        print(f"[WORKER] HTTP {exc.code}: {read_error_body(exc)}")
    except Exception as exc:
        print(f"[WORKER] Error getting active recordings: {exc}")
    return []


def trigger_recording(user_id: str, stream_id: str, stream_url: str, stream_name: str) -> bool:
    if not WORKER_API_URL or not AUTO_API_TOKEN:
        print("[AUTO] Worker API not configured")
        return False
    try:
        status, data = _request_json(
            f"{WORKER_API_URL}/api/auto-trigger/{user_id}",
            headers={"X-Auto-Token": AUTO_API_TOKEN},
            payload={"stream_url": stream_url, "stream_id": stream_id, "stream_name": stream_name},
        )
        if status == 200 and data.get("success"):
            print(f"[RECORDING] Starting recording for: {stream_name} (user_id={user_id})")
            print(f"[AUTO] URL: {stream_url}; Stream ID: {stream_id}")
            return True
        print(f"[AUTO] Failed to trigger {user_id}: {data.get('error', 'unknown error')}")
    except urllib.error.HTTPError as exc:
        body = read_error_body(exc)
        if exc.code == 409:
            print(f"[AUTO] SKIPPED {user_id}: already recording")
        elif exc.code == 404:
            print(f"[AUTO] NOT IN WATCHLIST: {user_id}")
        elif exc.code == 429:
            print(f"[AUTO] CONCURRENCY LIMIT REACHED for {user_id}")
        else:
            print(f"[AUTO] HTTP {exc.code} triggering {user_id}: {body}")
    except Exception as exc:
        print(f"[AUTO] Error triggering recording for {user_id}: {exc}")
    return False


async def check_user(discovery: SuperLiveDiscovery, user: dict, watchlist: list):
    user_id = str(user.get("stream_id") or user.get("user_id") or "").strip()
    if not user_id:
        print(f"[AUTO] Skip malformed watchlist entry: {user!r}")
        return None
        
    print(f"[AUTO] [Phase 1] Resolving {user_id}")
    try:
        profile_result = await asyncio.wait_for(
            discovery.discover_profile_id(user_id), timeout=PER_USER_TIMEOUT_SECONDS
        )
        if not isinstance(profile_result, dict) or not profile_result:
            print(f"[AUTO] Resolve failed for {user_id}: empty_or_invalid_response", flush=True)
            return None
            
        if profile_result.get("success") is False or profile_result.get("ok") is False:
            reason = profile_result.get("error") or "profile_resolution_failed"
            print(f"[AUTO] Resolve failed for {user_id}: {reason}", flush=True)
            return None
            
        profile_url = str(profile_result.get("profile_url") or "").strip()
        if not profile_url or "/profile/None" in profile_url or profile_url.endswith("/None"):
            reason = profile_result.get("error") or "profile_url_missing"
            print(f"[AUTO] Invalid profile URL for {user_id}: {profile_url!r}; reason={reason}", flush=True)
            return None
            
        username = str(profile_result.get("username") or "").strip()
        profile_id = str(profile_result.get("profile_id") or "").strip()
        if profile_id and not is_valid_profile_id(profile_id):
            print(f"[AUTO] Invalid profile_id ignored: {profile_id}")
            profile_id = ""
            
        print(f"[AUTO] [Phase 1] OK: {profile_url}")
        print(f"[AUTO] [Phase 2] Check live at {profile_url}")
        
        result = await asyncio.wait_for(
            discovery.check_live_status(
                profile_url=profile_url,
                user_id=user_id,
                profile_id=profile_id,
                phase1_username=username,
            ),
            timeout=PER_USER_TIMEOUT_SECONDS,
        )
        
        if not isinstance(result, dict):
            print(f"[AUTO] Invalid live-check response for {user_id}")
            return None
            
        if result.get("success") is False or result.get("ok") is False:
            print(f"[AUTO] status=UNKNOWN action=SKIP_CHECK_ERROR reason={result.get('error', 'inspection_failed')}")
            return None
            
        is_live = result.get("is_live") is True or result.get("live") is True
        is_premium = result.get("is_premium") is True or result.get("premium") is True
        stream_id = str(result.get("stream_id") or "").strip()
        stream_url = str(result.get("stream_url") or "").strip()
        ownership_verified = result.get("ownership_verified", False)
        
        # Extract Display Name
        display_name = str(result.get("display_name") or "").strip()
        if not display_name or display_name.lower() in ["superlive", "superlivetv", "profile", username]:
            # Fallback to watchlist display_name if provided, or user_id
            for item in watchlist:
                if str(item.get("stream_id") or item.get("user_id") or "") == user_id and item.get("display_name"):
                    display_name = str(item["display_name"]).strip()
                    break
        display_name = display_name or username or user_id
        
        stream_id_valid = is_valid_stream_id(stream_id, user_id, profile_id)
        
        print(f"[AUTO] Processing user: {display_name} (user_id={user_id})")
        print("[AUTO] source=discovery_layer")
        print(f"[AUTO] profile_url={profile_url}")
        if profile_id:
            print(f"[AUTO] profile_id={profile_id}")
        print(f"[AUTO] Real stream ID resolved: {stream_id or '(not found)'} (valid={stream_id_valid}, owned={ownership_verified})")
        
        if is_premium:
            print("[AUTO] phase=p2_premium status=LIVE_PREMIUM action=SKIP_PREMIUM reason=premium")
            return None
            
        if not is_live:
            evidence = result.get("live_evidence") or []
            print(f"[AUTO] phase=p2_unconfirmed status=UNKNOWN action=SKIP_UNCONFIRMED evidence={evidence}")
            return None
            
        if not stream_id_valid:
            print(f"[AUTO] status=LIVE_BUT_INVALID_STREAM_ID action=SKIP stream_id={stream_id or '(missing)'}")
            return None
            
        if not ownership_verified:
            print(f"[AUTO] status=LIVE_BUT_UNVERIFIED_OWNERSHIP action=SKIP stream_id={stream_id}")
            return None
            
        if not stream_url or not re.fullmatch(r"https://superlivetv\.com/fr/livestream/\d+/?", stream_url):
            stream_url = f"https://superlivetv.com/fr/livestream/{stream_id}"
            
        print("[AUTO] Stream URL validated for the expected user")
        print("[AUTO] phase=phase_3_done status=LIVE_NORMAL action=CANDIDATE reason=live")
        return {
            "user_id": user_id, 
            "stream_id": stream_id, 
            "stream_url": stream_url, 
            "stream_name": display_name
        }
        
    except asyncio.TimeoutError:
        print(f"[AUTO] TIMEOUT user={user_id} limit={PER_USER_TIMEOUT_SECONDS}s action=SKIP_AND_CONTINUE", flush=True)
        return None
    except Exception as exc:
        print(f"[AUTO] Error checking user {user_id}: {type(exc).__name__}: {exc}")
        import traceback
        traceback.print_exc()
        return None


async def main():
    print("[AUTO] Starting Auto Monitor")
    watchlist = get_watchlist()
    if not watchlist:
        print("[AUTO] Watchlist is empty - nothing to do")
        return
        
    print(f"[AUTO] Watchlist: {len(watchlist)} users")
    active = get_active_recordings()
    print(f"[AUTO] Active: {len(active)}/{MAX_RECORDINGS}")
    if len(active) >= MAX_RECORDINGS:
        print("[AUTO] Concurrency limit reached")
        return
        
    active_user_ids = {str(r.get("user_id")) for r in active if r.get("user_id") is not None}
    active_stream_ids = {str(r.get("stream_id")) for r in active if r.get("stream_id") is not None}
    
    to_check = []
    for item in watchlist:
        uid = str(item.get("stream_id") or item.get("user_id") or "").strip()
        if not uid or uid in active_user_ids:
            continue
        to_check.append(item)
        
    print(f"[AUTO] Discovery queue: {len(to_check)} users; per-user timeout={PER_USER_TIMEOUT_SECONDS}s; budget={MONITOR_BUDGET_SECONDS}s", flush=True)
    if not to_check:
        print("[AUTO] No users to check")
        return
        
    candidates = []
    deadline = time.monotonic() + MONITOR_BUDGET_SECONDS
    checked = 0
    timed_out_or_unchecked = 0
    
    discovery = SuperLiveDiscovery(timeout_ms=7000, navigation_timeout_ms=10000, retries=0)
    try:
        print("[AUTO] Starting Chromium (startup timeout=25s)", flush=True)
        await asyncio.wait_for(discovery.open(), timeout=25)
    except asyncio.TimeoutError:
        print("[AUTO] Browser startup timed out after 25s; stopping this run", flush=True)
        await discovery.close()
        return
    except Exception as exc:
        print(f"[AUTO] Browser startup failed: {type(exc).__name__}: {exc}", flush=True)
        await discovery.close()
        return
        
    try:
        for item in to_check:
            remaining = deadline - time.monotonic()
            if remaining <= 1:
                timed_out_or_unchecked += len(to_check) - checked
                print(f"[AUTO] Global time budget exhausted; leaving {len(to_check) - checked} user(s) unchecked", flush=True)
                break
                
            uid = str(item.get("stream_id") or item.get("user_id") or "").strip() or "(invalid)"
            print(f"[AUTO] Queue progress {checked + 1}/{len(to_check)} user={uid} budget_left={int(remaining)}s", flush=True)
            try:
                result = await asyncio.wait_for(
                    check_user(discovery, item, watchlist),
                    timeout=min(PER_USER_TIMEOUT_SECONDS * 2 + 1, remaining),
                )
                if result is not None:
                    candidates.append(result)
            except asyncio.TimeoutError:
                print(f"[AUTO] USER_TOTAL_TIMEOUT user={uid} action=SKIP_AND_CONTINUE", flush=True)
                timed_out_or_unchecked += 1
            except Exception as exc:
                print(f"[AUTO] Task failed for {uid}: {type(exc).__name__}: {exc}", flush=True)
            checked += 1
    finally:
        await discovery.close()
        
    print(f"[AUTO] Discovery summary: checked={checked}/{len(to_check)} candidates={len(candidates)} unchecked_or_timed_out={timed_out_or_unchecked}", flush=True)
    
    seen = set()
    deduplicated = []
    for record in candidates:
        sid = record["stream_id"]
        if sid in active_stream_ids:
            print(f"[AUTO] Skip stream already active: {sid}")
            continue
        if sid in seen:
            print(f"[AUTO] Duplicate stream_id {sid} for user {record['user_id']} - skipping")
            continue
        seen.add(sid)
        deduplicated.append(record)
        
    slots = max(0, MAX_RECORDINGS - len(active))
    to_record = deduplicated[:slots]
    print(f"[AUTO] Slots: {slots}; LIVE_NORMAL candidates: {len(to_record)}")
    
    if not to_record:
        print("[AUTO] No new confirmed LIVE_NORMAL streams to record")
        print("[AUTO] Monitor completed")
        return
        
    triggered_records = []
    for record in to_record:
        if trigger_recording(record["user_id"], record["stream_id"], record["stream_url"], record["stream_name"]):
            triggered_records.append(record)
            
    if triggered_records:
        for r in triggered_records:
            msg = f"🔴 <b>بدأ تسجيل بث جديد</b>\n\n"
            msg += f"👤 <b>الاسم:</b> {html.escape(r['stream_name'])}\n"
            msg += f"🆔 <b>المستخدم:</b> {html.escape(r['user_id'])}\n"
            msg += f"📡 <b>معرّف البث:</b> {html.escape(r['stream_id'])}\n\n"
            msg += f"🔗 <b>رابط البث:</b>\n{html.escape(r['stream_url'])}"
            send_telegram_message(msg, parse_mode="HTML")
            
    print(f"[AUTO] Monitor completed - triggered {len(triggered_records)}/{len(to_record)}")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("[AUTO] Interrupted", flush=True)
    except Exception as exc:
        print(f"[AUTO] FATAL {type(exc).__name__}: {exc}", flush=True)
        raise
