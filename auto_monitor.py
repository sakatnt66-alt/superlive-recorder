# -*- coding: utf-8 -*-
"""
auto_monitor.py - Version 5.4 (Strict Stream ID URL Enforcement)

CRITICAL FIX in v5.4:
- Strictly use stream_id to build the recording URL if available.
- Fallback to stream_url or user_id only if stream_id is missing.
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

MAX_CONCURRENT = int(os.environ.get("MAX_CONCURRENT", "5"))
API_TIMEOUT = int(os.environ.get("API_TIMEOUT", "20"))
DISCOVERY_CONCURRENCY = int(os.environ.get("DISCOVERY_CONCURRENCY", "4"))

LIVE_NORMAL = "LIVE_NORMAL"
LIVE_PREMIUM = "LIVE_PREMIUM"
OFFLINE = "OFFLINE"
UNKNOWN = "UNKNOWN"
DISCOVERY_FAILED = "DISCOVERY_FAILED"


def log(msg): print(f"[AUTO] {msg}", flush=True)


def log_result(uid, r):
    log(f"username={uid}")
    log(f"source=discovery_layer")
    if r.get("profile_url"): log(f"profile_url={r['profile_url']}")
    if r.get("profile_id"): log(f"profile_id={r['profile_id']}")
    if r.get("stream_id"): log(f"stream_id={r['stream_id']}")
    log(f"phase={r.get('phase', 'unknown')}")
    log(f"status={r.get('status', UNKNOWN)}")
    log(f"action={r.get('action', 'SKIP')}")
    log(f"reason={r.get('reason', 'unknown')}")


def split_text(t, limit=3900):
    chunks, current = [], ""
    for line in t.split("\n"):
        if len(current) + len(line) + 1 > limit:
            chunks.append(current); current = line
        else:
            current = f"{current}\n{line}" if current else line
    if current: chunks.append(current)
    return chunks or [""]


def send_report(text):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID: return False
    for chunk in split_text(text):
        try:
            req = urllib.request.Request(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                data=json.dumps({"chat_id": TELEGRAM_CHAT_ID, "text": chunk, "parse_mode": "HTML"}).encode(),
                method="POST",
                headers={"Content-Type": "application/json"}
            )
            with urllib.request.urlopen(req, timeout=20) as r: r.read()
        except: pass
    return True


def build_report(elapsed, stats):
    lines = [f"✅ انتهى الفحص في {elapsed:.1f} ثانية.", "",
             f"📊 ({stats['total_watchlist']}):",
             f"• قيد التسجيل: {stats['already_recording']}",
             f"• تم فحصه: {stats['checked_now']}", "",
             "📈 النتائج:",
             f"• 🟢 عادي: {stats['live_normal']}",
             f"• 🟡 مدفوع: {stats['live_premium']}",
             f"• ⚪ غير متصل: {stats['offline']}",
             f"• ⚠️ فشل اكتشاف: {stats.get('discovery_failed', 0)}",
             "",
             f"🔴 تم تشغيل {stats['started_recordings']} تسجيل."]
    return "\n".join(lines)


def api_sync(path, method="GET", payload=None):
    url = f"{WORKER_URL}{path}"
    headers = {"User-Agent": "AutoMonitor/1.0", "Content-Type": "application/json", "Accept": "application/json"}
    if AUTO_API_TOKEN: headers["X-Auto-Token"] = AUTO_API_TOKEN
    req = urllib.request.Request(url, data=json.dumps(payload).encode() if payload else None, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=API_TIMEOUT) as r:
            body = r.read().decode()
            return r.status, json.loads(body) if body else {}
    except urllib.error.HTTPError as e:
        return e.code, {"error": str(e)}
    except Exception as e:
        return 0, {"error": str(e)}


async def api(path, method="GET", payload=None):
    return await asyncio.to_thread(api_sync, path, method, payload)


async def load_watchlist():
    status, data = await api("/api/watchlist", "GET")
    if status != 200: raise RuntimeError(f"Watchlist error: {status}")
    users = []
    for it in data.get("watchlist", []):
        uid = str(it.get("stream_id") or "").strip()
        if uid and uid.isdigit():
            users.append(uid)
    return users


async def load_active_recordings():
    status, data = await api("/api/active-recordings", "GET")
    if status != 200: return {"active_count": 0, "max_concurrent": MAX_CONCURRENT, "active_ids": set()}
    ids = {str(r.get("stream_id")) for r in data.get("recordings", []) if r.get("status") == "recording"}
    return {"active_count": len(ids), "max_concurrent": MAX_CONCURRENT, "active_ids": ids}


async def trigger_recording(uid, url, name="", sid=""):
    status, data = await api(f"/api/auto-trigger/{uid}", "POST", {
        "stream_url": url, "source": "auto", "stream_name": name, "stream_id": sid, "user_id": uid
    })
    return (True, "started") if status == 200 and data.get("started") else (False, data.get("error", f"http_{status}"))


async def process_user(discovery, uid, sem):
    async with sem:
        r = {"user_id": uid, "profile_url": None, "profile_id": None, "stream_id": None,
             "status": UNKNOWN, "reason": "", "action": "SKIP", "phase": "none"}
        if not DISCOVERY_AVAILABLE:
            r.update({"status": DISCOVERY_FAILED, "reason": "no_discovery", "phase": "init"})
            return r

        try:
            log(f"[Phase 1] Resolving {uid}")
            info = await discovery.discover_profile_id(uid)
            if not info or not info.get("profile_url"):
                r.update({"status": DISCOVERY_FAILED, "reason": "url_not_found", "phase": "phase_1"})
                return r
            r["profile_url"] = info["profile_url"]
            r["profile_id"] = info.get("profile_id")
            r["phase"] = "phase_1_done"
            log(f"[Phase 1] OK: {r['profile_url']}")
        except Exception as e:
            r.update({"status": DISCOVERY_FAILED, "reason": f"p1:{str(e)[:60]}", "phase": "p1_error"})
            return r

        try:
            log(f"[Phase 2] Check live at {r['profile_url']}")
            live = await discovery.check_live_status(r["profile_url"], uid, r.get("profile_id", ""))
            if not live:
                r.update({"status": UNKNOWN, "reason": "check_failed", "phase": "p2_failed"})
                return r
            
            if not live.get("is_live"):
                r.update({"status": OFFLINE, "reason": "no_live", "action": "SKIP_OFFLINE", "phase": "p2_offline", "stream_id": uid})
                return r
            
            if live.get("is_premium"):
                r.update({"status": LIVE_PREMIUM, "reason": "premium", "action": "SKIP_PREMIUM", "phase": "p2_premium", "stream_id": live.get("stream_id") or uid})
                return r
            
            r.update({
                "status": LIVE_NORMAL, "reason": "live", "action": "CANDIDATE",
                "stream_url": live.get("stream_url"),
                "stream_id": live.get("stream_id"),
                "phase": "p2_live"
            })
            log(f"[Phase 2] LIVE OK, stream_id={r['stream_id']}")
        except Exception as e:
            r.update({"status": UNKNOWN, "reason": f"p2:{str(e)[:60]}", "phase": "p2_error"})
            return r
        return r


async def process_all(discovery, uids):
    if not uids: return {}
    sem = asyncio.Semaphore(DISCOVERY_CONCURRENCY)
    tasks = [process_user(discovery, uid, sem) for uid in uids]
    gathered = await asyncio.gather(*tasks, return_exceptions=True)
    results = {}
    for uid, res in zip(uids, gathered):
        if isinstance(res, Exception):
            results[uid] = {"user_id": uid, "status": DISCOVERY_FAILED, "reason": str(res)[:100]}
        else:
            results[uid] = res
    return results


async def main_async():
    t0 = time.monotonic()
    stats = {"total_watchlist": 0, "already_recording": 0, "checked_now": 0,
             "live_normal": 0, "live_premium": 0, "offline": 0, "unknown": 0,
             "discovery_failed": 0, "started_recordings": 0}

    log("Starting Auto Monitor (Optimized for Speed)")
    if not DISCOVERY_AVAILABLE or not WORKER_URL:
        log("FATAL: Missing dependencies"); return 1

    discovery = SuperLiveDiscovery()
    try:
        watchlist = await load_watchlist()
        stats["total_watchlist"] = len(watchlist)
        log(f"Watchlist: {len(watchlist)} users")
        if not watchlist: return 0

        active = await load_active_recordings()
        log(f"Active: {active['active_count']}/{active['max_concurrent']}")
        if active["active_count"] >= active["max_concurrent"]:
            log("Max reached"); return 0

        users_to_check = [u for u in watchlist if u not in active["active_ids"]]
        for u in watchlist:
            if u in active["active_ids"]:
                stats["already_recording"] += 1

        if not users_to_check:
            log("All recording"); return 0

        stats["checked_now"] = len(users_to_check)
        log(f"Discovery for {len(users_to_check)} users (Concurrency: {DISCOVERY_CONCURRENCY})")

        results = await process_all(discovery, users_to_check)
        for uid, r in results.items():
            log_result(uid, r)
            s = r.get("status")
            if s == LIVE_NORMAL: stats["live_normal"] += 1
            elif s == LIVE_PREMIUM: stats["live_premium"] += 1
            elif s == OFFLINE: stats["offline"] += 1
            elif s == DISCOVERY_FAILED: stats["discovery_failed"] += 1
            else: stats["unknown"] += 1

        active = await load_active_recordings()
        slots = active["max_concurrent"] - active["active_count"]
        log(f"Slots: {slots}")
        if slots <= 0: return 0

        for uid in watchlist:
            r = results.get(uid)
            if not r or r.get("status") != LIVE_NORMAL: continue
            if uid in active["active_ids"] or slots <= 0: continue
            
            # CRITICAL FIX: Strictly use stream_id to build the URL if available
            stream_id = r.get("stream_id")
            if stream_id and len(str(stream_id)) >= 7:
                url = f"https://superlivetv.com/fr/livestream/{stream_id}"
            else:
                stream_url = r.get("stream_url")
                if stream_url and "livestream" in stream_url:
                    url = stream_url
                else:
                    url = f"https://superlivetv.com/fr/livestream/{uid}"
            
            log(f"START_RECORDING {uid} (stream_id={stream_id}) -> {url}")
            ok, reason = await trigger_recording(uid, url, "", stream_id)
            if ok:
                stats["started_recordings"] += 1
                slots -= 1
                active["active_ids"].add(uid)
            elif reason == "concurrency_limit":
                break

        if SEND_REPORT: send_report(build_report(time.monotonic() - t0, stats))
        log("Monitor completed")
        return 0
    except Exception as e:
        log(f"FATAL: {e}")
        return 1


def main():
    try: return asyncio.run(main_async())
    except: return 1

if __name__ == "__main__": sys.exit(main())
