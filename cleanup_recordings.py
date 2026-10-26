#!/usr/bin/env python3
"""
cleanup_recordings.py - تنظيف التسجيلات العالقة تلقائياً
يتحقق من حالة كل تسجيل فعلياً ويزيل المنتهي/الفشل فقط

ADDED: cleanup_worker_stale_recordings() — cleans up stale recordings
in Cloudflare Worker KV that exceeded the 60-minute limit.
"""
import json
import os
import subprocess
import sys
import base64
from datetime import datetime, timedelta, timezone
from pathlib import Path
import requests

# المسارات
DATA_DIR = Path("data")
RECORDINGS_FILE = DATA_DIR / "recordings.json"
QUEUE_DIR = Path("queue")

# الإعدادات
MAX_RECORDING_MINUTES = 60  # ← Changed from 6 hours to 60 minutes
STUCK_THRESHOLD_MINUTES = 15
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN")
GITHUB_REPO = os.environ.get("GITHUB_REPOSITORY")
GITHUB_BRANCH = os.environ.get("GITHUB_REF_NAME", "main")

# Worker API configuration (for KV state cleanup)
WORKER_API_URL = os.environ.get("WORKER_API_URL", "").strip()
AUTO_API_TOKEN = os.environ.get("AUTO_API_TOKEN", "").strip()
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

if WORKER_API_URL and WORKER_API_URL.endswith('/'):
    WORKER_API_URL = WORKER_API_URL[:-1]


def load_recordings():
    if not RECORDINGS_FILE.exists():
        return []
    try:
        with open(RECORDINGS_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
            return data if isinstance(data, list) else []
    except Exception as e:
        print(f"❌ خطأ في تحميل التسجيلات: {e}")
        return []


def save_recordings(recordings):
    try:
        DATA_DIR.mkdir(exist_ok=True)
        content_str = json.dumps(recordings, indent=2, ensure_ascii=False)
        with open(RECORDINGS_FILE, 'w', encoding='utf-8') as f:
            f.write(content_str)
        if GITHUB_TOKEN and GITHUB_REPO:
            print("🔄 جاري مزامنة التغييرات مع GitHub عبر API...")
            api_url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/data/recordings.json"
            headers = {
                'Authorization': f'token {GITHUB_TOKEN}',
                'Accept': 'application/vnd.github.v3+json'
            }
            sha = ""
            res = requests.get(api_url, headers=headers)
            if res.status_code == 200:
                sha = res.json().get('sha', '')
            payload = {
                "message": "🧹 Auto cleanup stuck recordings",
                "content": base64.b64encode(content_str.encode('utf-8')).decode('utf-8'),
                "branch": GITHUB_BRANCH
            }
            if sha:
                payload["sha"] = sha
            res_put = requests.put(api_url, headers=headers, json=payload)
            if res_put.status_code in [200, 201]:
                print("✅ تم تحديث الملف بنجاح عبر GitHub API")
            else:
                print(f"⚠️ تنبيه: فشل المزامنة مع GitHub ({res_put.status_code})، لكن الملف حُفظ محلياً.")
        else:
            print("✅ تم حفظ الملف محلياً.")
    except Exception as e:
        print(f"❌ خطأ فادح في الحفظ: {e}")


def check_github_workflow(run_id):
    if not run_id or not GITHUB_TOKEN or not GITHUB_REPO:
        return None
    try:
        url = f"https://api.github.com/repos/{GITHUB_REPO}/actions/runs/{run_id}"
        headers = {'Authorization': f'token {GITHUB_TOKEN}', 'Accept': 'application/vnd.github.v3+json'}
        response = requests.get(url, headers=headers, timeout=10)
        if response.status_code == 200:
            data = response.json()
            if data.get('status') == 'completed':
                return False
            elif data.get('status') == 'in_progress':
                return True
            return None
    except:
        return None


def check_file_activity(recording):
    recording_id = recording.get('id') or recording.get('user_id')
    if not recording_id:
        return None
    possible_paths = [
        QUEUE_DIR / f"{recording_id}.mp4",
        QUEUE_DIR / f"{recording_id}.part",
        Path("recordings") / f"{recording_id}.mp4",
    ]
    for file_path in possible_paths:
        if file_path.exists():
            try:
                stat = file_path.stat()
                file_age = datetime.now() - datetime.fromtimestamp(stat.st_mtime)
                if file_age > timedelta(minutes=STUCK_THRESHOLD_MINUTES):
                    return False
                else:
                    return True
            except:
                pass
    return None


def is_recording_active(recording):
    username = recording.get('username', 'unknown')
    print(f"🔍 فحص: {username}")
    started_at_str = recording.get('started_at')
    if started_at_str:
        try:
            started_at = datetime.fromisoformat(started_at_str.replace('Z', '+00:00'))
            duration = datetime.now(started_at.tzinfo) - started_at if started_at.tzinfo else datetime.now() - started_at
            if duration > timedelta(minutes=MAX_RECORDING_MINUTES):
                print(f"  ⏰ تجاوز الحد الأقصى ({MAX_RECORDING_MINUTES} دقيقة) -> إزالة")
                return False
        except: pass
    workflow_run_id = recording.get('workflow_run_id')
    if workflow_run_id:
        wf_status = check_github_workflow(workflow_run_id)
        if wf_status is False:
            print(f"  🔄 Workflow انتهى -> إزالة")
            return False
    file_activity = check_file_activity(recording)
    if file_activity is False:
        print(f"  📁 الملف لم يتغير منذ {STUCK_THRESHOLD_MINUTES} دقيقة -> إزالة")
        return False
    elif file_activity is True:
        print(f"  ✅ الملف نشط")
        return True
    if started_at_str:
        try:
            started_at = datetime.fromisoformat(started_at_str.replace('Z', '+00:00'))
            duration = datetime.now(started_at.tzinfo) - started_at if started_at.tzinfo else datetime.now() - started_at
            if duration > timedelta(hours=2) and file_activity is None:
                print(f"  ⚠️ مر {duration} بدون ملف أو Workflow -> إزالة")
                return False
        except: pass
    print(f"  ✅ التسجيل يعتبر نشطاً")
    return True


# ═══════════════════════════════════════════════════════════
# WORKER KV STALE RECORDING CLEANUP
# ═══════════════════════════════════════════════════════════
def cleanup_worker_stale_recordings():
    """
    Clean up stale recordings in Cloudflare Worker KV.

    For each recording in 'recording' status:
    - If duration > MAX_RECORDING_MINUTES (60 min), update status to 'completed'
    - This removes it from the active recordings list in /status

    This runs every 5 minutes via cleanup.yml as a safety net for
    recordings that somehow escaped the time limit in record_launcher.py.
    """
    if not WORKER_API_URL or not AUTO_API_TOKEN:
        print("[WORKER-CLEANUP] Skipped: WORKER_API_URL or AUTO_API_TOKEN not set")
        return

    print("\n" + "=" * 50)
    print("🔄 Worker KV Stale Recording Cleanup")
    print("=" * 50)

    # Step 1: Get active recordings from Worker
    try:
        url = f"{WORKER_API_URL}/api/active-recordings"
        headers = {
            'X-Auto-Token': AUTO_API_TOKEN,
            'Content-Type': 'application/json'
        }
        response = requests.get(url, headers=headers, timeout=15)
        if response.status_code != 200:
            print(f"[WORKER-CLEANUP] Failed to fetch active recordings: HTTP {response.status_code}")
            return
        data = response.json()
        active_recordings = data.get("recordings", [])
        print(f"[WORKER-CLEANUP] Active recordings in Worker KV: {len(active_recordings)}")
    except Exception as e:
        print(f"[WORKER-CLEANUP] Error fetching active recordings: {e}")
        return

    if not active_recordings:
        print("[WORKER-CLEANUP] No active recordings to check")
        return

    # Step 2: Check each recording's duration
    now = datetime.now(timezone.utc)
    stale_count = 0

    for rec in active_recordings:
        stream_id = rec.get("stream_id")
        started_at_str = rec.get("started_at")
        stream_name = rec.get("stream_name", "Unknown")

        if not started_at_str:
            print(f"[WORKER-CLEANUP] ⚠️ {stream_id}: no started_at, skipping")
            continue

        try:
            started_at = datetime.fromisoformat(started_at_str.replace('Z', '+00:00'))
            duration = now - started_at
            duration_minutes = duration.total_seconds() / 60
        except Exception as e:
            print(f"[WORKER-CLEANUP] ⚠️ {stream_id}: cannot parse started_at: {e}")
            continue

        print(f"[WORKER-CLEANUP] Checking {stream_id} ({stream_name}): {duration_minutes:.0f} min")

        if duration_minutes > MAX_RECORDING_MINUTES:
            print(f"[WORKER-CLEANUP] ⏰ {stream_id} exceeded {MAX_RECORDING_MINUTES} min — marking as completed")

            # Update state in Worker KV
            try:
                update_url = f"{WORKER_API_URL}/api/update-state/{stream_id}"
                update_payload = {
                    "status": "completed",
                    "completed_at": now.isoformat(),
                    "completed_reason": "cleanup_stale_60min",
                    "duration_minutes": round(duration_minutes)
                }
                update_response = requests.post(
                    update_url,
                    json=update_payload,
                    headers=headers,
                    timeout=15
                )
                if update_response.status_code == 200:
                    print(f"[WORKER-CLEANUP] ✓ {stream_id} marked as completed")
                    stale_count += 1

                    # Send Telegram notification
                    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
                        msg = (
                            f"🧹 <b>تنظيف تلقائي</b>\n"
                            f"👤 الاسم: <b>{stream_name}</b>\n"
                            f"🆔 ID: <code>{stream_id}</code>\n"
                            f"⏱️ المدة: {int(duration_minutes)} دقيقة (تجاوز حد {MAX_RECORDING_MINUTES} دقيقة)\n"
                            f"✅ تم تحديث الحالة إلى «مكتمل» وإزالته من القائمة النشطة."
                        )
                        try:
                            tg_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
                            tg_payload = {
                                "chat_id": TELEGRAM_CHAT_ID,
                                "text": msg,
                                "parse_mode": "HTML"
                            }
                            requests.post(tg_url, json=tg_payload, timeout=15)
                        except Exception as tg_err:
                            print(f"[WORKER-CLEANUP] Telegram notification failed: {tg_err}")
                else:
                    print(f"[WORKER-CLEANUP] ✗ Failed to update {stream_id}: HTTP {update_response.status_code}")
            except Exception as e:
                print(f"[WORKER-CLEANUP] ✗ Error updating {stream_id}: {e}")

    print(f"\n[WORKER-CLEANUP] Result: {stale_count} stale recording(s) cleaned up")


def main():
    print("=" * 50)
    print("🧹 بدء فحص وتنظيف التسجيلات العالقة")
    print(f"⏰ الحد الأقصى: {MAX_RECORDING_MINUTES} دقيقة")
    print("=" * 50)

    # Part 1: Clean local recordings.json (existing behavior)
    recordings = load_recordings()
    print(f"\n📊 إجمالي التسجيلات في القائمة المحلية: {len(recordings)}")

    if recordings:
        active_recordings = []
        removed_count = 0
        for recording in recordings:
            if is_recording_active(recording):
                active_recordings.append(recording)
            else:
                removed_count += 1
                print(f"  🗑️ تم إزالة: {recording.get('username')} (ID: {recording.get('user_id')})")

        print(f"\n{'=' * 50}")
        print(f"📈 النتيجة المحلية: نشط ({len(active_recordings)}) | مُزال ({removed_count})")
        print(f"{'=' * 50}")
        save_recordings(active_recordings)
    else:
        print("✅ القائمة المحلية فارغة، لا يوجد شيء لتنظيفه.")

    # Part 2: Clean Worker KV stale recordings (NEW)
    cleanup_worker_stale_recordings()

    print("\n✅ انتهت عملية التنظيف بنجاح.")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"\n❌ خطأ فادح: {e}")
        sys.exit(1)
