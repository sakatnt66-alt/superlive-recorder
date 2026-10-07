import asyncio
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional, Set

# ============================================================
# CONFIG
# ============================================================
WORKER_URL = os.environ.get("WORKER_URL", "").rstrip("/")
AUTO_API_TOKEN = os.environ.get("AUTO_API_TOKEN", "")

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
SEND_REPORT = os.environ.get("SEND_REPORT", "1") not in ("0", "false", "False")
UPDATE_WATCHLIST_NAMES = os.environ.get("UPDATE_WATCHLIST_NAMES", "1") not in ("0", "false", "False")
NAME_MIN_SCORE = int(os.environ.get("NAME_MIN_SCORE", "50"))

MAX_CONCURRENT = int(os.environ.get("MAX_CONCURRENT", "5"))
HTTP_TIMEOUT = int(os.environ.get("HTTP_TIMEOUT", "15"))
API_TIMEOUT = int(os.environ.get("API_TIMEOUT", "20"))

USE_PLAYWRIGHT = os.environ.get("USE_PLAYWRIGHT", "1") not in ("0", "false", "False")
HTTP_CONCURRENCY = int(os.environ.get("HTTP_CONCURRENCY", "8"))
PLAYWRIGHT_CONCURRENCY = int(os.environ.get("PLAYWRIGHT_CONCURRENCY", "2"))
PLAYWRIGHT_WAIT_MS = int(os.environ.get("PLAYWRIGHT_WAIT_MS", "5000"))
PAGE_TIMEOUT_MS = int(os.environ.get("PAGE_TIMEOUT_MS", "25000"))
PLAYWRIGHT_MAX_USERS_PER_CYCLE = int(os.environ.get("PLAYWRIGHT_MAX_USERS_PER_CYCLE", "12"))

BASE_LIVE_URL = "https://superlivetv.com/fr/livestream/{user_id}"
SEARCH_URL = "https://superlivetv.com/fr/search"

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
    reason = result.get("reason") or ",".join(result.get("evidence", [])[:3]) or "no_signal"

    if status == LIVE_PREMIUM:
        action = "SKIP_PREMIUM"
        reason = reason or "premium_stream"
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
    log(f"reason={reason}")
    if result.get("display_name"):
        log(f"display_name={result['display_name']}")

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
        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": chunk,
            "parse_mode": "HTML",
        }

        try:
            data = json.dumps(payload).encode("utf-8")
            req = urllib.request.Request(
                url,
                data=data,
                method="POST",
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

# ============================================================
# DISPLAY NAME EXTRACTION
# ============================================================
NAME_KEY_SCORES = {
    "username": 100,
    "user_name": 100,
    "nickname": 95,
    "displayname": 90,
    "display_name": 90,
    "broadcaster_name": 85,
    "channel_name": 85,
    "streamer_name": 85,
    "author_name": 80,
    "name": 60,
    "full_name": 60,
    "fullname": 60,
    "user": 45,
    "channel": 40,
    "broadcaster": 40,
    "author": 35,
    "title": 20,
    "slug": 15,
}


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

    lowered = s.lower()
    blocked = {
        "live",
        "offline",
        "premium",
        "superlive",
        "super live",
        "recording",
        "unknown",
        "none",
        "null",
        "video",
        "stream",
    }

    if lowered in blocked:
        return None

    return s


def dict_contains_user_id(data, user_id):
    if not isinstance(data, dict):
        return False

    for key, value in data.items():
        if isinstance(value, (str, int, float)):
            if str(value) == str(user_id):
                k = str(key).lower()
                if any(token in k for token in (
                    "id",
                    "user",
                    "stream",
                    "channel",
                    "broadcaster",
                    "author",
                    "owner",
                )):
                    return True

    return False


def scan_json_for_names(obj, user_id, candidates, depth=0, associated=False):
    if depth > 8:
        return

    if isinstance(obj, dict):
        current_associated = associated or dict_contains_user_id(obj, user_id)

        for key, value in obj.items():
            key_norm = str(key).lower().replace("-", "_").replace(" ", "_")

            if isinstance(value, (dict, list)):
                scan_json_for_names(
                    value,
                    user_id,
                    candidates,
                    depth + 1,
                    current_associated,
                )
            else:
                base_score = NAME_KEY_SCORES.get(key_norm)
                if not base_score:
                    continue

                name = normalize_display_name(value)
                if not name:
                    continue

                score = base_score + (20 if current_associated else 0)
                candidates.append({
                    "value": name,
                    "source": f"json:{key_norm}",
                    "score": min(130, score),
                })

    elif isinstance(obj, list):
        for item in obj[:200]:
            scan_json_for_names(
                item,
                user_id,
                candidates,
                depth + 1,
                associated,
            )


def extract_json_name_candidates(user_id, json_bodies):
    candidates = []

    for body in json_bodies[:20]:
        try:
            obj = json.loads(body)
        except Exception:
            continue

        scan_json_for_names(obj, user_id, candidates)

    return candidates


def choose_display_name(candidates):
    dedup = {}

    for candidate in candidates or []:
        name = normalize_display_name(candidate.get("value"))
        if not name:
            continue

        score = int(candidate.get("score", 0) or 0)
        source = str(candidate.get("source", ""))

        key = name.lower()

        if key not in dedup or score > dedup[key]["score"]:
            dedup[key] = {
                "value": name,
                "score": score,
                "source": source,
            }

    if not dedup:
        return None, 0, None

    best = max(dedup.values(), key=lambda x: x["score"])
    return best["value"], best["score"], best["source"]


def extract_display_name_from_html(raw, user_id):
    candidates = []

    if not raw:
        return candidates

    title_match = re.search(r"<title[^>]*>(.*?)</title>", raw, re.IGNORECASE | re.DOTALL)
    if title_match:
        title = re.sub(r"<[^>]+>", " ", title_match.group(1))
        candidates.append({
            "value": title,
            "source": "html_title",
            "score": 35,
        })

    og_match = re.search(
        r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)',
        raw,
        re.IGNORECASE,
    )
    if og_match:
        candidates.append({
            "value": og_match.group(1),
            "source": "og_title",
            "score": 40,
        })

    twitter_match = re.search(
        r'<meta[^>]+name=["\']twitter:title["\'][^>]+content=["\']([^"\']+)',
        raw,
        re.IGNORECASE,
    )
    if twitter_match:
        candidates.append({
            "value": twitter_match.group(1),
            "source": "twitter_title",
            "score": 35,
        })

    return candidates


async def update_watchlist_name(user_id: str, display_name: str):
    try:
        payload = {"display_name": display_name}
        status, data = await api_request(
            f"/api/watchlist/name/{user_id}",
            "POST",
            payload,
        )

        if status == 200:
            log(f"Saved display name for {user_id}: {display_name}")
        elif status == 404:
            # User not in watchlist or endpoint missing.
            pass
        else:
            log(f"Name save warning for {user_id}: HTTP {status}")
    except Exception as e:
        log(f"Name save error for {user_id}: {e}")

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
        username = str(item.get("username", "")).strip()
        if username:
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


async def trigger_auto_recording(user_id: str, stream_url: str, stream_name: str = ""):
    payload = {
        "stream_url": stream_url,
        "source": "auto",
        "stream_name": stream_name,
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

# ============================================================
# SIGNAL PATTERNS
# ============================================================
def compile_patterns(patterns):
    return [(re.compile(pattern, re.IGNORECASE), weight, label) for pattern, weight, label in patterns]


STRONG_LIVE_PATTERNS = [
    (r'"(?:isLive|is_live|live|streaming|is_streaming|isLiveStream)"\s*:\s*true', 100, "json_live_true"),
    (r'"(?:status|state|streamStatus|liveStatus|stream_status|live_status)"\s*:\s*"?(?:live|streaming|online)"?', 100, "json_status_live"),
    (r'data-(?:live|stream|status|broadcast)\s*=\s*["\']?(?:live|true|online)', 80, "html_data_live"),
    (r'"(?:live_status|stream_status)"\s*:\s*1\b', 70, "json_live_numeric"),
]

MEDIUM_LIVE_PATTERNS = [
    (r'"(?:hasLive|has_live|liveEnabled|canRecord)"\s*:\s*true', 40, "json_live_medium"),
    (r'"liveVideoCandidate"\s*:\s*true', 40, "player_video_candidate"),
    (r'class="[^"]*\blive\b', 15, "html_class_live"),
    (r"\ben direct\b", 8, "text_fr_live"),
]

STRONG_PREMIUM_PATTERNS = [
    (r'"(?:isPremium|is_premium|premium|isPaywall|paywall|locked|paid|is_paid|requiresPayment|isPrivate|private)"\s*:\s*true', 100, "json_premium_true"),
    (r'"(?:type|streamType|stream_type|access|accessType|mode)"\s*:\s*"?(?:premium|paid|private|paywall|subscription)"?', 100, "json_type_premium"),
]

WEAK_PREMIUM_PATTERNS = [
    (r"\bpremium\b", 4, "text_premium"),
    (r"\bpayant\b", 4, "text_payant"),
    (r"\bprivate\b", 2, "text_private"),
]

STRONG_OFFLINE_PATTERNS = [
    (r'"(?:isLive|is_live|live|streaming|is_streaming)"\s*:\s*false', 100, "json_live_false"),
    (r'"(?:status|state|streamStatus|liveStatus|stream_status|live_status)"\s*:\s*"?(?:offline|ended|finished|completed|inactive|stopped)"?', 100, "json_status_offline"),
    (r"le direct s'est terminé", 100, "text_fr_ended"),
    (r'"offline"\s*:\s*true', 80, "json_offline_true"),
]

MEDIUM_OFFLINE_PATTERNS = [
    (r"\boffline\b", 8, "text_offline"),
    (r"\bended\b", 5, "text_ended"),
]

LIVE_PATTERNS = compile_patterns(STRONG_LIVE_PATTERNS + MEDIUM_LIVE_PATTERNS)
PREMIUM_PATTERNS = compile_patterns(STRONG_PREMIUM_PATTERNS + WEAK_PREMIUM_PATTERNS)
OFFLINE_PATTERNS = compile_patterns(STRONG_OFFLINE_PATTERNS + MEDIUM_OFFLINE_PATTERNS)

# ============================================================
# CLASSIFICATION ENGINE
# ============================================================
def score_text(text: str):
    live_score = 0
    premium_score = 0
    offline_score = 0
    evidence = []

    for rx, weight, label in LIVE_PATTERNS:
        if rx.search(text):
            live_score += weight
            evidence.append(label)

    for rx, weight, label in PREMIUM_PATTERNS:
        if rx.search(text):
            premium_score += weight
            evidence.append(label)

    for rx, weight, label in OFFLINE_PATTERNS:
        if rx.search(text):
            offline_score += weight
            evidence.append(label)

    return live_score, premium_score, offline_score, evidence


def decide_scores(live_score: int, premium_score: int, offline_score: int) -> str:
    if premium_score >= 100 and live_score >= 20:
        return LIVE_PREMIUM

    if offline_score >= 100 and live_score < 50:
        return OFFLINE

    if live_score >= 100 and premium_score < 100 and offline_score < 100:
        return LIVE_NORMAL

    return UNKNOWN


def extract_segments(text: str, user_id: str, window: int = 3500, max_segments: int = 10) -> List[str]:
    if not text or not user_id:
        return []

    segments = []
    start_index = 0
    count = 0

    while count < max_segments:
        idx = text.find(user_id, start_index)
        if idx == -1:
            break

        start = max(0, idx - window)
        end = min(len(text), idx + len(user_id) + window)
        segments.append(text[start:end])

        start_index = idx + len(user_id)
        count += 1

    return segments


def unique_evidence(evidence: List[str], limit: int = 10) -> List[str]:
    seen = set()
    out = []
    for item in evidence:
        if item not in seen:
            seen.add(item)
            out.append(item)
        if len(out) >= limit:
            break
    return out


def analyze_texts(texts: List[str], user_id: str) -> Dict[str, Any]:
    max_live = 0
    max_premium = 0
    max_offline = 0
    all_evidence = []

    for text in texts:
        if not text:
            continue

        if user_id not in text:
            continue

        segments = extract_segments(text, user_id)
        if not segments and len(text) < 20000:
            segments = [text]

        for segment in segments:
            live_score, premium_score, offline_score, evidence = score_text(segment)
            max_live = max(max_live, live_score)
            max_premium = max(max_premium, premium_score)
            max_offline = max(max_offline, offline_score)
            all_evidence.extend(evidence)

    status = decide_scores(max_live, max_premium, max_offline)

    return {
        "status": status,
        "scores": {
            "live": max_live,
            "premium": max_premium,
            "offline": max_offline,
        },
        "evidence": unique_evidence(all_evidence),
    }


def is_captcha_page(text: str) -> bool:
    lowered = text.lower()
    return ("recaptcha" in lowered or "cf-challenge" in lowered) and len(text) < 30000

# ============================================================
# HTTP DETECTION
# ============================================================
def http_classify_user(user_id: str) -> Dict[str, Any]:
    url = BASE_LIVE_URL.format(user_id=user_id)
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,application/json;q=0.8,*/*;q=0.7",
        "Accept-Language": "fr-FR,fr;q=0.9,en-US;q=0.8,en;q=0.7",
        "Referer": SEARCH_URL,
    }

    req = urllib.request.Request(url, headers=headers, method="GET")

    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as response:
            status_code = response.status
            raw = response.read(2_000_000).decode("utf-8", errors="ignore")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return {
                "status": OFFLINE,
                "reason": "http_404",
                "stream_url": url,
                "display_name": None,
                "name_score": 0,
                "name_source": None,
            }
        return {
            "status": UNKNOWN,
            "reason": f"http_{e.code}",
            "stream_url": url,
            "display_name": None,
            "name_score": 0,
            "name_source": None,
        }
    except Exception as e:
        return {
            "status": UNKNOWN,
            "reason": str(e)[:120],
            "stream_url": url,
            "display_name": None,
            "name_score": 0,
            "name_source": None,
        }

    if status_code != 200:
        return {
            "status": UNKNOWN,
            "reason": f"http_status_{status_code}",
            "stream_url": url,
            "display_name": None,
            "name_score": 0,
            "name_source": None,
        }

    if is_captcha_page(raw):
        return {
            "status": UNKNOWN,
            "reason": "captcha_or_challenge",
            "stream_url": url,
            "display_name": None,
            "name_score": 0,
            "name_source": None,
        }

    result = analyze_texts([raw], user_id)
    result["stream_url"] = url

    name_candidates = extract_display_name_from_html(raw, user_id)
    display_name, name_score, name_source = choose_display_name(name_candidates)

    if display_name and name_score >= NAME_MIN_SCORE:
        result["display_name"] = display_name
    else:
        result["display_name"] = None

    result["name_score"] = name_score
    result["name_source"] = name_source

    return result

# ============================================================
# PLAYWRIGHT DETECTION
# ============================================================
PAGE_NAME_EXTRACT_JS = r"""
(userId) => {
  const candidates = [];

  const clean = (value) => {
    if (!value) return '';
    return String(value).trim().replace(/\s+/g, ' ');
  };

  const add = (value, source, score) => {
    const v = clean(value);
    if (!v) return;
    if (v.length < 2 || v.length > 60) return;
    if (/^\d+$/.test(v)) return;
    candidates.push({ value: v, source, score });
  };

  try {
    add(document.title, 'title', 30);

    const og = document.querySelector('meta[property="og:title"]');
    if (og && og.content) add(og.content, 'og_title', 35);

    const twitter = document.querySelector('meta[name="twitter:title"]');
    if (twitter && twitter.content) add(twitter.content, 'twitter_title', 30);

    const h1 = document.querySelector('h1');
    if (h1 && h1.innerText) add(h1.innerText, 'h1', 45);

    const h2s = Array.from(document.querySelectorAll('h2')).slice(0, 5);
    for (const h2 of h2s) add(h2.innerText, 'h2', 20);

    const links = Array.from(document.querySelectorAll('a[href*="' + userId + '"]')).slice(0, 20);
    for (const a of links) {
      add(a.innerText, 'link_text', 80);
      add(a.getAttribute('aria-label'), 'link_aria', 70);
      add(a.getAttribute('title'), 'link_title', 60);
    }

    const selectors = [
      '[data-id="' + userId + '"]',
      '[data-user-id="' + userId + '"]',
      '[data-stream-id="' + userId + '"]',
      '[data-livestream-id="' + userId + '"]',
      '[data-channel-id="' + userId + '"]'
    ];

    for (const selector of selectors) {
      let elements = [];
      try {
        elements = Array.from(document.querySelectorAll(selector)).slice(0, 10);
      } catch (e) {}

      for (const el of elements) {
        const nameEl = el.querySelector('[class*="name" i], [class*="nickname" i], [class*="username" i], h1, h2, h3');
        if (nameEl && nameEl.innerText) add(nameEl.innerText, 'data_attr_name', 90);

        add(el.getAttribute('aria-label'), 'data_attr_aria', 70);
        add(el.getAttribute('title'), 'data_attr_title', 60);

        const firstLine = (el.innerText || '').split('\n')[0];
        add(firstLine, 'data_attr_text', 35);
      }
    }

    const nameEls = Array.from(document.querySelectorAll(
      '[class*="profile-name" i], [class*="user-name" i], [class*="username" i], [class*="nickname" i], [class*="streamer-name" i], [class*="broadcaster-name" i]'
    )).slice(0, 20);

    for (const el of nameEls) {
      add(el.innerText, 'name_class', 55);
    }
  } catch (e) {}

  return candidates;
}
"""


async def playwright_classify_user(context, user_id: str, semaphore: asyncio.Semaphore) -> Dict[str, Any]:
    async with semaphore:
        page = await context.new_page()
        texts: List[str] = []
        json_bodies: List[str] = []
        json_count = 0
        url = BASE_LIVE_URL.format(user_id=user_id)

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
                    texts.append(f"SOURCE_JSON {user_id} " + body[:200000])
            except Exception:
                pass

        page.on("response", on_response)

        try:
            await page.goto(url, timeout=PAGE_TIMEOUT_MS, wait_until="domcontentloaded")
            await page.wait_for_timeout(PLAYWRIGHT_WAIT_MS)

            try:
                html = await page.content()
            except Exception:
                html = ""

            try:
                body_text = await page.evaluate("() => document.body ? document.body.innerText : ''")
            except Exception:
                body_text = ""

            try:
                video_info = await page.evaluate(
                    """
                    () => {
                        const videos = Array.from(document.querySelectorAll('video'));
                        let srcObjectCount = 0;
                        let visibleCount = 0;
                        let liveVideoCandidate = false;

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

                        return {
                            videoCount: videos.length,
                            srcObjectCount,
                            visibleCount,
                            liveVideoCandidate
                        };
                    }
                    """
                )
            except Exception:
                video_info = {
                    "videoCount": 0,
                    "srcObjectCount": 0,
                    "visibleCount": 0,
                    "liveVideoCandidate": False,
                }

            try:
                dom_name_candidates = await page.evaluate(PAGE_NAME_EXTRACT_JS, user_id)
                if not isinstance(dom_name_candidates, list):
                    dom_name_candidates = []
            except Exception:
                dom_name_candidates = []

            texts.append(f"SOURCE_HTML {user_id} " + html[:300000])
            texts.append(f"SOURCE_TEXT {user_id} " + body_text[:100000])
            texts.append(f"SOURCE_VIDEO {user_id} " + json.dumps(video_info))

        except Exception as e:
            await page.close()
            return {
                "status": UNKNOWN,
                "reason": str(e)[:120],
                "stream_url": url,
                "display_name": None,
                "name_score": 0,
                "name_source": None,
            }

        await page.close()

        combined_captcha_check = " ".join(texts[:3])
        if is_captcha_page(combined_captcha_check):
            return {
                "status": UNKNOWN,
                "reason": "captcha_or_challenge",
                "stream_url": url,
                "display_name": None,
                "name_score": 0,
                "name_source": None,
            }

        result = analyze_texts(texts, user_id)
        result["stream_url"] = url

        json_name_candidates = extract_json_name_candidates(user_id, json_bodies)
        all_name_candidates = json_name_candidates + dom_name_candidates

        display_name, name_score, name_source = choose_display_name(all_name_candidates)

        if display_name and name_score >= NAME_MIN_SCORE:
            result["display_name"] = display_name
        else:
            result["display_name"] = None

        result["name_score"] = name_score
        result["name_source"] = name_source

        return result


async def playwright_classify_many(user_ids: List[str]) -> Dict[str, Dict[str, Any]]:
    results: Dict[str, Dict[str, Any]] = {}

    if not user_ids:
        return results

    try:
        from playwright.async_api import async_playwright
    except ImportError:
        log("Playwright is not installed. Skipping browser detection.")
        for user_id in user_ids:
            results[user_id] = {
                "status": UNKNOWN,
                "reason": "playwright_not_installed",
                "stream_url": BASE_LIVE_URL.format(user_id=user_id),
                "display_name": None,
                "name_score": 0,
                "name_source": None,
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
                    "--autoplay-policy=no-user-gesture-required",
                ],
            )

            context = await browser.new_context(
                user_agent=USER_AGENT,
                viewport={"width": 1280, "height": 800},
                locale="fr-FR",
            )

            semaphore = asyncio.Semaphore(PLAYWRIGHT_CONCURRENCY)
            tasks = [playwright_classify_user(context, user_id, semaphore) for user_id in user_ids]
            gathered = await asyncio.gather(*tasks, return_exceptions=True)

            for user_id, result in zip(user_ids, gathered):
                if isinstance(result, Exception):
                    results[user_id] = {
                        "status": UNKNOWN,
                        "reason": str(result)[:120],
                        "stream_url": BASE_LIVE_URL.format(user_id=user_id),
                        "display_name": None,
                        "name_score": 0,
                        "name_source": None,
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
                    "status": UNKNOWN,
                    "reason": "playwright_global_error",
                    "stream_url": BASE_LIVE_URL.format(user_id=user_id),
                    "display_name": None,
                    "name_score": 0,
                    "name_source": None,
                }

    return results

# ============================================================
# MAIN MONITOR
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

    # --------------------------------------------------------
    # Load watchlist
    # --------------------------------------------------------
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

    # --------------------------------------------------------
    # Load active recordings
    # --------------------------------------------------------
    try:
        active = await load_active_recordings()
    except Exception as e:
        log(f"FATAL: {e}")
        return 1

    log(f"Active recordings: {active['active_count']}/{active['max_concurrent']}")

    if active["active_count"] >= active["max_concurrent"]:
        log("Concurrency limit already reached. No recording slots are available.")

        elapsed = time.monotonic() - monitor_start_time
        if SEND_REPORT:
            report = build_report(elapsed, stats)
            send_telegram_report(report)

        log("Monitor completed")
        return 0

    # --------------------------------------------------------
    # Exclude users already recording
    # --------------------------------------------------------
    users_to_check: List[str] = []
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
            report = build_report(elapsed, stats)
            send_telegram_report(report)

        log("Monitor completed")
        return 0

    # --------------------------------------------------------
    # HTTP detection
    # --------------------------------------------------------
    results: Dict[str, Dict[str, Any]] = {}
    http_semaphore = asyncio.Semaphore(HTTP_CONCURRENCY)

    async def check_user_http(user_id: str):
        async with http_semaphore:
            log(f"Checking: {user_id}")
            stats["checked_now"] += 1
            try:
                result = await asyncio.to_thread(http_classify_user, user_id)
            except Exception as e:
                result = {
                    "status": UNKNOWN,
                    "reason": str(e)[:120],
                    "stream_url": BASE_LIVE_URL.format(user_id=user_id),
                    "display_name": None,
                    "name_score": 0,
                    "name_source": None,
                }
            log_detection(user_id, result, "http")
            return user_id, result

    http_tasks = [check_user_http(user_id) for user_id in users_to_check]
    http_results = await asyncio.gather(*http_tasks, return_exceptions=False)

    for user_id, result in http_results:
        results[user_id] = result

    # --------------------------------------------------------
    # Playwright detection for ambiguous users
    # --------------------------------------------------------
    ambiguous_users = [
        user_id
        for user_id in users_to_check
        if results.get(user_id, {}).get("status", UNKNOWN) == UNKNOWN
    ]

    if USE_PLAYWRIGHT and ambiguous_users:
        selected = ambiguous_users[:PLAYWRIGHT_MAX_USERS_PER_CYCLE]

        if len(ambiguous_users) > len(selected):
            log(
                f"Playwright capacity limit: checking {len(selected)} ambiguous users now, "
                f"{len(ambiguous_users) - len(selected)} remaining for next cycle"
            )

        log(f"Running browser detection for {len(selected)} ambiguous users")
        playwright_results = await playwright_classify_many(selected)

        for user_id, result in playwright_results.items():
            results[user_id] = result
            log_detection(user_id, result, "playwright")

    # --------------------------------------------------------
    # Update classification stats and names
    # --------------------------------------------------------
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

    # --------------------------------------------------------
    # Refresh active recordings before triggering
    # --------------------------------------------------------
    try:
        active = await load_active_recordings()
    except Exception as e:
        log(f"Failed to refresh active recordings before trigger: {e}")
        log("Skipping all trigger actions to stay fail-safe.")

        elapsed = time.monotonic() - monitor_start_time
        if SEND_REPORT:
            report = build_report(elapsed, stats)
            send_telegram_report(report)

        log("Monitor completed")
        return 0

    slots_available = active["max_concurrent"] - active["active_count"]
    log(f"Slots: {active['active_count']}/{active['max_concurrent']}")

    if slots_available <= 0:
        log("No recording slots available after refresh.")

        elapsed = time.monotonic() - monitor_start_time
        if SEND_REPORT:
            report = build_report(elapsed, stats)
            send_telegram_report(report)

        log("Monitor completed")
        return 0

    # --------------------------------------------------------
    # Trigger recordings
    # --------------------------------------------------------
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

    # --------------------------------------------------------
    # Send final report
    # --------------------------------------------------------
    elapsed = time.monotonic() - monitor_start_time

    if SEND_REPORT:
        report = build_report(elapsed, stats)
        send_telegram_report(report)

    log("Monitor completed")
    return 0


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
