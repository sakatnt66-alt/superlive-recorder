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
SEARCH_WAIT_MS = int(os.environ.get("SEARCH_WAIT_MS", "6000"))
VERIFY_WAIT_MS = int(os.environ.get("VERIFY_WAIT_MS", "8000"))
PAGE_TIMEOUT_MS = int(os.environ.get("PAGE_TIMEOUT_MS", "30000"))
PLAYWRIGHT_MAX_USERS_PER_CYCLE = int(os.environ.get("PLAYWRIGHT_MAX_USERS_PER_CYCLE", "12"))

SEARCH_URL_TEMPLATE = "https://superlivetv.com/fr/search?q={user_id}"
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

# ============================================================
# WEBRTC HOOK - EXACT SAME AS record_once.py
# This is the PROVEN logic that correctly identifies the target
# video among multiple videos on the page.
# ============================================================
WEBRTC_HOOK = r"""
(() => {
if (window.__superlive_hook_installed) return;
window.__superlive_hook_installed = true;
window.__superliveVideoTracks = [];
window.__superliveAudioTracks = [];
window.__superliveStreams = [];
window.__superliveTrackLinks = new Map();
window.__superliveTrackPeers = new WeakMap();
window.__superliveTrackStreams = new WeakMap();
window.__superliveTrackStreamIds = new WeakMap();
window.__superlivePeerConnections = [];
window.__superliveInboundStats = [];
window.__superliveTargetStreamId = null;

const OriginalRTCPeerConnection = window.RTCPeerConnection;
if (!OriginalRTCPeerConnection) return;

function rememberTrack(track, stream) {
    if (!track) return;
    if (track.kind === "video" && !window.__superliveVideoTracks.includes(track)) {
        window.__superliveVideoTracks.push(track);
    }
    if (track.kind === "audio" && !window.__superliveAudioTracks.includes(track)) {
        window.__superliveAudioTracks.push(track);
    }
    if (stream) {
        if (!window.__superliveStreams.includes(stream)) {
            window.__superliveStreams.push(stream);
        }
        if (!window.__superliveTrackLinks.has(track)) {
            window.__superliveTrackLinks.set(track, stream);
        }
    }
}

class WrappedRTCPeerConnection extends OriginalRTCPeerConnection {
    constructor(...args) {
        super(...args);
        window.__superlivePeerConnections.push(this);
        this.addEventListener("track", (event) => {
            try {
                const track = event.track;
                window.__superliveTrackPeers.set(track, this);
                const streams = event.streams || [];
                if (streams.length) {
                    window.__superliveTrackStreams.set(track, streams.slice());
                    window.__superliveTrackStreamIds.set(
                        track,
                        streams.map(stream => stream && stream.id).filter(Boolean)
                    );
                    for (const stream of streams) {
                        rememberTrack(track, stream);
                    }
                } else {
                    window.__superliveTrackStreams.set(track, []);
                    window.__superliveTrackStreamIds.set(track, []);
                    rememberTrack(track, null);
                }
            } catch (e) {}
        });
    }
}
window.RTCPeerConnection = WrappedRTCPeerConnection;

window.__superliveSelectTargetVideo = async () => {
    const videos = Array.from(document.querySelectorAll("video"));
    const viewportWidth = window.innerWidth || document.documentElement.clientWidth || 0;
    const viewportHeight = window.innerHeight || document.documentElement.clientHeight || 0;
    const targetId = window.__superliveTargetStreamId
        ? String(window.__superliveTargetStreamId) : null;

    const getInboundStats = async () => {
        const results = [];
        for (const pc of window.__superlivePeerConnections) {
            try {
                const stats = await pc.getStats();
                stats.forEach(report => {
                    if (report.type !== "inbound-rtp") return;
                    const kind = report.kind || report.mediaType || null;
                    if (kind !== "video" && kind !== "audio") return;
                    results.push({
                        kind,
                        trackIdentifier: report.trackIdentifier || null,
                        framesReceived: report.framesReceived ?? null,
                        framesDecoded: report.framesDecoded ?? null,
                        framesPerSecond: report.framesPerSecond ?? null,
                        packetsReceived: report.packetsReceived ?? null,
                        bytesReceived: report.bytesReceived ?? null,
                    });
                });
            } catch (e) {}
        }
        return results;
    };
    const inboundStats = await getInboundStats();
    window.__superliveInboundStats = inboundStats;
    const findTrackStats = trackId =>
        inboundStats.find(item => item.kind === "video" && item.trackIdentifier === trackId) || null;

    const domContainsTargetId = (element) => {
        if (!targetId || !element) return false;
        let node = element;
        let depth = 0;
        while (node && depth < 8) {
            try {
                const values = [
                    node.id, node.className,
                    node.getAttribute && node.getAttribute("data-stream-id"),
                    node.getAttribute && node.getAttribute("data-id"),
                    node.getAttribute && node.getAttribute("data-livestream-id"),
                    node.getAttribute && node.getAttribute("data-channel-id"),
                    node.getAttribute && node.getAttribute("data-video-id"),
                    node.getAttribute && node.getAttribute("href"),
                ];
                if (values.some(value => value != null && String(value).includes(targetId))) {
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
                        if (descendantValues.some(value => value != null && String(value).includes(targetId))) {
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
            if (style.display === "none" || style.visibility === "hidden" || style.opacity === "0") continue;
            const rect = video.getBoundingClientRect();
            const left = Math.max(0, rect.left);
            const top = Math.max(0, rect.top);
            const right = Math.min(viewportWidth, rect.right);
            const bottom = Math.min(viewportHeight, rect.bottom);
            const visibleWidth = Math.max(0, right - left);
            const visibleHeight = Math.max(0, bottom - top);
            const visibleArea = visibleWidth * visibleHeight;
            const layoutArea = Math.max(0, rect.width) * Math.max(0, rect.height);
            const stats = findTrackStats(videoTrack.id);
            const streamIds = window.__superliveTrackStreamIds.get(videoTrack) || [];
            const peer = window.__superliveTrackPeers.get(videoTrack) || null;
            const sameTargetDom = domContainsTargetId(video);
            const activePackets = stats && Number(stats.packetsReceived || 0) > 0;
            const activeDecoded = stats && Number(stats.framesDecoded || 0) > 0;
            const fps = stats && Number.isFinite(Number(stats.framesPerSecond))
                ? Number(stats.framesPerSecond) : 0;
            let identityScore = 0;
            if (sameTargetDom) identityScore += 1000000;
            if (activePackets) identityScore += 10000;
            if (activeDecoded) identityScore += 1000;
            const score = identityScore
                + Math.min(visibleArea, 1000000) / 100
                + Math.min(layoutArea, 1000000) / 10000
                + Math.min(fps, 120);
            candidates.push({
                index, video, stream, videoTrack, rect, visibleArea, layoutArea,
                streamIds, peer, sameTargetDom, stats, score,
            });
        } catch (e) {}
    }
    if (!candidates.length) {
        throw new Error("No visible live video target found");
    }
    candidates.sort((a, b) => b.score - a.score);
    const best = candidates[0];
    window.__superliveSelectedVideo = best.video;
    window.__superliveSelectedStream = best.stream;
    window.__superliveSelectedVideoTrack = best.videoTrack;
    return {
        selected: {
            index: best.index,
            width: best.video.videoWidth,
            height: best.video.videoHeight,
            visibleArea: best.visibleArea,
            rect: { x: best.rect.x, y: best.rect.y, width: best.rect.width, height: best.rect.height },
            trackId: best.videoTrack.id,
            streamId: best.stream.id || null,
            streamIds: best.streamIds,
            sameTargetDom: best.sameTargetDom,
            inboundStats: best.stats,
        },
        candidates: candidates.map(item => ({
            index: item.index,
            width: item.video.videoWidth,
            height: item.video.videoHeight,
            visibleArea: item.visibleArea,
            trackId: item.videoTrack.id,
            streamId: item.stream.id || null,
            sameTargetDom: item.sameTargetDom,
            score: item.score,
            inboundStats: item.stats,
        })),
    };
};
})();
"""


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
    si = result.get("search_info") or {}
    if si:
        log(
            f"search_state="
            f"found={si.get('found', False)},"
            f"is_live={si.get('is_live', False)},"
            f"is_premium={si.get('is_premium', False)},"
            f"raw_name={si.get('raw_name', '')[:40]}"
        )
    vi = result.get("verify_info") or {}
    if vi:
        log(
            f"verify_state="
            f"sameTargetDom={vi.get('sameTargetDom', False)},"
            f"activePackets={vi.get('activePackets', False)},"
            f"candidates={vi.get('candidates_count', 0)}"
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
    if "membre superlive" in lowered:
        return None
    return s


# ============================================================
# SEARCH PAGE JS - Extract name + live status from search results
# ============================================================
SEARCH_PAGE_JS = """
(userId) => {
    const result = {
        found: false,
        raw_name: '',
        is_live: false,
        is_premium: false,
        username: '',
        live_link: '',
        page_text_sample: '',
    };

    try {
        const bodyText = document.body ? document.body.innerText : '';
        result.page_text_sample = bodyText.substring(0, 500);

        // Check if user_id appears in the page
        if (!bodyText.includes(userId)) {
            return result;
        }

        // Look for result cards/containers containing the user_id
        const allElements = document.querySelectorAll('*');
        let matchedCard = null;

        for (const el of allElements) {
            try {
                const text = el.innerText || '';
                if (!text.includes(userId)) continue;
                if (text.length < 10 || text.length > 2000) continue;

                // Check if this element contains DIRECT/LIVE indicators
                const upper = text.toUpperCase();
                const hasDirect = upper.includes('DIRECT') || upper.includes('EN DIRECT');

                // Check for @username pattern
                const usernameMatch = text.match(/@([a-zA-Z0-9_]{2,30})/);

                // If we have a DIRECT indicator or username, this is likely our card
                if (hasDirect || usernameMatch) {
                    matchedCard = el;
                    break;
                }

                // Otherwise keep looking for a smaller card
                if (!matchedCard || text.length < (matchedCard.innerText || '').length) {
                    matchedCard = el;
                }
            } catch (e) {}
        }

        if (!matchedCard) {
            return result;
        }

        result.found = true;
        const cardText = matchedCard.innerText || '';

        // Extract @username
        const usernameMatch = cardText.match(/@([a-zA-Z0-9_]{2,30})/);
        if (usernameMatch) {
            result.username = usernameMatch[1];
        }

        // Check for DIRECT/LIVE status
        const upperText = cardText.toUpperCase();
        // Look for "DIRECT" as a standalone word or badge
        if (/\bDIRECT\b/.test(upperText) || /\bEN\s+DIRECT\b/.test(upperText) || /\bLIVE\b/.test(upperText)) {
            // Verify it's not just the word appearing in other context
            // Check if it appears near the user_id
            const idIndex = cardText.indexOf(userId);
            const directIndex = cardText.toUpperCase().indexOf('DIRECT');
            if (idIndex >= 0 && directIndex >= 0 && Math.abs(idIndex - directIndex) < 500) {
                result.is_live = true;
            }
        }

        // Look for links containing the user_id (to get the direct livestream URL)
        const links = matchedCard.querySelectorAll('a[href]');
        for (const link of links) {
            const href = link.getAttribute('href') || '';
            if (href.includes(userId) && href.includes('livestream')) {
                result.live_link = href;
                break;
            }
        }

        // Check for premium indicators
        const lowerText = cardText.toLowerCase();
        if (lowerText.includes('premium') || lowerText.includes('payant') || lowerText.includes('privé')) {
            result.is_premium = true;
        }

        // Extract the display name - the line BEFORE @username or the largest text
        const lines = cardText.split('\n').map(l => l.trim()).filter(l => l.length > 0);
        for (let i = 0; i < lines.length; i++) {
            const line = lines[i];
            if (line === userId) continue;
            if (line.match(/^\d+$/)) continue;
            if (line.startsWith('@')) continue;
            if (line.toUpperCase().includes('DIRECT')) continue;
            if (line.toLowerCase().includes('rechercher')) continue;
            if (line.toLowerCase().includes('résultats')) continue;
            if (line.includes('ProfilePicture')) continue;
            if (line.length < 2 || line.length > 50) continue;
            const wordCount = line.split(/\s+/).length;
            if (wordCount > 5) continue;

            // This is likely the display name
            result.raw_name = line;
            break;
        }

    } catch (e) {
        result.error = String(e);
    }

    return result;
}
"""


# ============================================================
# VERIFY JS - Use WEBRTC_HOOK to verify the correct video
# ============================================================
VERIFY_JS = """
async (userId) => {
    window.__superliveTargetStreamId = userId;

    // Wait for videos to load
    await new Promise(resolve => setTimeout(resolve, 2000));

    const result = {
        ok: false,
        sameTargetDom: false,
        activePackets: false,
        candidates_count: 0,
        video_count: 0,
        trackId: null,
        inboundStats: null,
        error: null,
    };

    try {
        if (!window.__superliveSelectTargetVideo) {
            result.error = "WEBRTC_HOOK not installed";
            return result;
        }

        const selection = await window.__superliveSelectTargetVideo();
        result.candidates_count = (selection.candidates || []).length;
        result.video_count = document.querySelectorAll("video").length;

        if (selection.selected) {
            result.ok = true;
            result.sameTargetDom = selection.selected.sameTargetDom === true;
            result.trackId = selection.selected.trackId;
            result.inboundStats = selection.selected.inboundStats || null;

            if (result.inboundStats) {
                result.activePackets = Number(result.inboundStats.packetsReceived || 0) > 0;
            }
        }
    } catch (e) {
        result.error = String(e);
    }

    return result;
}
"""


async def search_user(context, user_id: str, semaphore: asyncio.Semaphore) -> Dict[str, Any]:
    """
    Step 1: Open the search page with ?q=user_id to extract the correct name
    and check if the user is currently LIVE.
    """
    async with semaphore:
        page = await context.new_page()
        url = SEARCH_URL_TEMPLATE.format(user_id=user_id)

        result = {
            "found": False,
            "raw_name": "",
            "is_live": False,
            "is_premium": False,
            "username": "",
            "live_link": "",
            "error": None,
        }

        try:
            await page.goto(url, timeout=PAGE_TIMEOUT_MS, wait_until="domcontentloaded")
            await page.wait_for_timeout(SEARCH_WAIT_MS)

            try:
                search_result = await page.evaluate(SEARCH_PAGE_JS, user_id)
                if isinstance(search_result, dict):
                    result.update(search_result)
            except Exception as e:
                result["error"] = f"search_evaluate: {str(e)[:100]}"

        except Exception as e:
            result["error"] = f"search_goto: {str(e)[:100]}"

        await page.close()
        return result


async def verify_live_stream(context, user_id: str, semaphore: asyncio.Semaphore) -> Dict[str, Any]:
    """
    Step 2: Open the livestream page and use WEBRTC_HOOK to verify
    that the target user's video is actually playing.
    """
    async with semaphore:
        page = await context.new_page()
        url = BASE_LIVE_URL.format(user_id=user_id)

        result = {
            "ok": False,
            "sameTargetDom": False,
            "activePackets": False,
            "candidates_count": 0,
            "video_count": 0,
            "error": None,
        }

        try:
            # Install WEBRTC_HOOK before navigation
            await page.add_init_script(WEBRTC_HOOK)

            await page.goto(url, timeout=PAGE_TIMEOUT_MS, wait_until="domcontentloaded")

            # Wait for WebRTC connections to establish
            await page.wait_for_timeout(VERIFY_WAIT_MS)

            try:
                verify_result = await page.evaluate(VERIFY_JS, user_id)
                if isinstance(verify_result, dict):
                    result.update(verify_result)
            except Exception as e:
                result["error"] = f"verify_evaluate: {str(e)[:100]}"

        except Exception as e:
            result["error"] = f"verify_goto: {str(e)[:100]}"

        await page.close()
        return result


async def classify_user(context, user_id: str, semaphore: asyncio.Semaphore) -> Dict[str, Any]:
    """
    Two-step classification:
    1. Search page → get correct name + check LIVE status
    2. If LIVE → verify with WEBRTC_HOOK on stream page
    """
    # Step 1: Search
    search_result = await search_user(context, user_id, semaphore)

    search_info = {
        "found": search_result.get("found", False),
        "is_live": search_result.get("is_live", False),
        "is_premium": search_result.get("is_premium", False),
        "raw_name": search_result.get("raw_name", ""),
        "username": search_result.get("username", ""),
        "live_link": search_result.get("live_link", ""),
    }

    # Extract display name
    display_name = None
    raw_name = search_result.get("raw_name", "")
    if raw_name:
        normalized = normalize_display_name(raw_name)
        if normalized:
            display_name = normalized
    if not display_name:
        username = search_result.get("username", "")
        if username:
            normalized = normalize_display_name(username)
            if normalized:
                display_name = normalized

    # Handle search errors
    if search_result.get("error"):
        return {
            "status": UNKNOWN,
            "reason": f"search_error:{search_result['error'][:80]}",
            "stream_url": BASE_LIVE_URL.format(user_id=user_id),
            "display_name": display_name,
            "search_info": search_info,
            "verify_info": {},
        }

    # If user not found in search results
    if not search_result.get("found"):
        return {
            "status": OFFLINE,
            "reason": "search:not_found",
            "stream_url": BASE_LIVE_URL.format(user_id=user_id),
            "display_name": display_name,
            "search_info": search_info,
            "verify_info": {},
        }

    # If user is not LIVE (no DIRECT button)
    if not search_result.get("is_live"):
        return {
            "status": OFFLINE,
            "reason": "search:not_live",
            "stream_url": BASE_LIVE_URL.format(user_id=user_id),
            "display_name": display_name,
            "search_info": search_info,
            "verify_info": {},
        }

    # If user is PREMIUM
    if search_result.get("is_premium"):
        return {
            "status": LIVE_PREMIUM,
            "reason": "search:premium_detected",
            "stream_url": BASE_LIVE_URL.format(user_id=user_id),
            "display_name": display_name,
            "search_info": search_info,
            "verify_info": {},
        }

    # Step 2: Verify on stream page using WEBRTC_HOOK
    verify_result = await verify_live_stream(context, user_id, semaphore)

    verify_info = {
        "sameTargetDom": verify_result.get("sameTargetDom", False),
        "activePackets": verify_result.get("activePackets", False),
        "candidates_count": verify_result.get("candidates_count", 0),
        "video_count": verify_result.get("video_count", 0),
        "trackId": verify_result.get("trackId", ""),
    }

    if verify_result.get("error"):
        # If verify fails but search said LIVE, trust search cautiously
        return {
            "status": UNKNOWN,
            "reason": f"verify_error:{verify_result['error'][:80]}",
            "stream_url": BASE_LIVE_URL.format(user_id=user_id),
            "display_name": display_name,
            "search_info": search_info,
            "verify_info": verify_info,
        }

    # Final decision based on WEBRTC verification
    if verify_result.get("ok") and verify_result.get("sameTargetDom") and verify_result.get("activePackets"):
        return {
            "status": LIVE_NORMAL,
            "reason": "verified:video_active_belongs_to_user",
            "stream_url": BASE_LIVE_URL.format(user_id=user_id),
            "display_name": display_name,
            "search_info": search_info,
            "verify_info": verify_info,
        }
    elif verify_result.get("ok") and verify_result.get("sameTargetDom") and not verify_result.get("activePackets"):
        # Video belongs to user but not receiving packets yet
        return {
            "status": UNKNOWN,
            "reason": "verified:belongs_but_no_packets",
            "stream_url": BASE_LIVE_URL.format(user_id=user_id),
            "display_name": display_name,
            "search_info": search_info,
            "verify_info": verify_info,
        }
    else:
        # Video doesn't belong to user (it's a recommended stream)
        return {
            "status": OFFLINE,
            "reason": "verified:video_not_target_user",
            "stream_url": BASE_LIVE_URL.format(user_id=user_id),
            "display_name": display_name,
            "search_info": search_info,
            "verify_info": verify_info,
        }


async def classify_many_users(user_ids: List[str], stored_names: Dict[str, str] = None) -> Dict[str, Dict[str, Any]]:
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
                "status": UNKNOWN, "reason": "playwright_not_installed",
                "stream_url": BASE_LIVE_URL.format(user_id=user_id),
                "display_name": stored_names.get(user_id),
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
                        "status": UNKNOWN, "reason": str(result)[:120],
                        "stream_url": BASE_LIVE_URL.format(user_id=user_id),
                        "display_name": stored_names.get(user_id),
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
                    "status": UNKNOWN, "reason": "playwright_global_error",
                    "stream_url": BASE_LIVE_URL.format(user_id=user_id),
                    "display_name": stored_names.get(user_id),
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

        log(f"Running search+verify detection for {len(selected)} users")
        results = await classify_many_users(selected, stored_names)

        for user_id, result in results.items():
            log_detection(user_id, result, "search+verify")

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
