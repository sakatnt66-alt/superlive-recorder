#!/usr/bin/env python3
"""
cleanup_recordings.py - تنظيف التسجيلات العالقة تلقائياً من Cloudflare Worker KV
يجلب التسجيلات النشطة من Worker API ويوقف أي تسجيل تجاوز 60 دقيقة.
"""
import json
import os
import sys
from datetime import datetime, timedelta, timezone
import requests

# الإعدادات
MAX_RECORDING_MINUTES = 60  # الحد الأقصى: 60 دقيقة بالضبط
WORKER_API_URL = os.environ.get("WORKER_API_URL", "").strip()
AUTO_API_TOKEN = os.environ.get("AUTO_API_TOKEN", "").strip()

def get_active_recordings_from_worker():
    """جلب التسجيلات النشطة من Cloudflare Worker KV"""
    if not WORKER_API_URL or not AUTO_API_TOKEN:
        print("❌ WORKER_API_URL أو AUTO_API_TOKEN غير موجودة في البيئة.")
        return []
    
    url = f"{WORKER_API_URL}/api/active-recordings"
    headers = {'X-Auto-Token': AUTO_API_TOKEN, 'Accept': 'application/json'}
    
    try:
        res = requests.get(url, headers=headers, timeout=15)
        if res.status_code == 200:
            data = res.json()
            return data.get("recordings", [])
        else:
            print(f"⚠️ فشل جلب التسجيلات من Worker: {res.status_code}")
            return []
    except Exception as e:
        print(f"❌ خطأ في الاتصال بـ Worker: {e}")
        return []

def update_worker_state(stream_id, state):
    """إرسال تحديث الحالة إلى Cloudflare Worker KV"""
    if not WORKER_API_URL or not AUTO_API_TOKEN:
        return
    
    url = f"{WORKER_API_URL}/api/update-state/{stream_id}"
    headers = {'X-Auto-Token': AUTO_API_TOKEN, 'Content-Type': 'application/json'}
    
    try:
        res = requests.post(url, json=state, headers=headers, timeout=15)
        if res.status_code == 200:
            print(f"  🔄 [WORKER SYNC] تم تحديث حالة {stream_id} بنجاح إلى '{state.get('status')}'")
        else:
            print(f"  ⚠️ [WORKER SYNC] فشل التحديث: {res.status_code}")
    except Exception as e:
        print(f"  ❌ [WORKER SYNC] خطأ: {e}")

def is_recording_stuck(recording):
    """فحص ما إذا كان التسجيل قد تجاوز 60 دقيقة"""
    stream_id = recording.get('stream_id')
    stream_name = recording.get('stream_name', 'Unknown')
    started_at_str = recording.get('started_at')
    
    print(f"🔍 فحص: {stream_name} (ID: {stream_id})")
    
    if not started_at_str:
        print(f"  ⚠️ لا يوجد started_at -> اعتبارها عالقة وإيقافها")
        return True
        
    try:
        if started_at_str.endswith('Z'):
            started_at_str = started_at_str[:-1] + '+00:00'
        started_at = datetime.fromisoformat(started_at_str)
        if started_at.tzinfo is None:
            started_at = started_at.replace(tzinfo=timezone.utc)
            
        now = datetime.now(timezone.utc)
        duration = now - started_at
        minutes = duration.total_seconds() / 60
        
        print(f"  ⏱️ المدة: {minutes:.1f} دقيقة")
        
        if duration >= timedelta(minutes=MAX_RECORDING_MINUTES):
            print(f"  ⏰ تجاوز {MAX_RECORDING_MINUTES} دقيقة -> إيقاف فوري")
            return True
            
    except Exception as e:
        print(f"  ❌ خطأ في حساب المدة: {e} -> اعتبارها عالقة")
        return True
        
    print(f"  ✅ التسجيل يعمل ضمن المدة المسموحة")
    return False

def main():
    print("=" * 60)
    print("🧹 بدء فحص وتنظيف التسجيلات من Cloudflare KV")
    print(f"⏰ الحد الأقصى: {MAX_RECORDING_MINUTES} دقيقة")
    print("=" * 60)
    
    if not WORKER_API_URL or not AUTO_API_TOKEN:
        print("❌ لا يمكن التشغيل: متغيرات البيئة مفقودة.")
        sys.exit(1)

    active_recordings = get_active_recordings_from_worker()
    print(f"\n📊 إجمالي التسجيلات النشطة في Worker KV: {len(active_recordings)}")
    
    if not active_recordings:
        print("✅ لا توجد تسجيلات نشطة، لا يوجد شيء لتنظيفه.")
        return

    stuck_count = 0
    for recording in active_recordings:
        if is_recording_stuck(recording):
            stuck_count += 1
            stream_id = recording.get('stream_id')
            stream_name = recording.get('stream_name', 'Unknown')
            print(f"  🛑 جاري إيقاف: {stream_name} (ID: {stream_id})")
            
            update_worker_state(str(stream_id), {
                "status": "stopped",
                "stopped_at": datetime.now(timezone.utc).isoformat(),
                "reason": "auto_cleanup_60min_timeout"
            })

    print(f"\n{'=' * 60}")
    print(f"📈 النتيجة: تم إيقاف {stuck_count} تسجيل من أصل {len(active_recordings)}")
    print(f"{'=' * 60}")
    print("\n✅ انتهت عملية التنظيف بنجاح.")

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"\n❌ خطأ فادح: {e}")
        sys.exit(1)
