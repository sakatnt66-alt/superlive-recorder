#!/usr/bin/env python3
"""
postprocess_recording.py — PHASE 2: MKV→MP4 segmentation, verification, delivery.

ROOT CAUSE FIXES:
  1. Removed '-f mp4' that was overriding '-f segment' (caused single-file output)
  2. Audio is OPTIONAL — video-only segments are accepted and sent
  3. Timeline coverage verified against source duration before declaring success
  4. Isolated attempt directories prevent contamination from failed splits
  5. Temp files use correct final extension (.tmp.mp4 not .mp4.tmp)
  6. Source selection prioritizes verified complete MKV over raw/parts
  7. Size-aware segmentation with recursive oversize handling

PROTECTED FILES (NOT MODIFIED):
  - record_once.py
  - full_stream_contact_sheet.py (reads manifest if available)
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
MAX_INTERNAL_SIZE      = 42 * 1024 * 1024   # 42 MiB operational ceiling
HARD_SIZE_LIMIT        = 50 * 1024 * 1024   # 50 MiB Telegram hard limit
SEND_DELAY_SECONDS     = 3
MAX_RETRIES            = 3
MAX_RESPLIT_ATTEMPTS   = 3
MIN_FILE_SIZE          = 100 * 1024         # 100 KB
MIN_SOURCE_DURATION    = 10.0               # seconds

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


def sha256_file(path: str, chunk_size: int = 1024 * 1024) -> str:
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
    """Count packets in a specific stream."""
    r = run(
        f'ffprobe -v quiet -count_packets -select_streams {stream_type}:0 '
        f'-show_entries stream=nb_read_packets -of csv=p=0 "{path}"',
        120
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
    """Update worker state. Failures here do NOT affect media processing status."""
    if not WORKER_API_URL or not AUTO_API_TOKEN:
        log("⚠️ [WORKER-API] Not configured, skipping state update")
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
            log(f"✓ [WORKER-API] State updated: {status}")
    except urllib.error.HTTPError as e:
        log(f"⚠️ [WORKER-API] HTTP {e.code} updating state to '{status}' — "
            f"media processing status is unaffected")
    except Exception as e:
        log(f"⚠️ [WORKER-API] Error: {e} — media processing status is unaffected")


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
# SOURCE SELECTION — Deterministic priority
# ═══════════════════════════════════════════════════════════
def validate_source_thorough(path: str) -> dict:
    """Thorough source validation with full metadata."""
    result = {
        "valid": False, "reason": "", "path": path,
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

        result["sha256"] = sha256_file(path)

        info = ffprobe_json(path)
        if not info:
            result["reason"] = "ffprobe failed"
            return result

        dur = get_duration(info)
        result["duration"] = dur
        if dur < MIN_SOURCE_DURATION:
            result["reason"] = f"duration too short ({dur:.1f}s)"
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

        # Count actual packets for timeline verification
        result["video_packets"] = ffprobe_packets(path, "v")
        if ac:
            result["audio_packets"] = ffprobe_packets(path, "a")

        # Sanity check: duration vs packet count vs fps
        if result["fps"] > 0 and result["video_packets"] > 0:
            expected_dur = result["video_packets"] / result["fps"]
            if dur > expected_dur * 3:
                result["reason"] = (
                    f"duration metadata ({dur:.0f}s) inconsistent with "
                    f"packet count ({result['video_packets']} @ {result['fps']:.0f}fps "
                    f"≈ {expected_dur:.0f}s)"
                )
                return False

        result["valid"] = True
        result["reason"] = "ok"
    except Exception as e:
        result["reason"] = str(e)
    return result


def find_best_source() -> dict:
    """
    Deterministic source selection:
    Priority 1: Complete MKV (not parts, not encoded)
    Priority 2: Concatenated parts from ONE verified attempt
    Priority 3: Raw rebuild (last resort, with warnings)
    """
    log("🔍 SOURCE SELECTION")
    log("-" * 50)

    # ── Priority 1: Complete MKV ──
    mkv_candidates = []
    for d in [PRESERVED_DIR, RECORDINGS_DIR, TEMP_DIR]:
        if not d.exists():
            continue
        for f in d.glob("*.mkv"):
            name = f.name
            if "_part_" in name or "_encoded" in name:
                continue
            if "concatenated_" in name or "rebuilt_" in name:
                continue
            try:
                size = f.stat().st_size
                if size >= MIN_FILE_SIZE:
                    mkv_candidates.append((f, size))
            except OSError:
                continue

    mkv_candidates.sort(key=lambda x: x[1], reverse=True)

    for f, size in mkv_candidates:
        log(f"  Checking MKV: {f.name} ({human_size(size)})")
        validation = validate_source_thorough(str(f))
        if validation["valid"]:
            log(f"  ✓ VALID complete MKV: {f.name}")
            log(f"    Duration: {format_ts(validation['duration'])}")
            log(f"    Video: {validation['video_codec']} {validation['width']}x{validation['height']}")
            log(f"    Audio: {validation['audio_codec'] or 'NONE'}")
            log(f"    Packets: v={validation['video_packets']}, a={validation['audio_packets']}")
            return {"type": "complete_mkv", "validation": validation}
        else:
            log(f"  ✗ Rejected: {validation['reason']}")

    # ── Priority 2: Concatenate parts ──
    log("  No valid complete MKV. Checking parts...")
    parts = discover_parts_deduplicated()
    if parts:
        concat_path = concatenate_parts_isolated(parts)
        if concat_path:
            validation = validate_source_thorough(concat_path)
            if validation["valid"]:
                log(f"  ✓ VALID concatenated source: {human_size(validation['size'])}")
                return {"type": "concatenated", "validation": validation}
            else:
                log(f"  ✗ Concatenated source invalid: {validation['reason']}")

    # ── Priority 3: Raw rebuild ──
    log("  No valid parts. Checking raw sources...")
    raw_video = None
    raw_audio = None
    for d in [PRESERVED_DIR, RECORDINGS_DIR, TEMP_DIR]:
        if not d.exists():
            continue
        for f in d.glob("*_encoded.h264"):
            size = f.stat().st_size
            if size > MIN_FILE_SIZE:
                if raw_video is None or size > raw_video.stat().st_size:
                    raw_video = f
        for f in d.glob("*_audio.webm"):
            size = f.stat().st_size
            if size > MIN_FILE_SIZE:
                if raw_audio is None or size > raw_audio.stat().st_size:
                    raw_audio = f

    if raw_video:
        rebuild_path = rebuild_from_raw(
            str(raw_video),
            str(raw_audio) if raw_audio else None
        )
        if rebuild_path:
            validation = validate_source_thorough(rebuild_path)
            if validation["valid"]:
                log(f"  ✓ VALID rebuilt source")
                return {"type": "rebuilt_raw", "validation": validation}
            else:
                log(f"  ✗ Rebuilt source invalid: {validation['reason']}")
                log(f"  ⚠️ Raw H.264 may lack reliable timing info")

    log("  ✗ NO VALID SOURCE FOUND")
    return None


def discover_parts_deduplicated() -> list:
    """Find parts, deduplicated by part number, preferring _preserved."""
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
                    num = int(m.group(1))
                    if num in parts_by_num:
                        if "_preserved" in str(f):
                            parts_by_num[num] = (f, size)
                        elif size > parts_by_num[num][1]:
                            parts_by_num[num] = (f, size)
                    else:
                        parts_by_num[num] = (f, size)
                except OSError:
                    continue
    return [(parts_by_num[n][0], n, parts_by_num[n][1]) for n in sorted(parts_by_num.keys())]


def concatenate_parts_isolated(parts: list) -> str:
    """Concatenate parts in an isolated directory."""
    if not parts:
        return None

    sid = safe_id(STREAM_ID)
    work_dir = TEMP_DIR / f"concat_{sid}"
    work_dir.mkdir(parents=True, exist_ok=True)

    out_file = str(work_dir / f"concatenated_{sid}.mkv")
    if os.path.exists(out_file) and os.path.getsize(out_file) > MIN_FILE_SIZE:
        info = ffprobe_json(out_file)
        if info and get_duration(info) >= MIN_SOURCE_DURATION:
            return out_file

    concat_list = str(work_dir / "concat_list.txt")
    with open(concat_list, "w", encoding="utf-8") as cf:
        for f, num, size in parts:
            cf.write(f"file '{os.path.abspath(str(f))}'\n")

    log(f"  🔗 Concatenating {len(parts)} parts...")
    cmd = f'ffmpeg -y -f concat -safe 0 -i "{concat_list}" -c copy "{out_file}"'
    r = run(cmd, timeout=3600)
    if r and r.returncode == 0 and os.path.exists(out_file):
        if os.path.getsize(out_file) > MIN_FILE_SIZE:
            return out_file

    log(f"  ✗ Concatenation failed")
    return None


def rebuild_from_raw(video_path: str, audio_path: str) -> str:
    """Rebuild MKV from raw H.264 + audio. Returns path or None."""
    sid = safe_id(STREAM_ID)
    work_dir = TEMP_DIR / f"rebuild_{sid}"
    work_dir.mkdir(parents=True, exist_ok=True)

    # FIX: temp file ends with .mkv so FFmpeg infers correct muxer
    tmp_out = str(work_dir / f"rebuilt_{sid}.tmp.mkv")
    final_out = str(work_dir / f"rebuilt_{sid}.mkv")

    if os.path.exists(final_out) and os.path.getsize(final_out) > MIN_FILE_SIZE:
        return final_out

    log(f"  🔧 Rebuilding from raw: {os.path.basename(video_path)}")

    if audio_path:
        cmd = (
            f'ffmpeg -y '
            f'-i "{os.path.abspath(video_path)}" '
            f'-i "{os.path.abspath(audio_path)}" '
            f'-c:v copy -c:a copy '
            f'-f matroska '
            f'"{tmp_out}"'
        )
    else:
        cmd = (
            f'ffmpeg -y '
            f'-i "{os.path.abspath(video_path)}" '
            f'-c:v copy -an '
            f'-f matroska '
            f'"{tmp_out}"'
        )

    r = run(cmd, timeout=3600)
    if r is None or r.returncode != 0:
        stderr = (r.stderr or "")[-300:] if r else "timeout"
        log(f"  ✗ Rebuild failed: {stderr[:200]}")
        return None

    if not os.path.exists(tmp_out) or os.path.getsize(tmp_out) < MIN_FILE_SIZE:
        return None

    # Verify before atomic rename
    info = ffprobe_json(tmp_out)
    if not info:
        return None
    dur = get_duration(info)
    vc, _ = get_codecs(info)
    if not vc or dur < MIN_SOURCE_DURATION:
        log(f"  ✗ Rebuild verification failed: dur={dur:.1f}s")
        return None

    os.replace(tmp_out, final_out)
    log(f"  ✓ Rebuilt: {human_size(os.path.getsize(final_out))}, {format_ts(dur)}")
    return final_out


# ═══════════════════════════════════════════════════════════
# SEGMENTATION — Fixed FFmpeg command
# ═══════════════════════════════════════════════════════════
def calculate_segment_duration(source_size: int, source_duration: float) -> float:
    if source_duration <= 0 or source_size <= 0:
        return 60.0
    if source_size <= TARGET_SIZE_BYTES:
        return source_duration + 10
    bitrate = source_size / source_duration
    max_dur = TARGET_SIZE_BYTES / bitrate
    return max(max_dur * 0.90, 10.0)


def split_source(source_path: str, work_dir: str, has_audio: bool) -> list:
    """
    Split source into MP4 segments using FFmpeg segment muxer.

    CRITICAL FIX: Do NOT place '-f mp4' after '-f segment'.
    The segment muxer uses the output extension to determine format.
    Adding '-f mp4' overrides '-f segment' and produces a single file.
    """
    source_size = os.path.getsize(source_path)
    source_dur = get_duration_file(source_path)
    seg_dur = calculate_segment_duration(source_size, source_dur)
    expected_count = max(1, int(source_dur / seg_dur) + 1)

    log(f"📐 Segment duration: {seg_dur:.1f}s")
    log(f"📊 Expected segments: ~{expected_count}")

    os.makedirs(work_dir, exist_ok=True)
    pattern = os.path.join(work_dir, "part_%03d.mp4")

    # Build audio options
    # -map 0:v:0 maps first video stream
    # -map 0:a:0? maps first audio stream IF it exists (? = optional)
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

    log(f"🔪 FFmpeg command:")
    log(f"   {cmd}")

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
            cmd_fallback = (
                f'ffmpeg -y -i "{source_path}" '
                f'-map 0:v:0 -c:v copy -an '
                f'-f segment -segment_time {seg_dur:.1f} '
                f'-reset_timestamps 1 -movflags +faststart '
                f'"{pattern}"'
            )
            r2 = run(cmd_fallback, timeout=3600)
            if r2 is None or r2.returncode != 0:
                log("✗ Fallback also failed")
                return []

    # Collect outputs — verify numeric expansion happened
    segments = sorted(Path(work_dir).glob("part_*.mp4"), key=lambda p: p.name)
    segments = [str(s) for s in segments if s.stat().st_size > 0]

    # CRITICAL: reject files with literal %03d in name
    segments = [s for s in segments if "%" not in os.path.basename(s)]

    if not segments:
        log("✗ No segments produced")
        return []

    # Verify we got multiple segments if expected
    if len(segments) == 1 and expected_count > 1:
        single_dur = get_duration_file(segments[0])
        if single_dur < source_dur * 0.5:
            log(f"⚠️ WARNING: Only 1 segment ({single_dur:.1f}s) from {source_dur:.1f}s source")
            log(f"   Split may have failed silently. Checking coverage...")

    log(f"✓ Produced {len(segments)} segments")
    for i, seg in enumerate(segments):
        sd = get_duration_file(seg)
        ss = os.path.getsize(seg)
        log(f"  [{i+1}] {os.path.basename(seg)}: {human_size(ss)}, {sd:.1f}s")

    return segments


def resplit_oversized(seg_path: str, work_dir: str, idx: int) -> list:
    """Split an oversized segment. Uses isolated sub-directory."""
    size = os.path.getsize(seg_path)
    info = ffprobe_json(seg_path)
    if not info:
        return [seg_path]
    dur = get_duration(info)
    if dur <= 0:
        return [seg_path]

    bitrate = size / dur
    target_dur = max((TARGET_SIZE_BYTES * 0.85) / bitrate, 5.0)
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
    """
    Verify MP4 segment.
    AUDIO IS OPTIONAL — a video-only segment is VALID.
    """
    result = {
        "ok": False, "reason": "", "duration": 0.0,
        "width": 0, "height": 0, "fps": 0.0,
        "has_video": False, "has_audio": False,
        "size": 0, "decode_ok": False,
        "video_packets": 0,
    }

    if not os.path.exists(path):
        result["reason"] = "not found"
        return result

    size = os.path.getsize(path)
    result["size"] = size
    if size == 0:
        result["reason"] = "empty"
        return result
    if size > HARD_SIZE_LIMIT:
        result["reason"] = f"exceeds hard limit: {human_size(size)}"
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

    # AUDIO IS OPTIONAL — do NOT reject video-only segments
    # if source_has_audio and not ac:  ← THIS RULE IS REMOVED

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
    result["video_packets"] = ffprobe_packets(path, "v")
    result["ok"] = True
    result["reason"] = "ok"
    return result


# ═══════════════════════════════════════════════════════════
# TIMELINE COVERAGE
# ═══════════════════════════════════════════════════════════
def calculate_coverage(segment_durations: list, source_duration: float) -> dict:
    """Calculate actual timeline coverage."""
    total_sent = sum(segment_durations)
    coverage = min(total_sent / source_duration, 1.0) if source_duration > 0 else 0

    # Check for gaps (simplified: compare total vs source)
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
def tg_send_video(video_path: str, caption: str, duration: int,
                  width: int, height: int) -> dict:
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
            log(f"    send attempt {attempt}/{MAX_RETRIES}: HTTP {e.code}")
        except Exception as e:
            result["error"] = str(e)
            log(f"    send attempt {attempt}/{MAX_RETRIES}: {e}")
        if attempt < MAX_RETRIES:
            time.sleep(5 * attempt)
    return result


# ═══════════════════════════════════════════════════════════
# MAIN PIPELINE
# ═══════════════════════════════════════════════════════════
def main():
    log("=" * 60)
    log("POST-PROCESSING PIPELINE — PHASE 2")
    log(f"STREAM_ID = {STREAM_ID}")
    log(f"WORKER_API = {'SET' if WORKER_API_URL else 'MISSING'}")
    log("=" * 60)

    mf = load_manifest(STREAM_ID)
    if mf.get("state") == "SENT":
        log("⏭️ Already completed — skipping")
        return

    # ── Step 1: Source Selection ──
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
    log(f"  Packets:  v={v['video_packets']}, a={v['audio_packets']}")
    log(f"{'=' * 60}")

    mf["source"] = {
        "type": source_info["type"],
        "path": source_path,
        "sha256": v["sha256"],
        "size": v["size"],
        "duration": round(source_duration, 1),
        "has_audio": source_has_audio,
        "video_packets": v["video_packets"],
    }
    save_manifest(STREAM_ID, mf)

    # ── Step 2: Split ──
    log(f"\n{'=' * 60}")
    log("STEP 2: SEGMENTATION")
    log(f"{'=' * 60}")

    sid = safe_id(STREAM_ID)
    work_dir = str(TEMP_DIR / f"mp4_{sid}")

    segments = split_source(source_path, work_dir, source_has_audio)
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
        if seg_size > MAX_INTERNAL_SIZE:
            for try_n in range(MAX_RESPLIT_ATTEMPTS):
                subs = resplit_oversized(seg, work_dir, idx)
                if all(os.path.getsize(s) <= MAX_INTERNAL_SIZE for s in subs):
                    final_segments.extend(subs)
                    break
                if try_n == MAX_RESPLIT_ATTEMPTS - 1:
                    log(f"    ✗ Still oversized after {MAX_RESPLIT_ATTEMPTS} attempts")
                    mf["errors"].append(f"oversized:{os.path.basename(seg)}")
        else:
            final_segments.append(seg)

    total = len(final_segments)
    log(f"\n📦 Final segment count: {total}")

    # ── Step 3: Verify & Send ──
    log(f"\n{'=' * 60}")
    log("STEP 3: VERIFICATION & DELIVERY")
    log(f"{'=' * 60}")

    display_name = STREAM_NAME or f"recording_{STREAM_ID}"
    sent_count = 0
    failed_count = 0
    sent_durations = []
    failed_indices = []
    segment_report = []

    for idx, seg_path in enumerate(final_segments):
        seg_name = os.path.basename(seg_path)
        seg_key = f"seg_{idx:04d}"

        # Skip already sent (resume support)
        prev = mf["segments"].get(seg_key, {})
        if prev.get("sent") and prev.get("message_id"):
            log(f"  [{idx+1}/{total}] {seg_name}: already sent — skip")
            sent_count += 1
            sent_durations.append(prev.get("duration", 0))
            continue

        # Verify
        verdict = verify_segment(seg_path)

        if not verdict["ok"]:
            log(f"  [{idx+1}/{total}] {seg_name}: ✗ {verdict['reason']}")
            mf["segments"][seg_key] = {"sent": False, "error": verdict["reason"]}
            mf["errors"].append(f"{seg_name}:{verdict['reason']}")
            failed_count += 1
            failed_indices.append(idx + 1)
            segment_report.append({
                "index": idx + 1, "file": seg_name,
                "status": "REJECTED", "reason": verdict["reason"],
            })
            save_manifest(STREAM_ID, mf)
            continue

        audio_status = "YES" if verdict["has_audio"] else "NO (video-only, accepted)"
        log(f"  [{idx+1}/{total}] {seg_name}: ✓ {human_size(verdict['size'])}, "
            f"{verdict['duration']:.1f}s, audio={audio_status}, decode=OK")

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

        report_entry = {
            "index": idx + 1,
            "file": seg_name,
            "size": verdict["size"],
            "duration": round(verdict["duration"], 1),
            "has_audio": verdict["has_audio"],
            "decode_ok": verdict["decode_ok"],
        }

        if tg_res["ok"]:
            log(f"    📤 SENT ✅ (msg={tg_res['message_id']})")
            sent_count += 1
            sent_durations.append(verdict["duration"])
            report_entry["status"] = "SENT"
            report_entry["message_id"] = tg_res["message_id"]
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
            failed_indices.append(idx + 1)
            report_entry["status"] = "SEND_FAILED"
            report_entry["error"] = tg_res["error"]
            mf["segments"][seg_key] = {"sent": False, "error": tg_res["error"]}

        segment_report.append(report_entry)
        save_manifest(STREAM_ID, mf)

        if idx < total - 1:
            time.sleep(SEND_DELAY_SECONDS)

    # ── Step 4: Timeline Coverage ──
    coverage = calculate_coverage(sent_durations, source_duration)

    log(f"\n{'=' * 60}")
    log("FINAL REPORT")
    log(f"{'=' * 60}")
    log(f"  Source:              {os.path.basename(source_path)}")
    log(f"  Source SHA-256:      {v['sha256'][:16]}...")
    log(f"  Source size:         {v['size']} bytes ({human_size(v['size'])})")
    log(f"  Source duration:     {format_ts(source_duration)} ({source_duration:.1f}s)")
    log(f"  Source video:        {v['video_codec']} {v['width']}x{v['height']} @ {v['fps']:.1f}fps")
    log(f"  Source audio:        {v['audio_codec'] or 'NONE'}")
    log(f"  Source v-packets:    {v['video_packets']}")
    log(f"  Segments produced:   {total}")
    log(f"  Segments sent:       {sent_count}")
    log(f"  Segments failed:     {failed_count}")
    log(f"  Duration sent:       {format_ts(coverage['total_segment_duration'])}")
    log(f"  Coverage:            {coverage['coverage_percent']}%")
    log(f"  Gap:                 {coverage['gap_seconds']}s")
    log(f"  Coverage complete:   {'YES' if coverage['complete'] else 'NO'}")
    if failed_indices:
        log(f"  Failed segments:     {failed_indices}")
    log(f"{'=' * 60}")

    # Per-segment report
    for sr in segment_report:
        status_icon = "✅" if sr.get("status") == "SENT" else "❌"
        audio_str = "audio" if sr.get("has_audio") else "silent"
        log(f"  {status_icon} [{sr['index']}/{total}] {sr['file']}: "
            f"{human_size(sr.get('size', 0))}, {sr.get('duration', 0):.1f}s, {audio_str}"
            f"{', ' + sr.get('reason', '') if sr.get('reason') else ''}"
            f"{', ' + sr.get('error', '') if sr.get('error') else ''}")

    # ── Step 5: Final State ──
    if coverage["complete"] and failed_count == 0:
        mf["state"] = "SENT"
        mf["coverage"] = coverage
        mf["report"] = segment_report
        save_manifest(STREAM_ID, mf)
        log("\n✅ ALL SEGMENTS SENT — FULL TIMELINE COVERAGE")
        update_worker_final_state(STREAM_ID, "completed", {
            "completed_at": datetime.now(timezone.utc).isoformat(),
            "segments_sent": sent_count,
            "coverage_percent": coverage["coverage_percent"],
            "total_duration": round(coverage["total_segment_duration"], 1),
        })
    elif sent_count > 0:
        mf["state"] = "PARTIAL"
        mf["coverage"] = coverage
        save_manifest(STREAM_ID, mf)
        if not coverage["complete"]:
            log(f"\n⚠️ PARTIAL — INCOMPLETE SOURCE COVERAGE ({coverage['coverage_percent']}%)")
            log(f"   Missing approximately {coverage['gap_seconds']}s of source timeline")
        log(f"   Re-run to retry {failed_count} failed segments.")
    else:
        mf["state"] = "FAILED"
        save_manifest(STREAM_ID, mf)
        log("\n✗ NO SEGMENTS SENT")
        update_worker_final_state(STREAM_ID, "failed", {"error": "no_segments_sent"})

    log("Source files NEVER deleted by this pipeline.")


if __name__ == "__main__":
    main()
