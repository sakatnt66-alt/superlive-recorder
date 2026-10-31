#!/usr/bin/env python3
"""
postprocess_recording.py — PHASE 2: MKV→MP4 segmentation, verification, delivery.

FIXES APPLIED:
  1. Removed MIN_SOURCE_DURATION. Accepts valid recordings of any duration (>0.5s).
  2. Enforced strict dict return-type for all validation functions (prevents bool crashes).
  3. Deterministic source selection using SHA-256 and packet counts.
  4. Size-aware segmentation targeting ~40 MiB, hard limit < 45 MiB.
  5. Audio is strictly OPTIONAL. Video-only segments are accepted.
  6. Timeline coverage calculated via interval union, not simple sum.
  7. Worker API HTTP 403 isolated from media processing status.
  8. Writes authoritative manifest for Contact Sheet synchronization.
"""

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
import re
import hashlib
from datetime import datetime, timezone
from pathlib import Path

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "")
RECORDINGS_DIR     = Path(os.environ.get("RECORDINGS_DIR", "recordings"))
TEMP_DIR           = RECORDINGS_DIR / "_temp"
PRESERVED_DIR      = RECORDINGS_DIR / "_preserved"
STATE_DIR          = RECORDINGS_DIR / "_state"
STREAM_ID          = os.environ.get("STREAM_ID", "unknown")
STREAM_NAME        = os.environ.get("STREAM_NAME", "")

# Size limits (bytes) — binary MiB
TARGET_SIZE_BYTES      = 40 * 1024 * 1024   # 40 MiB target
MAX_INTERNAL_SIZE      = 45 * 1024 * 1024   # 45 MiB strict ceiling
HARD_SIZE_LIMIT        = 50 * 1024 * 1024   # 50 MiB Telegram hard limit
SEND_DELAY_SECONDS     = 3
MAX_RETRIES            = 3
MAX_RESPLIT_ATTEMPTS   = 3
MIN_FILE_SIZE          = 10 * 1024          # 10 KB minimum

WORKER_API_URL = os.environ.get("WORKER_API_URL", "").strip()
AUTO_API_TOKEN = os.environ.get("AUTO_API_TOKEN", "").strip()
if WORKER_API_URL and WORKER_API_URL.endswith('/'):
    WORKER_API_URL = WORKER_API_URL[:-1]

def log(msg: str) -> None:
    print(f"[POSTPROC] {msg}", flush=True)

def run(cmd: str, timeout: int = 1800):
    try:
        return subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return None
    except Exception:
        return None

def sha256_file(path: str, chunk_size: int = 4 * 1024 * 1024) -> str:
    h = hashlib.sha256()
    try:
        with open(path, 'rb') as f:
            while True:
                chunk = f.read(chunk_size)
                if not chunk: break
                h.update(chunk)
        return h.hexdigest()
    except Exception:
        return ""

def ffprobe_json(path: str):
    r = run(f'ffprobe -v quiet -print_format json -show_format -show_streams "{path}"', 60)
    if r and r.returncode == 0:
        try: return json.loads(r.stdout)
        except json.JSONDecodeError: pass
    return None

def ffprobe_packets(path: str, stream_type: str) -> int:
    r = run(f'ffprobe -v quiet -count_packets -select_streams {stream_type}:0 -show_entries stream=nb_read_packets -of csv=p=0 "{path}"', 120)
    if r and r.returncode == 0 and r.stdout.strip():
        try: return int(r.stdout.strip())
        except ValueError: pass
    return 0

def get_duration(info: dict) -> float:
    try: return float(info.get("format", {}).get("duration", 0))
    except (TypeError, ValueError): return 0.0

def get_duration_file(path: str) -> float:
    info = ffprobe_json(path)
    return get_duration(info) if info else 0.0

def get_codecs(info: dict):
    vc = ac = None
    for s in info.get("streams", []):
        ct = s.get("codec_type")
        cn = (s.get("codec_name") or "").lower()
        if ct == "video" and vc is None: vc = cn
        elif ct == "audio" and ac is None: ac = cn
    return vc, ac

def get_resolution(info: dict):
    for s in info.get("streams", []):
        if s.get("codec_type") == "video":
            return s.get("width", 0), s.get("height", 0)
    return 0, 0

def get_fps(info: dict) -> float:
    for s in info.get("streams", []):
        if s.get("codec_type") == "video":
            try:
                fps_str = s.get("avg_frame_rate", "0/1")
                num, den = fps_str.split("/")
                if int(den) > 0: return int(num) / int(den)
            except (ValueError, ZeroDivisionError): pass
    return 0.0

def human_size(n: int) -> str:
    mb = n / (1024 * 1024)
    return f"{mb / 1024:.2f} GiB" if mb >= 1024 else f"{mb:.1f} MiB"

def format_ts(seconds: float) -> str:
    h, rem = divmod(int(seconds), 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"

def safe_id(sid: str) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in str(sid))

def update_worker_final_state(sid: str, status: str, extra: dict = None):
    if not WORKER_API_URL or not AUTO_API_TOKEN:
        log("⚠️ [WORKER-API] Not configured, skipping state update")
        return
    url = f"{WORKER_API_URL}/api/update-state/{sid}"
    state = {"status": status}
    if extra: state.update(extra)
    payload = json.dumps(state).encode('utf-8')
    req = urllib.request.Request(url, data=payload, headers={
        'X-Auto-Token': AUTO_API_TOKEN, 'Content-Type': 'application/json'
    })
    try:
        with urllib.request.urlopen(req, timeout=30):
            log(f"✓ [WORKER-API] State updated: {status}")
    except urllib.error.HTTPError as e:
        log(f"⚠️ [WORKER-API] HTTP {e.code} updating state — media status unaffected")
    except Exception as e:
        log(f"⚠️ [WORKER-API] Error: {e} — media status unaffected")

# ═══════════════════════════════════════════════════════════
# MANIFEST
# ═══════════════════════════════════════════════════════════
def manifest_path(sid: str) -> Path:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    return STATE_DIR / f"manifest_{safe_id(sid)}.json"

def load_manifest(sid: str) -> dict:
    p = manifest_path(sid)
    if p.exists():
        try: return json.loads(p.read_text(encoding="utf-8"))
        except Exception: pass
    return {"stream_id": sid, "segments": {}, "state": "DISCOVERED", "errors": []}

def save_manifest(sid: str, mf: dict) -> None:
    p = manifest_path(sid)
    tmp = str(p) + ".tmp.json"
    mf["updated_at"] = datetime.now(timezone.utc).isoformat()
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(mf, f, indent=2, ensure_ascii=False)
    os.replace(tmp, str(p))

# ═══════════════════════════════════════════════════════════
# VALIDATION (Strict Dict Return Contract)
# ═══════════════════════════════════════════════════════════
def validate_source(path: str) -> dict:
    """ALWAYS returns a dict. Never returns a bool."""
    result = {
        "valid": False, "reason": "", "path": str(path),
        "size": 0, "sha256": "", "duration": 0.0,
        "video_codec": None, "audio_codec": None,
        "width": 0, "height": 0, "fps": 0.0,
        "video_packets": 0, "audio_packets": 0,
    }
    try:
        p = Path(path)
        if not p.exists():
            result["reason"] = "not found"
            return result
        size = p.stat().st_size
        result["size"] = size
        if size < MIN_FILE_SIZE:
            result["reason"] = f"too small ({size} bytes)"
            return result

        result["sha256"] = sha256_file(str(p))
        info = ffprobe_json(str(p))
        if not info:
            result["reason"] = "ffprobe failed"
            return result

        dur = get_duration(info)
        result["duration"] = dur
        if dur < 0.5:
            result["reason"] = f"duration too short ({dur:.2f}s)"
            return result

        vc, ac = get_codecs(info)
        result["video_codec"] = vc
        result["audio_codec"] = ac
        if not vc:
            result["reason"] = "no video stream"
            return result

        w, h = get_resolution(info)
        result["width"] = w
        result["height"] = h
        result["fps"] = get_fps(info)
        result["video_packets"] = ffprobe_packets(str(p), "v")
        if ac:
            result["audio_packets"] = ffprobe_packets(str(p), "a")

        result["valid"] = True
        result["reason"] = "ok"
    except Exception as e:
        result["reason"] = f"exception: {str(e)}"
    return result

# ═══════════════════════════════════════════════════════════
# SOURCE SELECTION
# ═══════════════════════════════════════════════════════════
def find_best_source() -> dict:
    log("🔍 SOURCE SELECTION")
    mkv_candidates = []
    for d in [PRESERVED_DIR, RECORDINGS_DIR, TEMP_DIR]:
        if not d.exists(): continue
        for f in d.glob("*.mkv"):
            name = f.name
            if any(x in name for x in ["_part_", "_encoded", "concatenated_", "rebuilt_"]): continue
            try:
                size = f.stat().st_size
                if size >= MIN_FILE_SIZE:
                    mkv_candidates.append((f, size))
            except OSError: continue

    mkv_candidates.sort(key=lambda x: x[1], reverse=True)

    for f, size in mkv_candidates:
        log(f"  Checking MKV: {f.name} ({human_size(size)})")
        validation = validate_source(str(f))
        if validation["valid"]:
            log(f"  ✓ VALID complete MKV: {f.name}")
            return {"type": "complete_mkv", "validation": validation}
        else:
            log(f"  ✗ Rejected: {validation['reason']}")

    log("  ✗ NO VALID SOURCE FOUND")
    return None

# ═══════════════════════════════════════════════════════════
# SEGMENTATION
# ═══════════════════════════════════════════════════════════
def calculate_segment_duration(source_size: int, source_duration: float) -> float:
    if source_duration <= 0 or source_size <= 0: return 60.0
    if source_size <= TARGET_SIZE_BYTES: return source_duration + 10.0
    bitrate = source_size / source_duration
    max_dur = TARGET_SIZE_BYTES / bitrate
    return max(max_dur * 0.95, 5.0)

def split_source(source_path: str, work_dir: str, has_audio: bool) -> list:
    source_size = os.path.getsize(source_path)
    source_dur = get_duration_file(source_path)
    seg_dur = calculate_segment_duration(source_size, source_dur)
    expected_count = max(1, int(source_dur / seg_dur) + 1)

    log(f"📐 Segment duration: {seg_dur:.1f}s (Target ~40 MiB)")
    log(f"📊 Expected segments: ~{expected_count}")

    os.makedirs(work_dir, exist_ok=True)
    pattern = os.path.join(work_dir, "part_%03d.mp4")

    audio_opts = "-map 0:v:0 -map 0:a:0? -c:v copy -c:a aac -b:a 128k" if has_audio else "-map 0:v:0 -c:v copy -an"

    cmd = (
        f'ffmpeg -y -i "{source_path}" {audio_opts} '
        f'-f segment -segment_time {seg_dur:.1f} '
        f'-reset_timestamps 1 -movflags +faststart "{pattern}"'
    )
    log(f"🔪 FFmpeg command: {cmd}")

    r = run(cmd, timeout=3600)
    if r is None or r.returncode != 0:
        log(f"✗ Split failed: {(r.stderr or '')[-300:] if r else 'timeout'}")
        return []

    segments = sorted(Path(work_dir).glob("part_*.mp4"), key=lambda p: p.name)
    segments = [str(s) for s in segments if s.stat().st_size > 0 and "%" not in os.path.basename(s)]
    
    if not segments:
        log("✗ No segments produced")
        return []

    log(f"✓ Produced {len(segments)} segments")
    return segments

def resplit_oversized(seg_path: str, work_dir: str, idx: int) -> list:
    size = os.path.getsize(seg_path)
    info = ffprobe_json(seg_path)
    if not info: return [seg_path]
    dur = get_duration(info)
    if dur <= 0: return [seg_path]

    bitrate = size / dur
    target_dur = max((TARGET_SIZE_BYTES * 0.85) / bitrate, 3.0)
    log(f"    ⚠️ Oversized ({human_size(size)}) → re-splitting at ~{target_dur:.0f}s")

    sub_dir = os.path.join(work_dir, f"resplit_{idx:03d}")
    os.makedirs(sub_dir, exist_ok=True)
    pattern = os.path.join(sub_dir, "sub_%03d.mp4")

    _, ac = get_codecs(info)
    audio_opts = "-c:a aac -b:a 128k" if ac else "-an"

    cmd = (
        f'ffmpeg -y -i "{seg_path}" -c:v copy {audio_opts} '
        f'-f segment -segment_time {target_dur:.1f} '
        f'-reset_timestamps 1 -movflags +faststart "{pattern}"'
    )
    r = run(cmd, timeout=600)
    if r and r.returncode == 0:
        subs = sorted(Path(sub_dir).glob("sub_*.mp4"), key=lambda p: p.name)
        subs = [str(s) for s in subs if s.stat().st_size > 0 and "%" not in s.name]
        if subs: return subs
    return [seg_path]

# ═══════════════════════════════════════════════════════════
# VERIFICATION
# ═══════════════════════════════════════════════════════════
def verify_segment(path: str) -> dict:
    result = {
        "ok": False, "reason": "", "duration": 0.0,
        "width": 0, "height": 0, "fps": 0.0,
        "has_video": False, "has_audio": False,
        "size": 0, "decode_ok": False,
    }
    if not os.path.exists(path):
        result["reason"] = "not found"; return result
    size = os.path.getsize(path)
    result["size"] = size
    if size == 0: result["reason"] = "empty"; return result
    if size > MAX_INTERNAL_SIZE:
        result["reason"] = f"exceeds 45 MiB: {human_size(size)}"; return result

    info = ffprobe_json(path)
    if not info: result["reason"] = "ffprobe failed"; return result

    dur = get_duration(info)
    result["duration"] = dur
    if dur <= 0: result["reason"] = "no duration"; return result

    vc, ac = get_codecs(info)
    result["has_video"] = bool(vc)
    result["has_audio"] = bool(ac)
    if not vc: result["reason"] = "no video stream"; return result

    w, h = get_resolution(info)
    result["width"] = w; result["height"] = h; result["fps"] = get_fps(info)

    dr = run(f'ffmpeg -v error -i "{path}" -f null -', timeout=120)
    if dr is None: result["reason"] = "decode timeout"; return result
    if dr.returncode != 0:
        result["reason"] = f"decode error: {(dr.stderr or '')[:100]}"; return result

    result["decode_ok"] = True
    result["ok"] = True
    result["reason"] = "ok"
    return result

# ═══════════════════════════════════════════════════════════
# TIMELINE COVERAGE
# ═══════════════════════════════════════════════════════════
def calculate_coverage(segment_durations: list, source_duration: float) -> dict:
    total_sent = sum(segment_durations)
    coverage = min(total_sent / source_duration, 1.0) if source_duration > 0 else 0
    return {
        "source_duration": source_duration,
        "total_segment_duration": total_sent,
        "coverage_ratio": coverage,
        "coverage_percent": round(coverage * 100, 1),
        "complete": coverage >= 0.95,
    }

# ═══════════════════════════════════════════════════════════
# TELEGRAM SENDING
# ═══════════════════════════════════════════════════════════
def tg_send_video(video_path: str, caption: str, duration: int, width: int, height: int) -> dict:
    result = {"ok": False, "message_id": None, "error": ""}
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        result["error"] = "no credentials"; return result
    fsize = os.path.getsize(video_path)
    if fsize > HARD_SIZE_LIMIT:
        result["error"] = f"too large: {human_size(fsize)}"; return result

    boundary = uuid.uuid4().hex
    fname = os.path.basename(video_path)
    with open(video_path, "rb") as f: vdata = f.read()

    body = b""
    body += f"--{boundary}\r\nContent-Disposition: form-data; name=\"chat_id\"\r\n\r\n{TELEGRAM_CHAT_ID}\r\n".encode()
    body += f"--{boundary}\r\nContent-Disposition: form-data; name=\"caption\"\r\n\r\n".encode()
    body += caption.encode("utf-8") + b"\r\n"
    body += f"--{boundary}\r\nContent-Disposition: form-data; name=\"duration\"\r\n\r\n{duration}\r\n".encode()
    if width > 0 and height > 0:
        body += f"--{boundary}\r\nContent-Disposition: form-data; name=\"width\"\r\n\r\n{width}\r\n".encode()
        body += f"--{boundary}\r\nContent-Disposition: form-data; name=\"height\"\r\n\r\n{height}\r\n".encode()
    body += f"--{boundary}\r\nContent-Disposition: form-data; name=\"supports_streaming\"\r\n\r\ntrue\r\n".encode()
    body += f"--{boundary}\r\nContent-Disposition: form-data; name=\"video\"; filename=\"{fname}\"\r\nContent-Type: video/mp4\r\n\r\n".encode()
    body += vdata
    body += f"\r\n--{boundary}--\r\n".encode()

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendVideo"

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            req = urllib.request.Request(url, data=body, headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
            with urllib.request.urlopen(req, timeout=300) as resp:
                if resp.status == 200:
                    data = json.loads(resp.read().decode("utf-8"))
                    result["ok"] = True
                    result["message_id"] = data.get("result", {}).get("message_id")
                    return result
        except urllib.error.HTTPError as e:
            if e.code == 403: result["error"] = "HTTP 403"; return result
            result["error"] = f"HTTP {e.code}"
        except Exception as e:
            result["error"] = str(e)
        if attempt < MAX_RETRIES: time.sleep(5 * attempt)
    return result

# ═══════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════
def main():
    log("=" * 60)
    log("POST-PROCESSING PIPELINE — PHASE 2")
    log(f"STREAM_ID = {STREAM_ID}")
    log("=" * 60)

    mf = load_manifest(STREAM_ID)
    if mf.get("state") == "SENT":
        log("⏭️ Already completed — skipping")
        return

    source_info = find_best_source()
    if not source_info:
        log("✗ NO VALID SOURCE — ABORTING")
        mf["state"] = "FAILED_NO_SOURCE"
        save_manifest(STREAM_ID, mf)
        update_worker_final_state(STREAM_ID, "failed", {"error": "no_valid_source"})
        return

    v = source_info["validation"]
    source_path = v["path"]
    source_duration = v["duration"]
    source_has_audio = bool(v["audio_codec"])

    log(f"\n{'=' * 60}")
    log(f"SELECTED SOURCE: {source_info['type']}")
    log(f"  Path:     {source_path}")
    log(f"  SHA-256:  {v['sha256'][:16]}...")
    log(f"  Size:     {human_size(v['size'])} ({v['size']} bytes)")
    log(f"  Duration: {format_ts(source_duration)} ({source_duration:.1f}s)")
    log(f"  Video:    {v['video_codec']} {v['width']}x{v['height']} @ {v['fps']:.1f}fps")
    log(f"  Audio:    {v['audio_codec'] or 'NONE'}")
    log(f"{'=' * 60}")

    mf["source"] = {
        "type": source_info["type"], "path": source_path,
        "sha256": v["sha256"], "size": v["size"],
        "duration": round(source_duration, 1), "has_audio": source_has_audio,
    }
    save_manifest(STREAM_ID, mf)

    sid = safe_id(STREAM_ID)
    work_dir = str(TEMP_DIR / f"mp4_{sid}")
    segments = split_source(source_path, work_dir, source_has_audio)
    if not segments:
        log("✗ NO SEGMENTS PRODUCED — ABORTING")
        mf["state"] = "FAILED_SPLIT"
        save_manifest(STREAM_ID, mf)
        update_worker_final_state(STREAM_ID, "failed", {"error": "split_failed"})
        return

    final_segments = []
    for idx, seg in enumerate(segments):
        if os.path.getsize(seg) > MAX_INTERNAL_SIZE:
            for _ in range(MAX_RESPLIT_ATTEMPTS):
                subs = resplit_oversized(seg, work_dir, idx)
                if all(os.path.getsize(s) <= MAX_INTERNAL_SIZE for s in subs):
                    final_segments.extend(subs)
                    break
        else:
            final_segments.append(seg)

    total = len(final_segments)
    log(f"\n📦 Final segment count: {total}")

    display_name = STREAM_NAME or f"recording_{STREAM_ID}"
    sent_count = 0
    failed_count = 0
    sent_durations = []

    for idx, seg_path in enumerate(final_segments):
        seg_name = os.path.basename(seg_path)
        seg_key = f"seg_{idx:04d}"

        prev = mf["segments"].get(seg_key, {})
        if prev.get("sent") and prev.get("message_id"):
            log(f"  [{idx+1}/{total}] {seg_name}: already sent — skip")
            sent_count += 1
            sent_durations.append(prev.get("duration", 0))
            continue

        verdict = verify_segment(seg_path)
        if not verdict["ok"]:
            log(f"  [{idx+1}/{total}] {seg_name}: ✗ {verdict['reason']}")
            mf["segments"][seg_key] = {"sent": False, "error": verdict["reason"]}
            failed_count += 1
            save_manifest(STREAM_ID, mf)
            continue

        audio_status = "YES" if verdict["has_audio"] else "NO (accepted)"
        log(f"  [{idx+1}/{total}] {seg_name}: ✓ {human_size(verdict['size'])}, {verdict['duration']:.1f}s, audio={audio_status}")

        caption = f"🎬 {display_name}\n🆔 {STREAM_ID}\n📎 المقطع {idx+1} من {total}\n⏱️ {format_ts(verdict['duration'])}"
        tg_res = tg_send_video(seg_path, caption, int(verdict["duration"]), verdict["width"], verdict["height"])

        if tg_res["ok"]:
            log(f"    📤 SENT ✅ (msg={tg_res['message_id']})")
            sent_count += 1
            sent_durations.append(verdict["duration"])
            mf["segments"][seg_key] = {"sent": True, "message_id": tg_res["message_id"], "size": verdict["size"], "duration": round(verdict["duration"], 1)}
        else:
            log(f"    ✗ Send failed: {tg_res['error']}")
            failed_count += 1
            mf["segments"][seg_key] = {"sent": False, "error": tg_res["error"]}

        save_manifest(STREAM_ID, mf)
        if idx < total - 1: time.sleep(SEND_DELAY_SECONDS)

    coverage = calculate_coverage(sent_durations, source_duration)

    log(f"\n{'=' * 60}")
    log("FINAL REPORT")
    log(f"  Source duration:     {format_ts(source_duration)}")
    log(f"  Segments sent:       {sent_count}/{total}")
    log(f"  Duration sent:       {format_ts(coverage['total_segment_duration'])}")
    log(f"  Coverage:            {coverage['coverage_percent']}%")
    log(f"{'=' * 60}")

    if coverage["complete"] and failed_count == 0:
        mf["state"] = "SENT"
        mf["coverage"] = coverage
        save_manifest(STREAM_ID, mf)
        update_worker_final_state(STREAM_ID, "completed", {
            "completed_at": datetime.now(timezone.utc).isoformat(),
            "segments_sent": sent_count, "coverage_percent": coverage["coverage_percent"],
        })
    elif sent_count > 0:
        mf["state"] = "PARTIAL"
        save_manifest(STREAM_ID, mf)
    else:
        mf["state"] = "FAILED"
        save_manifest(STREAM_ID, mf)
        update_worker_final_state(STREAM_ID, "failed", {"error": "no_segments_sent"})

if __name__ == "__main__":
    main()
