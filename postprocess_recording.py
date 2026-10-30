#!/usr/bin/env python3
"""
postprocess_recording.py — MKV-to-MP4 conversion, segmentation, delivery.

This is PHASE 2 of the pipeline. It takes the MKV segments produced by
record_once.py (PHASE 1) and converts them to MP4 for Telegram delivery.

CRITICAL DESIGN:
  1. Discovers ALL available MKV parts (_av_part_*.mkv or *_part_*.mkv)
  2. Concatenates them in correct order into a single source
  3. Validates the concatenated source (duration, size, streams)
  4. Splits into Telegram-safe MP4 segments (< 45 MB)
  5. Sends each segment with full state tracking
  6. NEVER declares success unless coverage >= 95%
  7. NEVER re-sends segments that were already sent successfully
"""

import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
import re
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

MAX_SEGMENT_BYTES  = 45 * 1024 * 1024
TG_HARD_LIMIT      = 50 * 1024 * 1024
MARKER_SUFFIX      = ".postprocessed"
MP4_VIDEO_OK       = {"h264", "hevc", "mpeg4", "mjpeg"}
SEND_DELAY_SECONDS = 3
MAX_RETRIES        = 3

# Minimum thresholds to reject corrupt/empty files
MIN_SOURCE_SIZE_BYTES = 1 * 1024 * 1024  # 1 MB minimum
MIN_SOURCE_DURATION   = 5.0              # 5 seconds minimum
MIN_PART_SIZE_BYTES   = 100 * 1024       # 100 KB minimum per part

# Worker API configuration
WORKER_API_URL = os.environ.get("WORKER_API_URL", "").strip()
AUTO_API_TOKEN = os.environ.get("AUTO_API_TOKEN", "").strip()

if WORKER_API_URL and WORKER_API_URL.endswith('/'):
    WORKER_API_URL = WORKER_API_URL[:-1]

def log(msg: str) -> None:
    print(f"[POSTPROC] {msg}", flush=True)

def run(cmd: str, timeout: int = 900):
    try:
        return subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return None
    except Exception:
        return None

def ffprobe_json(path: str):
    r = run(f'ffprobe -v quiet -print_format json -show_format -show_streams "{path}"', 60)
    if r and r.returncode == 0:
        try:
            return json.loads(r.stdout)
        except json.JSONDecodeError:
            pass
    return None

def get_duration(info: dict) -> float:
    try:
        return float(info.get("format", {}).get("duration", 0))
    except (TypeError, ValueError):
        return 0.0

def get_duration_from_file(path: str) -> float:
    info = ffprobe_json(path)
    if info:
        return get_duration(info)
    return 0.0

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

def human_size(n: int) -> str:
    mb = n / (1024 * 1024)
    return f"{mb / 1024:.2f} GB" if mb >= 1024 else f"{mb:.1f} MB"

def format_ts(seconds: float) -> str:
    h, rem = divmod(int(seconds), 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"

def safe_id(sid: str) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in str(sid))

# ═══════════════════════════════════════════════════════════
# WORKER STATE UPDATE
# ═══════════════════════════════════════════════════════════
def update_worker_final_state(sid: str, status: str, extra: dict = None):
    """Update worker state to free the recording slot."""
    if not WORKER_API_URL or not AUTO_API_TOKEN:
        log("⚠️ Worker API not configured, skipping final state update")
        return
    url = f"{WORKER_API_URL}/api/update-state/{sid}"
    state = {"status": status}
    if extra:
        state.update(extra)
    payload = json.dumps(state).encode('utf-8')
    req = urllib.request.Request(url, data=payload, headers={
        'X-Auto-Token': AUTO_API_TOKEN,
        'Content-Type': 'application/json'
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            log(f"✓ Worker state updated: {status}")
    except Exception as e:
        log(f"⚠️ Failed to update worker state: {e}")

# ═══════════════════════════════════════════════════════════
# STATE MANAGEMENT
# ═══════════════════════════════════════════════════════════
def state_path(sid: str) -> Path:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    return STATE_DIR / f"{safe_id(sid)}.json"

def load_state(sid: str) -> dict:
    p = state_path(sid)
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"stream_id": sid, "state": "DISCOVERED", "segments": {}, "errors": []}

def save_state(sid: str, st: dict) -> None:
    p = state_path(sid)
    tmp = p.with_suffix(".tmp")
    st["updated_at"] = datetime.now(timezone.utc).isoformat()
    tmp.write_text(json.dumps(st, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(p)

# ═══════════════════════════════════════════════════════════
# SOURCE DISCOVERY — Find and concatenate ALL parts
# ═══════════════════════════════════════════════════════════
def find_all_mkv_parts() -> list:
    """Find ALL MKV parts across all directories, sorted by part number."""
    parts = []
    search_dirs = [PRESERVED_DIR, RECORDINGS_DIR, TEMP_DIR]
    
    for d in search_dirs:
        if not d.exists():
            continue
        # Look for _av_part_*.mkv pattern (from record_once.py splitting)
        for f in d.glob("*_av_part_*.mkv"):
            try:
                size = f.stat().st_size
                if size >= MIN_PART_SIZE_BYTES:
                    parts.append((f, size))
            except OSError:
                continue
        # Also look for _part_*.mkv pattern
        for f in d.glob("*_part_*.mkv"):
            if "_av_part_" in f.name:
                continue  # Already counted above
            try:
                size = f.stat().st_size
                if size >= MIN_PART_SIZE_BYTES:
                    parts.append((f, size))
            except OSError:
                continue
    
    if not parts:
        return []
    
    # Sort by part number extracted from filename
    def get_part_num(entry):
        f = entry[0]
        m = re.search(r'_part_(\d+)', f.name)
        if m:
            return int(m.group(1))
        return 0
    
    parts.sort(key=get_part_num)
    return parts

def find_single_mkv_source() -> list:
    """Find a single complete MKV file (not parts)."""
    candidates = []
    search_dirs = [PRESERVED_DIR, RECORDINGS_DIR, TEMP_DIR]
    
    for d in search_dirs:
        if not d.exists():
            continue
        for f in d.glob("*.mkv"):
            name = f.name
            # Skip parts, encoded, and concatenated files
            if "_part_" in name or "_encoded" in name or "concatenated_" in name:
                continue
            try:
                size = f.stat().st_size
                if size >= MIN_SOURCE_SIZE_BYTES:
                    candidates.append((f, size))
            except OSError:
                continue
    
    if not candidates:
        return []
    
    # Sort by size descending
    candidates.sort(key=lambda x: x[1], reverse=True)
    
    # Validate and return the best one
    for f, size in candidates:
        info = ffprobe_json(str(f))
        if info is None:
            continue
        dur = get_duration(info)
        vc, ac = get_codecs(info)
        if vc and dur >= MIN_SOURCE_DURATION:
            log(f"  ✓ Single source: {f.name} ({human_size(size)}, {dur:.1f}s)")
            return [f]
    
    return []

def concatenate_parts(parts: list, sid: str) -> str:
    """Concatenate MKV parts into a single file with validation."""
    if not parts:
        return None
    
    log(f"🔗 Concatenating {len(parts)} parts...")
    for i, (f, size) in enumerate(parts):
        log(f"  Part {i+1}: {f.name} ({human_size(size)})")
    
    TEMP_DIR.mkdir(parents=True, exist_ok=True)
    concat_file = TEMP_DIR / f"concat_{safe_id(sid)}.txt"
    
    with open(concat_file, "w", encoding="utf-8") as f:
        for part_path, size in parts:
            f.write(f"file '{part_path}'\n")
    
    out_file = TEMP_DIR / f"concatenated_{safe_id(sid)}.mkv"
    
    # Check if already concatenated and valid
    if out_file.exists() and out_file.stat().st_size > MIN_SOURCE_SIZE_BYTES:
        info = ffprobe_json(str(out_file))
        if info:
            dur = get_duration(info)
            vc, _ = get_codecs(info)
            if vc and dur >= MIN_SOURCE_DURATION:
                log(f"✓ Concatenated file already exists: {out_file.name} ({human_size(out_file.stat().st_size)}, {dur:.1f}s)")
                return str(out_file)
    
    # Create new concatenation
    cmd = f'ffmpeg -y -f concat -safe 0 -i "{concat_file}" -c copy "{out_file}"'
    r = run(cmd, timeout=1800)
    
    if r is None or r.returncode != 0:
        log(f"✗ Concatenation failed: {(r.stderr or '')[-300:] if r else 'timeout'}")
        return None
    
    if not out_file.exists():
        log("✗ Concatenation produced no output file")
        return None
    
    # Validate the result
    out_size = out_file.stat().st_size
    if out_size < MIN_SOURCE_SIZE_BYTES:
        log(f"✗ Concatenated file too small: {human_size(out_size)}")
        return None
    
    info = ffprobe_json(str(out_file))
    if not info:
        log("✗ Cannot probe concatenated file")
        return None
    
    dur = get_duration(info)
    vc, ac = get_codecs(info)
    
    if not vc:
        log("✗ Concatenated file has no video")
        return None
    
    if dur < MIN_SOURCE_DURATION:
        log(f"✗ Concatenated file too short: {dur:.1f}s")
        return None
    
    # Calculate expected duration from parts
    expected_duration = 0.0
    for part_path, size in parts:
        part_dur = get_duration_from_file(str(part_path))
        expected_duration += part_dur
    
    log(f"✓ Concatenation complete: {out_file.name}")
    log(f"  Size: {human_size(out_size)}")
    log(f"  Duration: {dur:.1f}s (expected ~{expected_duration:.1f}s)")
    log(f"  Video: {vc}, Audio: {ac or 'NONE'}")
    
    if dur < expected_duration * 0.8:
        log(f"⚠️ WARNING: Duration mismatch! Got {dur:.1f}s, expected ~{expected_duration:.1f}s")
    
    return str(out_file)

def discover_source(sid: str) -> str:
    """
    Discover the best source for processing.
    Priority:
    1. Multiple parts → concatenate them
    2. Single large MKV file
    3. Concatenated parts if available
    """
    log("🔍 Discovering source files...")
    
    # Strategy 1: Find and concatenate parts
    parts = find_all_mkv_parts()
    if parts and len(parts) > 1:
        log(f"📦 Found {len(parts)} parts, will concatenate")
        total_size = sum(size for _, size in parts)
        log(f"  Total parts size: {human_size(total_size)}")
        concat_path = concatenate_parts(parts, sid)
        if concat_path:
            return concat_path
        log("⚠️ Concatenation failed, falling back to single source")
    
    # Strategy 2: Find single MKV source
    single = find_single_mkv_source()
    if single:
        return str(single[0])
    
    # Strategy 3: Single part only
    if parts and len(parts) == 1:
        f, size = parts[0]
        info = ffprobe_json(str(f))
        if info:
            dur = get_duration(info)
            vc, _ = get_codecs(info)
            if vc and dur >= MIN_SOURCE_DURATION:
                log(f"✓ Using single part as source: {f.name}")
                return str(f)
    
    log("✗ No valid source found")
    return None

# ═══════════════════════════════════════════════════════════
# SEGMENTATION
# ═══════════════════════════════════════════════════════════
def calc_segment_duration(file_size: int, duration: float) -> float:
    if duration <= 0 or file_size <= 0:
        return 60.0
    if file_size <= MAX_SEGMENT_BYTES:
        return duration + 10
    bitrate = file_size / duration
    max_dur = MAX_SEGMENT_BYTES / bitrate
    safe_dur = max_dur * 0.75
    return max(safe_dur, 10.0)

def split_to_segments(src: str, outdir: str, seg_dur: float) -> list:
    pattern = os.path.join(outdir, "segment_%03d.mp4")
    cmd = (
        f'ffmpeg -y -i "{src}" '
        f'-c:v copy -c:a aac -b:a 128k '
        f'-f segment -segment_time {seg_dur:.1f} '
        f'-reset_timestamps 1 -movflags +faststart '
        f'"{pattern}"'
    )
    log(f"Splitting: seg_time={seg_dur:.1f}s, src={human_size(os.path.getsize(src))}")
    r = run(cmd, timeout=1800)
    if r is None or r.returncode != 0:
        log(f"Split failed: {(r.stderr or '')[-300:] if r else 'timeout'}")
        return []
    return [str(s) for s in sorted(Path(outdir).glob("segment_*.mp4")) if s.stat().st_size > 0]

def resplit_oversized(seg_path: str, outdir: str, idx: int) -> list:
    size = os.path.getsize(seg_path)
    info = ffprobe_json(seg_path)
    if not info:
        return [seg_path]
    dur = get_duration(info)
    if dur <= 0:
        return [seg_path]
    bitrate = size / dur
    target_dur = max((MAX_SEGMENT_BYTES * 0.70) / bitrate, 5.0)
    log(f"Re-splitting segment {idx}: {human_size(size)} → ~{target_dur:.0f}s parts")
    pattern = os.path.join(outdir, f"resplit_{idx:03d}_%03d.mp4")
    cmd = (
        f'ffmpeg -y -i "{seg_path}" '
        f'-c:v copy -c:a aac -b:a 128k '
        f'-f segment -segment_time {target_dur:.1f} '
        f'-reset_timestamps 1 -movflags +faststart '
        f'"{pattern}"'
    )
    r = run(cmd, timeout=600)
    if r and r.returncode == 0:
        subs = [str(s) for s in sorted(Path(outdir).glob(f"resplit_{idx:03d}_*.mp4")) if s.stat().st_size > 0]
        if subs:
            log(f"Re-split produced {len(subs)} sub-segments")
            return subs
    return [seg_path]

def recreate_segment_from_source(src: str, start_time: float, duration: float, out_path: str) -> bool:
    """Re-create a segment from the source using exact start time and duration."""
    src_info = ffprobe_json(src)
    if not src_info:
        return False
    src_dur = get_duration(src_info)
    
    if start_time >= src_dur:
        log(f"  ✗ Start time {start_time:.1f}s >= source duration {src_dur:.1f}s")
        return False
    duration = min(duration, src_dur - start_time)
    if duration <= 0:
        return False
    
    log(f"  🔄 Recreating: {start_time:.1f}s → {start_time + duration:.1f}s")
    cmd = (
        f'ffmpeg -y -ss {start_time:.3f} -t {duration:.3f} -i "{src}" '
        f'-c:v copy -c:a aac -b:a 128k '
        f'-movflags +faststart "{out_path}"'
    )
    r = run(cmd, timeout=300)
    if r and r.returncode == 0 and os.path.exists(out_path) and os.path.getsize(out_path) > 0:
        info = ffprobe_json(out_path)
        if info:
            vc, _ = get_codecs(info)
            if vc:
                return True
    return False

def verify_segment(path: str) -> dict:
    """Verify segment has video (audio is optional)."""
    result = {"ok": False, "reason": "", "duration": 0.0, "width": 0, "height": 0, "has_audio": False}
    info = ffprobe_json(path)
    if not info:
        result["reason"] = "ffprobe failed"
        return result
    dur = get_duration(info)
    if dur <= 0:
        result["reason"] = "no duration"
        return result
    vc, ac = get_codecs(info)
    if not vc:
        result["reason"] = "no video"
        return result
    
    result["has_audio"] = bool(ac)
    result["reason"] = "ok" if ac else "ok (video only)"
    
    w, h = get_resolution(info)
    result.update({"ok": True, "duration": dur, "width": w, "height": h})
    return result

# ═══════════════════════════════════════════════════════════
# TELEGRAM SENDING
# ═══════════════════════════════════════════════════════════
def tg_send_video(video_path: str, caption: str, duration: int, width: int, height: int) -> dict:
    result = {"ok": False, "message_id": None, "error": ""}
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        result["error"] = "no credentials"
        return result
    fsize = os.path.getsize(video_path)
    if fsize > TG_HARD_LIMIT:
        result["error"] = f"too large: {human_size(fsize)}"
        return result

    boundary = uuid.uuid4().hex
    fname = os.path.basename(video_path)
    with open(video_path, "rb") as f:
        vdata = f.read()

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
            req = urllib.request.Request(url, data=body, headers={
                "Content-Type": f"multipart/form-data; boundary={boundary}"
            })
            with urllib.request.urlopen(req, timeout=300) as resp:
                if resp.status == 200:
                    data = json.loads(resp.read().decode("utf-8"))
                    result["ok"] = True
                    result["message_id"] = data.get("result", {}).get("message_id")
                    return result
        except urllib.error.HTTPError as e:
            if e.code == 403:
                result["error"] = "HTTP 403"
                return result
            result["error"] = f"HTTP {e.code}"
            log(f"sendVideo attempt {attempt}/{MAX_RETRIES}: HTTP {e.code}")
        except Exception as e:
            result["error"] = str(e)
            log(f"sendVideo attempt {attempt}/{MAX_RETRIES}: {e}")
        if attempt < MAX_RETRIES:
            wait = 5 * attempt
            log(f"Retrying in {wait}s...")
            time.sleep(wait)
    return result

# ═══════════════════════════════════════════════════════════
# MAIN PIPELINE
# ═══════════════════════════════════════════════════════════
def process_one(source_path: str, sid: str) -> bool:
    marker = source_path + MARKER_SUFFIX
    if os.path.exists(marker):
        log(f"⏭️ Already processed: {os.path.basename(source_path)}")
        return True

    st = load_state(sid)
    log("=" * 60)
    log(f"📂 Processing: {os.path.basename(source_path)} (sid={sid})")
    log("=" * 60)

    info = ffprobe_json(source_path)
    if not info:
        log("✗ ffprobe failed on source")
        st["state"] = "FAILED"
        save_state(sid, st)
        return False

    duration = get_duration(info)
    src_size = os.path.getsize(source_path)
    vc, ac = get_codecs(info)
    w, h = get_resolution(info)

    log(f"⏱️ Duration: {format_ts(duration)} ({duration:.1f}s)")
    log(f"📦 Size: {human_size(src_size)}")
    log(f"🎬 Video: {vc} {w}x{h}")
    log(f"🔊 Audio: {ac or 'NONE (video-only source)'}")

    # Validate source
    if src_size < MIN_SOURCE_SIZE_BYTES:
        log(f"✗ Source too small ({human_size(src_size)}) — REJECTING")
        st["state"] = "FAILED_SOURCE_TOO_SMALL"
        save_state(sid, st)
        return False

    if duration < MIN_SOURCE_DURATION:
        log(f"✗ Source too short ({duration:.1f}s) — REJECTING")
        st["state"] = "FAILED_SOURCE_TOO_SHORT"
        save_state(sid, st)
        return False

    if not vc:
        log("✗ Source has NO video — REJECTING")
        st["state"] = "FAILED_NO_VIDEO"
        save_state(sid, st)
        return False

    if not ac:
        log("⚠️ Source has NO audio — will send video-only segments")

    if vc and vc not in MP4_VIDEO_OK:
        log(f"✗ Codec '{vc}' not MP4-compatible")
        st["state"] = "FAILED_CODEC"
        save_state(sid, st)
        return False

    st["state"] = "PROCESSING"
    st["source_duration"] = round(duration, 1)
    st["source_size"] = src_size
    save_state(sid, st)

    seg_dur = calc_segment_duration(src_size, duration)
    log(f"📐 Segment duration: {seg_dur:.1f}s")

    with tempfile.TemporaryDirectory(prefix="pp_seg_") as segdir:
        segments = split_to_segments(source_path, segdir, seg_dur)
        if not segments:
            log("✗ No segments produced")
            st["state"] = "FAILED"
            save_state(sid, st)
            return False

        # Handle oversized segments
        final_segments = []
        for idx, seg in enumerate(segments):
            if os.path.getsize(seg) > MAX_SEGMENT_BYTES:
                log(f"⚠️ Segment {idx+1} oversized — re-splitting")
                subs = resplit_oversized(seg, segdir, idx)
                for sub in subs:
                    if os.path.getsize(sub) <= MAX_SEGMENT_BYTES:
                        final_segments.append(sub)
                    else:
                        st["errors"].append(f"oversize:{os.path.basename(sub)}")
            else:
                final_segments.append(seg)

        total = len(final_segments)
        log(f"📦 Final segments: {total}")

        if not final_segments:
            log("✗ All segments failed size check")
            st["state"] = "FAILED"
            save_state(sid, st)
            return False

        display_name = STREAM_NAME or os.path.basename(source_path).split('_')[0]
        sent_count = 0
        failed_count = 0
        retry_needed = []

        # Calculate actual start times
        cumulative_time = 0.0
        segment_times = []
        for seg_path in final_segments:
            seg_info = ffprobe_json(seg_path)
            seg_dur_actual = get_duration(seg_info) if seg_info else seg_dur
            segment_times.append((cumulative_time, seg_dur_actual))
            cumulative_time += seg_dur_actual

        # ═══════════════════════════════════════════════════
        # SEND ALL SEGMENTS
        # ═══════════════════════════════════════════════════
        for idx, seg_path in enumerate(final_segments):
            seg_name = os.path.basename(seg_path)
            seg_key = f"seg_{idx:04d}"
            seg_start, seg_dur_actual = segment_times[idx] if idx < len(segment_times) else (0, seg_dur)

            # Skip already-sent segments
            prev = st["segments"].get(seg_key, {})
            if prev.get("sent") and prev.get("message_id"):
                log(f"  [{idx+1}/{total}] Already sent (msg={prev['message_id']}) — skip")
                sent_count += 1
                continue

            # Verify segment
            verdict = verify_segment(seg_path)
            if not verdict["ok"]:
                log(f"  [{idx+1}/{total}] ✗ REJECTED: {verdict['reason']}")
                
                if "video" in verdict["reason"] or "duration" in verdict["reason"]:
                    log(f"  [{idx+1}/{total}] 🔄 Recreating from source...")
                    recreated_path = os.path.join(segdir, f"recreated_{idx:03d}.mp4")
                    if recreate_segment_from_source(source_path, seg_start, seg_dur_actual, recreated_path):
                        verdict = verify_segment(recreated_path)
                        if verdict["ok"]:
                            seg_path = recreated_path
                            seg_name = os.path.basename(recreated_path)
                            log(f"  [{idx+1}/{total}] ✓ Recreated successfully")
                        else:
                            st["segments"][seg_key] = {"sent": False, "error": verdict["reason"]}
                            st["errors"].append(f"{seg_name}:{verdict['reason']}")
                            failed_count += 1
                            save_state(sid, st)
                            continue
                    else:
                        st["segments"][seg_key] = {"sent": False, "error": verdict["reason"]}
                        st["errors"].append(f"{seg_name}:{verdict['reason']}")
                        failed_count += 1
                        save_state(sid, st)
                        continue
                else:
                    st["segments"][seg_key] = {"sent": False, "error": verdict["reason"]}
                    st["errors"].append(f"{seg_name}:{verdict['reason']}")
                    failed_count += 1
                    save_state(sid, st)
                    continue

            audio_note = ""
            if not verdict.get("has_audio", False):
                audio_note = "\n⚠️ بدون صوت"

            seg_size = os.path.getsize(seg_path)
            log(f"  [{idx+1}/{total}] {seg_name}: {human_size(seg_size)}, {verdict['duration']:.1f}s, audio={'YES' if verdict.get('has_audio') else 'NO'} ✓")

            if seg_size > TG_HARD_LIMIT:
                st["segments"][seg_key] = {"sent": False, "error": "exceeds_tg_limit"}
                failed_count += 1
                save_state(sid, st)
                continue

            caption = (
                f"🎬 {display_name}\n"
                f"🆔 {sid}\n"
                f"📎 المقطع {idx+1} من {total}\n"
                f"⏱️ {format_ts(verdict['duration'])}"
                f"{audio_note}"
            )

            tg_res = tg_send_video(
                seg_path, caption,
                int(verdict["duration"]),
                verdict["width"], verdict["height"]
            )

            if tg_res["ok"]:
                log(f"  [{idx+1}/{total}] 📤 SENT ✅ (msg={tg_res['message_id']})")
                sent_count += 1
                st["segments"][seg_key] = {
                    "sent": True,
                    "message_id": tg_res["message_id"],
                    "size": seg_size,
                    "duration": round(verdict["duration"], 1),
                    "has_audio": verdict.get("has_audio", False),
                    "start_time": round(seg_start, 1),
                }
            else:
                log(f"  [{idx+1}/{total}] ✗ Send failed: {tg_res['error']}")
                st["segments"][seg_key] = {"sent": False, "error": tg_res["error"]}
                st["errors"].append(f"{seg_name}:{tg_res['error']}")
                failed_count += 1
                retry_needed.append((seg_key, seg_path, verdict, seg_start))

            save_state(sid, st)

            if idx < total - 1:
                time.sleep(SEND_DELAY_SECONDS)

        # Retry failed segments
        if retry_needed:
            log(f"\n🔄 Retrying {len(retry_needed)} failed segments...")
            for seg_key, seg_path, verdict, seg_start in retry_needed:
                if st["segments"].get(seg_key, {}).get("sent"):
                    continue
                
                seg_size = os.path.getsize(seg_path)
                audio_note = "\n⚠️ بدون صوت" if not verdict.get("has_audio", False) else ""
                
                caption = (
                    f"🎬 {display_name}\n"
                    f"🆔 {sid}\n"
                    f"📎 المقطع {int(seg_key.split('_')[1])+1} من {total}\n"
                    f"⏱️ {format_ts(verdict['duration'])}"
                    f"{audio_note}"
                )
                
                tg_res = tg_send_video(
                    seg_path, caption,
                    int(verdict["duration"]),
                    verdict["width"], verdict["height"]
                )
                
                if tg_res["ok"]:
                    log(f"  🔄 [{seg_key}] 📤 SENT on retry ✅")
                    sent_count += 1
                    failed_count -= 1
                    st["segments"][seg_key] = {
                        "sent": True,
                        "message_id": tg_res["message_id"],
                        "size": seg_size,
                        "duration": round(verdict["duration"], 1),
                        "has_audio": verdict.get("has_audio", False),
                        "start_time": round(seg_start, 1),
                    }
                else:
                    log(f"  🔄 [{seg_key}] ✗ Retry failed: {tg_res['error']}")
                
                save_state(sid, st)
                time.sleep(SEND_DELAY_SECONDS)

    # Coverage Check
    total_sent_duration = sum(seg.get("duration", 0) for seg in st["segments"].values() if seg.get("sent"))
    coverage_ratio = min(total_sent_duration / duration, 1.0) if duration > 0 else 0
    
    log(f"\n{'=' * 60}")
    log(f"RESULT: {sent_count}/{total} sent, {failed_count} failed")
    log(f"COVERAGE: {total_sent_duration:.1f}s / {duration:.1f}s ({coverage_ratio*100:.1f}%)")
    log(f"{'=' * 60}")

    if coverage_ratio < 0.95:
        log("⚠️ COVERAGE INCOMPLETE (< 95%): Source preserved for retry.")
        st["state"] = "PARTIAL_COVERAGE"
        st["coverage_ratio"] = round(coverage_ratio, 3)
        save_state(sid, st)
        return False

    if sent_count > 0 and failed_count == 0 and coverage_ratio >= 0.95:
        st["state"] = "SENT"
        with open(marker, "w") as f:
            json.dump({
                "sent_at": datetime.now(timezone.utc).isoformat(), 
                "segments": sent_count,
                "coverage": round(coverage_ratio, 3),
                "source_duration": round(duration, 1),
            }, f)
        log("✅ ALL segments sent successfully with full coverage")
        update_worker_final_state(sid, "completed", {
            "completed_at": datetime.now(timezone.utc).isoformat(),
            "segments_sent": sent_count,
            "coverage": round(coverage_ratio, 3),
        })
        return True
    elif sent_count > 0:
        st["state"] = "PARTIAL"
        save_state(sid, st)
        log(f"⚠️ PARTIAL: {sent_count}/{total} sent.")
        return False
    else:
        st["state"] = "FAILED"
        save_state(sid, st)
        log("✗ No segments sent.")
        update_worker_final_state(sid, "failed", {
            "failed_at": datetime.now(timezone.utc).isoformat(),
            "error": "no_segments_sent",
        })
        return False

def main():
    log("=" * 60)
    log("Post-processing pipeline — PHASE 2")
    log(f"STREAM_ID={STREAM_ID}")
    log("=" * 60)

    source_path = discover_source(STREAM_ID)
    
    if not source_path:
        log("✗ No valid source found — ABORTING")
        update_worker_final_state(STREAM_ID, "failed", {
            "failed_at": datetime.now(timezone.utc).isoformat(),
            "error": "no_source_found",
        })
        return

    try:
        success = process_one(source_path, STREAM_ID)
        if success:
            log("✅ Post-processing completed successfully")
        else:
            log("⚠️ Post-processing incomplete — source preserved for retry")
    except Exception as e:
        log(f"❌ Fatal error: {e}")
        import traceback
        traceback.print_exc()

    log("Source files NEVER deleted by this pipeline.")

if __name__ == "__main__":
    main()
