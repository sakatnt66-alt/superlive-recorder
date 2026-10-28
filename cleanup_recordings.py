#!/usr/bin/env python3
"""
cleanup_recordings.py - تنظيف التسجيلات العالقة تلقائياً
يتحقق من حالة كل تسجيل فعلياً ويزيل المنتهي/الفشل فقط

CHANGES:
  - MAX_RECORDING_MINUTES = 60 (was MAX_RECORDING_HOURS = 6)
"""
import json
import os
import subprocess
import sys
import base64
from datetime import datetime, timedelta
from pathlib import Path
import requests

DATA_DIR = Path("data")
RECORDINGS_FILE = DATA_DIR / "recordings.json"
QUEUE_DIR = Path("queue")

# CHANGED: 60 minutes instead of 6 hours
MAX_RECORDING_MINUTES = 60
STUCK_THRESHOLD_MINUTES = 15
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN")
GITHUB_REPO = os.environ.get("GITHUB_REPOSITORY")
GITHUB_BRANCH = os.environ.get("GITHUB_REF_NAME", "main")


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
            # CHANGED: minutes instead of hours
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


def main():
    print("=" * 50)
    print("🧹 بدء فحص وتنظيف التسجيلات العالقة")
    print(f"⏰ الحد الأقصى: {MAX_RECORDING_MINUTES} دقيقة")
    print("=" * 50)
    recordings = load_recordings()
    print(f"\n📊 إجمالي التسجيلات في القائمة: {len(recordings)}")
    if not recordings:
        print("✅ القائمة فارغة، لا يوجد شيء لتنظيفه.")
        return
    active_recordings = []
    removed_count = 0
    for recording in recordings:
        if is_recording_active(recording):
            active_recordings.append(recording)
        else:
            removed_count += 1
            print(f"  🗑️ تم إزالة: {recording.get('username')} (ID: {recording.get('user_id')})")
    print(f"\n{'=' * 50}")
    print(f"📈 النتيجة: نشط ({len(active_recordings)}) | مُزال ({removed_count})")
    print(f"{'=' * 50}")
    save_recordings(active_recordings)
    print("\n✅ انتهت عملية التنظيف بنجاح.")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"\n❌ خطأ فادح: {e}")
        sys.exit(1)
