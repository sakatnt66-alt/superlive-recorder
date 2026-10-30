#!/usr/bin/env python3
"""
postprocess_recording.py — PHASE 2: Per-part MKV→MP4 conversion, delivery.

FIXES in this version:
  1. Logs FFmpeg stderr for diagnosis
  2. Deduplicates parts found in multiple directories
  3. Detects and corrects corrupt duration metadata
  4. Multiple conversion strategies with fallback
  5. Validates conversion output before accepting
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

MAX_SEGMENT_BYTES  = 45 * 1024 * 1024
TG_HARD_LIMIT      = 50 * 1024 * 1024
MARKER_SUFFIX      = ".postprocessed"
MP4_VIDEO_OK       = {"h264", "hevc", "mpeg4", "mjpeg"}
SEND_DELAY_SECONDS = 3
MAX_RETRIES        = 3

MIN_PART_SIZE_BYTES    = 100 * 1024
MIN_SINGLE_SOURCE_SIZE = 1 * 1024 * 1024
MIN_SOURCE_DURATION    = 5.0

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
    try:
        h = hashlib.md5()
        with open(path, 'rb') as f:
            h.update(f.read(1024 * 1024))
        return h.hexdigest()
    except Exception:
        return ""

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
# MANIFEST
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
# PART DISCOVERY — deduplicated across directories
# ═══════════════════════════════════════════════════════════
def discover_parts() -> list:
    """
    Find all MKV parts, deduplicated by content hash.
    Prefers files in _preserved over _temp.
    Returns list of (path, part_number, size) sorted by part number.
    """
    # Search _preserved FIRST (preferred), then others
    search_order = [PRESERVED_DIR, RECORDINGS_DIR, TEMP_DIR]
    parts_by_num = {}  # part_num -> (path, size, hash)
    
    for d in search_order:
        if not d.exists():
            continue
        for pattern in ["*_av_part_*.mkv", "*_av_part_*.webm"]:
            for f in d.glob(pattern):
                try:
                    size = f.stat().st_size
                    if size < MIN_PART_SIZE_BYTES:
                        continue
                    
                    m = re.search(r'_part_(\d+)', f.name)
                    part_num = int(m.group(1)) if m else -1
                    if part_num < 0:
                        continue
                    
                    fh = file_hash(str(f))
                    
                    # If this part number already found, keep the one from _preserved
                    if part_num in parts_by_num:
                        existing_path = parts_by_num[part_num][0]
                        # Prefer _preserved directory
                        if "_preserved" in str(f) and "_preserved" not in str(existing_path):
                            parts_by_num[part_num] = (f, size, fh)
                        # Or keep larger file if same directory
                        elif size > parts_by_num[part_num][1] and "_preserved" not in str(existing_path):
                            parts_by_num[part_num] = (f, size, fh)
                    else:
                        parts_by_num[part_num] = (f, size, fh)
                except OSError:
                    continue
    
    # Build sorted list
    parts = []
    for part_num in sorted(parts_by_num.keys()):
        f, size, fh = parts_by_num[part_num]
        parts.append((f, part_num, size))
    
    return parts

# ═══════════════════════════════════════════════════════════
# DURATION VALIDATION — detect corrupt metadata
# ═══════════════════════════════════════════════════════════
def estimate_real_duration(part_path: str, declared_duration: float, part_size: int,
                           all_parts_info: list) -> float:
    """
    Detect and correct corrupt duration metadata.
    
    If a part declares 2039s but is only 30MB while similar parts
    are 107s at 35MB, the metadata is corrupt.
    """
    # Calculate median duration/size ratio from other parts
    ratios = []
    for info in all_parts_info:
        if info["path"] == str(part_path):
            continue
        if info["size"] > 0 and info["duration"] > 0 and info["duration"] < 600:
            ratios.append(info["duration"] / info["size"])
    
    if not ratios:
        return declared_duration
    
    # Use median ratio to estimate expected duration
    ratios.sort()
    median_ratio = ratios[len(ratios) // 2]
    estimated_duration = part_size * median_ratio
    
    # If declared duration is more than 3x the estimate, it's corrupt
    if declared_duration > estimated_duration * 3:
        log(f"    ⚠️ Duration metadata corrupt: declared={declared_duration:.1f}s, "
            f"estimated={estimated_duration:.1f}s (from size {human_size(part_size)})")
        return estimated_duration
    
    return declared_duration

def validate_part_with_correction(path: str, all_parts_info: list) -> dict:
    """Validate a part and correct corrupt duration if detected."""
    result = {
        "valid": False, "reason": "", "duration": 0.0,
        "size": 0, "video_codec": None, "audio_codec": None,
        "width": 0, "height": 0
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
        
        declared_dur = get_duration(info)
        vc, ac = get_codecs(info)
        result["video_codec"] = vc
        result["audio_codec"] = ac
        if not vc:
            result["reason"] = "no video"
            return result
        
        # Correct corrupt duration
        real_dur = estimate_real_duration(path, declared_dur, size, all_parts_info)
        result["duration"] = real_dur
        
        if real_dur < 1.0:
            result["reason"] = f"duration too short ({real_dur:.1f}s)"
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
# PART CONVERSION — with detailed error logging
# ═══════════════════════════════════════════════════════════
def convert_part_to_mp4(part_path: str, output_path: str) -> bool:
    """
    Convert MKV/WebM part to MP4.
    Video: stream copy. Audio: AAC re-encode.
    Falls back to video-only if audio fails.
    LOGS ALL ERRORS for diagnosis.
    """
    abs_input = os.path.abspath(part_path)
    abs_output = os.path.abspath(output_path)
    
    # Ensure output directory exists
    os.makedirs(os.path.dirname(abs_output), exist_ok=True)
    
    # Strategy 1: Video copy + Audio AAC
    cmd1 = (
        f'ffmpeg -y -i "{abs_input}" '
        f'-c:v copy -c:a aac -b:a 128k '
        f'-movflags +faststart '
        f'"{abs_output}"'
    )
    r1 = run(cmd1, timeout=600)
    if r1 is not None and r1.returncode == 0:
        if os.path.exists(abs_output) and os.path.getsize(abs_output) > 0:
            return True
    
    # Log the actual error
    stderr1 = (r1.stderr or "")[-500:] if r1 else "TIMEOUT"
    log(f"    ⚠️ Strategy 1 failed: {stderr1}")
    
    # Strategy 2: Video copy + Audio copy into MKV, then remux to MP4
    # (Sometimes direct AAC encoding fails but copy works)
    tmp_mkv = abs_output + ".tmp_mkv"
    cmd2 = (
        f'ffmpeg -y -i "{abs_input}" '
        f'-c:v copy -c:a copy '
        f'"{tmp_mkv}"'
    )
    r2 = run(cmd2, timeout=300)
    if r2 is not None and r2.returncode == 0 and os.path.exists(tmp_mkv) and os.path.getsize(tmp_mkv) > 0:
        # Now convert MKV to MP4
        cmd3 = (
            f'ffmpeg -y -i "{tmp_mkv}" '
            f'-c:v copy -c:a aac -b:a 128k '
            f'-movflags +faststart '
            f'"{abs_output}"'
        )
        r3 = run(cmd3, timeout=300)
        try:
            os.unlink(tmp_mkv)
        except:
            pass
        if r3 is not None and r3.returncode == 0:
            if os.path.exists(abs_output) and os.path.getsize(abs_output) > 0:
                return True
        stderr3 = (r3.stderr or "")[-300:] if r3 else "TIMEOUT"
        log(f"    ⚠️ Strategy 2 remux failed: {stderr3}")
    
    # Strategy 3: Video copy, NO audio (video-only MP4)
    cmd4 = (
        f'ffmpeg -y -i "{abs_input}" '
        f'-c:v copy -an '
        f'-movflags +faststart '
        f'"{abs_output}"'
    )
    r4 = run(cmd4, timeout=300)
    if r4 is not None and r4.returncode == 0:
        if os.path.exists(abs_output) and os.path.getsize(abs_output) > 0:
            log(f"    ⚠️ Converted without audio (audio encoding failed)")
            return True
    
    stderr4 = (r4.stderr or "")[-500:] if r4 else "TIMEOUT"
    log(f"    ✗ All conversion strategies failed. Last error: {stderr4}")
    return False

def verify_mp4(path: str) -> dict:
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

def split_oversized_mp4(mp4_path: str, outdir: str, idx: int) -> list:
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
            time.sleep(wait)
    return result

# ═══════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════
def main():
    log("=" * 60)
    log("Post-processing pipeline — PHASE 2 (Per-part)")
    log(f"STREAM_ID={STREAM_ID}")
    log(f"WORKER_API_URL={'SET' if WORKER_API_URL else 'MISSING'}")
    log("=" * 60)
    
    mf = load_manifest(STREAM_ID)
    
    if mf.get("state") == "SENT":
        log("⏭️ Already completed — skipping")
        return
    
    # ── Step 1: Discover parts (deduplicated) ──
    parts = discover_parts()
    
    if not parts:
        log("✗ No parts found — checking for single source...")
        # Try single source as last resort
        candidates = []
        for d in [PRESERVED_DIR, RECORDINGS_DIR, TEMP_DIR]:
            if not d.exists():
                continue
            for f in d.glob("*.mkv"):
                if "_part_" in f.name or "_encoded" in f.name or "concatenated_" in f.name:
                    continue
                try:
                    size = f.stat().st_size
                    if size >= MIN_SINGLE_SOURCE_SIZE:
                        candidates.append((f, size))
                except OSError:
                    continue
        if candidates:
            candidates.sort(key=lambda x: x[1], reverse=True)
            f, size = candidates[0]
            dur = get_duration_file(str(f))
            if dur >= MIN_SOURCE_DURATION:
                log(f"✓ Using single source: {f.name} ({human_size(size)}, {dur:.1f}s)")
                parts = [(f, 0, size)]
            else:
                log(f"✗ Single source too short: {dur:.1f}s")
                mf["state"] = "FAILED_NO_SOURCE"
                save_manifest(STREAM_ID, mf)
                return
        else:
            log("✗ No valid source found — ABORTING")
            mf["state"] = "FAILED_NO_SOURCE"
            save_manifest(STREAM_ID, mf)
            update_worker_final_state(STREAM_ID, "failed", {"error": "no_source_found"})
            return
    
    total_size = sum(size for _, _, size in parts)
    log(f"📦 Found {len(parts)} unique parts, total size: {human_size(total_size)}")
    
    # ── Step 2: Validate all parts ──
    log("\n" + "=" * 60)
    log("PHASE 2a: Validating all parts")
    log("=" * 60)
    
    # First pass: collect basic info for duration estimation
    all_parts_info = []
    for f, part_num, size in parts:
        info = ffprobe_json(str(f))
        dur = get_duration(info) if info else 0.0
        vc, ac = get_codecs(info) if info else (None, None)
        all_parts_info.append({
            "path": str(f), "part_num": part_num, "size": size,
            "duration": dur, "video_codec": vc, "audio_codec": ac
        })
    
    # Second pass: validate with duration correction
    valid_parts = []
    total_expected_duration = 0.0
    
    for info in all_parts_info:
        part_key = f"part_{info['part_num']:04d}"
        
        # Check manifest cache
        prev = mf["parts"].get(part_key, {})
        if prev.get("valid") is True and prev.get("duration", 0) > 0 and prev.get("sent"):
            valid_parts.append(info)
            total_expected_duration += prev["duration"]
            log(f"  Part {info['part_num']}: cached & sent")
            continue
        
        corrected_dur = estimate_real_duration(
            info["path"], info["duration"], info["size"], all_parts_info
        )
        
        if not info["video_codec"]:
            log(f"  Part {info['part_num']}: ✗ no video")
            mf["parts"][part_key] = {"valid": False, "error": "no_video"}
            continue
        
        if corrected_dur < 1.0:
            log(f"  Part {info['part_num']}: ✗ duration too short")
            mf["parts"][part_key] = {"valid": False, "error": "too_short"}
            continue
        
        info["duration"] = corrected_dur
        valid_parts.append(info)
        total_expected_duration += corrected_dur
        
        mf["parts"][part_key] = {
            "valid": True,
            "path": info["path"],
            "size": info["size"],
            "duration": round(corrected_dur, 2),
            "video_codec": info["video_codec"],
            "audio_codec": info["audio_codec"],
        }
        
        dur_note = f" (corrected from {info['duration']:.0f}s)" if abs(corrected_dur - info['duration']) > 1 else ""
        log(f"  Part {info['part_num']}: ✓ {corrected_dur:.1f}s, {human_size(info['size'])}, "
            f"v={info['video_codec']}, a={info['audio_codec'] or 'NONE'}{dur_note}")
    
    save_manifest(STREAM_ID, mf)
    
    if not valid_parts:
        log("✗ No valid parts — ABORTING")
        mf["state"] = "FAILED_NO_VALID_PARTS"
        save_manifest(STREAM_ID, mf)
        update_worker_final_state(STREAM_ID, "failed", {"error": "no_valid_parts"})
        return
    
    log(f"\n✓ {len(valid_parts)} valid parts, expected duration: {format_ts(total_expected_duration)}")
    
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
    
    for idx, part_info in enumerate(valid_parts):
        part_num = part_info["part_num"]
        part_path = part_info["path"]
        part_size = part_info["size"]
        part_duration = part_info["duration"]
        part_key = f"part_{part_num:04d}"
        
        prev = mf["parts"].get(part_key, {})
        if prev.get("sent") and prev.get("message_id"):
            log(f"  [{idx+1}/{total}] Part {part_num}: Already sent (msg={prev['message_id']}) — skip")
            sent_count += 1
            total_sent_duration += prev.get("duration", 0)
            continue
        
        log(f"  [{idx+1}/{total}] Part {part_num}: Converting {human_size(part_size)}, {part_duration:.1f}s")
        
        mp4_path = str(mp4_dir / f"part_{part_num:04d}.mp4")
        tmp_mp4 = mp4_path + ".tmp"
        
        # Check if already converted and valid
        need_convert = True
        if os.path.exists(mp4_path) and os.path.getsize(mp4_path) > 0:
            existing = verify_mp4(mp4_path)
            if existing["ok"]:
                log(f"    ✓ MP4 already exists and valid")
                need_convert = False
        
        if need_convert:
            if os.path.exists(tmp_mp4):
                try:
                    os.unlink(tmp_mp4)
                except:
                    pass
            
            if not convert_part_to_mp4(part_path, tmp_mp4):
                log(f"    ✗ Conversion failed for part {part_num}")
                mf["parts"][part_key]["sent"] = False
                mf["parts"][part_key]["error"] = "conversion_failed"
                mf["errors"].append(f"part_{part_num}: conversion_failed")
                failed_count += 1
                save_manifest(STREAM_ID, mf)
                continue
            
            tmp_verdict = verify_mp4(tmp_mp4)
            if not tmp_verdict["ok"]:
                log(f"    ✗ Converted MP4 invalid: {tmp_verdict['reason']}")
                try:
                    os.unlink(tmp_mp4)
                except:
                    pass
                mf["parts"][part_key]["sent"] = False
                mf["parts"][part_key]["error"] = f"invalid_mp4: {tmp_verdict['reason']}"
                failed_count += 1
                save_manifest(STREAM_ID, mf)
                continue
            
            if os.path.exists(mp4_path):
                try:
                    os.unlink(mp4_path)
                except:
                    pass
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
        
        # Handle oversized
        if verdict["size"] > MAX_SEGMENT_BYTES:
            log(f"    ⚠️ Oversized ({human_size(verdict['size'])}), splitting...")
            subs = split_oversized_mp4(mp4_path, str(mp4_dir), part_num)
            for sub_idx, sub_path in enumerate(subs):
                sub_verdict = verify_mp4(sub_path)
                if not sub_verdict["ok"] or sub_verdict["size"] > TG_HARD_LIMIT:
                    continue
                sub_key = f"{part_key}_sub{sub_idx:02d}"
                sub_prev = mf["parts"].get(sub_key, {})
                if sub_prev.get("sent") and sub_prev.get("message_id"):
                    sent_count += 1
                    total_sent_duration += sub_verdict["duration"]
                    continue
                
                audio_note = "\n⚠️ بدون صوت" if not sub_verdict["has_audio"] else ""
                caption = (
                    f"🎬 {display_name}\n🆔 {STREAM_ID}\n"
                    f"📎 الجزء {part_num+1}.{sub_idx+1}\n"
                    f"⏱️ {format_ts(sub_verdict['duration'])}{audio_note}"
                )
                tg_res = tg_send_video(sub_path, caption, int(sub_verdict["duration"]),
                                       sub_verdict["width"], sub_verdict["height"])
                if tg_res["ok"]:
                    sent_count += 1
                    total_sent_duration += sub_verdict["duration"]
                    mf["parts"][sub_key] = {"sent": True, "message_id": tg_res["message_id"],
                                            "duration": round(sub_verdict["duration"], 1)}
                else:
                    failed_count += 1
                    mf["parts"][sub_key] = {"sent": False, "error": tg_res["error"]}
                save_manifest(STREAM_ID, mf)
                time.sleep(SEND_DELAY_SECONDS)
            continue
        
        if verdict["size"] > TG_HARD_LIMIT:
            log(f"    ✗ Exceeds Telegram limit")
            mf["parts"][part_key]["sent"] = False
            mf["parts"][part_key]["error"] = "exceeds_tg_limit"
            failed_count += 1
            save_manifest(STREAM_ID, mf)
            continue
        
        # Send
        audio_note = "\n⚠️ بدون صوت" if not verdict["has_audio"] else ""
        caption = (
            f"🎬 {display_name}\n🆔 {STREAM_ID}\n"
            f"📎 المقطع {idx+1} من {total}\n"
            f"⏱️ {format_ts(verdict['duration'])}{audio_note}"
        )
        
        tg_res = tg_send_video(mp4_path, caption, int(verdict["duration"]),
                               verdict["width"], verdict["height"])
        
        if tg_res["ok"]:
            log(f"    📤 SENT ✅ (msg={tg_res['message_id']}, {verdict['duration']:.1f}s, "
                f"audio={'YES' if verdict['has_audio'] else 'NO'})")
            sent_count += 1
            total_sent_duration += verdict["duration"]
            mf["parts"][part_key].update({
                "sent": True, "message_id": tg_res["message_id"],
                "mp4_path": mp4_path, "mp4_size": verdict["size"],
                "mp4_duration": round(verdict["duration"], 1),
                "has_audio": verdict["has_audio"],
            })
        else:
            log(f"    ✗ Send failed: {tg_res['error']}")
            mf["parts"][part_key]["sent"] = False
            mf["parts"][part_key]["send_error"] = tg_res["error"]
            failed_count += 1
        
        save_manifest(STREAM_ID, mf)
        if idx < total - 1:
            time.sleep(SEND_DELAY_SECONDS)
    
    # ── Step 4: Coverage ──
    log("\n" + "=" * 60)
    log("PHASE 2c: Final Coverage Verification")
    log("=" * 60)
    
    coverage = min(total_sent_duration / total_expected_duration, 1.0) if total_expected_duration > 0 else 0
    
    log(f"RESULT: {sent_count}/{total} parts sent, {failed_count} failed")
    log(f"COVERAGE: {total_sent_duration:.1f}s / {total_expected_duration:.1f}s ({coverage*100:.1f}%)")
    
    if coverage >= 0.95 and failed_count == 0:
        mf["state"] = "SENT"
        mf["coverage"] = round(coverage, 3)
        save_manifest(STREAM_ID, mf)
        log("✅ ALL parts sent with full coverage")
        update_worker_final_state(STREAM_ID, "completed", {
            "completed_at": datetime.now(timezone.utc).isoformat(),
            "segments_sent": sent_count,
            "coverage": round(coverage, 3),
        })
    elif sent_count > 0:
        mf["state"] = "PARTIAL"
        mf["coverage"] = round(coverage, 3)
        save_manifest(STREAM_ID, mf)
        log(f"⚠️ PARTIAL: {sent_count}/{total}. Re-run to retry failed parts.")
    else:
        mf["state"] = "FAILED"
        save_manifest(STREAM_ID, mf)
        log("✗ No parts sent.")
        update_worker_final_state(STREAM_ID, "failed", {"error": "no_parts_sent"})
    
    log("Source files NEVER deleted by this pipeline.")

if __name__ == "__main__":
    main()
