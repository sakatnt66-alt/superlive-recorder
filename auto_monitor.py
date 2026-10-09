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

def trigger_recording(stream_id: str, stream_url: str, stream_name: str) -> bool:
    """Trigger recording via worker"""
    if not WORKER_API_URL or not AUTO_API_TOKEN:
        print(f"[AUTO] Worker API not configured")
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
                    print(f"[AUTO] ✓ Triggered recording for {stream_id} ({stream_name})")
                    return True
                else:
                    print(f"[AUTO] ✗ Failed to trigger: {data.get('error')}")
            else:
                print(f"[AUTO] ✗ HTTP {response.status}")
    except Exception as e:
        print(f"[AUTO] Error triggering recording: {e}")
    
    return False

async def check_user(discovery: SuperLiveDiscovery, user: dict) -> dict:
    """Check a single user's live state"""
    user_id = str(user.get("stream_id"))
    print(f"[AUTO] [Phase 1] Resolving {user_id}")
    
    try:
        result = await discovery.resolve_user_identity(user_id)
        
        if not result:
            print(f"[AUTO] ✗ Failed to resolve {user_id}")
            return None
        
        profile_url = result.get("profile_url")
        if not profile_url:
            print(f"[AUTO] ✗ No profile URL for {user_id}")
            return None
        
        print(f"[AUTO] [Phase 1] OK: {profile_url}")
        print(f"[AUTO] [Phase 2] Check live at {profile_url}")
        
        live_state = await discovery.check_live_state(profile_url)
        
        if not live_state:
            print(f"[AUTO] ✗ Failed to check live state for {user_id}")
            return None
        
        is_live = live_state.get("is_live", False)
        is_premium = live_state.get("premium", False)
        detected_stream_id = live_state.get("stream_id")
        username = result.get("username") or result.get("display_name") or user_id
        
        # Log results
        print(f"[AUTO] username={user_id}")
        print(f"[AUTO] source=discovery_layer")
        print(f"[AUTO] profile_url={profile_url}")
        
        if detected_stream_id:
            print(f"[AUTO] profile_id={detected_stream_id}")
        
        print(f"[AUTO] stream_id={user_id}")
        
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
        
        # LIVE_NORMAL - trigger recording
        print(f"[AUTO] phase=p2_live_normal")
        print(f"[AUTO] status=LIVE_NORMAL")
        print(f"[AUTO] action=TRIGGER_RECORDING")
        
        return {
            "stream_id": user_id,
            "stream_url": f"https://superlivetv.com/fr/livestream/{user_id}",
            "stream_name": username
        }
        
    except Exception as e:
        print(f"[AUTO] Error checking user {user_id}: {e}")
        import traceback
        traceback.print_exc()
        return None

async def main():
    """Main entry point"""
    print("[AUTO] Starting Auto Monitor (Optimized for Speed)")
    
    # Load watchlist
    if not WATCHLIST_FILE.exists():
        print("[AUTO] Watchlist file not found")
        return
    
    try:
        with open(WATCHLIST_FILE, 'r', encoding='utf-8') as f:
            watchlist_data = json.load(f)
            watchlist = watchlist_data.get("watchlist", [])
    except Exception as e:
        print(f"[AUTO] Error loading watchlist: {e}")
        return
    
    print(f"[AUTO] Watchlist: {len(watchlist)} users")
    
    # Get active recordings
    active = get_active_recordings()
    print(f"[AUTO] Active: {len(active)}/5")
    
    if len(active) >= 5:
        print("[AUTO] Concurrency limit reached")
        return
    
    # Filter users not already recording
    active_ids = {str(r.get("stream_id")) for r in active}
    to_check = [u for u in watchlist if str(u.get("stream_id")) not in active_ids]
    
    print(f"[AUTO] Discovery for {len(to_check)} users (Concurrency: {MAX_CONCURRENT})")
    
    if not to_check:
        print("[AUTO] No users to check")
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
    
    # Trigger recordings
    for record in to_record:
        trigger_recording(
            record["stream_id"],
            record["stream_url"],
            record["stream_name"]
        )
    
    print("[AUTO] Monitor completed")

if __name__ == "__main__":
    asyncio.run(main())
