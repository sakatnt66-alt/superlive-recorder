#!/usr/bin/env python3
"""
SuperLive Auto Monitor - Optimized for Speed
Discovers live streams and triggers recording
"""

import asyncio
import json
import os
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

from superlive_discovery import SuperLiveDiscovery

# Configuration
GITHUB_REPOSITORY = os.environ.get("GITHUB_REPOSITORY", "")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
WORKER_API_URL = os.environ.get("WORKER_API_URL", "")
AUTO_API_TOKEN = os.environ.get("AUTO_API_TOKEN", "")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

WATCHLIST_FILE = Path("data/watchlist.json")
MAX_CONCURRENT = 4

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

def get_active_recordings() -> list:
    """Get active recordings from worker"""
    if not WORKER_API_URL or not AUTO_API_TOKEN:
        return []
    
    url = f"{WORKER_API_URL}/api/active-recordings"
    req = urllib.request.Request(url, headers={
        'X-Auto-Token': AUTO_API_TOKEN,
        'Content-Type': 'application/json'
    })
    
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            if response.status == 200:
                data = json.loads(response.read().decode('utf-8'))
                return data.get("recordings", [])
    except Exception as e:
        print(f"[WORKER] Error getting active recordings: {e}")
    
    return []

def trigger_recording(user_id: str, stream_id: str, stream_url: str, stream_name: str) -> bool:
    """Trigger recording via worker"""
    if not WORKER_API_URL or not AUTO_API_TOKEN:
        print(f"[AUTO] ✗ Worker API not configured (WORKER_API_URL or AUTO_API_TOKEN missing)")
        return False
    
    url = f"{WORKER_API_URL}/api/auto-trigger/{stream_id}"
    payload = json.dumps({
        "stream_url": stream_url,
        "stream_name": stream_name
    }).encode('utf-8')
    
    req = urllib.request.Request(url, data=payload, headers={
        'X-Auto-Token': AUTO_API_TOKEN,
        'Content-Type': 'application/json'
    })
    
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            if response.status == 200:
                data = json.loads(response.read().decode('utf-8'))
                if data.get("success"):
                    print(f"[AUTO] START_RECORDING {user_id} (stream_id={stream_id}) -> {stream_url}")
                    return True
                else:
                    error = data.get('error', 'unknown')
                    print(f"[AUTO] ✗ Failed to trigger {user_id}: {error}")
                    return False
            else:
                print(f"[AUTO] ✗ HTTP {response.status} when triggering {user_id}")
                return False
    except Exception as e:
        print(f"[AUTO] ✗ Error triggering recording for {user_id}: {e}")
        return False

async def check_user(discovery: SuperLiveDiscovery, user: dict) -> dict:
    """Check a single user's live state"""
    user_id = str(user.get("stream_id"))
    print(f"[AUTO] [Phase 1] Resolving {user_id}")
    
    try:
        # Phase 1: Discover profile
        profile_result = await discovery.discover_profile_id(user_id)
        
        if not profile_result:
            print(f"[AUTO] ✗ Failed to resolve {user_id}")
            return None
        
        profile_url = profile_result.get("profile_url")
        if not profile_url or profile_url.endswith("/None") or "/profile/None" in profile_url:
            print(f"[AUTO] ✗ Invalid profile URL for {user_id}: {profile_url}")
            return None
        
        username = profile_result.get("username", "")
        profile_id = profile_result.get("profile_id", "")
        
        print(f"[AUTO] [Phase 1] OK: {profile_url}")
        print(f"[AUTO] [Phase 2] Check live at {profile_url}")
        
        # Phase 2: Check live status
        live_result = await discovery.check_live_status(
            profile_url=profile_url,
            user_id=user_id,
            profile_id=profile_id,
            phase1_username=username
        )
        
        if not live_result:
            print(f"[AUTO] ✗ Failed to check live state for {user_id}")
            return None
        
        is_live = live_result.get("is_live", False)
        is_premium = live_result.get("is_premium", False)
        stream_id = live_result.get("stream_id", user_id)
        final_username = live_result.get("username") or username or user_id
        
        # Log results in old format
        print(f"[AUTO] username={user_id}")
        print(f"[AUTO] source=discovery_layer")
        print(f"[AUTO] profile_url={profile_url}")
        
        if profile_id:
            print(f"[AUTO] profile_id={profile_id}")
        
        print(f"[AUTO] stream_id={stream_id}")
        print(f"[AUTO] display_name={final_username}")
        
        if is_premium:
            print(f"[AUTO] phase=p2_premium")
            print(f"[AUTO] status=LIVE_PREMIUM")
            print(f"[AUTO] action=SKIP_PREMIUM")
            print(f"[AUTO] reason=premium")
            return None
        
        if not is_live:
            print(f"[AUTO] phase=p2_offline")
            print(f"[AUTO] status=OFFLINE")
            print(f"[AUTO] action=SKIP_OFFLINE")
            print(f"[AUTO] reason=no_live")
            return None
        
        # LIVE_NORMAL - candidate for recording
        print(f"[AUTO] phase=phase_3_done")
        print(f"[AUTO] status=LIVE_NORMAL")
        print(f"[AUTO] action=CANDIDATE")
        print(f"[AUTO] reason=live")
        
        return {
            "user_id": user_id,
            "stream_id": stream_id,
            "stream_url": f"https://superlivetv.com/fr/livestream/{stream_id}",
            "stream_name": final_username
        }
        
    except Exception as e:
        print(f"[AUTO] ✗ Error checking user {user_id}: {e}")
        import traceback
        traceback.print_exc()
        return None

async def main():
    """Main entry point"""
    print("[AUTO] Starting Auto Monitor (Optimized for Speed)")
    
    # Load watchlist
    if not WATCHLIST_FILE.exists():
        print("[AUTO] ✗ Watchlist file not found")
        return
    
    try:
        with open(WATCHLIST_FILE, 'r', encoding='utf-8') as f:
            watchlist_data = json.load(f)
            watchlist = watchlist_data.get("watchlist", [])
    except Exception as e:
        print(f"[AUTO] ✗ Error loading watchlist: {e}")
        return
    
    print(f"[AUTO] Watchlist: {len(watchlist)} users")
    
    # Get active recordings
    active = get_active_recordings()
    print(f"[AUTO] Active: {len(active)}/5")
    
    if len(active) >= 5:
        print("[AUTO] ✗ Concurrency limit reached (5/5)")
        return
    
    # Filter users not already recording
    active_ids = {str(r.get("stream_id")) for r in active}
    to_check = [u for u in watchlist if str(u.get("stream_id")) not in active_ids]
    
    print(f"[AUTO] Discovery for {len(to_check)} users (Concurrency: {MAX_CONCURRENT})")
    
    if not to_check:
        print("[AUTO] ✓ No users to check (all already recording)")
        return
    
    # Phase 1 & 2: Discovery
    discovery = SuperLiveDiscovery()
    
    semaphore = asyncio.Semaphore(MAX_CONCURRENT)
    
    async def check_with_semaphore(user):
        async with semaphore:
            return await check_user(discovery, user)
    
    # Run checks concurrently
    tasks = [check_with_semaphore(u) for u in to_check]
    results = await asyncio.gather(*tasks)
    
    # Filter successful results
    to_record = [r for r in results if r is not None]
    
    # Check available slots
    available_slots = 5 - len(active)
    to_record = to_record[:available_slots]
    
    print(f"[AUTO] Slots: {available_slots}")
    
    if not to_record:
        print("[AUTO] ✓ No LIVE_NORMAL streams detected")
        print("[AUTO] Monitor completed")
        return
    
    # Trigger recordings
    triggered = 0
    for record in to_record:
        if trigger_recording(
            record["user_id"],
            record["stream_id"],
            record["stream_url"],
            record["stream_name"]
        ):
            triggered += 1
    
    print(f"[AUTO] Monitor completed")

if __name__ == "__main__":
    asyncio.run(main())
