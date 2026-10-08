"""
auto_monitor.py - Auto Monitoring System (Fixed Logic)
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
DISCOVERY_MAX_USERS_PER_CYCLE = int(os.environ.get("DISCOVERY_MAX_USERS_PER_CYCLE", "12"))

BASE_LIVE_URL = "https://superlivetv.com/fr/livestream/{user_id}"

LIVE_NORMAL = "LIVE_NORMAL"
LIVE_PREMIUM = "LIVE_PREMIUM"
OFFLINE = "OFFLINE"
UNKNOWN = "UNKNOWN"
DISCOVERY_FAILED = "DISCOVERY_FAILED"

def log(message: str) -> None:
    print(f"[AUTO] {message}", flush=True)

def log_discovery(user_id: str, result: Dict[str, Any]) -> None:
    log(f"username={user_id}")
    log(f"source=discovery_layer")
    log(f"profile_url={result.get('profile_url', 'N/A')}")
    log(f"status={result.get('status', UNKNOWN)}")
    log(f"action={result.get('action', 'SKIP')}")
    log(f"reason={result.get('reason', 'unknown')}")

def html_escape(text: str) -> str:
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

def split_telegram_text(text: str, limit: int = 3900) -> List[str]:
    lines = text.split("\n")
    chunks = []
    current = ""
    for line in lines:
        if len(current) + len(line) + 1 > limit:
            chunks.append(current)
            current = line
        else:
            current = f"{current}\n{line}" if current else line
    if current: chunks.append(current)
    return chunks or [""]

def send_telegram_report(text: str) -> bool:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID: return False
    chunks = split_telegram_text(text)
    for chunk in chunks:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {"chat_id": TELEGRAM_CHAT_ID, "text": chunk, "parse_mode": "HTML"}
        try:
            data = json.dumps(payload).encode("utf-8")
            req = urllib.request.Request(url, data=data, method="POST", headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=20) as response: response.read()
        except: pass
    return True

def build_report(elapsed_seconds: float, stats: Dict[str, Any]) -> str:
    lines = [f"✅ انتهى الفحص في {elapsed_seconds:.1f} ثانية.", ""]
    lines.append(f"📊 إحصائيات ({stats['total_watchlist']}):")
    lines.append(f"• 🟢 بث عادي: {stats['live_normal']}")
    lines.append(f"• ⚪ غير متصل: {stats['offline']}")
    lines.append(f"• 🔴 تم تشغيل {stats['started_recordings']} تسجيل.")
    return "\n".join(lines)

def api_request_sync(path: str, method: str = "GET", payload: Optional[dict] = None):
    url = f"{WORKER_URL}{path}"
    headers = {"User-Agent": "AutoMonitor/1.0", "Content-Type": "application/json", "Accept": "application/json"}
    if AUTO_API_TOKEN: headers["X-Auto-Token"] = AUTO_API_TOKEN
    data = json.dumps(payload).encode("utf-8") if payload else None
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=API_TIMEOUT) as response:
            body = response.read().decode("utf-8", errors="replace")
            return response.status, json.loads(body) if body else {}
    except urllib.error.HTTPError as e:
        return e.code, {"error": str(e)}
    except Exception as e:
        return 0, {"error": str(e)}

async def api_request(path: str, method: str = "GET", payload: Optional[dict] = None):
    return await asyncio.to_thread(api_request_sync, path, method, payload)

async def load_watchlist():
    status, data = await api_request("/api/watchlist", "GET")
    if status != 200: raise RuntimeError(f"Failed to load watchlist")
    items = data.get("watchlist", [])
    users = [str(item.get("stream_id")) for item in items if item.get("stream_id")]
    names = {str(item.get("stream_id")): item.get("display_name") for item in items if item.get("display_name")}
    return users, names

async def load_active_recordings():
    status, data = await api_request("/api/active-recordings", "GET")
    if status != 200: return {"active_count": 0, "max_concurrent": 5, "active_ids": set()}
    active_ids = {str(rec.get("stream_id")) for rec in data.get("recordings", []) if rec.get("status") == "recording"}
    return {"active_count": len(active_ids), "max_concurrent": 5, "active_ids": active_ids}

async def trigger_auto_recording(user_id: str, stream_url: str, stream_name: str = ""):
    payload = {"stream_url": stream_url, "source": "auto", "stream_name": stream_name, "user_id": user_id}
    status, data = await api_request(f"/api/auto-trigger/{user_id}", "POST", payload)
    if status == 200 and data.get("started"): return True, "started"
    return False, data.get("error", f"http_{status}")

async def update_watchlist_name(user_id: str, display_name: str):
    try: await api_request(f"/api/watchlist/name/{user_id}", "POST", {"display_name": display_name})
    except: pass

async def process_user_with_discovery(discovery, user_id: str, stored_names: Dict, semaphore):
    async with semaphore:
        result = {"user_id": user_id, "status": UNKNOWN, "reason": "", "action": "SKIP"}

        if not DISCOVERY_AVAILABLE:
            result.update({"status": DISCOVERY_FAILED, "reason": "no_discovery_module"})
            return result

        # --- PHASE 1: RESOLVE IDENTITY ---
        # الهدف: الحصول على الرابط الصحيح (Profile URL)
        try:
            log(f"[Phase 1] Resolving identity for {user_id}")
            profile_info = await discovery.discover_profile_id(user_id)
            
            if not profile_info or not profile_info.get("profile_url"):
                result.update({"status": DISCOVERY_FAILED, "reason": "profile_url_not_found", "action": "SKIP_DISCOVERY_FAILED"})
                return result

            profile_url = profile_info["profile_url"]
            profile_id = profile_info.get("profile_id") # قد يكون None في حالة الـ slug
            username = profile_info.get("username")
            
            result["profile_url"] = profile_url
            result["profile_id"] = profile_id
            result["display_name"] = username or stored_names.get(user_id)
            
            log(f"[Phase 1] ✓ Resolved: {profile_url}")

        except Exception as e:
            result.update({"status": DISCOVERY_FAILED, "reason": f"phase1_error:{str(e)[:50]}"})
            return result

        # --- PHASE 2: CHECK LIVE STATUS ---
        # الهدف: زيارة الرابط والتحقق من "DIRECT"
        try:
            log(f"[Phase 2] Checking live status at {profile_url}")
            live_status = await discovery.check_live_status(profile_url)

            if not live_status or not live_status.get("is_live"):
                result.update({"status": OFFLINE, "reason": "no_live_indicator_found", "action": "SKIP_OFFLINE"})
                log(f"[Phase 2] ✗ User is OFFLINE")
                return result

            # إذا وجدنا رابط بث محدد في صفحة البروفايل نستخدمه، وإلا نستخدم الرابط الافتراضي
            stream_url = live_status.get("stream_url") or BASE_LIVE_URL.format(user_id=user_id)
            
            result.update({
                "status": LIVE_NORMAL,
                "reason": "live_indicator_found",
                "action": "CANDIDATE",
                "stream_url": stream_url
            })
            log(f"[Phase 2] ✓ User is LIVE")

        except Exception as e:
            result.update({"status": UNKNOWN, "reason": f"phase2_error:{str(e)[:50]}"})
            return result

        return result

async def process_many_users(discovery, user_ids, stored_names):
    results = {}
    if not user_ids: return results
    semaphore = asyncio.Semaphore(DISCOVERY_CONCURRENCY)
    tasks = [process_user_with_discovery(discovery, uid, stored_names, semaphore) for uid in user_ids]
    gathered = await asyncio.gather(*tasks, return_exceptions=True)
    for uid, res in zip(user_ids, gathered):
        if isinstance(res, Exception):
            results[uid] = {"user_id": uid, "status": DISCOVERY_FAILED, "reason": str(res)[:100]}
        else:
            results[uid] = res
    return results

async def async_main():
    start_time = time.monotonic()
    stats = {"total_watchlist": 0, "already_recording": 0, "checked_now": 0, "live_normal": 0, "offline": 0, "started_recordings": 0, "names": {}}
    
    log("Starting monitor (Discovery Layer v3.0)")
    if not DISCOVERY_AVAILABLE:
        log("FATAL: superlive_discovery.py missing")
        return 1

    discovery = SuperLiveDiscovery()
    
    try:
        watchlist, stored_names = await load_watchlist()
        stats["total_watchlist"] = len(watchlist)
        if not watchlist: return 0

        active = await load_active_recordings()
        if active["active_count"] >= active["max_concurrent"]:
            log("Max concurrency reached")
            return 0

        users_to_check = [u for u in watchlist if u not in active["active_ids"]]
        if not users_to_check: return 0

        selected = users_to_check[:DISCOVERY_MAX_USERS_PER_CYCLE]
        stats["checked_now"] = len(selected)

        results = await process_many_users(discovery, selected, stored_names)

        for uid, res in results.items():
            log_discovery(uid, res)
            if res.get("status") == LIVE_NORMAL: stats["live_normal"] += 1
            elif res.get("status") == OFFLINE: stats["offline"] += 1
            
            name = res.get("display_name") or stored_names.get(uid)
            if name: stats["names"][uid] = name
            if UPDATE_WATCHLIST_NAMES and name: await update_watchlist_name(uid, name)

        active = await load_active_recordings() # Refresh
        slots = active["max_concurrent"] - active["active_count"]
        
        for uid in watchlist:
            res = results.get(uid)
            if not res or res.get("status") != LIVE_NORMAL: continue
            if uid in active["active_ids"] or slots <= 0: continue

            stream_url = res.get("stream_url") or BASE_LIVE_URL.format(user_id=uid)
            name = res.get("display_name") or stats["names"].get(uid, "")
            
            log(f"Action: START_RECORDING for {uid}")
            ok, reason = await trigger_auto_recording(uid, stream_url, name)
            if ok:
                stats["started_recordings"] += 1
                slots -= 1
                active["active_ids"].add(uid)

        if SEND_REPORT: send_telegram_report(build_report(time.monotonic() - start_time, stats))
        return 0
    finally:
        pass

def main():
    try: return asyncio.run(async_main())
    except Exception as e:
        log(f"FATAL: {e}")
        return 1

if __name__ == "__main__":
    sys.exit(main())
