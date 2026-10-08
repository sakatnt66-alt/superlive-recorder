# -*- coding: utf-8 -*-
"""
auto_monitor.py - Version 5.2 (Correct Recording URL)

CRITICAL FIX in v5.2:
- Always use BASE_LIVE_URL (/fr/livestream/{user_id}) for recording
- record_once.py ONLY works with this URL pattern
- Do NOT use stream_url or profile_url from discovery layer
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

BASE_LIVE_URL = "https://superlivetv.com/fr/livestream/{user_id}"

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
    if r.get("display_name"): log(f"display_name={r['display_name']}")
    log(f"phase={r.get('phase', 'unknown')}")
    log(f"status={r.get('status', UNKNOWN)}")
    log(f"action={r.get('action', 'SKIP')}")
    log(f"reason={r.get('reason', 'unknown')}")


def html_escape(t): return str(t).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


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
    names = stats.get("names", {})
    if names:
        lines.append(""); lines.append("👤 الأسماء:")
        for uid, name in names.items():
            pid = stats.get("profile_map", {}).get(uid, "?")
            lines.append(f"• {uid} [P:{pid}] -> {html_escape(name)}")
    return "\n".join(lines)


def api_sync(path, method="GET", payload=None):
    url = f"{WORKER_URL}{path}"
    headers = {"User-Agent": "AutoMonitor/1.0", "Content-Type": "application/json", "Accept": "application/json"}
    if AUTO_API_TOKEN: headers["X-Auto-Token"] = AUTO_API_TOKEN
    data = json.dumps(payload).encode() if payload else None
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
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
    items = data.get("watchlist", [])
    users = []
    names = {}
    for it in items:
        uid = str(it.get("stream_id") or "").strip()
        if uid and uid.isdigit():
            users.append(uid)
            if it.get("display_name"): names[uid] = str(it["display_name"])
    return users, names


async def load_active_recordings():
    status, data = await api("/api/active-recordings", "GET")
    if status != 200: return {"active_count": 0, "max_concurrent": MAX_CONCURRENT, "active_ids": set()}
    ids = {str(r.get("stream_id")) for r in data.get("recordings", []) if r.get("status") == "recording"}
    return {"active_count": len(ids), "max_concurrent": MAX_CONCURRENT, "active_ids": ids}


async def trigger_recording(uid, url, name="", pid=""):
    status, data = await api(f"/api/auto-trigger/{uid}", "POST",
                             {"stream_url": url, "source": "auto", "stream_name": name, "profile_id": pid, "user_id": uid})
    if status == 200 and data.get("started"): return True, "started"
    return False, data.get("error", f"http_{status}")


async def update_name(uid, name):
    try: await api(f"/api/watchlist/name/{uid}", "POST", {"display_name": name})
    except: pass


async def process_user(discovery, uid, names, sem):
    async with sem:
        r = {"user_id": uid, "profile_url": None, "profile_id": None,
             "display_name": names.get(uid), "stream_url": None,
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
            if info.get("username"): r["display_name"] = info["username"]
            r["phase"] = "phase_1_done"
            log(f"[Phase 1] OK: {r['profile_url']}")
        except Exception as e:
            r.update({"status": DISCOVERY_FAILED, "reason": f"p1:{str(e)[:60]}", "phase": "p1_error"})
            return r

        try:
            log(f"[Phase 2] Check live at {r['profile_url']}")
            live = await discovery.check_live_status(
                r["profile_url"],
                uid,
                r.get("profile_id", ""),
                r.get("display_name")
            )
            if not live:
                r.update({"status": UNKNOWN, "reason": "check_failed", "phase": "p2_failed"})
                return r
            if live.get("username") and not r.get("display_name"): r["display_name"] = live["username"]
            if not live.get("is_live"):
                r.update({"status": OFFLINE, "reason": "no_live", "action": "SKIP_OFFLINE", "phase": "p2_offline"})
                return r
            if live.get("is_premium"):
                r.update({"status": LIVE_PREMIUM, "reason": "premium", "action": "SKIP_PREMIUM", "phase": "p2_premium"})
                return r
            # For LIVE_NORMAL, we only need to know it's live.
            # The actual recording URL will be BASE_LIVE_URL (set in main_async).
            r.update({"status": LIVE_NORMAL, "reason": "live", "action": "CANDIDATE",
                      "stream_url": live.get("stream_url") or r["profile_url"], "phase": "p2_live"})
            log(f"[Phase 2] LIVE OK")
        except Exception as e:
            r.update({"status": UNKNOWN, "reason": f"p2:{str(e)[:60]}", "phase": "p2_error"})
            return r

        try:
            validation = await discovery.validate_stream(uid, r.get("profile_id", uid), r.get("stream_url", ""))
            if not validation.get("validation_passed"):
                r.update({"status": UNKNOWN, "reason": "validation_failed", "phase": "p3_failed"})
                return r
            r["phase"] = "phase_3_done"
        except Exception as e:
            log(f"[Phase 3] Error (continuing): {e}")

        return r


async def process_all(discovery, uids, names):
    if not uids: return {}
    sem = asyncio.Semaphore(DISCOVERY_CONCURRENCY)
    tasks = [process_user(discovery, uid, names, sem) for uid in uids]
    gathered = await asyncio.gather(*tasks, return_exceptions=True)
    results = {}
    for uid, res in zip(uids, gathered):
        if isinstance(res, Exception):
            results[uid] = {"user_id": uid, "status": DISCOVERY_FAILED, "reason": str(res)[:100], "display_name": names.get(uid)}
        else:
            if not res.get("display_name"): res["display_name"] = names.get(uid)
            results[uid] = res
    return results


async def main_async():
    t0 = time.monotonic()
    stats = {"total_watchlist": 0, "already_recording": 0, "checked_now": 0,
             "live_normal": 0, "live_premium": 0, "offline": 0, "unknown": 0,
             "discovery_failed": 0, "started_recordings": 0, "names": {}, "profile_map": {}}

    log("Starting Auto Monitor v5.2 (Correct Recording URL)")
    if not DISCOVERY_AVAILABLE: log("FATAL: no discovery module"); return 1
    if not WORKER_URL: log("FATAL: no WORKER_URL"); return 1

    discovery = SuperLiveDiscovery()

    try:
        watchlist, names = await load_watchlist()
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
                if names.get(u): stats["names"][u] = names[u]

        if not users_to_check:
            log("All recording"); return 0

        stats["checked_now"] = len(users_to_check)
        log(f"Discovery for {len(users_to_check)} users")

        results = await process_all(discovery, users_to_check, names)
        for uid, r in results.items():
            log_result(uid, r)
            s = r.get("status")
            if s == LIVE_NORMAL: stats["live_normal"] += 1
            elif s == LIVE_PREMIUM: stats["live_premium"] += 1
            elif s == OFFLINE: stats["offline"] += 1
            elif s == DISCOVERY_FAILED: stats["discovery_failed"] += 1
            else: stats["unknown"] += 1
            if r.get("profile_id"): stats["profile_map"][uid] = r["profile_id"]
            name = r.get("display_name")
            if name: stats["names"][uid] = name
            if UPDATE_WATCHLIST_NAMES and name: await update_name(uid, name)

        active = await load_active_recordings()
        slots = active["max_concurrent"] - active["active_count"]
        log(f"Slots: {slots}")
        if slots <= 0: return 0

        for uid in watchlist:
            r = results.get(uid)
            if not r or r.get("status") != LIVE_NORMAL: continue
            if uid in active["active_ids"] or slots <= 0: continue

            # ================================================================
            # CRITICAL FIX: record_once.py ONLY works with /fr/livestream/{user_id}
            # Do NOT use stream_url (might be .m3u8 or profile/slug URL)
            # Always construct the canonical livestream URL for recording
            # ================================================================
            url = BASE_LIVE_URL.format(user_id=uid)

            name = r.get("display_name") or stats["names"].get(uid, "")
            log(f"START_RECORDING {uid} -> {url}")
            ok, reason = await trigger_recording(uid, url, name, r.get("profile_id", ""))
            if ok:
                stats["started_recordings"] += 1
                slots -= 1
                active["active_ids"].add(uid)
            elif reason == "concurrency_limit": break

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
