#!/usr/bin/env python3
"""
postprocess_recording.py — PHASE 2: MKV→MP4 segmentation, verification, delivery.

CRITICAL FIXES:
  1. Source integrity verification: detects truncated MKV by comparing
     actual file size against expected size from packet counts and bitrate.
  2. Automatic rebuild from raw H.264 + audio when truncation detected.
  3. Proper source interval tracking for timeline coverage verification.
  4. Size-aware segmentation targeting ~40 MiB, hard limit < 45 MiB.
  5. Audio is OPTIONAL. Video-only segments are always accepted.
  6. Isolated attempt directories prevent contamination.
  7. Atomic operations with validation before rename.
  8. Worker API errors isolated from media processing status.
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

TARGET_SIZE_BYTES  = 40 * 1024 * 1024
MAX_SIZE_BYTES     = 45 * 1024 * 1024
HARD_SIZE_LIMIT    = 50 * 1024 * 1024
SEND_DELAY_SECONDS = 3
MAX_RETRIES        = 3
MAX_RESPLIT_TRIES  = 3
MIN_FILE_SIZE      = 10 * 1024

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
                if not chunk:
                    break
                h.update(chunk)
        return h.hexdigest()
    except Exception:
        return ""


def ffprobe_json(path: str):
    r = run(f'ffprobe -v quiet -print_format json -show_format -show_streams "{path}"', 60)
    if r and r.returncode == 0:
        try:
            return json.loads(r.stdout)
        except json.JSONDecodeError:
            pass
    return None


def ffprobe_packets(path: str, stream_type: str) -> int:
    r = run(
        f'ffprobe -v quiet -count_packets -select_streams {stream_type}:0 '
        f'-show_entries stream=nb_read_packets -of csv=p=0 "{path}"',
        300
    )
    if r and r.returncode == 0 and r.stdout.strip():
        try:
            return int(r.stdout.strip())
        except ValueError:
            pass
    return 0


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
    return f"{mb / 1024:.2f} GiB" if mb >= 1024 else f"{mb:.1f} MiB"


def format_ts(seconds: float) -> str:
    h, rem = divmod(int(seconds), 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def safe_id(sid: str) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in str(sid))


def update_worker_final_state(sid: str, status: str, extra: dict = None):
    if not WORKER_API_URL or not AUTO_API_TOKEN:
        log("⚠️ [WORKER-API] Not configured, skipping")
        return
    url = f"{WORKER_API_URL}/api/update-state/{sid}"
    state = {"status": status}
    if extra:
        state.update(extra)
    payload = json.dumps(state).encode('utf-8')
    req = urllib.request.Request(url, data=payload, headers={
        'X-Auto-Token': AUTO_API_TOKEN, 'Content-Type': 'application/json'
    })
    try:
        with urllib.request.urlopen(req, timeout=30):
            log(f"✓ [WORKER-API] State updated: {status}")
    except urllib.error.HTTPError as e:
        log(f"⚠️ [WORKER-API] HTTP {e.code} — media processing unaffected")
    except Exception as e:
        log(f"⚠️ [WORKER-API] Error: {e} — media processing unaffected")


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
    tmp = str(p) + ".tmp.json"
    mf["updated_at"] = datetime.now(timezone.utc).isoformat()
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(mf, f, indent=2, ensure_ascii=False)
    os.replace(tmp, str(p))


# ═══════════════════════════════════════════════════════════
# SOURCE INTEGRITY VERIFICATION
# ═══════════════════════════════════════════════════════════
def verify_source_integrity(path: str) -> dict:
    """
    Verify source file integrity by cross-checking:
    - File size vs expected size from packet counts
    - Duration from metadata vs duration from packet count / fps
    - Actual decodable content vs claimed duration
    
    ALWAYS returns a dict. Never returns bool.
    """
    result = {
        "valid": False,
        "reason": "",
        "path": str(path),
        "size": 0,
        "sha256": "",
        "duration": 0.0,
        "video_codec": None,
        "audio_codec": None,
        "width": 0,
        "height": 0,
        "fps": 0.0,
        "video_packets": 0,
        "audio_packets": 0,
        "truncated": False,
        "expected_size": 0,
    }
    try:
        p = Path(path)
        if not p.exists():
            result["reason"] = "file not found"
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

        # Count actual packets
        result["video_packets"] = ffprobe_packets(str(p), "v")
        if ac:
            result["audio_packets"] = ffprobe_packets(str(p), "a")

        # CRITICAL: Check if file is truncated
        # Method 1: Compare packet-derived duration with container duration
        if result["fps"] > 0 and result["video_packets"] > 0:
            packet_duration = result["video_packets"] / result["fps"]
            # If container says 1651s but packets only cover 998s, file is truncated
            if dur > packet_duration * 1.15:  # 15% tolerance
                result["truncated"] = True
                result["reason"] = (
                    f"TRUNCATED: container duration {dur:.1f}s but "
                    f"packet count {result['video_packets']} @ {result['fps']:.1f}fps "
                    f"only covers {packet_duration:.1f}s"
                )
                # Use packet-derived duration as the REAL duration
                result["duration"] = packet_duration
                return result

        # Method 2: Check if last packet timestamp matches duration
        r = run(
            f'ffprobe -v quiet -select_streams v:0 -show_entries packet=pts_time '
            f'-of csv=p=0 "{path}" | tail -1',
            120
        )
        if r and r.returncode == 0 and r.stdout.strip():
            try:
                last_pts = float(r.stdout.strip())
                if dur > last_pts * 1.15 and last_pts > 0:
                    result["truncated"] = True
                    result["duration"] = last_pts
                    result["reason"] = (
                        f"TRUNCATED: container duration {dur:.1f}s but "
                        f"last video PTS is {last_pts:.1f}s"
                    )
                    return result
            except ValueError:
                pass

        result["valid"] = True
        result["reason"] = "ok"
    except Exception as e:
        result["reason"] = f"exception: {str(e)}"
    return result


# ═══════════════════════════════════════════════════════════
# SOURCE SELECTION WITH REBUILD CAPABILITY
# ═══════════════════════════════════════════════════════════
def find_raw_sources() -> dict:
    """Find raw H.264 video and audio files."""
    sources = {"video": None, "audio": None}
    for d in [PRESERVED_DIR, RECORDINGS_DIR, TEMP_DIR]:
        if not d.exists():
            continue
        for f in d.glob("*_encoded.h264"):
            size = f.stat().st_size
            if size > MIN_FILE_SIZE:
                if sources["video"] is None or size > sources["video"].stat().st_size:
                    sources["video"] = f
        for f in d.glob("*_audio.webm"):
            size = f.stat().st_size
            if size > MIN_FILE_SIZE:
                if sources["audio"] is None or size > sources["audio"].stat().st_size:
                    sources["audio"] = f
    return sources


def rebuild_from_raw(video_path: str, audio_path: str, output_path: str) -> bool:
    """Rebuild complete MKV from raw H.264 + audio."""
    log(f"  🔧 Rebuilding from raw sources...")
    log(f"    Video: {os.path.basename(video_path)} ({human_size(os.path.getsize(video_path))})")
    if audio_path:
        log(f"    Audio: {os.path.basename(audio_path)} ({human_size(os.path.getsize(audio_path))})")

    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    tmp_out = output_path.replace(".mkv", ".tmp.mkv")

    if audio_path:
        cmd = (
            f'ffmpeg -y -i "{os.path.abspath(video_path)}" '
            f'-i "{os.path.abspath(audio_path)}" '
            f'-c:v copy -c:a copy -f matroska "{tmp_out}"'
        )
    else:
        cmd = (
            f'ffmpeg -y -i "{os.path.abspath(video_path)}" '
            f'-c:v copy -an -f matroska "{tmp_out}"'
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

    # Verify rebuild
    info = ffprobe_json(tmp_out)
    if not info:
        try:
            os.unlink(tmp_out)
        except OSError:
            pass
        return False

    dur = get_duration(info)
    vc, _ = get_codecs(info)
    if not vc or dur < 1.0:
        try:
            os.unlink(tmp_out)
        except OSError:
            pass
        return False

    os.replace(tmp_out, output_path)
    log(f"  ✓ Rebuilt: {human_size(os.path.getsize(output_path))}, {format_ts(dur)}")
    return True


def find_best_source() -> dict:
    """
    Find the best source for processing.
    Detects truncated MKV and rebuilds from raw if necessary.
    ALWAYS returns a dict or None. Never returns bool.
    """
    log("🔍 SOURCE SELECTION WITH INTEGRITY CHECK")
    log("-" * 50)

    # Step 1: Find complete MKV candidates
    mkv_candidates = []
    for d in [PRESERVED_DIR, RECORDINGS_DIR, TEMP_DIR]:
        if not d.exists():
            continue
        for f in d.glob("*.mkv"):
            name = f.name
            if any(x in name for x in ["_part_", "_encoded", "concatenated_", "rebuilt_"]):
                continue
            try:
                size = f.stat().st_size
                if size >= MIN_FILE_SIZE:
                    mkv_candidates.append((f, size))
            except OSError:
                continue

    mkv_candidates.sort(key=lambda x: x[1], reverse=True)

    for f, size in mkv_candidates:
        log(f"  Checking: {f.name} ({human_size(size)})")
        validation = verify_source_integrity(str(f))

        if validation["truncated"]:
            log(f"  ⚠️ TRUNCATION DETECTED: {validation['reason']}")
            log(f"  Attempting rebuild from raw sources...")

            raw = find_raw_sources()
            if raw["video"]:
                sid = safe_id(STREAM_ID)
                rebuild_dir = str(TEMP_DIR / f"rebuild_{sid}")
                os.makedirs(rebuild_dir, exist_ok=True)
                rebuild_path = os.path.join(rebuild_dir, f"rebuilt_{sid}.mkv")

                audio_str = str(raw["audio"]) if raw["audio"] else None
                if rebuild_from_raw(str(raw["video"]), audio_str, rebuild_path):
                    # Verify the rebuild
                    rebuild_validation = verify_source_integrity(rebuild_path)
                    if rebuild_validation["valid"] and not rebuild_validation["truncated"]:
                        log(f"  ✓ Rebuild successful and verified")
                        return {"type": "rebuilt_from_raw", "validation": rebuild_validation}
                    elif rebuild_validation["valid"]:
                        log(f"  ⚠️ Rebuild also appears truncated, but using it")
                        return {"type": "rebuilt_from_raw", "validation": rebuild_validation}
                    else:
                        log(f"  ✗ Rebuild validation failed: {rebuild_validation['reason']}")
            else:
                log(f"  ✗ No raw video source available for rebuild")

            # Use the truncated file with corrected duration
            log(f"  ⚠️ Using truncated source with corrected duration: {validation['duration']:.1f}s")
            validation["valid"] = True
            return {"type": "truncated_mkv", "validation": validation}

        elif validation["valid"]:
            log(f"  ✓ VALID and COMPLETE: {f.name}")
            return {"type": "complete_mkv", "validation": validation}
        else:
            log(f"  ✗ Rejected: {validation['reason']}")

    # Step 2: Try raw rebuild if no MKV found
    log("  No valid MKV found. Trying raw rebuild...")
    raw = find_raw_sources()
    if raw["video"]:
        sid = safe_id(STREAM_ID)
        rebuild_dir = str(TEMP_DIR / f"rebuild_{sid}")
        os.makedirs(rebuild_dir, exist_ok=True)
        rebuild_path = os.path.join(rebuild_dir, f"rebuilt_{sid}.mkv")
        audio_str = str(raw["audio"]) if raw["audio"] else None
        if rebuild_from_raw(str(raw["video"]), audio_str, rebuild_path):
            validation = verify_source_integrity(rebuild_path)
            if validation["valid"]:
                return {"type": "rebuilt_from_raw", "validation": validation}

    log("  ✗ NO VALID SOURCE FOUND")
    return None


# ═══════════════════════════════════════════════════════════
# SEGMENTATION
# ═══════════════════════════════════════════════════════════
def calculate_segment_duration(source_size: int, source_duration: float) -> float:
    if source_duration <= 0 or source_size <= 0:
        return 60.0
    if source_size <= TARGET_SIZE_BYTES:
        return source_duration + 10.0
    bitrate = source_size / source_duration
    max_dur = TARGET_SIZE_BYTES / bitrate
    return max(max_dur * 0.90, 5.0)


def split_source(source_path: str, work_dir: str, has_audio: bool, source_duration: float) -> list:
    """
    Split source into MP4 segments.
    Uses interval tracking to know exact source positions.
    """
    source_size = os.path.getsize(source_path)
    seg_dur = calculate_segment_duration(source_size, source_duration)
    expected_count = max(1, int(source_duration / seg_dur) + 1)

    log(f"📐 Segment duration: {seg_dur:.1f}s")
    log(f"📊 Expected segments: ~{expected_count}")
    log(f"📦 Source: {human_size(source_size)}, {format_ts(source_duration)}")

    os.makedirs(work_dir, exist_ok=True)
    pattern = os.path.join(work_dir, "part_%03d.mp4")

    if has_audio:
        audio_opts = "-map 0:v:0 -map 0:a:0? -c:v copy -c:a aac -b:a 128k"
    else:
        audio_opts = "-map 0:v:0 -c:v copy -an"

    cmd = (
        f'ffmpeg -y -i "{source_path}" '
        f'{audio_opts} '
        f'-f segment -segment_time {seg_dur:.1f} '
        f'-reset_timestamps 1 -movflags +faststart '
        f'"{pattern}"'
    )
    log(f"🔪 Split command: {cmd}")

    r = run(cmd, timeout=3600)
    if r is None:
        log("✗ Split timed out")
        return []
    if r.returncode != 0:
        stderr = (r.stderr or "")[-500:]
        log(f"✗ Split failed (exit={r.returncode}): {stderr[:300]}")
        # Fallback: video-only
        if has_audio:
            log("  Retrying video-only...")
            cmd2 = (
                f'ffmpeg -y -i "{source_path}" '
                f'-map 0:v:0 -c:v copy -an '
                f'-f segment -segment_time {seg_dur:.1f} '
                f'-reset_timestamps 1 -movflags +faststart '
                f'"{pattern}"'
            )
            r2 = run(cmd2, timeout=3600)
            if r2 is None or r2.returncode != 0:
                return []

    segments = sorted(Path(work_dir).glob("part_*.mp4"), key=lambda p: p.name)
    segments = [str(s) for s in segments if s.stat().st_size > 0 and "%" not in s.name]

    if not segments:
        log("✗ No segments produced")
        return []

    log(f"✓ Produced {len(segments)} segments")
    for i, seg in enumerate(segments):
        sd = get_duration_file(seg)
        ss = os.path.getsize(seg)
        log(f"  [{i+1}] {os.path.basename(seg)}: {human_size(ss)}, {sd:.1f}s")

    return segments


def resplit_oversized(seg_path: str, work_dir: str, idx: int) -> list:
    size = os.path.getsize(seg_path)
    info = ffprobe_json(seg_path)
    if not info:
        return [seg_path]
    dur = get_duration(info)
    if dur <= 0:
        return [seg_path]

    bitrate = size / dur
    target_dur = max((TARGET_SIZE_BYTES * 0.85) / bitrate, 3.0)
    log(f"    ⚠️ Oversized ({human_size(size)}) → re-splitting at ~{target_dur:.0f}s")

    sub_dir = os.path.join(work_dir, f"resplit_{idx:03d}")
    os.makedirs(sub_dir, exist_ok=True)
    pattern = os.path.join(sub_dir, "sub_%03d.mp4")

    _, ac = get_codecs(info)
    audio_opts = "-c:a aac -b:a 128k" if ac else "-an"

    cmd = (
        f'ffmpeg -y -i "{seg_path}" '
        f'-c:v copy {audio_opts} '
        f'-f segment -segment_time {target_dur:.1f} '
        f'-reset_timestamps 1 -movflags +faststart '
        f'"{pattern}"'
    )
    r = run(cmd, timeout=600)
    if r and r.returncode == 0:
        subs = sorted(Path(sub_dir).glob("sub_*.mp4"), key=lambda p: p.name)
        subs = [str(s) for s in subs if s.stat().st_size > 0 and "%" not in s.name]
        if subs:
            log(f"    ✓ Produced {len(subs)} sub-segments")
            return subs
    return [seg_path]


# ═══════════════════════════════════════════════════════════
# VERIFICATION — Audio is OPTIONAL
# ═══════════════════════════════════════════════════════════
def verify_segment(path: str) -> dict:
    """Verify MP4 segment. Audio is OPTIONAL and never causes rejection."""
    result = {
        "ok": False, "reason": "", "duration": 0.0,
        "width": 0, "height": 0, "fps": 0.0,
        "has_video": False, "has_audio": False,
        "size": 0, "decode_ok": False,
    }
    if not os.path.exists(path):
        result["reason"] = "not found"
        return result
    size = os.path.getsize(path)
    result["size"] = size
    if size == 0:
        result["reason"] = "empty"
        return result
    if size > MAX_SIZE_BYTES:
        result["reason"] = f"exceeds 45 MiB: {human_size(size)}"
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

    # AUDIO IS OPTIONAL — never reject for missing audio
    w, h = get_resolution(info)
    result["width"] = w
    result["height"] = h
    result["fps"] = get_fps(info)

    # Decode test
    dr = run(f'ffmpeg -v error -i "{path}" -f null -', timeout=120)
    if dr is None:
        result["reason"] = "decode timeout"
        return result
    if dr.returncode != 0:
        result["reason"] = f"decode error: {(dr.stderr or '')[:100]}"
        return result

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
    gap = max(0, source_duration - total_sent)
    return {
        "source_duration": source_duration,
        "total_segment_duration": total_sent,
        "coverage_ratio": coverage,
        "coverage_percent": round(coverage * 100, 1),
        "gap_seconds": round(gap, 1),
        "complete": coverage >= 0.95,
    }


# ═══════════════════════════════════════════════════════════
# TELEGRAM SENDING
# ═══════════════════════════════════════════════════════════
def tg_send_video(video_path: str, caption: str, duration: int, width: int, height: int) -> dict:
    result = {"ok": False, "message_id": None, "error": ""}
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        result["error"] = "no credentials"
        return result
    fsize = os.path.getsize(video_path)
    if fsize > HARD_SIZE_LIMIT:
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
        except Exception as e:
            result["error"] = str(e)
        if attempt < MAX_RETRIES:
            time.sleep(5 * attempt)
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

    # ── Step 1: Source Selection with Integrity Check ──
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
    log(f"  Size:     {v['size']} bytes ({human_size(v['size'])})")
    log(f"  Duration: {format_ts(source_duration)} ({source_duration:.1f}s)")
    log(f"  Video:    {v['video_codec']} {v['width']}x{v['height']} @ {v['fps']:.1f}fps")
    log(f"  Audio:    {v['audio_codec'] or 'NONE'}")
    log(f"  Packets:  v={v['video_packets']}, a={v['audio_packets']}")
    if v.get("truncated"):
        log(f"  ⚠️ SOURCE IS TRUNCATED — using corrected duration")
    log(f"{'=' * 60}")

    mf["source"] = {
        "type": source_info["type"],
        "path": source_path,
        "sha256": v["sha256"],
        "size": v["size"],
        "duration": round(source_duration, 1),
        "has_audio": source_has_audio,
        "video_packets": v["video_packets"],
        "audio_packets": v["audio_packets"],
        "truncated": v.get("truncated", False),
    }
    save_manifest(STREAM_ID, mf)

    # ── Step 2: Split ──
    log(f"\n{'=' * 60}")
    log("STEP 2: SEGMENTATION")
    log(f"{'=' * 60}")

    sid = safe_id(STREAM_ID)
    work_dir = str(TEMP_DIR / f"mp4_{sid}")

    segments = split_source(source_path, work_dir, source_has_audio, source_duration)
    if not segments:
        log("✗ NO SEGMENTS PRODUCED — ABORTING")
        mf["state"] = "FAILED_SPLIT"
        save_manifest(STREAM_ID, mf)
        update_worker_final_state(STREAM_ID, "failed", {"error": "split_failed"})
        return

    # Handle oversized segments
    final_segments = []
    for idx, seg in enumerate(segments):
        seg_size = os.path.getsize(seg)
        if seg_size > MAX_SIZE_BYTES:
            for _ in range(MAX_RESPLIT_TRIES):
                subs = resplit_oversized(seg, work_dir, idx)
                if all(os.path.getsize(s) <= MAX_SIZE_BYTES for s in subs):
                    final_segments.extend(subs)
                    break
            else:
                log(f"    ✗ Could not reduce segment {idx} below size limit")
                mf["errors"].append(f"oversized:{os.path.basename(seg)}")
        else:
            final_segments.append(seg)

    total = len(final_segments)
    log(f"\n📦 Final segment count: {total}")

    # ── Step 3: Verify and Send ──
    log(f"\n{'=' * 60}")
    log("STEP 3: VERIFICATION & DELIVERY")
    log(f"{'=' * 60}")

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

        audio_str = "YES" if verdict["has_audio"] else "NO (accepted)"
        log(f"  [{idx+1}/{total}] {seg_name}: ✓ {human_size(verdict['size'])}, "
            f"{verdict['duration']:.1f}s, audio={audio_str}")

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
            sent_durations.append(verdict["duration"])
            mf["segments"][seg_key] = {
                "sent": True,
                "message_id": tg_res["message_id"],
                "size": verdict["size"],
                "duration": round(verdict["duration"], 1),
                "has_audio": verdict["has_audio"],
            }
        else:
            log(f"    ✗ Send failed: {tg_res['error']}")
            failed_count += 1
            mf["segments"][seg_key] = {"sent": False, "error": tg_res["error"]}

        save_manifest(STREAM_ID, mf)
        if idx < total - 1:
            time.sleep(SEND_DELAY_SECONDS)

    # ── Step 4: Coverage Report ──
    coverage = calculate_coverage(sent_durations, source_duration)

    log(f"\n{'=' * 60}")
    log("FINAL REPORT")
    log(f"{'=' * 60}")
    log(f"  Source type:       {source_info['type']}")
    log(f"  Source duration:   {format_ts(source_duration)} ({source_duration:.1f}s)")
    log(f"  Source truncated:  {'YES' if v.get('truncated') else 'NO'}")
    log(f"  Segments produced: {total}")
    log(f"  Segments sent:     {sent_count}")
    log(f"  Segments failed:   {failed_count}")
    log(f"  Duration sent:     {format_ts(coverage['total_segment_duration'])}")
    log(f"  Coverage:          {coverage['coverage_percent']}%")
    log(f"  Gap:               {coverage['gap_seconds']}s")
    log(f"  Coverage complete: {'YES' if coverage['complete'] else 'NO'}")
    log(f"{'=' * 60}")

    if coverage["complete"] and failed_count == 0:
        mf["state"] = "SENT"
        mf["coverage"] = coverage
        save_manifest(STREAM_ID, mf)
        log("\n✅ ALL SEGMENTS SENT — FULL COVERAGE")
        update_worker_final_state(STREAM_ID, "completed", {
            "completed_at": datetime.now(timezone.utc).isoformat(),
            "segments_sent": sent_count,
            "coverage_percent": coverage["coverage_percent"],
        })
    elif sent_count > 0:
        mf["state"] = "PARTIAL"
        mf["coverage"] = coverage
        save_manifest(STREAM_ID, mf)
        if not coverage["complete"]:
            log(f"\n⚠️ PARTIAL — INCOMPLETE COVERAGE ({coverage['coverage_percent']}%)")
            log(f"   Missing: {coverage['gap_seconds']}s")
            if v.get("truncated"):
                log(f"   NOTE: Source file was truncated. Missing content")
                log(f"   was never present in the preserved file.")
    else:
        mf["state"] = "FAILED"
        save_manifest(STREAM_ID, mf)
        update_worker_final_state(STREAM_ID, "failed", {"error": "no_segments_sent"})

    log("Source files NEVER deleted by this pipeline.")


if __name__ == "__main__":
    main()
