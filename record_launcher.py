#!/usr/bin/env python3
"""
SuperLive Recorder - Launcher with Pre-flight Validation
Calls the original record_once.py after validation
"""

import asyncio
import json
import os
import sys
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

from superlive_discovery import SuperLiveDiscovery

# Configuration
WORKER_API_URL = os.environ.get("WORKER_API_URL", "")
AUTO_API_TOKEN = os.environ.get("AUTO_API_TOKEN", "")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

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
        'Content-Type': 'application/json'
    })
    
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            if response.status != 200:
                print(f"[TELEGRAM] Failed to send message: {response.status}")
    except Exception as e:
        print(f"[TELEGRAM] Error sending message: {e}")

def update_worker_state(stream_id: str, state: dict):
    """Update recording state in Cloudflare Worker"""
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
            if response.status != 200:
                print(f"[WORKER] Failed to update state: {response.status}")
    except Exception as e:
        print(f"[WORKER] Error updating state: {e}")

async def verify_stream_is_accessible(stream_url: str) -> dict:
    """
    Smart verification: check if video is ACTUALLY playing.
    If video plays = stream is accessible (LIVE_NORMAL), regardless of VIP badges on page.
    Returns: {accessible: bool, video_found: bool, video_playing: bool, error: str}
    """
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
                print(f"[VERIFY] Navigation error: {e}")
                await browser.close()
                return {"accessible": False, "error": f"Navigation failed: {e}"}
            
            # Wait for video elements
            await page.wait_for_timeout(5000)
            
            # Check all video elements
            videos = await page.query_selector_all('video')
            print(f"[VERIFY] Found {len(videos)} video element(s)")
            
            if not videos:
                await browser.close()
                return {"accessible": False, "video_found": False, "error": "No video elements"}
            
            video_playing = False
            accessible_video = None
            
            for idx, video in enumerate(videos):
                try:
                    dims = await video.bounding_box()
                    if not dims:
                        continue
                    
                    ready_state = await video.evaluate('v => v.readyState')
                    paused = await video.evaluate('v => v.paused')
                    src = await video.evaluate('v => v.src || v.currentSrc || ""')
                    width = int(dims['width'])
                    height = int(dims['height'])
                    
                    # Filter out small/promo videos (gifts, ads, previews)
                    if width < 300 or height < 300:
                        print(f"[VERIFY] Video {idx}: too small ({width}x{height}) - skipping")
                        continue
                    
                    # Check if video is ready and playing
                    is_playing = ready_state >= 3 and not paused
                    
                    # Try to play if paused
                    if not is_playing:
                        try:
                            can_play = await video.evaluate('''v => {
                                return new Promise(resolve => {
                                    v.muted = true;
                                    v.play().then(() => resolve(true)).catch(() => resolve(false));
                                    setTimeout(() => resolve(false), 3000);
                                });
                            }''')
                            if can_play:
                                await page.wait_for_timeout(2000)
                                ready_state = await video.evaluate('v => v.readyState')
                                paused = await video.evaluate('v => v.paused')
                                is_playing = ready_state >= 3 and not paused
                        except Exception as e:
                            print(f"[VERIFY] Video {idx}: play() failed - {e}")
                    
                    print(f"[VERIFY] Video {idx}: {width}x{height}, readyState={ready_state}, paused={paused}, playing={is_playing}")
                    
                    if is_playing:
                        video_playing = True
                        accessible_video = {
                            "index": idx,
                            "width": width,
                            "height": height,
                            "ready_state": ready_state
                        }
                        break
                except Exception as e:
                    print(f"[VERIFY] Video {idx}: error - {e}")
                    continue
            
            await browser.close()
            
            if video_playing and accessible_video:
                return {
                    "accessible": True,
                    "video_found": True,
                    "video_playing": True,
                    "video_info": accessible_video,
                    "error": None
                }
            else:
                return {
                    "accessible": False,
                    "video_found": True,
                    "video_playing": False,
                    "error": "No playable video found"
                }
    
    except Exception as e:
        print(f"[VERIFY] Unexpected error: {e}")
        import traceback
        traceback.print_exc()
        return {"accessible": False, "error": str(e)}

async def pre_flight_check(stream_url: str, stream_id: str) -> dict:
    """
    Pre-flight validation before recording.
    Returns dict with: success, status, username, error
    """
    print(f"[PRE-FLIGHT] Starting validation for stream {stream_id}")
    
    try:
        discovery = SuperLiveDiscovery()
        
        # ============================================================
        # SMART PATH: If URL is already a livestream URL, check it directly
        # ============================================================
        is_livestream_url = '/livestream/' in stream_url
        
        if is_livestream_url:
            print(f"[PRE-FLIGHT] URL is a livestream URL - checking directly")
            
            live_result = await discovery.check_live_status(
                profile_url=stream_url,
                user_id=stream_id,
                profile_id="",
                phase1_username=None
            )
            
            if live_result:
                is_live = live_result.get("is_live", False)
                is_premium = live_result.get("is_premium", False)
                username = live_result.get("username")
                
                print(f"[PRE-FLIGHT] Discovery says: is_live={is_live}, is_premium={is_premium}")
                
                # ============================================================
                # SMART OVERRIDE: If Discovery says PREMIUM but video is playing,
                # it's a false positive (VIP badges on page, but stream is free)
                # ============================================================
                if is_live and is_premium:
                    print(f"[PRE-FLIGHT] ⚠️ Discovery flagged PREMIUM - verifying video accessibility...")
                    
                    verification = await verify_stream_is_accessible(stream_url)
                    
                    if verification.get("accessible"):
                        print(f"[PRE-FLIGHT] ✓ SMART OVERRIDE: Video is actually playing!")
                        print(f"[PRE-FLIGHT]   → Video info: {verification.get('video_info')}")
                        print(f"[PRE-FLIGHT]   → Treating as LIVE_NORMAL (not Premium)")
                        
                        # Try to get username via discovery fallback
                        if not username:
                            profile_result = await discovery.discover_profile_id(stream_id)
                            if profile_result:
                                username = profile_result.get("username")
                        
                        return {
                            "success": True,
                            "status": "LIVE_NORMAL",
                            "username": username or f"user_{stream_id}",
                            "error": None
                        }
                    else:
                        print(f"[PRE-FLIGHT] ✗ Video verification failed: {verification.get('error')}")
                        # Premium is real - video can't play
                        return {
                            "success": False,
                            "status": "LIVE_PREMIUM",
                            "username": username,
                            "error": "Stream is Premium (paywalled) - video cannot play"
                        }
                
                # LIVE_NORMAL without Premium flag
                if is_live and not is_premium:
                    print(f"[PRE-FLIGHT] ✓ Stream validated: LIVE_NORMAL, username={username}")
                    return {
                        "success": True,
                        "status": "LIVE_NORMAL",
                        "username": username,
                        "error": None
                    }
                
                # Not live
                if not is_live:
                    # Double-check with verification
                    print(f"[PRE-FLIGHT] Discovery says offline - double-checking...")
                    verification = await verify_stream_is_accessible(stream_url)
                    if verification.get("accessible"):
                        print(f"[PRE-FLIGHT] ✓ SMART OVERRIDE: Video is playing despite offline flag!")
                        return {
                            "success": True,
                            "status": "LIVE_NORMAL",
                            "username": username or f"user_{stream_id}",
                            "error": None
                        }
                    
                    return {
                        "success": False,
                        "status": "OFFLINE",
                        "username": username,
                        "error": "User is offline"
                    }
        
        # ============================================================
        # FALLBACK: Discovery flow for non-livestream URLs
        # ============================================================
        print(f"[PRE-FLIGHT] Using discovery flow")
        
        profile_result = await discovery.discover_profile_id(stream_id)
        
        if not profile_result:
            return {
                "success": False,
                "status": "UNKNOWN",
                "username": None,
                "error": "Failed to resolve user identity"
            }
        
        profile_url = profile_result.get("profile_url")
        username = profile_result.get("username")
        profile_id = profile_result.get("profile_id", "")
        
        if not profile_url:
            return {
                "success": False,
                "status": "UNKNOWN",
                "username": username,
                "error": "No profile URL found"
            }
        
        live_result = await discovery.check_live_status(
            profile_url=profile_url,
            user_id=stream_id,
            profile_id=profile_id,
            phase1_username=username
        )
        
        if not live_result:
            return {
                "success": False,
                "status": "UNKNOWN",
                "username": username,
                "error": "Failed to check live state"
            }
        
        is_live = live_result.get("is_live", False)
        is_premium = live_result.get("is_premium", False)
        final_username = live_result.get("username") or username
        
        # Smart override for false PREMIUM detection
        if is_live and is_premium:
            print(f"[PRE-FLIGHT] ⚠️ Verifying PREMIUM flag via direct stream access...")
            verification = await verify_stream_is_accessible(stream_url)
            if verification.get("accessible"):
                print(f"[PRE-FLIGHT] ✓ SMART OVERRIDE: False Premium flag corrected")
                return {
                    "success": True,
                    "status": "LIVE_NORMAL",
                    "username": final_username or f"user_{stream_id}",
                    "error": None
                }
            return {
                "success": False,
                "status": "LIVE_PREMIUM",
                "username": final_username,
                "error": "Stream is Premium (paywalled)"
            }
        
        if is_premium:
            return {
                "success": False,
                "status": "LIVE_PREMIUM",
                "username": final_username,
                "error": "Stream is Premium (paywalled)"
            }
        
        if not is_live:
            return {
                "success": False,
                "status": "OFFLINE",
                "username": final_username,
                "error": "User is offline"
            }
        
        print(f"[PRE-FLIGHT] ✓ Stream validated: LIVE_NORMAL, username={final_username}")
        return {
            "success": True,
            "status": "LIVE_NORMAL",
            "username": final_username,
            "error": None
        }
        
    except Exception as e:
        print(f"[PRE-FLIGHT] Error during validation: {e}")
        import traceback
        traceback.print_exc()
        return {
            "success": False,
            "status": "ERROR",
            "username": None,
            "error": str(e)
        }

async def main():
    """Main entry point"""
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
    
    validation = await pre_flight_check(stream_url, stream_id)
    
    if not validation["success"]:
        status = validation["status"]
        username = validation["username"] or "Unknown"
        error = validation["error"]
        
        if status == "LIVE_PREMIUM":
            msg = f"💎 <b>بث Premium</b>\n👤 المستخدم: <code>{stream_id}</code>\n👤 الاسم: <b>{username}</b>\n\n⚠️ هذا البث مدفوع ولا يمكن تسجيله."
        elif status == "OFFLINE":
            msg = f"⚫ <b>غير متصل</b>\n👤 المستخدم: <code>{stream_id}</code>\n👤 الاسم: <b>{username}</b>\n\n⚠️ المستخدم غير متصل حالياً."
        elif status == "MISMATCH":
            msg = f"⚠️ <b>خطأ في الهوية</b>\n👤 المستخدم: <code>{stream_id}</code>\n\n❌ {error}"
        else:
            msg = f"❌ <b>فشل التحقق</b>\n👤 المستخدم: <code>{stream_id}</code>\n\n⚠️ {error}"
        
        send_telegram_message(msg)
        
        update_worker_state(stream_id, {
            "status": "failed",
            "error": error,
            "failed_at": datetime.now(timezone.utc).isoformat()
        })
        
        print(f"\n[PRE-FLIGHT] ✗ Validation failed: {status} - {error}")
        sys.exit(1)
    
    detected_username = validation["username"]
    if detected_username and not stream_name:
        stream_name = detected_username
        print(f"[PRE-FLIGHT] ✓ Using detected username: {stream_name}")
        os.environ["STREAM_NAME"] = stream_name
    
    print(f"\n[PRE-FLIGHT] ✓ All checks passed - proceeding with recording")
    
    update_worker_state(stream_id, {
        "stream_name": stream_name,
        "status": "recording",
        "source": recording_source
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
            "status": "failed",
            "error": str(e),
            "failed_at": datetime.now(timezone.utc).isoformat()
        })
        
        print(f"\n[ERROR] Recording failed: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

if __name__ == "__main__":
    asyncio.run(main())
