#!/usr/bin/env python3
"""
postprocess_recording.py — PHASE 2: Per-part MKV→MP4 conversion, delivery.

ARCHITECTURE: Per-part independent processing.
- Discovers ALL parts for this recording
- Converts each part independently (no massive concat needed)
- Validates each output before sending
- Tracks state per-part for safe resume
- Never falls back to a small/incomplete source without proof

VIDEO: Stream copy (no re-encode) — preserves H.264, FPS, resolution exactly.
AUDIO: Re-encoded to AAC 128k for MP4/Telegram compatibility.
       Opus cannot be stream-copied into MP4 container reliably.
       This is NOT bit-perfect but is high quality.
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

MAX_SEGMENT_BYTES  = 45 * 1024 * 1024  # Safe limit under Telegram's 50MB
TG_HARD_LIMIT      = 50 * 1024 * 1024
MARKER_SUFFIX      = ".postprocessed"
MP4_VIDEO_OK       = {"h264", "hevc", "mpeg4", "mjpeg"}
SEND_DELAY_SECONDS = 3
MAX_RETRIES        = 3

# Minimum thresholds
MIN_PART_SIZE_BYTES    = 100 * 1024        # 100 KB per part minimum
MIN_SINGLE_SOURCE_SIZE = 1 * 1024 * 1024   # 1 MB for single source
MIN_SOURCE_DURATION    = 5.0               # 5 seconds minimum

# Worker API
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

def human_size(n: int) -> str:
    mb = n / (1024 * 1024)
    return f"{mb / 1024:.2f} GB" if mb >= 1024 else f"{mb:.1f} MB"

def format_ts(seconds: float) -> str:
    h, rem = divmod(int(seconds), 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"

def safe_id(sid: str) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in str(sid))

def file_hash(path: str) -> str:
    """MD5 hash of first 1MB for quick identity check."""
    try:
        h = hashlib.md5()
        with open(path, 'rb') as f:
            h.update(f.read(1024 * 1024))
        return h.hexdigest()
    except Exception:
        return ""

# ═══════════════════════════════════════════════════════════
# WORKER STATE UPDATE
# ═══════════════════════════════════════════════════════════
def update_worker_final_state(sid: str, status: str, extra: dict = None):
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
# MANIFEST — persistent per-recording state
# ═══════════════════════════════════════════════════════════
def manifest_path(sid: str) -> Path:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    return STATE_DIR / f"manifest_{safe_id(sid)}.json"

def load_manifest(sid: str) -> dict:
    p = manifest_path(sid)
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"stream_id": sid, "parts": {}, "state": "DISCOVERED", "errors": []}

def save_manifest(sid: str, mf: dict) -> None:
    p = manifest_path(sid)
    tmp = p.with_suffix(".tmp")
    mf["updated_at"] = datetime.now(timezone.utc).isoformat()
    tmp.write_text(json.dumps(mf, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(p)

# ═══════════════════════════════════════════════════════════
# PART DISCOVERY — find all parts for THIS recording
# ═══════════════════════════════════════════════════════════
def discover_parts() -> list:
    """
    Find all MKV/WebM parts belonging to this recording.
    Returns list of (path, part_number, size) sorted by part number.
    """
    parts = []
    search_dirs = [PRESERVED_DIR, RECORDINGS_DIR, TEMP_DIR]
    seen_hashes = set()
    
    for d in search_dirs:
        if not d.exists():
            continue
        # Look for _av_part_*.mkv and _av_part_*.webm
        for pattern in ["*_av_part_*.mkv", "*_av_part_*.webm", "*_part_*.mkv", "*_part_*.webm"]:
            for f in d.glob(pattern):
                try:
                    size = f.stat().st_size
                    if size < MIN_PART_SIZE_BYTES:
                        log(f"  ⚠️ Skipping small part: {f.name} ({size} bytes)")
                        continue
                    
                    # Deduplicate by hash
                    fh = file_hash(str(f))
                    if fh in seen_hashes:
                        continue
                    seen_hashes.add(fh)
                    
                    # Extract part number
                    m = re.search(r'_part_(\d+)', f.name)
                    part_num = int(m.group(1)) if m else 0
                    
                    parts.append((f, part_num, size))
                except OSError:
                    continue
    
    # Sort by part number (numeric, not alphabetical)
    parts.sort(key=lambda x: x[1])
    return parts

def discover_single_source() -> str:
    """
    Find a single complete MKV source.
    CRITICAL: Must validate actual content, not just metadata duration.
    """
    candidates = []
    search_dirs = [PRESERVED_DIR, RECORDINGS_DIR, TEMP_DIR]
    
    for d in search_dirs:
        if not d.exists():
            continue
        for f in d.glob("*.mkv"):
            name = f.name
            if "_part_" in name or "_encoded" in name or "concatenated_" in name:
                continue
            try:
                size = f.stat().st_size
                if size >= MIN_SINGLE_SOURCE_SIZE:
                    candidates.append((f, size))
            except OSError:
                continue
    
    if not candidates:
        return None
    
    candidates.sort(key=lambda x: x[1], reverse=True)
    
    for f, size in candidates:
        info = ffprobe_json(str(f))
        if not info:
            continue
        dur = get_duration(info)
        vc, ac = get_codecs(info)
        if not vc:
            continue
        if dur < MIN_SOURCE_DURATION:
            continue
        
        # CRITICAL: Verify actual content by checking video packets
        r = run(f'ffprobe -v quiet -count_packets -select_streams v:0 -show_entries stream=nb_read_packets -of csv=p=0 "{f}"', 120)
        video_packets = 0
        if r and r.returncode == 0 and r.stdout.strip():
            try:
                video_packets = int(r.stdout.strip())
            except ValueError:
                pass
        
        # Estimate expected packets from duration (assume ~30fps minimum)
        expected_min_packets = int(dur * 10)  # Very conservative: 10 fps minimum
        if video_packets < expected_min_packets:
            log(f"  ⚠️ Rejecting {f.name}: only {video_packets} video packets for {dur:.1f}s duration (expected ≥{expected_min_packets})")
            continue
        
        log(f"  ✓ Single source validated: {f.name} ({human_size(size)}, {dur:.1f}s, {video_packets} packets)")
        return str(f)
    
    return None

# ═══════════════════════════════════════════════════════════
# PART VALIDATION
# ═══════════════════════════════════════════════════════════
def validate_part(path: str) -> dict:
    """Validate a single part file."""
    result = {
        "valid": False, "reason": "", "duration": 0.0,
        "size": 0, "video_codec": None, "audio_codec": None,
        "width": 0, "height": 0, "video_packets": 0
    }
    try:
        p = Path(path)
        if not p.exists():
            result["reason"] = "not found"
            return result
        size = p.stat().st_size
        result["size"] = size
        if size < MIN_PART_SIZE_BYTES:
            result["reason"] = f"too small ({size})"
            return result
        
        info = ffprobe_json(path)
        if not info:
            result["reason"] = "ffprobe failed"
            return result
        
        dur = get_duration(info)
        result["duration"] = dur
        if dur <= 0:
            result["reason"] = "no duration"
            return result
        
        vc, ac = get_codecs(info)
        result["video_codec"] = vc
        result["audio_codec"] = ac
        if not vc:
            result["reason"] = "no video"
            return result
        
        w, h = get_resolution(info)
        result["width"] = w
        result["height"] = h
        
        result["valid"] = True
        result["reason"] = "ok"
    except Exception as e:
        result["reason"] = str(e)
    return result

# ═══════════════════════════════════════════════════════════
# PART CONVERSION — MKV/WebM → MP4
# ═══════════════════════════════════════════════════════════
def convert_part_to_mp4(part_path: str, output_path: str) -> bool:
    """
    Convert a single MKV/WebM part to MP4.
    Video: stream copy (preserves H.264, FPS, resolution exactly)
    Audio: re-encode to AAC 128k for MP4 compatibility
    """
    # First try with audio
    cmd = (
        f'ffmpeg -y -i "{part_path}" '
        f'-c:v copy -c:a aac -b:a 128k '
        f'-movflags +faststart '
        f'"{output_path}"'
    )
    r = run(cmd, timeout=600)
    if r and r.returncode == 0 and os.path.exists(output_path) and os.path.getsize(output_path) > 0:
        return True
    
    # If audio encoding failed, try video-only
    cmd_no_audio = (
        f'ffmpeg -y -i "{part_path}" '
        f'-c:v copy -an '
        f'-movflags +faststart '
        f'"{output_path}"'
    )
    r2 = run(cmd_no_audio, timeout=600)
    if r2 and r2.returncode == 0 and os.path.exists(output_path) and os.path.getsize(output_path) > 0:
        log(f"  ⚠️ Converted without audio (audio encoding failed)")
        return True
    
    return False

def split_oversized_mp4(mp4_path: str, outdir: str, idx: int) -> list:
    """Split an oversized MP4 into smaller segments."""
    size = os.path.getsize(mp4_path)
    info = ffprobe_json(mp4_path)
    if not info:
        return [mp4_path]
    dur = get_duration(info)
    if dur <= 0:
        return [mp4_path]
    
    bitrate = size / dur
    target_dur = max((MAX_SEGMENT_BYTES * 0.70) / bitrate, 5.0)
    log(f"  Splitting oversized MP4: {human_size(size)} → ~{target_dur:.0f}s segments")
    
    pattern = os.path.join(outdir, f"split_{idx:03d}_%03d.mp4")
    cmd = (
        f'ffmpeg -y -i "{mp4_path}" '
        f'-c:v copy -c:a aac -b:a 128k '
        f'-f segment -segment_time {target_dur:.1f} '
        f'-reset_timestamps 1 -movflags +faststart '
        f'"{pattern}"'
    )
    r = run(cmd, timeout=600)
    if r and r.returncode == 0:
        subs = [str(s) for s in sorted(Path(outdir).glob(f"split_{idx:03d}_*.mp4")) if s.stat().st_size > 0]
        if subs:
            return subs
    return [mp4_path]

def verify_mp4(path: str) -> dict:
    """Verify MP4 output is valid."""
    result = {"ok": False, "reason": "", "duration": 0.0, "width": 0, "height": 0, "has_audio": False, "size": 0}
    try:
        if not os.path.exists(path):
            result["reason"] = "not found"
            return result
        size = os.path.getsize(path)
        result["size"] = size
        if size == 0:
            result["reason"] = "empty"
            return result
        
        info = ffprobe_json(path)
        if not info:
            result["reason"] = "ffprobe failed"
            return result
        
        dur = get_duration(info)
        result["duration"] = dur
        if dur <= 0:
            result["reason"] = "no duration"
            return result
        
        vc, ac = get_codecs(info)
        if not vc:
            result["reason"] = "no video"
            return result
        
        result["has_audio"] = bool(ac)
        w, h = get_resolution(info)
        result.update({"ok": True, "reason": "ok", "width": w, "height": h})
    except Exception as e:
        result["reason"] = str(e)
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
            log(f"  sendVideo attempt {attempt}/{MAX_RETRIES}: HTTP {e.code}")
        except Exception as e:
            result["error"] = str(e)
            log(f"  sendVideo attempt {attempt}/{MAX_RETRIES}: {e}")
        if attempt < MAX_RETRIES:
            wait = 5 * attempt
            log(f"  Retrying in {wait}s...")
            time.sleep(wait)
    return result

# ═══════════════════════════════════════════════════════════
# SINGLE SOURCE PROCESSING (fallback for recordings without parts)
# ═══════════════════════════════════════════════════════════
def process_single_source(source_path: str, sid: str, mf: dict) -> bool:
    """Process a single complete MKV file (no parts)."""
    log(f"📂 Processing single source: {os.path.basename(source_path)}")
    
    info = ffprobe_json(source_path)
    if not info:
        return False
    
    duration = get_duration(info)
    src_size = os.path.getsize(source_path)
    vc, ac = get_codecs(info)
    
    log(f"⏱️ Duration: {format_ts(duration)} ({duration:.1f}s)")
    log(f"📦 Size: {human_size(src_size)}")
    log(f"🎬 Video: {vc}, Audio: {ac or 'NONE'}")
    
    if src_size < MIN_SINGLE_SOURCE_SIZE or duration < MIN_SOURCE_DURATION or not vc:
        log("✗ Source validation failed")
        return False
    
    # Split into segments
    seg_dur = duration + 10 if src_size <= MAX_SEGMENT_BYTES else (MAX_SEGMENT_BYTES / (src_size / duration)) * 0.75
    seg_dur = max(seg_dur, 10.0)
    
    with tempfile.TemporaryDirectory(prefix="pp_single_") as segdir:
        pattern = os.path.join(segdir, "segment_%03d.mp4")
        cmd = (
            f'ffmpeg -y -i "{source_path}" '
            f'-c:v copy -c:a aac -b:a 128k '
            f'-f segment -segment_time {seg_dur:.1f} '
            f'-reset_timestamps 1 -movflags +faststart '
            f'"{pattern}"'
        )
        r = run(cmd, timeout=1800)
        if not r or r.returncode != 0:
            log("✗ Segmentation failed")
            return False
        
        segments = [str(s) for s in sorted(Path(segdir).glob("segment_*.mp4")) if s.stat().st_size > 0]
        if not segments:
            log("✗ No segments produced")
            return False
        
        total = len(segments)
        sent_count = 0
        display_name = STREAM_NAME or os.path.basename(source_path).split('_')[0]
        
        for idx, seg_path in enumerate(segments):
            seg_key = f"single_seg_{idx:04d}"
            
            # Skip already sent
            prev = mf["parts"].get(seg_key, {})
            if prev.get("sent") and prev.get("message_id"):
                log(f"  [{idx+1}/{total}] Already sent — skip")
                sent_count += 1
                continue
            
            verdict = verify_mp4(seg_path)
            if not verdict["ok"]:
                log(f"  [{idx+1}/{total}] ✗ {verdict['reason']}")
                mf["parts"][seg_key] = {"sent": False, "error": verdict["reason"]}
                continue
            
            if verdict["size"] > TG_HARD_LIMIT:
                log(f"  [{idx+1}/{total}] ✗ Exceeds Telegram limit")
                mf["parts"][seg_key] = {"sent": False, "error": "too_large"}
                continue
            
            audio_note = "\n⚠️ بدون صوت" if not verdict["has_audio"] else ""
            caption = (
                f"🎬 {display_name}\n"
                f"🆔 {sid}\n"
                f"📎 المقطع {idx+1} من {total}\n"
                f"⏱️ {format_ts(verdict['duration'])}"
                f"{audio_note}"
            )
            
            tg_res = tg_send_video(seg_path, caption, int(verdict["duration"]), verdict["width"], verdict["height"])
            
            if tg_res["ok"]:
                log(f"  [{idx+1}/{total}] 📤 SENT ✅")
                sent_count += 1
                mf["parts"][seg_key] = {
                    "sent": True, "message_id": tg_res["message_id"],
                    "duration": round(verdict["duration"], 1),
                    "has_audio": verdict["has_audio"],
                }
            else:
                log(f"  [{idx+1}/{total}] ✗ Send failed: {tg_res['error']}")
                mf["parts"][seg_key] = {"sent": False, "error": tg_res["error"]}
            
            save_manifest(sid, mf)
            if idx < total - 1:
                time.sleep(SEND_DELAY_SECONDS)
        
        # Coverage check
        total_sent_dur = sum(p.get("duration", 0) for p in mf["parts"].values() if p.get("sent"))
        coverage = min(total_sent_dur / duration, 1.0) if duration > 0 else 0
        log(f"COVERAGE: {total_sent_dur:.1f}s / {duration:.1f}s ({coverage*100:.1f}%)")
        
        if coverage >= 0.95 and sent_count == total:
            mf["state"] = "SENT"
            save_manifest(sid, mf)
            update_worker_final_state(sid, "completed", {
                "completed_at": datetime.now(timezone.utc).isoformat(),
                "segments_sent": sent_count,
                "coverage": round(coverage, 3),
            })
            return True
        else:
            mf["state"] = "PARTIAL"
            save_manifest(sid, mf)
            return False

# ═══════════════════════════════════════════════════════════
# MAIN — Per-part processing pipeline
# ═══════════════════════════════════════════════════════════
def main():
    log("=" * 60)
    log("Post-processing pipeline — PHASE 2 (Per-part)")
    log(f"STREAM_ID={STREAM_ID}")
    log("=" * 60)
    
    mf = load_manifest(STREAM_ID)
    
    # If already completed, skip
    if mf.get("state") == "SENT":
        log("⏭️ Already completed — skipping")
        return
    
    # ── Step 1: Discover parts ──
    parts = discover_parts()
    
    if parts and len(parts) > 0:
        log(f"📦 Found {len(parts)} parts")
        total_parts_size = sum(size for _, _, size in parts)
        log(f"  Total parts size: {human_size(total_parts_size)}")
    else:
        log("📦 No parts found, checking for single source...")
        single = discover_single_source()
        if single:
            success = process_single_source(single, STREAM_ID, mf)
            if success:
                log("✅ Single source processing completed")
            else:
                log("⚠️ Single source processing incomplete")
            return
        else:
            log("✗ No valid source found — ABORTING")
            mf["state"] = "FAILED_NO_SOURCE"
            save_manifest(STREAM_ID, mf)
            update_worker_final_state(STREAM_ID, "failed", {
                "failed_at": datetime.now(timezone.utc).isoformat(),
                "error": "no_source_found",
            })
            return
    
    # ── Step 2: Validate all parts ──
    log("\n" + "=" * 60)
    log("PHASE 2a: Validating all parts")
    log("=" * 60)
    
    valid_parts = []
    total_expected_duration = 0.0
    
    for f, part_num, size in parts:
        part_key = f"part_{part_num:04d}"
        
        # Check if already processed in manifest
        prev = mf["parts"].get(part_key, {})
        if prev.get("valid") is not None and prev.get("duration", 0) > 0:
            # Use cached validation
            valid_parts.append((f, part_num, size, prev["duration"]))
            total_expected_duration += prev["duration"]
            log(f"  Part {part_num}: cached (dur={prev['duration']:.1f}s)")
            continue
        
        validation = validate_part(str(f))
        if validation["valid"]:
            valid_parts.append((f, part_num, size, validation["duration"]))
            total_expected_duration += validation["duration"]
            mf["parts"][part_key] = {
                "valid": True,
                "path": str(f),
                "size": size,
                "duration": round(validation["duration"], 2),
                "video_codec": validation["video_codec"],
                "audio_codec": validation["audio_codec"],
                "width": validation["width"],
                "height": validation["height"],
            }
            log(f"  Part {part_num}: ✓ {validation['duration']:.1f}s, {human_size(size)}, v={validation['video_codec']}, a={validation['audio_codec'] or 'NONE'}")
        else:
            mf["parts"][part_key] = {
                "valid": False,
                "path": str(f),
                "error": validation["reason"],
            }
            mf["errors"].append(f"part_{part_num}: {validation['reason']}")
            log(f"  Part {part_num}: ✗ {validation['reason']}")
    
    save_manifest(STREAM_ID, mf)
    
    if not valid_parts:
        log("✗ No valid parts found — ABORTING")
        mf["state"] = "FAILED_NO_VALID_PARTS"
        save_manifest(STREAM_ID, mf)
        update_worker_final_state(STREAM_ID, "failed", {
            "failed_at": datetime.now(timezone.utc).isoformat(),
            "error": "no_valid_parts",
        })
        return
    
    log(f"\n✓ {len(valid_parts)} valid parts, total expected duration: {format_ts(total_expected_duration)}")
    
    # ── Step 3: Convert each part to MP4 ──
    log("\n" + "=" * 60)
    log("PHASE 2b: Converting parts to MP4")
    log("=" * 60)
    
    TEMP_DIR.mkdir(parents=True, exist_ok=True)
    mp4_dir = TEMP_DIR / f"mp4_{safe_id(STREAM_ID)}"
    mp4_dir.mkdir(parents=True, exist_ok=True)
    
    display_name = STREAM_NAME or f"recording_{STREAM_ID}"
    total_sent_duration = 0.0
    sent_count = 0
    failed_count = 0
    total = len(valid_parts)
    
    for idx, (part_path, part_num, part_size, part_duration) in enumerate(valid_parts):
        part_key = f"part_{part_num:04d}"
        prev = mf["parts"].get(part_key, {})
        
        # Skip already sent parts
        if prev.get("sent") and prev.get("message_id"):
            log(f"  [{idx+1}/{total}] Part {part_num}: Already sent (msg={prev['message_id']}) — skip")
            sent_count += 1
            total_sent_duration += prev.get("duration", 0)
            continue
        
        log(f"  [{idx+1}/{total}] Part {part_num}: Converting {human_size(part_size)}, {part_duration:.1f}s")
        
        # Output path
        mp4_path = str(mp4_dir / f"part_{part_num:04d}.mp4")
        
        # Check if MP4 already exists and is valid
        need_convert = True
        if os.path.exists(mp4_path) and os.path.getsize(mp4_path) > 0:
            existing = verify_mp4(mp4_path)
            if existing["ok"] and abs(existing["duration"] - part_duration) < 5.0:
                log(f"    ✓ MP4 already exists and valid")
                need_convert = False
        
        if need_convert:
            # Use temp file first, then rename (atomic)
            tmp_mp4 = mp4_path + ".tmp"
            if not convert_part_to_mp4(str(part_path), tmp_mp4):
                log(f"    ✗ Conversion failed")
                mf["parts"][part_key]["sent"] = False
                mf["parts"][part_key]["error"] = "conversion_failed"
                mf["errors"].append(f"part_{part_num}: conversion_failed")
                failed_count += 1
                save_manifest(STREAM_ID, mf)
                continue
            
            # Validate the output
            tmp_verdict = verify_mp4(tmp_mp4)
            if not tmp_verdict["ok"]:
                log(f"    ✗ Converted MP4 invalid: {tmp_verdict['reason']}")
                os.unlink(tmp_mp4)
                mf["parts"][part_key]["sent"] = False
                mf["parts"][part_key]["error"] = f"invalid_mp4: {tmp_verdict['reason']}"
                mf["errors"].append(f"part_{part_num}: invalid_mp4")
                failed_count += 1
                save_manifest(STREAM_ID, mf)
                continue
            
            # Atomic rename
            if os.path.exists(mp4_path):
                os.unlink(mp4_path)
            os.rename(tmp_mp4, mp4_path)
        
        # Verify final MP4
        verdict = verify_mp4(mp4_path)
        if not verdict["ok"]:
            log(f"    ✗ Final verification failed: {verdict['reason']}")
            mf["parts"][part_key]["sent"] = False
            mf["parts"][part_key]["error"] = verdict["reason"]
            failed_count += 1
            save_manifest(STREAM_ID, mf)
            continue
        
        # Check size limit
        if verdict["size"] > MAX_SEGMENT_BYTES:
            log(f"    ⚠️ MP4 oversized ({human_size(verdict['size'])}), splitting...")
            subs = split_oversized_mp4(mp4_path, str(mp4_dir), part_num)
            if len(subs) > 1:
                # Send each sub-segment
                for sub_idx, sub_path in enumerate(subs):
                    sub_verdict = verify_mp4(sub_path)
                    if not sub_verdict["ok"]:
                        continue
                    if sub_verdict["size"] > TG_HARD_LIMIT:
                        continue
                    
                    sub_key = f"{part_key}_sub{sub_idx:02d}"
                    sub_prev = mf["parts"].get(sub_key, {})
                    if sub_prev.get("sent") and sub_prev.get("message_id"):
                        sent_count += 1
                        total_sent_duration += sub_verdict["duration"]
                        continue
                    
                    audio_note = "\n⚠️ بدون صوت" if not sub_verdict["has_audio"] else ""
                    caption = (
                        f"🎬 {display_name}\n"
                        f"🆔 {STREAM_ID}\n"
                        f"📎 الجزء {part_num+1}.{sub_idx+1}\n"
                        f"⏱️ {format_ts(sub_verdict['duration'])}"
                        f"{audio_note}"
                    )
                    tg_res = tg_send_video(sub_path, caption, int(sub_verdict["duration"]), sub_verdict["width"], sub_verdict["height"])
                    if tg_res["ok"]:
                        log(f"    📤 Sub {sub_idx+1} SENT ✅")
                        sent_count += 1
                        total_sent_duration += sub_verdict["duration"]
                        mf["parts"][sub_key] = {
                            "sent": True, "message_id": tg_res["message_id"],
                            "duration": round(sub_verdict["duration"], 1),
                            "has_audio": sub_verdict["has_audio"],
                        }
                    else:
                        failed_count += 1
                        mf["parts"][sub_key] = {"sent": False, "error": tg_res["error"]}
                    save_manifest(STREAM_ID, mf)
                    time.sleep(SEND_DELAY_SECONDS)
                continue
        
        if verdict["size"] > TG_HARD_LIMIT:
            log(f"    ✗ Exceeds Telegram limit: {human_size(verdict['size'])}")
            mf["parts"][part_key]["sent"] = False
            mf["parts"][part_key]["error"] = "exceeds_tg_limit"
            failed_count += 1
            save_manifest(STREAM_ID, mf)
            continue
        
        # Send
        audio_note = "\n⚠️ بدون صوت" if not verdict["has_audio"] else ""
        caption = (
            f"🎬 {display_name}\n"
            f"🆔 {STREAM_ID}\n"
            f"📎 المقطع {idx+1} من {total}\n"
            f"⏱️ {format_ts(verdict['duration'])}"
            f"{audio_note}"
        )
        
        tg_res = tg_send_video(mp4_path, caption, int(verdict["duration"]), verdict["width"], verdict["height"])
        
        if tg_res["ok"]:
            log(f"    📤 SENT ✅ (msg={tg_res['message_id']}, {verdict['duration']:.1f}s, audio={'YES' if verdict['has_audio'] else 'NO'})")
            sent_count += 1
            total_sent_duration += verdict["duration"]
            mf["parts"][part_key].update({
                "sent": True,
                "message_id": tg_res["message_id"],
                "mp4_path": mp4_path,
                "mp4_size": verdict["size"],
                "mp4_duration": round(verdict["duration"], 1),
                "has_audio": verdict["has_audio"],
            })
        else:
            log(f"    ✗ Send failed: {tg_res['error']}")
            mf["parts"][part_key]["sent"] = False
            mf["parts"][part_key]["send_error"] = tg_res["error"]
            mf["errors"].append(f"part_{part_num}: send_{tg_res['error']}")
            failed_count += 1
        
        save_manifest(STREAM_ID, mf)
        
        if idx < total - 1:
            time.sleep(SEND_DELAY_SECONDS)
    
    # ── Step 4: Final coverage check ──
    log("\n" + "=" * 60)
    log("PHASE 2c: Final Coverage Verification")
    log("=" * 60)
    
    coverage = min(total_sent_duration / total_expected_duration, 1.0) if total_expected_duration > 0 else 0
    
    log(f"RESULT: {sent_count}/{total} parts sent, {failed_count} failed")
    log(f"COVERAGE: {total_sent_duration:.1f}s / {total_expected_duration:.1f}s ({coverage*100:.1f}%)")
    
    if coverage >= 0.95 and failed_count == 0:
        mf["state"] = "SENT"
        mf["coverage"] = round(coverage, 3)
        mf["total_sent_duration"] = round(total_sent_duration, 1)
        mf["total_expected_duration"] = round(total_expected_duration, 1)
        save_manifest(STREAM_ID, mf)
        
        # Create marker file
        marker_path = str(mp4_dir / f"{safe_id(STREAM_ID)}{MARKER_SUFFIX}")
        with open(marker_path, "w") as f:
            json.dump({
                "sent_at": datetime.now(timezone.utc).isoformat(),
                "parts": sent_count,
                "coverage": round(coverage, 3),
                "total_duration": round(total_sent_duration, 1),
            }, f)
        
        log("✅ ALL parts sent successfully with full coverage")
        update_worker_final_state(STREAM_ID, "completed", {
            "completed_at": datetime.now(timezone.utc).isoformat(),
            "segments_sent": sent_count,
            "coverage": round(coverage, 3),
            "total_duration": round(total_sent_duration, 1),
        })
    elif sent_count > 0:
        mf["state"] = "PARTIAL"
        mf["coverage"] = round(coverage, 3)
        save_manifest(STREAM_ID, mf)
        log(f"⚠️ PARTIAL: {sent_count}/{total} sent. Manifest preserved for retry.")
        log(f"  Re-run this script to retry failed parts only.")
    else:
        mf["state"] = "FAILED"
        save_manifest(STREAM_ID, mf)
        log("✗ No parts sent.")
        update_worker_final_state(STREAM_ID, "failed", {
            "failed_at": datetime.now(timezone.utc).isoformat(),
            "error": "no_parts_sent",
        })
    
    log("Source files NEVER deleted by this pipeline.")

if __name__ == "__main__":
    main()
