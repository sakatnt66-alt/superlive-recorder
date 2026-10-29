#!/usr/bin/env python3
"""
record_launcher.py — SuperLive Recorder Launcher
Handles pre-flight validation, recording with timeout, post-processing recovery
"""

import asyncio
import json
import os
import sys
import time
import urllib.request
import urllib.error
import re
import glob
import shutil
import threading
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from superlive_discovery import SuperLiveDiscovery

# Configuration
GITHUB_REPOSITORY = os.environ.get("GITHUB_REPOSITORY", "")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
WORKER_API_URL = os.environ.get("WORKER_API_URL", "").strip()
AUTO_API_TOKEN = os.environ.get("AUTO_API_TOKEN", "").strip()
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# User-Agent to avoid Cloudflare WAF blocking GitHub Actions
USER_AGENT = "SuperLive-AutoMonitor/1.0 (GitHub Actions; +https://github.com/sakatnt66-alt/superlive-recorder)"

# Debug: Print configuration status
print(f"[DEBUG] WORKER_API_URL: {'SET' if WORKER_API_URL else 'MISSING'} (length={len(WORKER_API_URL)})")
print(f"[DEBUG] AUTO_API_TOKEN: {'SET' if AUTO_API_TOKEN else 'MISSING'} (length={len(AUTO_API_TOKEN)})")

# Remove trailing slash from WORKER_API_URL if present
if WORKER_API_URL and WORKER_API_URL.endswith('/'):
    WORKER_API_URL = WORKER_API_URL[:-1]

WATCHLIST_FILE = Path("data/watchlist.json")  # Fallback only
MAX_CONCURRENT = 4

RECORDINGS_DIR = Path(os.environ.get("RECORDINGS_DIR", "recordings"))
TEMP_DIR = Path(os.environ.get("TEMP_DIR", "recordings/_temp"))
PRESERVED_DIR = RECORDINGS_DIR / "_preserved"

RECORDING_MAX_SECONDS = int(os.environ.get("RECORDING_MAX_SECONDS", "3600"))
STOP_CHECK_INTERVAL = int(os.environ.get("STOP_CHECK_INTERVAL", "30"))

def is_valid_stream_id(stream_id: str, user_id: str) -> bool:
    """Check if stream_id is valid (different from user_id, 9+ digits, numeric)."""
    stream_id_str = str(stream_id)
    user_id_str = str(user_id)
    
    if stream_id_str == user_id_str:
        return False
    if len(stream_id_str) < 9:
        return False
    if not stream_id_str.isdigit():
        return False
    return True

def is_valid_profile_id(profile_id: str) -> bool:
    """Check if profile_id is valid (numeric, not a hash)."""
    if not profile_id:
        return False
    profile_id_str = str(profile_id)
    if not profile_id_str.isdigit():
        return False
    if len(profile_id_str) < 5 or len(profile_id_str) > 15:
        return False
    return True

def read_error_body(error):
    """Safely read HTTPError response body for debugging."""
    try:
        body = error.read().decode('utf-8', errors='replace')
        return body[:500] if body else '(empty)'
    except Exception:
        return '(unreadable)'

def send_telegram_message(text: str, parse_mode: str = "HTML"):
    """Send message to Telegram"""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = json.dumps({
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": parse_mode
    }).encode('utf-8')
    
    req = urllib.request.Request(url, data=payload, headers={
        'Content-Type': 'application/json',
        'User-Agent': USER_AGENT,
        'Accept': 'application/json'
    })
    
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            if response.status != 200:
                print(f"[TELEGRAM] Failed to send message: {response.status}")
    except urllib.error.HTTPError as e:
        print(f"[TELEGRAM] HTTP Error {e.code}: {e.reason}")
    except Exception as e:
        print(f"[TELEGRAM] Error sending message: {e}")

def update_worker_state(stream_id: str, state: dict):
    if not WORKER_API_URL or not AUTO_API_TOKEN:
        return
    url = f"{WORKER_API_URL}/api/update-state/{stream_id}"
    payload = json.dumps(state).encode('utf-8')
    req = urllib.request.Request(url, data=payload, headers={
        'X-Auto-Token': AUTO_API_TOKEN,
        'Content-Type': 'application/json'
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            pass
    except Exception as e:
        print(f"[WORKER] Error updating state: {e}")

def cleanup_failed_recording(stream_id: str, reason: str):
    """Immediately remove failed recording from active list in Worker KV."""
    if not WORKER_API_URL or not AUTO_API_TOKEN:
        return
    url = f"{WORKER_API_URL}/api/cleanup-failed/{stream_id}"
    payload = json.dumps({"reason": reason}).encode('utf-8')
    req = urllib.request.Request(url, data=payload, method='POST', headers={
        'X-Auto-Token': AUTO_API_TOKEN,
        'Content-Type': 'application/json'
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            print(f"[WORKER] ✓ Cleaned up failed recording {stream_id}: {reason}")
    except Exception as e:
        print(f"[WORKER] Error cleaning up {stream_id}: {e}")

def is_valid_stream_id(stream_id: str, user_id: str) -> bool:
    stream_id_str = str(stream_id)
    user_id_str = str(user_id)
    if stream_id_str == user_id_str:
        return False
    if len(stream_id_str) < 9:
        return False
    if not stream_id_str.isdigit():
        return False
    return True

def sanitize_filename(name: str) -> str:
    """Sanitize a string to be used as a filename."""
    if not name:
        return "unknown"
    # Remove or replace invalid characters
    safe = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', name)
    # Remove leading/trailing dots and spaces
    safe = safe.strip('. ')
    # Limit length
    return safe[:100] if safe else "unknown"

# ═══════════════════════════════════════════════════════════
# FIX: Kill orphan ffmpeg/yt-dlp processes to ensure files are closed
# ═══════════════════════════════════════════════════════════
def kill_orphan_processes():
    print("[LAUNCHER] Killing orphan ffmpeg/yt-dlp processes to ensure files are closed...")
    try:
        subprocess.run("pkill -f 'ffmpeg'", shell=True, stderr=subprocess.DEVNULL, stdout=subprocess.DEVNULL)
        subprocess.run("pkill -f 'yt-dlp'", shell=True, stderr=subprocess.DEVNULL, stdout=subprocess.DEVNULL)
        subprocess.run("pkill -f 'record_once'", shell=True, stderr=subprocess.DEVNULL, stdout=subprocess.DEVNULL)
    except Exception as e:
        print(f"[LAUNCHER] Kill error: {e}")

# ═══════════════════════════════════════════════════════════
# FILE PRESERVER — watches MKV, H264, WEBM in background thread
# ═══════════════════════════════════════════════════════════
class FilePreserverThread:
    def __init__(self, watch_dirs, preserve_dir, poll_interval=0.3):
        self.watch_dirs = [Path(d) for d in watch_dirs]
        self.preserve_dir = Path(preserve_dir)
        self.poll_interval = poll_interval
        self._stop_event = threading.Event()
        self._preserved = set()
        self._thread = None

    def start(self):
        self.preserve_dir.mkdir(parents=True, exist_ok=True)
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        print(f"[PRESERVE] Thread started (interval={self.poll_interval}s)")

    def stop(self):
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)
        print(f"[PRESERVE] Thread stopped — {len(self._preserved)} file(s) preserved")

    def _run(self):
        while not self._stop_event.is_set():
            try:
                self._scan()
            except Exception as exc:
                print(f"[PRESERVE] scan error: {exc}")
            self._stop_event.wait(self.poll_interval)

    def _scan(self):
        for watch_dir in self.watch_dirs:
            if not watch_dir.exists():
                continue
            for pattern in ["*.mkv", "*.h264", "*.webm"]:
                for f in watch_dir.glob(pattern):
                    src = str(f)
                    if src in self._preserved:
                        continue
                    try:
                        size = f.stat().st_size
                        if size == 0:
                            continue
                        dest = self.preserve_dir / f.name
                        if not dest.exists():
                            shutil.copy2(src, str(dest))
                            self._preserved.add(src)
                            print(f"[PRESERVE] ✓ {f.name} ({size / 1024 / 1024:.1f} MB)")
                    except FileNotFoundError:
                        pass
                    except Exception as e:
                        print(f"[PRESERVE] ✗ copy failed for {f.name}: {e}")

# ═══════════════════════════════════════════════════════════
# RECOVERY: Mux raw video+audio into MKV after timeout/stop
# ═══════════════════════════════════════════════════════════
def recover_and_mux_recording() -> list:
    """
    After record_once.py is cancelled (timeout or manual stop),
    the muxing step inside record_once.py never runs.
    This function finds raw video and audio files and muxes them
    into a proper MKV using FFmpeg stream copy (no re-encoding).
    """
    print("\n" + "=" * 70)
    print("[RECOVERY] Searching for raw recording files to mux...")
    print("=" * 70)

    created_mkvs = []
    search_dirs = [TEMP_DIR, RECORDINGS_DIR, PRESERVED_DIR]

    video_files = []
    audio_files = []

    for d in search_dirs:
        if not d.exists():
            continue
        for f in d.glob("*.h264"):
            if f.stat().st_size > 0:
                video_files.append(f)
                print(f"[RECOVERY] Found video: {f.name} ({f.stat().st_size / 1024 / 1024:.1f} MB)")
        for f in d.glob("*_video.webm"):
            if f.stat().st_size > 0:
                video_files.append(f)
                print(f"[RECOVERY] Found video: {f.name} ({f.stat().st_size / 1024 / 1024:.1f} MB)")
        for f in d.glob("*_audio.webm"):
            if f.stat().st_size > 0:
                audio_files.append(f)
                print(f"[RECOVERY] Found audio: {f.name} ({f.stat().st_size / 1024 / 1024:.1f} MB)")
        for f in d.glob("*.opus"):
            if f.stat().st_size > 0:
                audio_files.append(f)
                print(f"[RECOVERY] Found audio: {f.name} ({f.stat().st_size / 1024 / 1024:.1f} MB)")

    # Check for existing MKV files
    for d in search_dirs:
        if not d.exists():
            continue
        for f in d.glob("*_av.mkv"):
            if f.stat().st_size > 0:
                print(f"[RECOVERY] Existing MKV found: {f.name}")
                created_mkvs.append(str(f))

    if created_mkvs:
        return created_mkvs

    if not video_files and not audio_files:
        print("[RECOVERY] No raw files found")
        return []

    # Match by timestamp
    video_by_ts = {}
    audio_by_ts = {}

    for f in video_files:
        match = re.search(r'recording_(\d{8}_\d{6})', f.name)
        ts = match.group(1) if match else 'unknown'
        video_by_ts[ts] = f

    for f in audio_files:
        match = re.search(r'recording_(\d{8}_\d{6})', f.name)
        ts = match.group(1) if match else 'unknown'
        audio_by_ts[ts] = f

    all_ts = set(list(video_by_ts.keys()) + list(audio_by_ts.keys()))

    for ts in all_ts:
        vf = video_by_ts.get(ts)
        af = audio_by_ts.get(ts)

        PRESERVED_DIR.mkdir(parents=True, exist_ok=True)
        out_name = f"recording_{ts}_av.mkv" if ts != 'unknown' else f"recovered_av.mkv"
        out_path = PRESERVED_DIR / out_name

        if out_path.exists() and out_path.stat().st_size > 0:
            created_mkvs.append(str(out_path))
            continue

        if vf and af:
            print(f"[RECOVERY] Muxing video + audio → {out_name}")
            cmd = f'ffmpeg -y -i "{vf}" -i "{af}" -c:v copy -c:a copy -movflags +faststart "{out_path}"'
        elif vf:
            print(f"[RECOVERY] Muxing video only → {out_name}")
            cmd = f'ffmpeg -y -i "{vf}" -c:v copy -movflags +faststart "{out_path}"'
        elif af:
            print(f"[RECOVERY] Muxing audio only → {out_name}")
            cmd = f'ffmpeg -y -i "{af}" -c:a copy "{out_path}"'
        else:
            continue

        try:
            result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=300)
            if result.returncode == 0 and out_path.exists() and out_path.stat().st_size > 0:
                print(f"[RECOVERY] ✓ Created: {out_name} ({out_path.stat().st_size / 1024 / 1024:.1f} MB)")
                created_mkvs.append(str(out_path))
            else:
                print(f"[RECOVERY] ✗ FFmpeg failed: {(result.stderr or '')[-200:]}")
        except Exception as e:
            print(f"[RECOVERY] ✗ Error: {e}")

    print(f"[RECOVERY] Result: {len(created_mkvs)} MKV file(s) available")
    return created_mkvs

# ═══════════════════════════════════════════════════════════
# STOP SIGNAL CHECKER
# ═══════════════════════════════════════════════════════════
async def stop_signal_checker(stream_id: str, stop_event: asyncio.Event):
    """Polls /api/check-stop/{stream_id} for manual stop from Telegram."""
    print(f"[STOP-CHECK] Monitoring (interval={STOP_CHECK_INTERVAL}s)")
    while not stop_event.is_set():
        try:
            if WORKER_API_URL and AUTO_API_TOKEN:
                url = f"{WORKER_API_URL}/api/check-stop/{stream_id}"
                req = urllib.request.Request(url, headers={
                    'X-Auto-Token': AUTO_API_TOKEN,
                    'Content-Type': 'application/json'
                })
                loop = asyncio.get_event_loop()
                response = await loop.run_in_executor(
                    None, lambda: urllib.request.urlopen(req, timeout=10)
                )
                data = json.loads(response.read().decode('utf-8'))
                if data.get("should_stop"):
                    print(f"[STOP-CHECK] ⚠️ STOP SIGNAL for {stream_id}")
                    stop_event.set()
                    return
        except Exception as e:
            print(f"[STOP-CHECK] Poll error: {e}")
        await asyncio.sleep(STOP_CHECK_INTERVAL)

async def verify_stream_is_accessible(stream_url: str) -> dict:
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        return {"accessible": False, "error": "Playwright not available"}
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=['--no-sandbox', '--disable-setuid-sandbox', '--disable-dev-shm-usage']
            )
            context = await browser.new_context(
                viewport={'width': 1280, 'height': 720},
                user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                locale='fr-FR'
            )
            page = await context.new_page()
            try:
                await page.goto(stream_url, wait_until='networkidle', timeout=45000)
            except Exception as e:
                await browser.close()
                return {"accessible": False, "error": f"Navigation failed: {e}"}
            await page.wait_for_timeout(5000)
            videos = await page.query_selector_all('video')
            if not videos:
                await browser.close()
                return {"accessible": False, "error": "No video elements"}
            for idx, video in enumerate(videos):
                try:
                    dims = await video.bounding_box()
                    if not dims:
                        continue
                    ready_state = await video.evaluate('v => v.readyState')
                    paused = await video.evaluate('v => v.paused')
                    width = int(dims['width'])
                    height = int(dims['height'])
                    if width < 300 or height < 300:
                        continue
                    is_playing = ready_state >= 3 and not paused
                    if not is_playing:
                        try:
                            can_play = await video.evaluate('''v => new Promise(r => {
                                v.muted = true;
                                v.play().then(() => r(true)).catch(() => r(false));
                                setTimeout(() => r(false), 3000);
                            })''')
                            if can_play:
                                await page.wait_for_timeout(2000)
                                ready_state = await video.evaluate('v => v.readyState')
                                paused = await video.evaluate('v => v.paused')
                                is_playing = ready_state >= 3 and not paused
                        except:
                            pass
                    if is_playing:
                        await browser.close()
                        return {"accessible": True, "video_playing": True}
                except:
                    continue
            await browser.close()
            return {"accessible": False, "error": "No playable video"}
    except Exception as e:
        return {"accessible": False, "error": str(e)}

async def pre_flight_check(stream_url: str, stream_id: str) -> dict:
    print(f"[PRE-FLIGHT] Starting validation for stream {stream_id}")
    try:
        discovery = SuperLiveDiscovery()
        profile_result = await discovery.discover_profile_id(stream_id)
        discovered_username = None
        discovered_profile_id = ""
        if profile_result:
            discovered_username = profile_result.get("username")
            discovered_profile_id = profile_result.get("profile_id", "")
        print(f"[PRE-FLIGHT] Identity resolved: username={discovered_username}, profile_id={discovered_profile_id}")
        is_livestream_url = '/livestream/' in stream_url
        if is_livestream_url:
            print(f"[PRE-FLIGHT] URL is a livestream URL - checking directly")
            live_result = await discovery.check_live_status(
                profile_url=stream_url,
                user_id=stream_id,
                profile_id=discovered_profile_id,
                phase1_username=discovered_username
            )
            if live_result:
                is_live = live_result.get("is_live", False)
                is_premium = live_result.get("is_premium", False)
                raw_stream_id = live_result.get("stream_id")
                username = live_result.get("username") or discovered_username
                actual_stream_id = stream_id
                if raw_stream_id and is_valid_stream_id(raw_stream_id, stream_id):
                    actual_stream_id = str(raw_stream_id)
                if is_live and is_premium:
                    print(f"[PRE-FLIGHT] ⚠️ Discovery flagged PREMIUM - verifying...")
                    verification = await verify_stream_is_accessible(stream_url)
                    if verification.get("accessible"):
                        print(f"[PRE-FLIGHT] ✓ SMART OVERRIDE: LIVE_NORMAL")
                        return {
                            "success": True, "status": "LIVE_NORMAL",
                            "username": username or stream_id,
                            "actual_stream_id": actual_stream_id, "error": None
                        }
                    return {
                        "success": False, "status": "LIVE_PREMIUM",
                        "username": username,
                        "actual_stream_id": actual_stream_id,
                        "error": "Stream is Premium"
                    }
                if is_live and not is_premium:
                    return {
                        "success": True, "status": "LIVE_NORMAL",
                        "username": username,
                        "actual_stream_id": actual_stream_id, "error": None
                    }
                if not is_live:
                    verification = await verify_stream_is_accessible(stream_url)
                    if verification.get("accessible"):
                        return {
                            "success": True, "status": "LIVE_NORMAL",
                            "username": username or stream_id,
                            "actual_stream_id": actual_stream_id, "error": None
                        }
                    return {
                        "success": False, "status": "OFFLINE",
                        "username": username,
                        "actual_stream_id": actual_stream_id,
                        "error": "User is offline"
                    }
        if not profile_result:
            return {
                "success": False, "status": "UNKNOWN", "username": None,
                "actual_stream_id": stream_id, "error": "Failed to resolve identity"
            }
        profile_url = profile_result.get("profile_url")
        username = profile_result.get("username")
        profile_id = profile_result.get("profile_id", "")
        if not profile_url:
            return {
                "success": False, "status": "UNKNOWN", "username": username,
                "actual_stream_id": stream_id, "error": "No profile URL"
            }
        live_result = await discovery.check_live_status(
            profile_url=profile_url, user_id=stream_id,
            profile_id=profile_id, phase1_username=username
        )
        if not live_result:
            return {
                "success": False, "status": "UNKNOWN", "username": username,
                "actual_stream_id": stream_id, "error": "Failed to check live"
            }
        is_live = live_result.get("is_live", False)
        is_premium = live_result.get("is_premium", False)
        raw_stream_id = live_result.get("stream_id")
        final_username = live_result.get("username") or username
        actual_stream_id = stream_id
        if raw_stream_id and is_valid_stream_id(raw_stream_id, stream_id):
            actual_stream_id = str(raw_stream_id)
        if is_live and is_premium:
            verification = await verify_stream_is_accessible(stream_url)
            if verification.get("accessible"):
                return {
                    "success": True, "status": "LIVE_NORMAL",
                    "username": final_username or stream_id,
                    "actual_stream_id": actual_stream_id, "error": None
                }
            return {
                "success": False, "status": "LIVE_PREMIUM", "username": final_username,
                "actual_stream_id": actual_stream_id, "error": "Stream is Premium"
            }
        if is_premium:
            return {
                "success": False, "status": "LIVE_PREMIUM", "username": final_username,
                "actual_stream_id": actual_stream_id, "error": "Stream is Premium"
            }
        if not is_live:
            return {
                "success": False, "status": "OFFLINE", "username": final_username,
                "actual_stream_id": actual_stream_id, "error": "User is offline"
            }
        return {
            "success": True, "status": "LIVE_NORMAL", "username": final_username,
            "actual_stream_id": actual_stream_id, "error": None
        }
    except Exception as e:
        print(f"[PRE-FLIGHT] Error: {e}")
        return {
            "success": False, "status": "ERROR", "username": None,
            "actual_stream_id": stream_id, "error": str(e)
        }

def get_latest_files(before_time: float) -> list:
    files = []
    for ext in ['*.mkv', '*.webm', '*.mp4']:
        for pattern in [str(RECORDINGS_DIR / ext), str(TEMP_DIR / ext)]:
            for f in glob.glob(pattern):
                try:
                    if os.path.getmtime(f) >= before_time:
                        files.append(f)
                except:
                    pass
    return files

def rename_files_by_username(files: list, username: str) -> list:
    if not files or not username or username.startswith("user_"):
        return []
    safe_name = sanitize_filename(username)
    renamed = []
    files_sorted = sorted(files, key=lambda f: os.path.getmtime(f))
    if len(files_sorted) == 1:
        old_path = files_sorted[0]
        ext = os.path.splitext(old_path)[1]
        dir_path = os.path.dirname(old_path)
        new_name = f"{safe_name}{ext}"
        new_path = os.path.join(dir_path, new_name)
        counter = 1
        while os.path.exists(new_path):
            new_name = f"{safe_name}_{counter}{ext}"
            new_path = os.path.join(dir_path, new_name)
            counter += 1
        try:
            shutil.move(old_path, new_path)
            renamed.append((old_path, new_path))
            print(f"[RENAME] ✓ {os.path.basename(old_path)} → {new_name}")
        except Exception as e:
            print(f"[RENAME] ✗ Failed to rename {old_path}: {e}")
        return renamed
    for idx, old_path in enumerate(files_sorted, start=1):
        ext = os.path.splitext(old_path)[1]
        dir_path = os.path.dirname(old_path)
        new_name = f"{safe_name}_part{idx:03d}{ext}"
        new_path = os.path.join(dir_path, new_name)
        counter = 1
        base_name = f"{safe_name}_part{idx:03d}"
        while os.path.exists(new_path):
            new_name = f"{base_name}_{counter}{ext}"
            new_path = os.path.join(dir_path, new_name)
            counter += 1
        try:
            shutil.move(old_path, new_path)
            renamed.append((old_path, new_path))
            print(f"[RENAME] ✓ {os.path.basename(old_path)} → {new_name}")
        except Exception as e:
            print(f"[RENAME] ✗ Failed to rename {old_path}: {e}")
    return renamed

async def main():
    stream_url = os.environ.get("RECORD_URL")
    stream_id = os.environ.get("STREAM_ID")
    stream_name = os.environ.get("STREAM_NAME", "")
    recording_source = os.environ.get("RECORDING_SOURCE", "manual")

    if not stream_url or not stream_id:
        print("[ERROR] Missing RECORD_URL or STREAM_ID")
        sys.exit(1)

    print("=" * 70)
    print("SUPERLIVE RECORDER - LAUNCHER")
    print(f"TIME LIMIT: {RECORDING_MAX_SECONDS}s ({RECORDING_MAX_SECONDS // 60} min)")
    print(f"STOP CHECK: every {STOP_CHECK_INTERVAL}s")
    print("=" * 70)
    print(f"URL: {stream_url}")
    print(f"Stream ID: {stream_id}")
    print(f"Stream Name: {stream_name or '(will be detected)'}")
    print(f"Source: {recording_source}")
    print("=" * 70)

    print("\n" + "=" * 70)
    print("PRE-FLIGHT VALIDATION")
    print("=" * 70)

    start_time = datetime.now().timestamp()

    existing_files = set()
    for ext in ['*.mkv', '*.webm', '*.mp4', '*.h264']:
        for pattern in [str(RECORDINGS_DIR / ext), str(TEMP_DIR / ext)]:
            existing_files.update(glob.glob(pattern))

    validation = await pre_flight_check(stream_url, stream_id)

    if not validation["success"]:
        status = validation["status"]
        username = validation["username"] or "Unknown"
        error = validation["error"]
        actual_stream_id = validation["actual_stream_id"]
        if status == "LIVE_PREMIUM":
            msg = f"💎 <b>بث Premium</b>\n👤 المستخدم: <code>{actual_stream_id}</code>\n👤 الاسم: <b>{username}</b>\n⚠️ هذا البث مدفوع ولا يمكن تسجيله."
        elif status == "OFFLINE":
            msg = f"⚫ <b>غير متصل</b>\n👤 المستخدم: <code>{actual_stream_id}</code>\n👤 الاسم: <b>{username}</b>\n⚠️ المستخدم غير متصل حالياً."
        else:
            msg = f"❌ <b>فشل التحقق</b>\n👤 المستخدم: <code>{actual_stream_id}</code>\n⚠️ {error}"
        send_telegram_message(msg)
        
        # ═══════════════════════════════════════════════════════════
        # IMMEDIATE CLEANUP: Remove from active recordings
        # ═══════════════════════════════════════════════════════════
        cleanup_failed_recording(actual_stream_id, f"pre_flight_{status}")
        
        update_worker_state(actual_stream_id, {
            "status": "failed", "error": error,
            "failed_at": datetime.now(timezone.utc).isoformat()
        })
        print(f"\n[PRE-FLIGHT] ✗ Validation failed: {status} - {error}")
        sys.exit(1)

    detected_username = validation["username"]
    actual_stream_id = validation["actual_stream_id"]

    if detected_username and not stream_name:
        stream_name = detected_username
        print(f"[PRE-FLIGHT] ✓ Using detected username: {stream_name}")

    if actual_stream_id != stream_id:
        print(f"[PRE-FLIGHT] ✓ Using detected stream_id: {actual_stream_id}")
        os.environ["STREAM_ID"] = actual_stream_id
        stream_id = actual_stream_id

    if stream_name:
        os.environ["STREAM_NAME"] = stream_name
        os.environ["VIDEO_FILENAME_PREFIX"] = sanitize_filename(stream_name)
        os.environ["OUTPUT_PREFIX"] = sanitize_filename(stream_name)
        os.environ["FILENAME_PREFIX"] = sanitize_filename(stream_name)
        os.environ["RECORD_NAME"] = stream_name
        print(f"[PRE-FLIGHT] ✓ Set env vars with username: {stream_name}")

    print(f"\n[PRE-FLIGHT] ✓ All checks passed - proceeding with recording")

    update_worker_state(stream_id, {
        "stream_name": stream_name, "status": "recording", "source": recording_source
    })

    source_emoji = "✋" if recording_source == "manual" else "🤖"
    start_msg = (
        f"{source_emoji} <b>بدأ التسجيل</b>\n"
        f"👤 الاسم: <b>{stream_name or 'Unknown'}</b>\n"
        f"🆔 ID: <code>{stream_id}</code>\n"
        f"🕒 الوقت: {datetime.now().strftime('%H:%M:%S')}\n"
        f"⏱️ الحد الأقصى: {RECORDING_MAX_SECONDS // 60} دقيقة"
    )
    send_telegram_message(start_msg)

    print("\n" + "=" * 70)
    print("STARTING RECORDING")
    print(f"TIME LIMIT: {RECORDING_MAX_SECONDS}s | STOP CHECK: {STOP_CHECK_INTERVAL}s")
    print("=" * 70)

    # Start FilePreserver
    PRESERVED_DIR.mkdir(parents=True, exist_ok=True)
    preserver = FilePreserverThread(
        watch_dirs=[RECORDINGS_DIR, TEMP_DIR],
        preserve_dir=PRESERVED_DIR,
        poll_interval=0.3,
    )
    preserver.start()

    # ── Recording with timeout + stop signal ──
    stop_event = asyncio.Event()
    stop_checker_task = asyncio.create_task(stop_signal_checker(stream_id, stop_event))
    recording_stopped_manually = False
    recording_timed_out = False

    try:
        import record_once
        recording_task = asyncio.create_task(record_once.main())
        stop_waiter = asyncio.create_task(stop_event.wait())

        # Wait for: recording finishes, OR stop signal, OR timeout
        done, pending = await asyncio.wait(
            [recording_task, stop_waiter],
            timeout=RECORDING_MAX_SECONDS,
            return_when=asyncio.FIRST_COMPLETED
        )

        if stop_event.is_set() and recording_task not in done:
            # ── MANUAL STOP from Telegram ──
            recording_stopped_manually = True
            print(f"\n[LAUNCHER] 🛑 STOP SIGNAL — cancelling recording")
            recording_task.cancel()
            kill_orphan_processes()
            try:
                await asyncio.wait_for(asyncio.shield(recording_task), timeout=20)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass
            except Exception as e:
                print(f"[LAUNCHER] Recording ended during stop: {e}")
            stop_waiter.cancel()
            update_worker_state(stream_id, {
                "status": "stopped",
                "stopped_at": datetime.now(timezone.utc).isoformat(),
                "stopped_reason": "manual_stop_from_telegram"
            })
            elapsed = datetime.now().timestamp() - start_time
            send_telegram_message(
                f"🛑 <b>تم إيقاف التسجيل يدوياً</b>\n"
                f"👤 الاسم: <b>{stream_name or 'Unknown'}</b>\n"
                f"🆔 ID: <code>{stream_id}</code>\n"
                f"⏰ المدة: {int(elapsed // 60)} دقيقة و {int(elapsed % 60)} ثانية\n"
                f"📦 جارٍ الحفظ والمعالجة..."
            )

        elif recording_task in done:
            # ── Natural completion or exception ──
            stop_waiter.cancel()
            try:
                await recording_task
                print("\n[LAUNCHER] ✓ record_once.py completed successfully")
                update_worker_state(stream_id, {
                    "status": "completed",
                    "completed_at": datetime.now(timezone.utc).isoformat(),
                    "completed_reason": "natural_end"
                })
            except asyncio.CancelledError:
                recording_stopped_manually = True
                update_worker_state(stream_id, {
                    "status": "stopped",
                    "stopped_at": datetime.now(timezone.utc).isoformat()
                })
            except Exception as e:
                error_str = str(e)
                
                # ═══════════════════════════════════════════════════
                # IMMEDIATE CLEANUP on recording failure
                # ═══════════════════════════════════════════════════
                cleanup_failed_recording(stream_id, f"recording_error:{error_str[:50]}")
                
                if "Unable to split Matroska" in error_str:
                    print("[LAUNCHER] Matroska split failed — RESCUE MODE")
                    send_telegram_message(
                        f"⚠️ <b>فشل تقسيم Matroska — جارٍ الإنقاذ</b>\n"
                        f"👤 الاسم: <b>{stream_name or 'Unknown'}</b>\n"
                        f"🆔 ID: <code>{stream_id}</code>\n"
                        f"📦 مرحلة المعالجة اللاحقة ستُكمل التقسيم والإرسال."
                    )
                    update_worker_state(stream_id, {
                        "split_error": error_str[:300],
                        "rescue_pending": True,
                        "status": "completed",
                        "completed_at": datetime.now(timezone.utc).isoformat(),
                        "completed_reason": "split_failed_rescue_pending"
                    })
                    sys.exit(0)
                else:
                    raise

        else:
            # ── TIMEOUT: 3600 seconds reached ──
            recording_timed_out = True
            elapsed = datetime.now().timestamp() - start_time
            print(f"\n[LAUNCHER] ⏰ Recording reached {RECORDING_MAX_SECONDS}s — STOPPING NOW")
            print(f"[LAUNCHER] Actual elapsed: {int(elapsed)}s")
            recording_task.cancel()
            kill_orphan_processes()
            stop_waiter.cancel()
            try:
                await asyncio.wait_for(asyncio.shield(recording_task), timeout=20)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass
            except Exception as e:
                print(f"[LAUNCHER] Recording ended during timeout: {e}")

            update_worker_state(stream_id, {
                "status": "completed",
                "completed_at": datetime.now(timezone.utc).isoformat(),
                "completed_reason": "time_limit_3600s",
                "duration_seconds": round(elapsed)
            })
            send_telegram_message(
                f"⏱️ <b>تم إيقاف التسجيل تلقائياً (حد الساعة)</b>\n"
                f"👤 الاسم: <b>{stream_name or 'Unknown'}</b>\n"
                f"🆔 ID: <code>{stream_id}</code>\n"
                f"⏰ المدة: {int(elapsed // 60)} دقيقة\n"
                f"📦 جارٍ حفظ الفيديو والمعالجة..."
            )

    except asyncio.CancelledError:
        recording_stopped_manually = True
        print("\n[LAUNCHER] Recording cancelled externally")
        update_worker_state(stream_id, {
            "status": "stopped",
            "stopped_at": datetime.now(timezone.utc).isoformat()
        })
    except Exception as e:
        error_str = str(e)
        
        # ═══════════════════════════════════════════════════════════
        # IMMEDIATE CLEANUP on any unhandled exception
        # ═══════════════════════════════════════════════════════════
        cleanup_failed_recording(stream_id, f"unhandled_error:{error_str[:50]}")
        
        if "Unable to split Matroska" in error_str:
            print("[LAUNCHER] Matroska split failed — RESCUE MODE")
            update_worker_state(stream_id, {
                "split_error": error_str[:300],
                "rescue_pending": True,
                "status": "completed",
                "completed_at": datetime.now(timezone.utc).isoformat(),
                "completed_reason": "split_failed_rescue_pending"
            })
        else:
            send_telegram_message(
                f"❌ <b>فشل التسجيل</b>\n"
                f"👤 الاسم: <b>{stream_name or 'Unknown'}</b>\n"
                f"🆔 ID: <code>{stream_id}</code>\n"
                f"⚠️ {error_str[:200]}"
            )
            update_worker_state(stream_id, {
                "status": "failed", "error": error_str[:300],
                "failed_at": datetime.now(timezone.utc).isoformat()
            })
            preserver.stop()
            stop_checker_task.cancel()
            sys.exit(1)
    finally:
        stop_checker_task.cancel()
        try:
            await stop_checker_task
        except asyncio.CancelledError:
            pass
        print("[STOP-CHECK] Stopped")

    # Wait for file writes to flush
    if recording_stopped_manually or recording_timed_out:
        print("[LAUNCHER] Waiting 15s for file finalization...")
        await asyncio.sleep(15)

    preserver.stop()

    # ── RECOVERY: Mux raw files if needed ──
    if recording_stopped_manually or recording_timed_out:
        print("\n[LAUNCHER] RECOVERY: Muxing raw files → MKV")
        recovered = recover_and_mux_recording()
        if recovered:
            print(f"[LAUNCHER] ✓ Recovery: {len(recovered)} MKV available")
        else:
            print("[LAUNCHER] ⚠️ No files recovered")

    # ── RENAME FILES ──
    print("\n" + "=" * 70)
    print("POST-PROCESSING: Renaming files by username")
    print("=" * 70)

    if stream_name:
        await asyncio.sleep(2)
        all_current_files = set()
        for ext in ['*.mkv', '*.webm', '*.mp4']:
            # FIX: Exclude PRESERVED_DIR from renaming!
            # Files in _preserved must keep original names for postprocess and contact sheet to find them.
            for pattern in [str(RECORDINGS_DIR / ext), str(TEMP_DIR / ext)]:
                all_current_files.update(glob.glob(pattern))
        new_files = list(all_current_files - existing_files)
        if new_files:
            print(f"[RENAME] Found {len(new_files)} new file(s)")
            safe_name = sanitize_filename(stream_name)
            files_to_rename = [f for f in new_files if safe_name not in os.path.basename(f)]
            if files_to_rename:
                renamed = rename_files_by_username(files_to_rename, stream_name)
                if renamed:
                    print(f"[RENAME] ✓ Renamed {len(renamed)} file(s)")
                    file_list = "\n".join([f"📁 <code>{os.path.basename(new)}</code>" for _, new in renamed[:5]])
                    send_telegram_message(
                        f"📦 <b>ملفات التسجيل جاهزة</b>\n"
                        f"👤 الاسم: <b>{stream_name}</b>\n"
                        f"🆔 ID: <code>{stream_id}</code>\n"
                        f"{file_list}",
                        parse_mode="HTML"
                    )
            else:
                print(f"[RENAME] ℹ️ Files already have username")
        else:
            print(f"[RENAME] ℹ️ No new files found")
    else:
        print(f"[RENAME] ⚠️ No username - skipping")

    # Report preserved files
    preserved = list(PRESERVED_DIR.glob("*.mkv")) if PRESERVED_DIR.exists() else []
    if preserved:
        print(f"\n[PRESERVE] 📦 {len(preserved)} MKV in _preserved/:")
        for p in preserved:
            print(f"[PRESERVE]   • {p.name} ({p.stat().st_size / 1024 / 1024:.1f} MB)")
    else:
        print("\n[PRESERVE] ⚠️ No preserved MKV files")

if __name__ == "__main__":
    asyncio.run(main())
