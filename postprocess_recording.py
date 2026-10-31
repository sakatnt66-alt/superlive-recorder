#!/usr/bin/env python3
"""
postprocess_recording.py — PHASE 2: MKV→MP4 conversion, segmentation, delivery.

CRITICAL FIXES:
  1. Source selection: MKV complete file FIRST, then parts, then raw (last resort)
  2. Split command: removed conflicting '-f mp4' after '-f segment'
  3. Temp files: use .tmp.mp4 extension (not .mp4.tmp)
  4. Full verification: ffprobe + decode test + audio check
  5. Detailed final report

PROTECTED (DO NOT MODIFY):
  - record_once.py
  - full_stream_contact_sheet.py
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

# Size limits (bytes)
TARGET_SIZE_BYTES  = 40 * 1024 * 1024   # 40 MiB target
MAX_SIZE_BYTES     = 42 * 1024 * 1024   # 42 MiB hard limit
TG_HARD_LIMIT      = 50 * 1024 * 1024   # Telegram absolute limit
SEND_DELAY_SECONDS = 3
MAX_RETRIES        = 3
MAX_RESPLIT_TRIES  = 3
MIN_FILE_SIZE      = 100 * 1024         # 100 KB minimum

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
        if ct == "video" and vc is None:
            vc = cn
        elif ct == "audio" and ac is None:
            ac = cn
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
                if int(den) > 0:
                    return int(num) / int(den)
            except (ValueError, ZeroDivisionError):
                pass
    return 0.0


def human_size(n: int) -> str:
    mb = n / (1024 * 1024)
    return f"{mb / 1024:.2f} GB" if mb >= 1024 else f"{mb:.1f} MB"


def format_ts(seconds: float) -> str:
    h, rem = divmod(int(seconds), 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def safe_id(sid: str) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in str(sid))


def update_worker_final_state(sid: str, status: str, extra: dict = None):
    if not WORKER_API_URL or not AUTO_API_TOKEN:
        log("⚠️ Worker API not configured, skipping state update")
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
        with urllib.request.urlopen(req, timeout=30):
            log(f"✓ Worker state updated: {status}")
    except Exception as e:
        log(f"⚠️ Worker state update failed: {e}")


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
    return {"stream_id": sid, "segments": {}, "state": "DISCOVERED", "errors": []}


def save_manifest(sid: str, mf: dict) -> None:
    p = manifest_path(sid)
    tmp = p.with_suffix(".tmp")
    mf["updated_at"] = datetime.now(timezone.utc).isoformat()
    tmp.write_text(json.dumps(mf, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(p)


# ═══════════════════════════════════════════════════════════
# SOURCE SELECTION — Priority: MKV complete > Parts > Raw
# ═══════════════════════════════════════════════════════════
def validate_source_file(path: str, min_duration: float = 30.0) -> dict:
    """Validate a source file thoroughly."""
    result = {
        "valid": False, "reason": "", "duration": 0.0,
        "size": 0, "video_codec": None, "audio_codec": None,
        "width": 0, "height": 0, "fps": 0.0
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

        info = ffprobe_json(path)
        if not info:
            result["reason"] = "ffprobe failed"
            return result

        dur = get_duration(info)
        result["duration"] = dur
        if dur < min_duration:
            result["reason"] = f"duration too short ({dur:.1f}s < {min_duration}s)"
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

        result["valid"] = True
        result["reason"] = "ok"
    except Exception as e:
        result["reason"] = str(e)
    return result


def find_best_source() -> dict:
    """
    Find the best source for processing.
    Priority:
    1. Complete MKV file (not parts, not encoded) with valid duration
    2. Concatenated parts
    3. Raw H.264 + audio (last resort, with rebuild)
    """
    log("🔍 Searching for best source...")

    # ── Priority 1: Complete MKV file ──
    mkv_candidates = []
    for d in [PRESERVED_DIR, RECORDINGS_DIR, TEMP_DIR]:
        if not d.exists():
            continue
        for f in d.glob("*.mkv"):
            name = f.name
            # Skip parts, encoded, concatenated, temp files
            if "_part_" in name or "_encoded" in name or "concatenated_" in name or "rebuilt_" in name:
                continue
            try:
                size = f.stat().st_size
                if size >= MIN_FILE_SIZE:
                    mkv_candidates.append((f, size))
            except OSError:
                continue

    # Sort by size descending
    mkv_candidates.sort(key=lambda x: x[1], reverse=True)

    for f, size in mkv_candidates:
        validation = validate_source_file(str(f), min_duration=30.0)
        if validation["valid"]:
            log(f"  ✓ Complete MKV found: {f.name}")
            log(f"    Size: {human_size(size)}, Duration: {format_ts(validation['duration'])}")
            log(f"    Video: {validation['video_codec']} {validation['width']}x{validation['height']} @ {validation['fps']:.1f}fps")
            log(f"    Audio: {validation['audio_codec'] or 'NONE'}")
            return {
                "type": "complete_mkv",
                "path": str(f),
                "validation": validation
            }
        else:
            log(f"  ⚠️ Skipping {f.name}: {validation['reason']}")

    # ── Priority 2: Concatenate parts ──
    log("  No complete MKV found. Checking parts...")
    parts = discover_parts()
    if parts and len(parts) > 0:
        concat_result = concatenate_parts(parts)
        if concat_result:
            validation = validate_source_file(concat_result, min_duration=10.0)
            if validation["valid"]:
                log(f"  ✓ Concatenated {len(parts)} parts successfully")
                return {
                    "type": "concatenated_parts",
                    "path": concat_result,
                    "validation": validation
                }
            else:
                log(f"  ⚠️ Concatenated file invalid: {validation['reason']}")

    # ── Priority 3: Raw H.264 + audio rebuild (last resort) ──
    log("  No parts found. Checking raw sources...")
    raw_video = None
    raw_audio = None
    for d in [PRESERVED_DIR, RECORDINGS_DIR, TEMP_DIR]:
        if not d.exists():
            continue
        for f in d.glob("*_encoded.h264"):
            size = f.stat().st_size
            if size > MIN_FILE_SIZE and (raw_video is None or size > raw_video.stat().st_size):
                raw_video = f
        for f in d.glob("*_audio.webm"):
            size = f.stat().st_size
            if size > MIN_FILE_SIZE and (raw_audio is None or size > raw_audio.stat().st_size):
                raw_audio = f

    if raw_video is not None:
        rebuild_path = str(TEMP_DIR / f"rebuilt_{safe_id(STREAM_ID)}.mkv")
        if rebuild_from_raw(str(raw_video), str(raw_audio) if raw_audio else None, rebuild_path):
            validation = validate_source_file(rebuild_path, min_duration=10.0)
            if validation["valid"]:
                log(f"  ✓ Rebuilt from raw sources")
                return {
                    "type": "rebuilt_raw",
                    "path": rebuild_path,
                    "validation": validation
                }
            else:
                log(f"  ⚠️ Rebuilt file invalid: {validation['reason']}")

    log("  ✗ No valid source found")
    return None


def discover_parts() -> list:
    """Find all MKV parts, deduplicated by part number."""
    parts_by_num = {}
    for d in [PRESERVED_DIR, RECORDINGS_DIR, TEMP_DIR]:
        if not d.exists():
            continue
        for pattern in ["*_av_part_*.mkv", "*_av_part_*.webm"]:
            for f in d.glob(pattern):
                try:
                    size = f.stat().st_size
                    if size < MIN_FILE_SIZE:
                        continue
                    m = re.search(r'_part_(\d+)', f.name)
                    if not m:
                        continue
                    part_num = int(m.group(1))
                    if part_num in parts_by_num:
                        if "_preserved" in str(f):
                            parts_by_num[part_num] = (f, size)
                        elif size > parts_by_num[part_num][1]:
                            parts_by_num[part_num] = (f, size)
                    else:
                        parts_by_num[part_num] = (f, size)
                except OSError:
                    continue

    parts = []
    for part_num in sorted(parts_by_num.keys()):
        f, size = parts_by_num[part_num]
        parts.append((f, part_num, size))
    return parts


def concatenate_parts(parts: list) -> str:
    """Concatenate parts into a single MKV file."""
    if not parts:
        return None

    sid = safe_id(STREAM_ID)
    TEMP_DIR.mkdir(parents=True, exist_ok=True)
    concat_file = str(TEMP_DIR / f"concat_{sid}.txt")
    out_file = str(TEMP_DIR / f"concatenated_{sid}.mkv")

    # Check if already concatenated
    if os.path.exists(out_file) and os.path.getsize(out_file) > MIN_FILE_SIZE:
        info = ffprobe_json(out_file)
        if info and get_duration(info) >= 10.0:
            return out_file

    log(f"  🔗 Concatenating {len(parts)} parts...")

    with open(concat_file, "w", encoding="utf-8") as cf:
        for f, part_num, size in parts:
            cf.write(f"file '{os.path.abspath(str(f))}'\n")

    cmd = f'ffmpeg -y -f concat -safe 0 -i "{concat_file}" -c copy "{out_file}"'
    r = run(cmd, timeout=3600)
    if r and r.returncode == 0 and os.path.exists(out_file) and os.path.getsize(out_file) > MIN_FILE_SIZE:
        return out_file

    log(f"  ✗ Concatenation failed: {(r.stderr or '')[-200:] if r else 'timeout'}")
    return None


def rebuild_from_raw(video_path: str, audio_path: str, output_path: str) -> bool:
    """Rebuild MKV from raw H.264 + audio files."""
    log(f"  🔧 Rebuilding from raw sources...")

    abs_out = os.path.abspath(output_path)
    os.makedirs(os.path.dirname(abs_out), exist_ok=True)
    tmp_out = abs_out + ".tmp.mkv"

    if audio_path:
        cmd = (
            f'ffmpeg -y -i "{os.path.abspath(video_path)}" '
            f'-i "{os.path.abspath(audio_path)}" '
            f'-c:v copy -c:a copy "{tmp_out}"'
        )
    else:
        cmd = (
            f'ffmpeg -y -i "{os.path.abspath(video_path)}" '
            f'-c:v copy -an "{tmp_out}"'
        )

    r = run(cmd, timeout=3600)
    if r is None or r.returncode != 0:
        log(f"  ✗ Rebuild failed: {(r.stderr or '')[-200:] if r else 'timeout'}")
        try:
            os.unlink(tmp_out)
        except OSError:
            pass
        return False

    if not os.path.exists(tmp_out) or os.path.getsize(tmp_out) < MIN_FILE_SIZE:
        try:
            os.unlink(tmp_out)
        except OSError:
            pass
        return False

    # Verify before accepting
    info = ffprobe_json(tmp_out)
    if not info:
        try:
            os.unlink(tmp_out)
        except OSError:
            pass
        return False

    dur = get_duration(info)
    vc, _ = get_codecs(info)
    if not vc or dur < 10.0:
        log(f"  ✗ Rebuild verification failed: dur={dur:.1f}s")
        try:
            os.unlink(tmp_out)
        except OSError:
            pass
        return False

    os.rename(tmp_out, abs_out)
    log(f"  ✓ Rebuilt: {human_size(os.path.getsize(abs_out))}, {format_ts(dur)}")
    return True


# ═══════════════════════════════════════════════════════════
# SEGMENTATION — Fixed FFmpeg command
# ═══════════════════════════════════════════════════════════
def calculate_segment_duration(source_size: int, source_duration: float) -> float:
    """Calculate segment duration to stay under TARGET_SIZE_BYTES."""
    if source_duration <= 0 or source_size <= 0:
        return 60.0
    if source_size <= TARGET_SIZE_BYTES:
        return source_duration + 10
    bitrate = source_size / source_duration
    max_dur = TARGET_SIZE_BYTES / bitrate
    safe_dur = max_dur * 0.90
    return max(safe_dur, 10.0)


def split_source_to_mp4(source_path: str, output_dir: str, has_audio: bool) -> list:
    """
    Split source into MP4 segments using FFmpeg segment muxer.

    CRITICAL FIX: Do NOT add '-f mp4' after '-f segment'.
    The .mp4 extension in the output pattern is sufficient.
    Adding '-f mp4' overrides '-f segment' and produces a single file.
    """
    source_size = os.path.getsize(source_path)
    source_dur = get_duration_file(source_path)
    seg_dur = calculate_segment_duration(source_size, source_dur)

    expected_segments = max(1, int(source_dur / seg_dur) + 1)
    log(f"📐 Segment duration: {seg_dur:.1f}s")
    log(f"📊 Expected segments: ~{expected_segments}")
    log(f"📦 Source: {human_size(source_size)}, {format_ts(source_dur)}")

    os.makedirs(output_dir, exist_ok=True)

    # Output pattern — FFmpeg will replace %03d with actual numbers
    pattern = os.path.join(output_dir, "part_%03d.mp4")

    if has_audio:
        # Video: stream copy. Audio: AAC re-encode for MP4 compatibility.
        cmd = (
            f'ffmpeg -y -i "{source_path}" '
            f'-c:v copy -c:a aac -b:a 128k '
            f'-f segment -segment_time {seg_dur:.1f} '
            f'-reset_timestamps 1 -movflags +faststart '
            f'"{pattern}"'
        )
    else:
        cmd = (
            f'ffmpeg -y -i "{source_path}" '
            f'-c:v copy -an '
            f'-f segment -segment_time {seg_dur:.1f} '
            f'-reset_timestamps 1 -movflags +faststart '
            f'"{pattern}"'
        )

    log(f"🔪 Splitting...")
    r = run(cmd, timeout=3600)

    if r is None:
        log("✗ Split command timed out")
        return []

    if r.returncode != 0:
        stderr = (r.stderr or "")[-500:]
        log(f"✗ Split failed (exit={r.returncode}): {stderr[:300]}")

        # Fallback: try video-only
        if has_audio:
            log("  Trying fallback: video-only split...")
            cmd_fallback = (
                f'ffmpeg -y -i "{source_path}" '
                f'-c:v copy -an '
                f'-f segment -segment_time {seg_dur:.1f} '
                f'-reset_timestamps 1 -movflags +faststart '
                f'"{pattern}"'
            )
            r2 = run(cmd_fallback, timeout=3600)
            if r2 is None or r2.returncode != 0:
                log("✗ Fallback split also failed")
                return []

    # Collect actual output files
    segments = sorted(Path(output_dir).glob("part_*.mp4"), key=lambda p: p.name)
    segments = [str(s) for s in segments if s.stat().st_size > 0]

    # CRITICAL: Verify no file has literal %03d in its name
    segments = [s for s in segments if "%03d" not in os.path.basename(s)]

    if not segments:
        log("✗ No segments produced")
        return []

    log(f"✓ Produced {len(segments)} segments")
    for i, seg in enumerate(segments):
        seg_size = os.path.getsize(seg)
        seg_dur = get_duration_file(seg)
        log(f"  [{i+1}] {os.path.basename(seg)}: {human_size(seg_size)}, {seg_dur:.1f}s")

    return segments


def resplit_oversized(seg_path: str, output_dir: str, idx: int) -> list:
    """Split an oversized segment into smaller pieces."""
    size = os.path.getsize(seg_path)
    info = ffprobe_json(seg_path)
    if not info:
        return [seg_path]
    dur = get_duration(info)
    if dur <= 0:
        return [seg_path]

    bitrate = size / dur
    target_dur = max((TARGET_SIZE_BYTES * 0.85) / bitrate, 5.0)
    log(f"    ⚠️ Oversized ({human_size(size)}) → splitting into ~{target_dur:.0f}s pieces")

    pattern = os.path.join(output_dir, f"resplit_{idx:03d}_%03d.mp4")

    # Check if source has audio
    _, ac = get_codecs(info)
    audio_flag = "-c:a aac -b:a 128k" if ac else "-an"

    cmd = (
        f'ffmpeg -y -i "{seg_path}" '
        f'-c:v copy {audio_flag} '
        f'-f segment -segment_time {target_dur:.1f} '
        f'-reset_timestamps 1 -movflags +faststart '
        f'"{pattern}"'
    )
    r = run(cmd, timeout=600)
    if r and r.returncode == 0:
        subs = sorted(Path(output_dir).glob(f"resplit_{idx:03d}_*.mp4"), key=lambda p: p.name)
        subs = [str(s) for s in subs if s.stat().st_size > 0]
        if subs:
            log(f"    ✓ Produced {len(subs)} sub-segments")
            return subs
    return [seg_path]


# ═══════════════════════════════════════════════════════════
# VERIFICATION
# ═══════════════════════════════════════════════════════════
def verify_mp4_full(path: str, source_has_audio: bool) -> dict:
    """Full verification of an MP4 segment."""
    result = {
        "ok": False, "reason": "", "duration": 0.0,
        "width": 0, "height": 0, "fps": 0.0,
        "has_audio": False, "has_video": False,
        "size": 0, "decode_ok": False
    }

    if not os.path.exists(path):
        result["reason"] = "file not found"
        return result

    size = os.path.getsize(path)
    result["size"] = size
    if size == 0:
        result["reason"] = "empty file"
        return result

    if size > MAX_SIZE_BYTES:
        result["reason"] = f"too large: {human_size(size)} > {human_size(MAX_SIZE_BYTES)}"
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
    result["has_video"] = bool(vc)
    result["has_audio"] = bool(ac)

    if not vc:
        result["reason"] = "no video stream"
        return result

    if source_has_audio and not ac:
        result["reason"] = "source has audio but segment is silent"
        return result

    w, h = get_resolution(info)
    result["width"] = w
    result["height"] = h
    result["fps"] = get_fps(info)

    # Actual decode test
    decode_cmd = f'ffmpeg -v error -i "{path}" -f null -'
    dr = run(decode_cmd, timeout=120)
    if dr is None:
        result["reason"] = "decode test timeout"
        return result
    if dr.returncode != 0:
        result["reason"] = f"decode failed: {(dr.stderr or '')[:100]}"
        return result

    result["decode_ok"] = True
    result["ok"] = True
    result["reason"] = "ok"
    return result


# ═══════════════════════════════════════════════════════════
# TELEGRAM SENDING
# ═══════════════════════════════════════════════════════════
def tg_send_video(video_path: str, caption: str, duration: int,
                  width: int, height: int) -> dict:
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
            log(f"    sendVideo attempt {attempt}/{MAX_RETRIES}: HTTP {e.code}")
        except Exception as e:
            result["error"] = str(e)
            log(f"    sendVideo attempt {attempt}/{MAX_RETRIES}: {e}")
        if attempt < MAX_RETRIES:
            time.sleep(5 * attempt)
    return result


# ═══════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════
def main():
    log("=" * 60)
    log("Post-processing pipeline — PHASE 2")
    log(f"STREAM_ID={STREAM_ID}")
    log(f"WORKER_API={'SET' if WORKER_API_URL else 'MISSING'}")
    log("=" * 60)

    mf = load_manifest(STREAM_ID)

    if mf.get("state") == "SENT":
        log("⏭️ Already completed — skipping")
        return

    # ── Step 1: Find best source ──
    log("\n" + "=" * 60)
    log("STEP 1: Source Selection")
    log("=" * 60)

    source_info = find_best_source()
    if not source_info:
        log("✗ No valid source found — ABORTING")
        mf["state"] = "FAILED_NO_SOURCE"
        save_manifest(STREAM_ID, mf)
        update_worker_final_state(STREAM_ID, "failed", {"error": "no_source_found"})
        return

    source_path = source_info["path"]
    validation = source_info["validation"]
    source_duration = validation["duration"]
    source_has_audio = bool(validation["audio_codec"])

    log(f"\n✓ Selected source: {source_info['type']}")
    log(f"  File: {os.path.basename(source_path)}")
    log(f"  Size: {human_size(validation['size'])}")
    log(f"  Duration: {format_ts(source_duration)} ({source_duration:.1f}s)")
    log(f"  Video: {validation['video_codec']} {validation['width']}x{validation['height']} @ {validation['fps']:.1f}fps")
    log(f"  Audio: {validation['audio_codec'] or 'NONE'}")

    mf["source"] = {
        "type": source_info["type"],
        "path": source_path,
        "duration": round(source_duration, 1),
        "size": validation["size"],
        "has_audio": source_has_audio,
    }
    save_manifest(STREAM_ID, mf)

    # ── Step 2: Split into MP4 segments ──
    log("\n" + "=" * 60)
    log("STEP 2: Splitting into MP4 segments")
    log("=" * 60)

    TEMP_DIR.mkdir(parents=True, exist_ok=True)
    mp4_dir = str(TEMP_DIR / f"mp4_{safe_id(STREAM_ID)}")

    segments = split_source_to_mp4(source_path, mp4_dir, source_has_audio)

    if not segments:
        log("✗ No segments produced — ABORTING")
        mf["state"] = "FAILED_SPLIT"
        save_manifest(STREAM_ID, mf)
        update_worker_final_state(STREAM_ID, "failed", {"error": "split_failed"})
        return

    # Handle oversized segments
    final_segments = []
    for idx, seg in enumerate(segments):
        seg_size = os.path.getsize(seg)
        if seg_size > MAX_SIZE_BYTES:
            for try_num in range(MAX_RESPLIT_TRIES):
                subs = resplit_oversized(seg, mp4_dir, idx)
                all_ok = all(os.path.getsize(s) <= MAX_SIZE_BYTES for s in subs)
                if all_ok:
                    final_segments.extend(subs)
                    break
                if try_num == MAX_RESPLIT_TRIES - 1:
                    log(f"    ✗ Still oversized after {MAX_RESPLIT_TRIES} tries")
                    mf["errors"].append(f"oversized:{os.path.basename(seg)}")
        else:
            final_segments.append(seg)

    total = len(final_segments)
    log(f"\n📦 Final segments: {total}")

    if not final_segments:
        log("✗ No valid segments — ABORTING")
        mf["state"] = "FAILED_NO_SEGMENTS"
        save_manifest(STREAM_ID, mf)
        return

    # ── Step 3: Verify and send ──
    log("\n" + "=" * 60)
    log("STEP 3: Verifying and sending")
    log("=" * 60)

    display_name = STREAM_NAME or f"recording_{STREAM_ID}"
    sent_count = 0
    failed_count = 0
    total_sent_duration = 0.0
    failed_segments = []
    segment_report = []

    for idx, seg_path in enumerate(final_segments):
        seg_name = os.path.basename(seg_path)
        seg_key = f"seg_{idx:04d}"

        # Skip already sent
        prev = mf["segments"].get(seg_key, {})
        if prev.get("sent") and prev.get("message_id"):
            log(f"  [{idx+1}/{total}] {seg_name}: Already sent (msg={prev['message_id']}) — skip")
            sent_count += 1
            total_sent_duration += prev.get("duration", 0)
            continue

        # Verify
        verdict = verify_mp4_full(seg_path, source_has_audio)

        seg_report = {
            "index": idx + 1,
            "file": seg_name,
            "size": verdict["size"],
            "duration": verdict["duration"],
            "has_audio": verdict["has_audio"],
            "decode_ok": verdict["decode_ok"],
            "sent": False,
            "error": None,
        }

        if not verdict["ok"]:
            log(f"  [{idx+1}/{total}] {seg_name}: ✗ {verdict['reason']}")
            mf["segments"][seg_key] = {"sent": False, "error": verdict["reason"]}
            mf["errors"].append(f"{seg_name}:{verdict['reason']}")
            failed_count += 1
            failed_segments.append(idx + 1)
            seg_report["error"] = verdict["reason"]
            segment_report.append(seg_report)
            save_manifest(STREAM_ID, mf)
            continue

        log(f"  [{idx+1}/{total}] {seg_name}: ✓ {human_size(verdict['size'])}, "
            f"{verdict['duration']:.1f}s, audio={'YES' if verdict['has_audio'] else 'NO'}, "
            f"decode=OK")

        # Send
        caption = (
            f"🎬 {display_name}\n"
            f"🆔 {STREAM_ID}\n"
            f"📎 المقطع {idx+1} من {total}\n"
            f"⏱️ {format_ts(verdict['duration'])}"
        )

        tg_res = tg_send_video(
            seg_path, caption,
            int(verdict["duration"]),
            verdict["width"], verdict["height"]
        )

        if tg_res["ok"]:
            log(f"    📤 SENT ✅ (msg={tg_res['message_id']})")
            sent_count += 1
            total_sent_duration += verdict["duration"]
            seg_report["sent"] = True
            seg_report["message_id"] = tg_res["message_id"]
            mf["segments"][seg_key] = {
                "sent": True,
                "message_id": tg_res["message_id"],
                "size": verdict["size"],
                "duration": round(verdict["duration"], 1),
                "has_audio": verdict["has_audio"],
            }
        else:
            log(f"    ✗ Send failed: {tg_res['error']}")
            mf["segments"][seg_key] = {"sent": False, "error": tg_res["error"]}
            failed_count += 1
            failed_segments.append(idx + 1)
            seg_report["error"] = tg_res["error"]

        segment_report.append(seg_report)
        save_manifest(STREAM_ID, mf)

        if idx < total - 1:
            time.sleep(SEND_DELAY_SECONDS)

    # ── Final Report ──
    log("\n" + "=" * 60)
    log("FINAL REPORT")
    log("=" * 60)

    coverage = min(total_sent_duration / source_duration, 1.0) if source_duration > 0 else 0

    log(f"  Source type:           {source_info['type']}")
    log(f"  Source file:           {os.path.basename(source_path)}")
    log(f"  Source size:           {human_size(validation['size'])}")
    log(f"  Source duration:       {format_ts(source_duration)} ({source_duration:.1f}s)")
    log(f"  Source video:          {validation['video_codec']} {validation['width']}x{validation['height']}")
    log(f"  Source audio:          {validation['audio_codec'] or 'NONE'}")
    log(f"  Source FPS:            {validation['fps']:.1f}")
    log(f"  Segments produced:     {total}")
    log(f"  Sent successfully:     {sent_count}")
    log(f"  Failed:                {failed_count}")
    log(f"  Duration sent:         {format_ts(total_sent_duration)} ({total_sent_duration:.1f}s)")
    log(f"  Coverage:              {coverage*100:.1f}%")
    if failed_segments:
        log(f"  Failed segments:       {failed_segments}")
    log("=" * 60)

    # Per-segment summary
    for sr in segment_report:
        status = "✅" if sr["sent"] else "❌"
        audio_str = "audio=YES" if sr["has_audio"] else "audio=NO"
        err_str = f" ({sr['error']})" if sr["error"] else ""
        log(f"  {status} [{sr['index']}/{total}] {sr['file']}: "
            f"{human_size(sr['size'])}, {sr['duration']:.1f}s, {audio_str}{err_str}")

    if coverage >= 0.95 and failed_count == 0:
        mf["state"] = "SENT"
        mf["coverage"] = round(coverage, 3)
        mf["report"] = segment_report
        save_manifest(STREAM_ID, mf)
        log("\n✅ ALL segments sent with full coverage")
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
        log(f"\n⚠️ PARTIAL: {sent_count}/{total} sent. Re-run to retry failed segments.")
    else:
        mf["state"] = "FAILED"
        save_manifest(STREAM_ID, mf)
        log("\n✗ No segments sent.")
        update_worker_final_state(STREAM_ID, "failed", {"error": "no_segments_sent"})

    log("Source files NEVER deleted by this pipeline.")


if __name__ == "__main__":
    main()
