#!/usr/bin/env python3
"""
postprocess_recording.py — PHASE 2: Smart rebuild, segment, verify, deliver.

ARCHITECTURE:
  1. Discover all available sources (raw files, parts, complete MKV)
  2. Rebuild the full recording from the BEST available source
  3. Split into Telegram-safe MP4 segments (target 38-40 MiB, max 42 MiB)
  4. Full verification (ffprobe + decode test + audio check)
  5. Send with state tracking for safe resume

PROTECTED FILES (DO NOT MODIFY):
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

# Size limits
TARGET_SIZE_BYTES  = 40 * 1024 * 1024   # 40 MiB target
MAX_SIZE_BYTES     = 42 * 1024 * 1024   # 42 MiB hard limit
TG_HARD_LIMIT      = 50 * 1024 * 1024   # Telegram absolute limit
SEND_DELAY_SECONDS = 3
MAX_RETRIES        = 3
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
# SOURCE DISCOVERY
# ═══════════════════════════════════════════════════════════
def find_raw_sources() -> dict:
    """Find raw H.264 video and audio files for full rebuild."""
    sources = {"video": None, "audio": None, "complete_mkv": None}

    for d in [PRESERVED_DIR, RECORDINGS_DIR, TEMP_DIR]:
        if not d.exists():
            continue
        for f in d.glob("*.h264"):
            size = f.stat().st_size
            if size > MIN_FILE_SIZE and (sources["video"] is None or size > sources["video"].stat().st_size):
                sources["video"] = f
        for f in d.glob("*_audio.webm"):
            size = f.stat().st_size
            if size > MIN_FILE_SIZE and (sources["audio"] is None or size > sources["audio"].stat().st_size):
                sources["audio"] = f
        for f in d.glob("*_av.mkv"):
            if "_part_" not in f.name:
                size = f.stat().st_size
                if size > MIN_FILE_SIZE and (sources["complete_mkv"] is None or size > sources["complete_mkv"].stat().st_size):
                    sources["complete_mkv"] = f

    return sources


def rebuild_from_raw(video_path: str, audio_path: str, output_path: str) -> bool:
    """Rebuild complete MKV from raw H.264 + audio files."""
    log(f"🔧 Rebuilding from raw sources...")
    log(f"  Video: {os.path.basename(video_path)} ({human_size(os.path.getsize(video_path))})")
    if audio_path:
        log(f"  Audio: {os.path.basename(audio_path)} ({human_size(os.path.getsize(audio_path))})")

    abs_out = os.path.abspath(output_path)
    os.makedirs(os.path.dirname(abs_out), exist_ok=True)
    tmp_out = abs_out + ".tmp.mkv"

    if audio_path:
        cmd = (
            f'ffmpeg -y -i "{os.path.abspath(video_path)}" -i "{os.path.abspath(audio_path)}" '
            f'-c:v copy -c:a copy '
            f'"{tmp_out}"'
        )
    else:
        cmd = (
            f'ffmpeg -y -i "{os.path.abspath(video_path)}" '
            f'-c:v copy -an '
            f'"{tmp_out}"'
        )

    r = run(cmd, timeout=3600)
    if r is None:
        log("  ✗ Rebuild timeout")
        return False
    if r.returncode != 0:
        log(f"  ✗ Rebuild failed: {(r.stderr or '')[-300:]}")
        try: os.unlink(tmp_out)
        except: pass
        return False

    if not os.path.exists(tmp_out) or os.path.getsize(tmp_out) < MIN_FILE_SIZE:
        log("  ✗ Rebuild produced empty/small file")
        try: os.unlink(tmp_out)
        except: pass
        return False

    # Verify
    info = ffprobe_json(tmp_out)
    if not info:
        try: os.unlink(tmp_out)
        except: pass
        return False

    dur = get_duration(info)
    vc, ac = get_codecs(info)
    if not vc or dur < 5.0:
        log(f"  ✗ Rebuild verification failed: dur={dur:.1f}s, v={vc}")
        try: os.unlink(tmp_out)
        except: pass
        return False

    os.rename(tmp_out, abs_out)
    log(f"  ✓ Rebuilt: {human_size(os.path.getsize(abs_out))}, {format_ts(dur)}, v={vc}, a={ac or 'NONE'}")
    return True


def prepare_source() -> tuple:
    """
    Find or rebuild the best complete source.
    Returns (source_path, duration, has_audio) or (None, 0, False).
    """
    sid = safe_id(STREAM_ID)
    TEMP_DIR.mkdir(parents=True, exist_ok=True)
    rebuilt_path = str(TEMP_DIR / f"rebuilt_{sid}.mkv")

    # Check if already rebuilt and valid
    if os.path.exists(rebuilt_path) and os.path.getsize(rebuilt_path) > MIN_FILE_SIZE:
        info = ffprobe_json(rebuilt_path)
        if info:
            dur = get_duration(info)
            vc, ac = get_codecs(info)
            if vc and dur >= 5.0:
                log(f"✓ Using existing rebuilt source: {human_size(os.path.getsize(rebuilt_path))}, {format_ts(dur)}")
                return rebuilt_path, dur, bool(ac)

    # Strategy 1: Rebuild from raw sources (BEST option)
    sources = find_raw_sources()
    if sources["video"] is not None:
        video_str = str(sources["video"])
        audio_str = str(sources["audio"]) if sources["audio"] else None
        if rebuild_from_raw(video_str, audio_str, rebuilt_path):
            info = ffprobe_json(rebuilt_path)
            dur = get_duration(info) if info else 0
            vc, ac = get_codecs(info) if info else (None, None)
            return rebuilt_path, dur, bool(ac)
        log("  ⚠️ Raw rebuild failed, trying other sources...")

    # Strategy 2: Use complete MKV if large enough
    if sources["complete_mkv"] is not None:
        mkv_path = str(sources["complete_mkv"])
        mkv_size = os.path.getsize(mkv_path)
        info = ffprobe_json(mkv_path)
        if info:
            dur = get_duration(info)
            vc, ac = get_codecs(info)
            if vc and dur >= 30.0 and mkv_size > 50 * 1024 * 1024:
                log(f"✓ Using complete MKV: {os.path.basename(mkv_path)} "
                    f"({human_size(mkv_size)}, {format_ts(dur)})")
                return mkv_path, dur, bool(ac)

    # Strategy 3: Concatenate parts from the BEST split attempt
    log("  ⚠️ No complete source available. Attempting parts concatenation...")
    return concatenate_best_parts(sid)


def concatenate_best_parts(sid: str) -> tuple:
    """Concatenate parts, handling multiple split attempts."""
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
                        if size > parts_by_num[part_num][1]:
                            parts_by_num[part_num] = (f, size)
                    else:
                        parts_by_num[part_num] = (f, size)
                except OSError:
                    continue

    if not parts_by_num:
        return None, 0, False

    # Sort by part number
    sorted_parts = sorted(parts_by_num.items(), key=lambda x: x[0])
    log(f"  Found {len(sorted_parts)} unique parts")

    # Get durations and filter corrupt ones
    valid_parts = []
    all_sizes = [size for _, size in sorted_parts]
    all_durs = []

    for part_num, (f, size) in sorted_parts:
        dur = get_duration_file(str(f))
        all_durs.append(dur)

    # Calculate median size/duration ratio for normal parts
    normal_ratios = []
    for i, (part_num, (f, size)) in enumerate(sorted_parts):
        if 0 < all_durs[i] < 600 and size > 0:
            normal_ratios.append(all_durs[i] / size)

    if normal_ratios:
        normal_ratios.sort()
        median_ratio = normal_ratios[len(normal_ratios) // 2]
    else:
        median_ratio = 0.00001

    for i, (part_num, (f, size)) in enumerate(sorted_parts):
        dur = all_durs[i]
        # Detect corrupt duration
        estimated = size * median_ratio
        if dur > estimated * 3 and estimated > 0:
            dur = estimated
            log(f"  Part {part_num}: corrected duration to {dur:.1f}s")
        if dur < 0.5:
            continue
        valid_parts.append((str(f), part_num, size, dur))

    if not valid_parts:
        return None, 0, False

    # Concatenate
    concat_file = str(TEMP_DIR / f"concat_{sid}.txt")
    with open(concat_file, "w", encoding="utf-8") as cf:
        for path, _, _, _ in valid_parts:
            cf.write(f"file '{os.path.abspath(path)}'\n")

    out_file = str(TEMP_DIR / f"concatenated_{sid}.mkv")
    cmd = f'ffmpeg -y -f concat -safe 0 -i "{concat_file}" -c copy "{out_file}"'
    r = run(cmd, timeout=3600)

    if r and r.returncode == 0 and os.path.exists(out_file):
        info = ffprobe_json(out_file)
        if info:
            dur = get_duration(info)
            vc, ac = get_codecs(info)
            if vc and dur >= 5.0:
                log(f"  ✓ Concatenated {len(valid_parts)} parts: {format_ts(dur)}")
                return out_file, dur, bool(ac)

    return None, 0, False


# ═══════════════════════════════════════════════════════════
# SMART SEGMENTATION
# ═══════════════════════════════════════════════════════════
def calculate_segment_duration(source_size: int, source_duration: float) -> float:
    """Calculate optimal segment duration to stay under TARGET_SIZE_BYTES."""
    if source_duration <= 0 or source_size <= 0:
        return 60.0

    # If entire source fits in one segment
    if source_size <= TARGET_SIZE_BYTES:
        return source_duration + 10

    # Calculate bitrate and determine duration
    bitrate = source_size / source_duration  # bytes per second
    max_dur = TARGET_SIZE_BYTES / bitrate
    safe_dur = max_dur * 0.90  # 10% safety margin

    return max(safe_dur, 10.0)


def split_source_to_mp4(source_path: str, output_dir: str, has_audio: bool) -> list:
    """
    Split source into MP4 segments.
    Video: stream copy. Audio: AAC 128k (required for MP4 compatibility).
    """
    seg_dur = calculate_segment_duration(os.path.getsize(source_path), get_duration_file(source_path))
    log(f"📐 Segment duration: {seg_dur:.1f}s (target size: {human_size(TARGET_SIZE_BYTES)})")

    pattern = os.path.join(output_dir, "segment_%03d.mp4")

    if has_audio:
        cmd = (
            f'ffmpeg -y -i "{source_path}" '
            f'-c:v copy -c:a aac -b:a 128k '
            f'-f segment -segment_time {seg_dur:.1f} '
            f'-reset_timestamps 1 -movflags +faststart '
            f'-f mp4 "{pattern}"'
        )
    else:
        cmd = (
            f'ffmpeg -y -i "{source_path}" '
            f'-c:v copy -an '
            f'-f segment -segment_time {seg_dur:.1f} '
            f'-reset_timestamps 1 -movflags +faststart '
            f'-f mp4 "{pattern}"'
        )

    log(f"🔪 Splitting source ({human_size(os.path.getsize(source_path))})...")
    r = run(cmd, timeout=3600)
    if r is None or r.returncode != 0:
        stderr = (r.stderr or "")[-500:] if r else "TIMEOUT"
        log(f"✗ Split failed: {stderr[:300]}")

        # Fallback: try without audio re-encode
        log("  Trying fallback: video-only segments...")
        cmd_fallback = (
            f'ffmpeg -y -i "{source_path}" '
            f'-c:v copy -an '
            f'-f segment -segment_time {seg_dur:.1f} '
            f'-reset_timestamps 1 -movflags +faststart '
            f'-f mp4 "{pattern}"'
        )
        r2 = run(cmd_fallback, timeout=3600)
        if r2 is None or r2.returncode != 0:
            log("✗ Fallback split also failed")
            return []

    segments = sorted(Path(output_dir).glob("segment_*.mp4"), key=lambda p: p.name)
    return [str(s) for s in segments if s.stat().st_size > 0]


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
    cmd = (
        f'ffmpeg -y -i "{seg_path}" '
        f'-c:v copy -c:a aac -b:a 128k '
        f'-f segment -segment_time {target_dur:.1f} '
        f'-reset_timestamps 1 -movflags +faststart '
        f'-f mp4 "{pattern}"'
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
    """Full verification of MP4 segment."""
    result = {
        "ok": False, "reason": "", "duration": 0.0,
        "width": 0, "height": 0, "has_audio": False,
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

    # Size check
    if size > MAX_SIZE_BYTES:
        result["reason"] = f"too large: {human_size(size)} > {human_size(MAX_SIZE_BYTES)}"
        return result

    # ffprobe check
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
        result["reason"] = "no video stream"
        return result

    result["has_audio"] = bool(ac)

    # Audio check: if source has audio, segment must have audio
    if source_has_audio and not ac:
        result["reason"] = "source has audio but segment is silent"
        return result

    w, h = get_resolution(info)
    result["width"] = w
    result["height"] = h

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
    log("Post-processing pipeline — PHASE 2 (Smart Rebuild)")
    log(f"STREAM_ID={STREAM_ID}")
    log("=" * 60)

    mf = load_manifest(STREAM_ID)

    if mf.get("state") == "SENT":
        log("⏭️ Already completed — skipping")
        return

    # ── Step 1: Prepare source ──
    log("\n" + "=" * 60)
    log("STEP 1: Preparing complete source")
    log("=" * 60)

    source_path, source_duration, source_has_audio = prepare_source()

    if not source_path or not os.path.exists(source_path):
        log("✗ No valid source available — ABORTING")
        mf["state"] = "FAILED_NO_SOURCE"
        save_manifest(STREAM_ID, mf)
        update_worker_final_state(STREAM_ID, "failed", {"error": "no_source"})
        return

    log(f"\n✓ Source ready: {os.path.basename(source_path)}")
    log(f"  Size: {human_size(os.path.getsize(source_path))}")
    log(f"  Duration: {format_ts(source_duration)} ({source_duration:.1f}s)")
    log(f"  Has audio: {'YES' if source_has_audio else 'NO'}")

    mf["source_path"] = source_path
    mf["source_duration"] = round(source_duration, 1)
    mf["source_has_audio"] = source_has_audio
    save_manifest(STREAM_ID, mf)

    # ── Step 2: Split into MP4 segments ──
    log("\n" + "=" * 60)
    log("STEP 2: Splitting into MP4 segments")
    log("=" * 60)

    TEMP_DIR.mkdir(parents=True, exist_ok=True)
    mp4_dir = str(TEMP_DIR / f"mp4_{safe_id(STREAM_ID)}")
    os.makedirs(mp4_dir, exist_ok=True)

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
        if os.path.getsize(seg) > MAX_SIZE_BYTES:
            subs = resplit_oversized(seg, mp4_dir, idx)
            for sub in subs:
                if os.path.getsize(sub) <= MAX_SIZE_BYTES:
                    final_segments.append(sub)
                else:
                    log(f"    ✗ Still oversized after resplit: {human_size(os.path.getsize(sub))}")
                    mf["errors"].append(f"oversized:{os.path.basename(sub)}")
        else:
            final_segments.append(sub if 'sub' in dir() else seg)

    # Fix: rebuild final_segments properly
    final_segments = []
    for idx, seg in enumerate(segments):
        if os.path.getsize(seg) > MAX_SIZE_BYTES:
            subs = resplit_oversized(seg, mp4_dir, idx)
            final_segments.extend(subs)
        else:
            final_segments.append(seg)

    total = len(final_segments)
    log(f"\n📦 Total segments: {total}")

    if not final_segments:
        log("✗ No valid segments — ABORTING")
        mf["state"] = "FAILED_NO_SEGMENTS"
        save_manifest(STREAM_ID, mf)
        return

    # ── Step 3: Verify and send ──
    log("\n" + "=" * 60)
    log("STEP 3: Verifying and sending segments")
    log("=" * 60)

    display_name = STREAM_NAME or f"recording_{STREAM_ID}"
    sent_count = 0
    failed_count = 0
    total_sent_duration = 0.0
    failed_segments = []

    for idx, seg_path in enumerate(final_segments):
        seg_name = os.path.basename(seg_path)
        seg_key = f"seg_{idx:04d}"

        # Skip already sent
        prev = mf["segments"].get(seg_key, {})
        if prev.get("sent") and prev.get("message_id"):
            log(f"  [{idx+1}/{total}] {seg_name}: Already sent — skip")
            sent_count += 1
            total_sent_duration += prev.get("duration", 0)
            continue

        # Verify
        verdict = verify_mp4_full(seg_path, source_has_audio)
        if not verdict["ok"]:
            log(f"  [{idx+1}/{total}] {seg_name}: ✗ {verdict['reason']}")
            mf["segments"][seg_key] = {"sent": False, "error": verdict["reason"]}
            mf["errors"].append(f"{seg_name}:{verdict['reason']}")
            failed_count += 1
            failed_segments.append(idx)
            save_manifest(STREAM_ID, mf)
            continue

        seg_size = verdict["size"]
        log(f"  [{idx+1}/{total}] {seg_name}: ✓ {human_size(seg_size)}, "
            f"{verdict['duration']:.1f}s, audio={'YES' if verdict['has_audio'] else 'NO'}")

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
            mf["segments"][seg_key] = {
                "sent": True,
                "message_id": tg_res["message_id"],
                "size": seg_size,
                "duration": round(verdict["duration"], 1),
                "has_audio": verdict["has_audio"],
            }
        else:
            log(f"    ✗ Send failed: {tg_res['error']}")
            mf["segments"][seg_key] = {"sent": False, "error": tg_res["error"]}
            failed_count += 1
            failed_segments.append(idx)

        save_manifest(STREAM_ID, mf)

        if idx < total - 1:
            time.sleep(SEND_DELAY_SECONDS)

    # ── Final Report ──
    log("\n" + "=" * 60)
    log("FINAL REPORT")
    log("=" * 60)

    coverage = min(total_sent_duration / source_duration, 1.0) if source_duration > 0 else 0

    log(f"  Source duration:       {format_ts(source_duration)}")
    log(f"  Segments produced:     {total}")
    log(f"  Sent successfully:     {sent_count}")
    log(f"  Failed:                {failed_count}")
    log(f"  Duration sent:         {format_ts(total_sent_duration)}")
    log(f"  Coverage:              {coverage*100:.1f}%")
    log(f"  Source has audio:      {'YES' if source_has_audio else 'NO'}")
    if failed_segments:
        log(f"  Failed segment indices: {failed_segments}")
    log("=" * 60)

    if coverage >= 0.95 and failed_count == 0:
        mf["state"] = "SENT"
        mf["coverage"] = round(coverage, 3)
        save_manifest(STREAM_ID, mf)
        log("✅ ALL segments sent with full coverage")
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
        log(f"⚠️ PARTIAL: {sent_count}/{total}. Re-run to retry.")
    else:
        mf["state"] = "FAILED"
        save_manifest(STREAM_ID, mf)
        log("✗ No segments sent.")
        update_worker_final_state(STREAM_ID, "failed", {"error": "no_segments_sent"})

    log("Source files NEVER deleted by this pipeline.")


if __name__ == "__main__":
    main()
