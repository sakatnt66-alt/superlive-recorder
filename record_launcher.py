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

USER_AGENT = "SuperLive-AutoMonitor/1.0 (GitHub Actions; +https://github.com/sakatnt66-alt/superlive-recorder)"

print(f"[DEBUG] WORKER_API_URL: {'SET' if WORKER_API_URL else 'MISSING'} (length={len(WORKER_API_URL)})")
print(f"[DEBUG] AUTO_API_TOKEN: {'SET' if AUTO_API_TOKEN else 'MISSING'} (length={len(AUTO_API_TOKEN)})")

if WORKER_API_URL and WORKER_API_URL.endswith('/'):
    WORKER_API_URL = WORKER_API_URL[:-1]

WATCHLIST_FILE = Path("data/watchlist.json")
MAX_CONCURRENT = 4

RECORDINGS_DIR = Path(os.environ.get("RECORDINGS_DIR", "recordings"))
TEMP_DIR = Path(os.environ.get("TEMP_DIR", "recordings/_temp"))
PRESERVED_DIR = RECORDINGS_DIR / "_preserved"

RECORDING_MAX_SECONDS = int(os.environ.get("RECORDING_MAX_SECONDS", "3600"))
STOP_CHECK_INTERVAL = int(os.environ.get("STOP_CHECK_INTERVAL", "30"))

# Minimum size thresholds to reject corrupt/empty files
MIN_VIDEO_SIZE = 100 * 1024       # 100 KB minimum for video
MIN_AUDIO_SIZE = 10 * 1024        # 10 KB minimum for audio
MIN_MKV_SIZE   = 1 * 1024 * 1024  # 1 MB minimum for final MKV
MIN_MKV_DURATION = 5.0            # 5 seconds minimum duration

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

def is_valid_profile_id(profile_id: str) -> bool:
    if not profile_id:
        return False
    profile_id_str = str(profile_id)
    if not profile_id_str.isdigit():
        return False
    if len(profile_id_str) < 5 or len(profile_id_str) > 15:
        return False
    return True

def read_error_body(error):
    try:
        body = error.read().decode('utf-8', errors='replace')
        return body[:500] if body else '(empty)'
    except Exception:
        return '(unreadable)'

def send_telegram_message(text: str, parse_mode: str = "HTML"):
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

def sanitize_filename(name: str) -> str:
    if not name:
        return "unknown"
    safe = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', name)
    safe = safe.strip('. ')
    return safe[:100] if safe else "unknown"

def kill_orphan_processes():
    print("[LAUNCHER] Killing orphan ffmpeg/yt-dlp processes to ensure files are closed...")
    try:
        subprocess.run("pkill -f 'ffmpeg'", shell=True, stderr=subprocess.DEVNULL, stdout=subprocess.DEVNULL)
        subprocess.run("pkill -f 'yt-dlp'", shell=True, stderr=subprocess.DEVNULL, stdout=subprocess.DEVNULL)
    except Exception as e:
        print(f"[LAUNCHER] Kill error: {e}")

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

def validate_mkv_file(path: str) -> dict:
    """Validate an MKV file has real content."""
    result = {"valid": False, "duration": 0.0, "size": 0, "video": None, "audio": None, "reason": ""}
    try:
        p = Path(path)
        if not p.exists():
            result["reason"] = "file not found"
            return result
        size = p.stat().st_size
        result["size"] = size
        if size < MIN_MKV_SIZE:
            result["reason"] = f"too small ({size} bytes < {MIN_MKV_SIZE})"
            return result
        
        r = subprocess.run(
            f'ffprobe -v quiet -print_format json -show_format -show_streams "{path}"',
            shell=True, capture_output=True, text=True, timeout=60
        )
        if r.returncode != 0:
            result["reason"] = "ffprobe failed"
            return result
        
        data = json.loads(r.stdout)
        duration = float(data.get("format", {}).get("duration", 0))
        result["duration"] = duration
        
        if duration < MIN_MKV_DURATION:
            result["reason"] = f"duration too short ({duration:.1f}s < {MIN_MKV_DURATION}s)"
            return result
        
        for s in data.get("streams", []):
            ct = s.get("codec_type")
            cn = (s.get("codec_name") or "").lower()
            if ct == "video" and result["video"] is None:
                result["video"] = cn
            elif ct == "audio" and result["audio"] is None:
                result["audio"] = cn
        
        if not result["video"]:
            result["reason"] = "no video stream"
            return result
        
        result["valid"] = True
        result["reason"] = "ok"
    except Exception as e:
        result["reason"] = str(e)
    return result

def recover_and_mux_recording() -> list:
    """
    After record_once.py is cancelled (timeout or manual stop),
    find raw video and audio files and mux them into a proper MKV.
    CRITICAL: Rejects empty/corrupt files and validates the result.
    """
    print("\n" + "=" * 70)
    print("[RECOVERY] Searching for raw recording files to mux...")
    print("=" * 70)

    created_mkvs = []
    search_dirs = [TEMP_DIR, RECORDINGS_DIR, PRESERVED_DIR]

    # First, check for existing valid MKV files
    for d in search_dirs:
        if not d.exists():
            continue
        for f in d.glob("*_av.mkv"):
            validation = validate_mkv_file(str(f))
            if validation["valid"]:
                print(f"[RECOVERY] ✓ Existing valid MKV: {f.name} ({validation['size']/1024/1024:.1f} MB, {validation['duration']:.1f}s)")
                created_mkvs.append(str(f))
            else:
                print(f"[RECOVERY] ✗ Skipping invalid MKV: {f.name} — {validation['reason']}")

    if created_mkvs:
        return created_mkvs

    # Find raw video and audio files with MINIMUM SIZE CHECKS
    video_files = []
    audio_files = []

    for d in search_dirs:
        if not d.exists():
            continue
        for f in d.glob("*.h264"):
            size = f.stat().st_size
            if size >= MIN_VIDEO_SIZE:
                video_files.append((f, size))
                print(f"[RECOVERY] Found video: {f.name} ({size / 1024 / 1024:.1f} MB)")
            else:
                print(f"[RECOVERY] ✗ Skipping small video: {f.name} ({size} bytes)")
        for f in d.glob("*_video.webm"):
            size = f.stat().st_size
            if size >= MIN_VIDEO_SIZE:
                video_files.append((f, size))
                print(f"[RECOVERY] Found video: {f.name} ({size / 1024 / 1024:.1f} MB)")
            else:
                print(f"[RECOVERY] ✗ Skipping small video: {f.name} ({size} bytes)")
        for f in d.glob("*_audio.webm"):
            size = f.stat().st_size
            if size >= MIN_AUDIO_SIZE:
                audio_files.append((f, size))
                print(f"[RECOVERY] Found audio: {f.name} ({size / 1024 / 1024:.1f} MB)")
            else:
                print(f"[RECOVERY] ✗ Skipping small audio: {f.name} ({size} bytes)")
        for f in d.glob("*.opus"):
            size = f.stat().st_size
            if size >= MIN_AUDIO_SIZE:
                audio_files.append((f, size))
                print(f"[RECOVERY] Found audio: {f.name} ({size / 1024 / 1024:.1f} MB)")
            else:
                print(f"[RECOVERY] ✗ Skipping small audio: {f.name} ({size} bytes)")

    if not video_files and not audio_files:
        print("[RECOVERY] ✗ No valid raw files found (all empty or too small)")
        return []

    # Match by timestamp — pick LARGEST file for each timestamp
    video_by_ts = {}
    audio_by_ts = {}

    for f, size in video_files:
        match = re.search(r'recording_(\d{8}_\d{6})', f.name)
        ts = match.group(1) if match else 'unknown'
        # Keep the LARGEST file for each timestamp
        if ts not in video_by_ts or size > video_by_ts[ts][1]:
            video_by_ts[ts] = (f, size)

    for f, size in audio_files:
        match = re.search(r'recording_(\d{8}_\d{6})', f.name)
        ts = match.group(1) if match else 'unknown'
        if ts not in audio_by_ts or size > audio_by_ts[ts][1]:
            audio_by_ts[ts] = (f, size)

    all_ts = set(list(video_by_ts.keys()) + list(audio_by_ts.keys()))

    for ts in all_ts:
        vf_entry = video_by_ts.get(ts)
        af_entry = audio_by_ts.get(ts)

        PRESERVED_DIR.mkdir(parents=True, exist_ok=True)
        out_name = f"recording_{ts}_av.mkv" if ts != 'unknown' else f"recovered_av.mkv"
        out_path = PRESERVED_DIR / out_name

        # Don't overwrite existing valid files
        if out_path.exists():
            validation = validate_mkv_file(str(out_path))
            if validation["valid"]:
                print(f"[RECOVERY] ✓ Existing valid output: {out_name}")
                created_mkvs.append(str(out_path))
                continue
            else:
                print(f"[RECOVERY] ⚠️ Existing output invalid ({validation['reason']}), will recreate")

        vf = vf_entry[0] if vf_entry else None
        af = af_entry[0] if af_entry else None

        # Use a temp file first, then validate, then move
        tmp_path = PRESERVED_DIR / f"{out_name}.tmp"

        if vf and af:
            print(f"[RECOVERY] Muxing video + audio → {out_name}")
            cmd = f'ffmpeg -y -i "{vf}" -i "{af}" -c:v copy -c:a copy -movflags +faststart "{tmp_path}"'
        elif vf:
            print(f"[RECOVERY] Muxing video only → {out_name}")
            cmd = f'ffmpeg -y -i "{vf}" -c:v copy -movflags +faststart "{tmp_path}"'
        elif af:
            print(f"[RECOVERY] Muxing audio only → {out_name}")
            cmd = f'ffmpeg -y -i "{af}" -c:a copy "{tmp_path}"'
        else:
            continue

        try:
            result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=300)
            if result.returncode == 0 and tmp_path.exists():
                # CRITICAL: Validate the output before accepting it
                validation = validate_mkv_file(str(tmp_path))
                if validation["valid"]:
                    shutil.move(str(tmp_path), str(out_path))
                    print(f"[RECOVERY] ✓ Created and validated: {out_name} ({validation['size']/1024/1024:.1f} MB, {validation['duration']:.1f}s)")
                    created_mkvs.append(str(out_path))
                else:
                    print(f"[RECOVERY] ✗ Output validation failed: {validation['reason']}")
                    tmp_path.unlink(missing_ok=True)
            else:
                print(f"[RECOVERY] ✗ FFmpeg failed: {(result.stderr or '')[-200:]}")
                tmp_path.unlink(missing_ok=True)
        except Exception as e:
            print(f"[RECOVERY] ✗ Error: {e}")
            tmp_path.unlink(missing_ok=True)

    print(f"[RECOVERY] Result: {len(created_mkvs)} valid MKV file(s) available")
    return created_mkvs

async def stop_signal_checker(stream_id: str, stop_event: asyncio.Event):
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

    PRESERVED_DIR.mkdir(parents=True, exist_ok=True)
    preserver = FilePreserverThread(
        watch_dirs=[RECORDINGS_DIR, TEMP_DIR],
        preserve_dir=PRESERVED_DIR,
        poll_interval=0.3,
    )
    preserver.start()

    stop_event = asyncio.Event()
    stop_checker_task = asyncio.create_task(stop_signal_checker(stream_id, stop_event))
    recording_stopped_manually = False
    recording_timed_out = False

    try:
        import record_once
        recording_task = asyncio.create_task(record_once.main())
        stop_waiter = asyncio.create_task(stop_event.wait())

        done, pending = await asyncio.wait(
            [recording_task, stop_waiter],
            timeout=RECORDING_MAX_SECONDS,
            return_when=asyncio.FIRST_COMPLETED
        )

        if stop_event.is_set() and recording_task not in done:
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
                "status": "processing",
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
            stop_waiter.cancel()
            try:
                await recording_task
                print("\n[LAUNCHER] ✓ record_once.py completed successfully")
                update_worker_state(stream_id, {
                    "status": "processing",
                    "completed_at": datetime.now(timezone.utc).isoformat(),
                    "completed_reason": "natural_end"
                })
            except asyncio.CancelledError:
                recording_stopped_manually = True
                update_worker_state(stream_id, {
                    "status": "processing",
                    "stopped_at": datetime.now(timezone.utc).isoformat()
                })
            except Exception as e:
                error_str = str(e)
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
                        "status": "processing",
                        "completed_at": datetime.now(timezone.utc).isoformat(),
                        "completed_reason": "split_failed_rescue_pending"
                    })
                    sys.exit(0)
                else:
                    raise

        else:
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
                "status": "processing",
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
            "status": "processing",
            "stopped_at": datetime.now(timezone.utc).isoformat()
        })
    except Exception as e:
        error_str = str(e)
        cleanup_failed_recording(stream_id, f"unhandled_error:{error_str[:50]}")
        if "Unable to split Matroska" in error_str:
            print("[LAUNCHER] Matroska split failed — RESCUE MODE")
            update_worker_state(stream_id, {
                "split_error": error_str[:300],
                "rescue_pending": True,
                "status": "processing",
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

    if recording_stopped_manually or recording_timed_out:
        print("[LAUNCHER] Waiting 15s for file finalization...")
        await asyncio.sleep(15)

    preserver.stop()

    if recording_stopped_manually or recording_timed_out:
        print("\n[LAUNCHER] RECOVERY: Muxing raw files → MKV")
        recovered = recover_and_mux_recording()
        if recovered:
            print(f"[LAUNCHER] ✓ Recovery: {len(recovered)} valid MKV available")
        else:
            print("[LAUNCHER] ⚠️ No valid files recovered")

    print("\n" + "=" * 70)
    print("POST-PROCESSING: Renaming files by username")
    print("=" * 70)

    if stream_name:
        await asyncio.sleep(2)
        all_current_files = set()
        for ext in ['*.mkv', '*.webm', '*.mp4']:
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
            else:
                print(f"[RENAME] ℹ️ Files already have username")
        else:
            print(f"[RENAME] ℹ️ No new files found")
    else:
        print(f"[RENAME] ⚠️ No username - skipping")

    preserved = list(PRESERVED_DIR.glob("*.mkv")) if PRESERVED_DIR.exists() else []
    if preserved:
        print(f"\n[PRESERVE] 📦 {len(preserved)} MKV in _preserved/:")
        for p in preserved:
            print(f"[PRESERVE]   • {p.name} ({p.stat().st_size / 1024 / 1024:.1f} MB)")
    else:
        print("\n[PRESERVE] ⚠️ No preserved MKV files")

if __name__ == "__main__":
    asyncio.run(main())
