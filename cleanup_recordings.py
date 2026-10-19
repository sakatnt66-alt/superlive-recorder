#!/usr/bin/env python3
"""
cleanup_recordings.py - تنظيف التسجيلات العالقة تلقائياً
يتحقق من حالة كل تسجيل فعلياً ويزيل المنتهي/الفشل فقط
"""

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path
import requests

# المسارات
DATA_DIR = Path("data")
RECORDINGS_FILE = DATA_DIR / "recordings.json"
QUEUE_DIR = Path("queue")

# الإعدادات
MAX_RECORDING_HOURS = 6  # الحد الأقصى لساعات التسجيل (6 ساعات)
STUCK_THRESHOLD_MINUTES = 10  # إذا لم يتغير حجم الملف خلال 10 دقائق = عالق
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN")
GITHUB_REPO = os.environ.get("GITHUB_REPOSITORY")  # owner/repo

def load_recordings():
    """تحميل قائمة التسجيلات"""
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
    """حفظ قائمة التسجيلات"""
    try:
        DATA_DIR.mkdir(exist_ok=True)
        with open(RECORDINGS_FILE, 'w', encoding='utf-8') as f:
            json.dump(recordings, f, indent=2, ensure_ascii=False)
        print(f"✅ تم حفظ {len(recordings)} تسجيل")
    except Exception as e:
        print(f"❌ خطأ في حفظ التسجيلات: {e}")

def check_process_alive(pid):
    """التحقق من أن العملية ما زالت تعمل"""
    if not pid:
        return False
    
    try:
        # على Linux/Mac
        result = subprocess.run(
            ['ps', '-p', str(pid)],
            capture_output=True,
            text=True
        )
        return result.returncode == 0
    except:
        return False

def check_github_workflow(run_id):
    """التحقق من حالة GitHub Actions workflow"""
    if not run_id or not GITHUB_TOKEN or not GITHUB_REPO:
        return None
    
    try:
        url = f"https://api.github.com/repos/{GITHUB_REPO}/actions/runs/{run_id}"
        headers = {
            'Authorization': f'token {GITHUB_TOKEN}',
            'Accept': 'application/vnd.github.v3+json'
        }
        response = requests.get(url, headers=headers, timeout=10)
        
        if response.status_code == 200:
            data = response.json()
            status = data.get('status')  # queued, in_progress, completed
            conclusion = data.get('conclusion')  # success, failure, cancelled
            
            print(f"  🔍 Workflow {run_id}: status={status}, conclusion={conclusion}")
            
            if status == 'completed':
                return False  # انتهى
            elif status == 'in_progress':
                return True  # ما زال يعمل
            else:
                return None  # حالة غير واضحة
        return None
    except Exception as e:
        print(f"  ⚠️ خطأ في فحص workflow: {e}")
        return None

def check_recording_file_activity(recording):
    """التحقق من نشاط ملف التسجيل"""
    recording_id = recording.get('id') or recording.get('user_id')
    if not recording_id:
        return None
    
    # ابحث عن ملف التسجيل في queue أو recordings
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
                
                # إذا الملف لم يتغير خلال 10 دقائق = عالق
                if file_age > timedelta(minutes=STUCK_THRESHOLD_MINUTES):
                    print(f"  📁 ملف التسجيل {file_path.name} لم يتغير منذ {file_age}")
                    return False
                else:
                    print(f"  📁 ملف التسجيل {file_path.name} نشط (آخر تعديل: {file_age})")
                    return True
            except Exception as e:
                print(f"  ⚠️ خطأ في فحص الملف: {e}")
    
    return None  # لا يوجد ملف

def is_recording_active(recording):
    """
    التحقق الشامل من أن التسجيل ما زال نشطاً
    يرجع: True (نشط), False (منتهي), None (غير متأكد)
    """
    username = recording.get('username', 'unknown')
    print(f"\n🔍 فحص تسجيل: {username}")
    
    # 1. تحقق من المدة
    started_at_str = recording.get('started_at')
    if started_at_str:
        try:
            started_at = datetime.fromisoformat(started_at_str.replace('Z', '+00:00'))
            duration = datetime.now(started_at.tzinfo) - started_at if started_at.tzinfo else datetime.now() - started_at
            
            if duration > timedelta(hours=MAX_RECORDING_HOURS):
                print(f"  ⏰ تجاوز الحد الأقصى ({MAX_RECORDING_HOURS} ساعات)")
                return False
        except Exception as e:
            print(f"  ⚠️ خطأ في حساب المدة: {e}")
    
    # 2. تحقق من العملية (PID)
    pid = recording.get('pid')
    if pid:
        is_alive = check_process_alive(pid)
        print(f"  🔧 عملية PID {pid}: {'نشطة' if is_alive else 'منتهية'}")
        if not is_alive:
            return False
    
    # 3. تحقق من GitHub Actions workflow
    workflow_run_id = recording.get('workflow_run_id')
    if workflow_run_id:
        workflow_status = check_github_workflow(workflow_run_id)
        if workflow_status is False:
            print(f"  🔄 Workflow انتهى")
            return False
        elif workflow_status is True:
            print(f"  🔄 Workflow ما زال يعمل")
            # لا ترجع True هنا، تحقق من حالات أخرى أيضاً
    
    # 4. تحقق من نشاط ملف التسجيل
    file_activity = check_recording_file_activity(recording)
    if file_activity is False:
        print(f"  📁 ملف التسجيل غير نشط")
        return False
    elif file_activity is True:
        print(f"  📁 ملف التسجيل نشط")
        return True
    
    # 5. إذا لم نتأكد من أي شيء، اعتبره عالق إذا مر وقت طويل
    if started_at_str:
        try:
            started_at = datetime.fromisoformat(started_at_str.replace('Z', '+00:00'))
            duration = datetime.now(started_at.tzinfo) - started_at if started_at.tzinfo else datetime.now() - started_at
            
            # إذا مر أكثر من ساعة ولا يوجد دليل على النشاط = عالق
            if duration > timedelta(hours=1):
                print(f"  ⚠️ مر {duration} بدون دليل على النشاط")
                return False
        except:
            pass
    
    print(f"  ✅ التسجيل نشط")
    return True

def cleanup_recordings():
    """تنظيف التسجيلات العالقة"""
    print("=" * 60)
    print("🧹 بدء تنظيف التسجيلات العالقة")
    print("=" * 60)
    
    recordings = load_recordings()
    print(f"\n📊 عدد التسجيلات الحالي: {len(recordings)}")
    
    if not recordings:
        print("✅ القائمة فارغة، لا حاجة للتنظيف")
        return
    
    active_recordings = []
    removed_count = 0
    
    for recording in recordings:
        is_active = is_recording_active(recording)
        
        if is_active:
            active_recordings.append(recording)
        else:
            removed_count += 1
            username = recording.get('username', 'unknown')
            user_id = recording.get('user_id', 'unknown')
            print(f"  🗑️ إزالة تسجيل عالق: {username} (ID: {user_id})")
    
    print(f"\n{'=' * 60}")
    print(f"📈 النتيجة:")
    print(f"  ✅ التسجيلات النشطة: {len(active_recordings)}")
    print(f"  🗑️ التسجيلات المزالة: {removed_count}")
    print(f"{'=' * 60}")
    
    # احفظ القائمة المحدثة
    save_recordings(active_recordings)
    
    return active_recordings

if __name__ == "__main__":
    try:
        cleanup_recordings()
        print("\n✅ تم التنظيف بنجاح")
    except Exception as e:
        print(f"\n❌ خطأ فادح: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
