#!/usr/bin/env python3
"""
SuperLive Recorder - Launcher with Pre-flight Validation
Calls the original record_once.py after validation
"""

import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
import aiohttp

# Import Discovery for pre-flight checks
from superlive_discovery import SuperLiveDiscovery

# Configuration
WORKER_API_URL = os.environ.get("WORKER_API_URL", "")
AUTO_API_TOKEN = os.environ.get("AUTO_API_TOKEN", "")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

async def send_telegram_message(text: str, parse_mode: str = "HTML"):
    """Send message to Telegram"""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": parse_mode
    }
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload) as response:
                if response.status != 200:
                    print(f"[TELEGRAM] Failed to send message: {response.status}")
    except Exception as e:
        print(f"[TELEGRAM] Error sending message: {e}")

async def update_worker_state(stream_id: str, state: dict):
    """Update recording state in Cloudflare Worker"""
    if not WORKER_API_URL or not AUTO_API_TOKEN:
        return
    
    url = f"{WORKER_API_URL}/api/update-state/{stream_id}"
    headers = {"X-Auto-Token": AUTO_API_TOKEN}
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=state, headers=headers) as response:
                if response.status != 200:
                    print(f"[WORKER] Failed to update state: {response.status}")
    except Exception as e:
        print(f"[WORKER] Error updating state: {e}")

async def pre_flight_check(stream_url: str, stream_id: str) -> dict:
    """
    Pre-flight validation before recording.
    Returns dict with: success, status, username, error
    """
    print(f"[PRE-FLIGHT] Starting validation for stream {stream_id}")
    
    try:
        # Use Discovery to check stream state
        discovery = SuperLiveDiscovery()
        
        # Resolve identity and check state
        result = await discovery.resolve_user_identity(stream_id)
        
        if not result:
            return {
                "success": False,
                "status": "UNKNOWN",
                "username": None,
                "error": "Failed to resolve user identity"
            }
        
        profile_url = result.get("profile_url")
        username = result.get("username") or result.get("display_name")
        
        if not profile_url:
            return {
                "success": False,
                "status": "UNKNOWN",
                "username": username,
                "error": "No profile URL found"
            }
        
        # Check live state
        live_state = await discovery.check_live_state(profile_url)
        
        if not live_state:
            return {
                "success": False,
                "status": "UNKNOWN",
                "username": username,
                "error": "Failed to check live state"
            }
        
        is_live = live_state.get("is_live", False)
        is_premium = live_state.get("premium", False)
        detected_stream_id = live_state.get("stream_id")
        
        # Verify stream ID matches
        if detected_stream_id and str(detected_stream_id) != str(stream_id):
            return {
                "success": False,
                "status": "MISMATCH",
                "username": username,
                "error": f"Stream ID mismatch: expected {stream_id}, got {detected_stream_id}"
            }
        
        # Check states
        if is_premium:
            return {
                "success": False,
                "status": "LIVE_PREMIUM",
                "username": username,
                "error": "Stream is Premium (paywalled)"
            }
        
        if not is_live:
            return {
                "success": False,
                "status": "OFFLINE",
                "username": username,
                "error": "User is offline"
            }
        
        # Success - LIVE_NORMAL
        print(f"[PRE-FLIGHT] ✓ Stream validated: LIVE_NORMAL, username={username}")
        return {
            "success": True,
            "status": "LIVE_NORMAL",
            "username": username,
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
    # Get environment variables
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
    
    # Pre-flight check
    print("\n" + "=" * 70)
    print("PRE-FLIGHT VALIDATION")
    print("=" * 70)
    
    validation = await pre_flight_check(stream_url, stream_id)
    
    if not validation["success"]:
        status = validation["status"]
        username = validation["username"] or "Unknown"
        error = validation["error"]
        
        # Send appropriate error message
        if status == "LIVE_PREMIUM":
            msg = f"💎 <b>بث Premium</b>\n👤 المستخدم: <code>{stream_id}</code>\n\n⚠️ هذا البث مدفوع ولا يمكن تسجيله."
        elif status == "OFFLINE":
            msg = f"⚫ <b>غير متصل</b>\n👤 المستخدم: <code>{stream_id}</code>\n\n⚠️ المستخدم غير متصل حالياً."
        elif status == "MISMATCH":
            msg = f"⚠️ <b>خطأ في الهوية</b>\n👤 المستخدم: <code>{stream_id}</code>\n\n❌ {error}"
        else:
            msg = f"❌ <b>فشل التحقق</b>\n👤 المستخدم: <code>{stream_id}</code>\n\n⚠️ {error}"
        
        await send_telegram_message(msg)
        
        # Update worker state
        await update_worker_state(stream_id, {
            "status": "failed",
            "error": error,
            "failed_at": datetime.now(timezone.utc).isoformat()
        })
        
        print(f"\n[PRE-FLIGHT] ✗ Validation failed: {status} - {error}")
        sys.exit(1)
    
    # Validation succeeded
    detected_username = validation["username"]
    if detected_username and not stream_name:
        stream_name = detected_username
        print(f"[PRE-FLIGHT] ✓ Using detected username: {stream_name}")
        # Update environment variable for record_once.py
        os.environ["STREAM_NAME"] = stream_name
    
    print(f"\n[PRE-FLIGHT] ✓ All checks passed - proceeding with recording")
    
    # Update worker state with username
    await update_worker_state(stream_id, {
        "stream_name": stream_name,
        "status": "recording",
        "source": recording_source
    })
    
    # Send start message
    source_emoji = "✋" if recording_source == "manual" else "🤖"
    start_msg = (
        f"{source_emoji} <b>بدأ التسجيل</b>\n"
        f"👤 الاسم: <b>{stream_name or 'Unknown'}</b>\n"
        f"🆔 ID: <code>{stream_id}</code>\n"
        f"🕒 الوقت: {datetime.now().strftime('%H:%M:%S')}"
    )
    await send_telegram_message(start_msg)
    
    # Call the original record_once.py
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
        await send_telegram_message(error_msg)
        
        await update_worker_state(stream_id, {
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
