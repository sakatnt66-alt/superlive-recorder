#!/usr/bin/env python3
"""
SuperLive Recorder - Launcher with Pre-flight Validation
Passes stream name to record_once.py and attempts to rename files
"""

import asyncio
import json
import os
import sys
import shutil
import glob
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path
import re

from superlive_discovery import SuperLiveDiscovery

# Configuration
WORKER_API_URL = os.environ.get("WORKER_API_URL", "")
AUTO_API_TOKEN = os.environ.get("AUTO_API_TOKEN", "")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

RECORDINGS_DIR = Path("recordings")
TEMP_DIR = RECORDINGS_DIR / "_temp"


def sanitize_filename(name: str) -> str:
    """Sanitize filename to be filesystem-safe"""
    if not name:
        return "unknown"
    # Remove unsafe characters
    name = re.sub(r'[<>:"/\\|?*]', '', name)
    # Replace whitespace with underscore
    name = re.sub(r'\s+', '_', name).strip('_')
    # Limit length
    if len(name) > 80:
        name = name[:80]
    return name or "unknown"


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
        'Content-Type': 'application/json'
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            pass
    except Exception as e:
        print(f"[TELEGRAM] Error: {e}")


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
        print(f"[WORKER] Error: {e}")


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
        
        # Fallback discovery flow
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
    """Get all video files created after a specific time."""
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
    """
    Rename video files to use username instead of generic names.
    Handles multiple parts: username_part001.mkv, username_part002.mkv, etc.
    """
    if not files or not username or username.startswith("user_"):
        return []
    
    safe_name = sanitize_filename(username)
    renamed = []
    
    # Sort by modification time (oldest first)
    files_sorted = sorted(files, key=lambda f: os.path.getmtime(f))
    
    # If only one file, use username directly
    if len(files_sorted) == 1:
        old_path = files_sorted[0]
        ext = os.path.splitext(old_path)[1]
        dir_path = os.path.dirname(old_path)
        new_name = f"{safe_name}{ext}"
        new_path = os.path.join(dir_path, new_name)
        
        # Handle name collision
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
    
    # Multiple files: use username_part001, username_part002, etc.
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
    print("SUPERLIVE RECORDER - LAUNCHER (WITH PRE-FLIGHT)")
    print("=" * 70)
    print(f"URL: {stream_url}")
    print(f"Stream ID: {stream_id}")
    print(f"Stream Name: {stream_name or '(will be detected)'}")
    print(f"Source: {recording_source}")
    print("=" * 70)
    
    print("\n" + "=" * 70)
    print("PRE-FLIGHT VALIDATION")
    print("=" * 70)
    
    # Mark time before recording to detect new files later
    start_time = datetime.now().timestamp()
    
    # Get list of existing files before recording starts
    existing_files = set()
    for ext in ['*.mkv', '*.webm', '*.mp4']:
        for pattern in [str(RECORDINGS_DIR / ext), str(TEMP_DIR / ext)]:
            existing_files.update(glob.glob(pattern))
    
    validation = await pre_flight_check(stream_url, stream_id)
    
    if not validation["success"]:
        status = validation["status"]
        username = validation["username"] or "Unknown"
        error = validation["error"]
        actual_stream_id = validation["actual_stream_id"]
        
        if status == "LIVE_PREMIUM":
            msg = f"💎 <b>بث Premium</b>\n👤 المستخدم: <code>{actual_stream_id}</code>\n👤 الاسم: <b>{username}</b>\n\n⚠️ هذا البث مدفوع ولا يمكن تسجيله."
        elif status == "OFFLINE":
            msg = f"⚫ <b>غير متصل</b>\n👤 المستخدم: <code>{actual_stream_id}</code>\n👤 الاسم: <b>{username}</b>\n\n⚠️ المستخدم غير متصل حالياً."
        else:
            msg = f"❌ <b>فشل التحقق</b>\n👤 المستخدم: <code>{actual_stream_id}</code>\n\n⚠️ {error}"
        
        send_telegram_message(msg)
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
    
    # ============================================================
    # PASS STREAM NAME to record_once.py via multiple env vars
    # This maximizes the chance that record_once.py uses the name
    # ============================================================
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
        f"🕒 الوقت: {datetime.now().strftime('%H:%M:%S')}"
    )
    send_telegram_message(start_msg)
    
    print("\n" + "=" * 70)
    print("CALLING ORIGINAL record_once.py")
    print("=" * 70)
    
    try:
        import record_once
        await record_once.main()
        print("\n[LAUNCHER] ✓ record_once.py completed successfully")
    except Exception as e:
        error_msg = (
            f"❌ <b>فشل التسجيل</b>\n"
            f"👤 الاسم: <b>{stream_name or 'Unknown'}</b>\n"
            f"🆔 ID: <code>{stream_id}</code>\n\n"
            f"⚠️ {str(e)}"
        )
        send_telegram_message(error_msg)
        update_worker_state(stream_id, {
            "status": "failed", "error": str(e),
            "failed_at": datetime.now(timezone.utc).isoformat()
        })
        sys.exit(1)
    
    # ============================================================
    # RENAME FILES: After record_once.py finishes, rename files
    # from generic names (like "recording_*.mkv") to username
    # ============================================================
    print("\n" + "=" * 70)
    print("POST-PROCESSING: Renaming files by username")
    print("=" * 70)
    
    if stream_name:
        # Wait a bit for all files to be written
        await asyncio.sleep(2)
        
        # Find new files created during recording
        all_current_files = set()
        for ext in ['*.mkv', '*.webm', '*.mp4']:
            for pattern in [str(RECORDINGS_DIR / ext), str(TEMP_DIR / ext)]:
                all_current_files.update(glob.glob(pattern))
        
        new_files = list(all_current_files - existing_files)
        
        if new_files:
            print(f"[RENAME] Found {len(new_files)} new file(s) to rename")
            
            # Filter out files that already have the username in them
            safe_name = sanitize_filename(stream_name)
            files_to_rename = [f for f in new_files if safe_name not in os.path.basename(f)]
            
            if files_to_rename:
                renamed = rename_files_by_username(files_to_rename, stream_name)
                if renamed:
                    print(f"[RENAME] ✓ Renamed {len(renamed)} file(s)")
                    
                    # Send notification about renamed files
                    file_list = "\n".join([f"📁 <code>{os.path.basename(new)}</code>" for _, new in renamed[:5]])
                    send_telegram_message(
                        f"📦 <b>ملفات التسجيل جاهزة</b>\n\n"
                        f"👤 الاسم: <b>{stream_name}</b>\n"
                        f"🆔 ID: <code>{stream_id}</code>\n\n"
                        f"{file_list}",
                        parse_mode="HTML"
                    )
            else:
                print(f"[RENAME] ℹ️ All new files already have username in name")
        else:
            print(f"[RENAME] ℹ️ No new files found (record_once.py may upload & delete)")
    else:
        print(f"[RENAME] ⚠️ No username available - skipping rename")


if __name__ == "__main__":
    asyncio.run(main())
