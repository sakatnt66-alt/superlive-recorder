#!/usr/bin/env python3
"""
postprocess_recording.py — PHASE 2: Per-part MKV/WebM → MP4 conversion & delivery.

FIXED ISSUES:
  1. Temp files now use .tmp.mp4 extension (FFmpeg reads last extension)
  2. Added explicit -f mp4 to all conversion commands
  3. Duration calculated via ffprobe with corrupt-metadata detection
  4. Actual decode test after conversion (not just exit code)
  5. Atomic rename only after full verification passes

PROTECTED (DO NOT MODIFY):
  - record_once.py (PHASE 1 recording engine)
  - full_stream_contact_sheet.py (JPG Contact Sheet)
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

MAX_SEGMENT_BYTES  = 45 * 1024 * 1024   # Safe limit under Telegram's 50 MiB
TG_HARD_LIMIT      = 50 * 1024 * 1024   # Telegram absolute limit
MARKER_SUFFIX      = ".postprocessed"
SEND_DELAY_SECONDS = 3
MAX_RETRIES        = 3

MIN_PART_SIZE_BYTES = 100 * 1024  # 100 KB minimum per part

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
        with urllib.request.urlopen(req, timeout=30) as response:
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
    return {"stream_id": sid, "parts": {}, "state": "DISCOVERED", "errors": []}


def save_manifest(sid: str, mf: dict) -> None:
    p = manifest_path(sid)
    tmp = p.with_suffix(".tmp")
    mf["updated_at"] = datetime.now(timezone.utc).isoformat()
    tmp.write_text(json.dumps(mf, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(p)


# ═══════════════════════════════════════════════════════════
# PART DISCOVERY — deduplicated
# ═══════════════════════════════════════════════════════════
def discover_parts() -> list:
    """Find all MKV/WebM parts, deduplicated by part number. Prefers _preserved."""
    search_order = [PRESERVED_DIR, RECORDINGS_DIR, TEMP_DIR]
    parts_by_num = {}

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
                    if part_num in parts_by_num:
                        # Prefer _preserved directory
                        if "_preserved" in str(f):
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


# ═══════════════════════════════════════════════════════════
# DURATION — reliable extraction via ffprobe
# ═══════════════════════════════════════════════════════════
def get_real_duration(path: str) -> float:
    """Get duration from ffprobe. Returns 0 if file is unreadable."""
    info = ffprobe_json(path)
    if not info:
        return 0.0
    return get_duration(info)


def detect_corrupt_duration(path: str, declared_dur: float, size: int,
                           all_sizes: list, all_durations: list) -> float:
    """
    Detect if declared duration is corrupt by comparing with
    size/duration ratio of other parts.
    """
    if declared_dur <= 0 or size <= 0:
        return declared_dur

    # Build ratio from parts with "normal" durations (< 600s)
    ratios = []
    for i, (s, d) in enumerate(zip(all_sizes, all_durations)):
        if s > 0 and 0 < d < 600:
            ratios.append(d / s)

    if len(ratios) < 3:
        return declared_dur

    ratios.sort()
    median_ratio = ratios[len(ratios) // 2]
    estimated = size * median_ratio

    # If declared is > 3x estimate, it's corrupt
    if declared_dur > estimated * 3:
        return estimated

    return declared_dur


# ═══════════════════════════════════════════════════════════
# CONVERSION — MKV/WebM → MP4
# ═══════════════════════════════════════════════════════════
def convert_part_to_mp4(input_path: str, output_path: str) -> bool:
    """
    Convert MKV/WebM to MP4.
    Video: stream copy (-c:v copy)
    Audio: AAC re-encode (-c:a aac -b:a 128k)
    Explicit format: -f mp4
    Temp file uses .tmp.mp4 extension so FFmpeg recognizes the format.
    """
    abs_input = os.path.abspath(input_path)
    abs_output = os.path.abspath(output_path)
    os.makedirs(os.path.dirname(abs_output), exist_ok=True)

    # Strategy 1: Video copy + Audio AAC + explicit format
    cmd1 = (
        f'ffmpeg -y -i "{abs_input}" '
        f'-c:v copy -c:a aac -b:a 128k '
        f'-f mp4 -movflags +faststart '
        f'"{abs_output}"'
    )
    r1 = run(cmd1, timeout=600)
    if r1 is not None and r1.returncode == 0:
        if os.path.exists(abs_output) and os.path.getsize(abs_output) > 0:
            return True

    stderr1 = (r1.stderr or "")[-500:] if r1 else "TIMEOUT"
    log(f"    ⚠️ Strategy 1 (copy+aac) failed: {stderr1[:200]}")

    # Strategy 2: Video copy + No audio + explicit format
    cmd2 = (
        f'ffmpeg -y -i "{abs_input}" '
        f'-c:v copy -an '
        f'-f mp4 -movflags +faststart '
        f'"{abs_output}"'
    )
    r2 = run(cmd2, timeout=300)
    if r2 is not None and r2.returncode == 0:
        if os.path.exists(abs_output) and os.path.getsize(abs_output) > 0:
            log(f"    ⚠️ Converted without audio (video-only)")
            return True

    stderr2 = (r2.stderr or "")[-500:] if r2 else "TIMEOUT"
    log(f"    ⚠️ Strategy 2 (copy video only) failed: {stderr2[:200]}")

    # Strategy 3: Re-encode video if copy fails entirely
    cmd3 = (
        f'ffmpeg -y -i "{abs_input}" '
        f'-c:v libx264 -preset fast -crf 23 -c:a aac -b:a 128k '
        f'-f mp4 -movflags +faststart '
        f'"{abs_output}"'
    )
    r3 = run(cmd3, timeout=900)
    if r3 is not None and r3.returncode == 0:
        if os.path.exists(abs_output) and os.path.getsize(abs_output) > 0:
            log(f"    ⚠️ Used full re-encode (libx264 crf23)")
            return True

    stderr3 = (r3.stderr or "")[-500:] if r3 else "TIMEOUT"
    log(f"    ✗ All strategies failed. Last: {stderr3[:200]}")
    return False


# ═══════════════════════════════════════════════════════════
# VERIFICATION — full decode test
# ═══════════════════════════════════════════════════════════
def verify_mp4_full(path: str) -> dict:
    """
    Full verification:
    1. ffprobe check (streams, duration, resolution, fps)
    2. Actual decode test (ffmpeg reads entire file)
    3. Size check
    """
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

    # Step 1: ffprobe
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

    w, h = get_resolution(info)
    result["width"] = w
    result["height"] = h
    result["fps"] = get_fps(info)

    # Step 2: Actual decode test
    decode_cmd = (
        f'ffmpeg -v error -i "{path}" -f null -'
    )
    dr = run(decode_cmd, timeout=120)
    if dr is None:
        result["reason"] = "decode test timeout"
        return result
    if dr.returncode != 0:
        result["reason"] = f"decode failed: {(dr.stderr or '')[:100]}"
        return result

    result["decode_ok"] = True

    # Step 3: Size check
    if size > TG_HARD_LIMIT:
        result["reason"] = f"too large for Telegram: {human_size(size)}"
        return result

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
# MAIN PIPELINE
# ═══════════════════════════════════════════════════════════
def main():
    log("=" * 60)
    log("Post-processing pipeline — PHASE 2 (Per-part MKV→MP4)")
    log(f"STREAM_ID={STREAM_ID}")
    log("=" * 60)

    mf = load_manifest(STREAM_ID)

    if mf.get("state") == "SENT":
        log("⏭️ Already completed — skipping")
        return

    # ── Discover parts ──
    parts = discover_parts()
    if not parts:
        log("✗ No parts found — ABORTING")
        mf["state"] = "FAILED_NO_PARTS"
        save_manifest(STREAM_ID, mf)
        update_worker_final_state(STREAM_ID, "failed", {"error": "no_parts_found"})
        return

    total_size = sum(size for _, _, size in parts)
    log(f"📦 Found {len(parts)} unique parts, total size: {human_size(total_size)}")

    # ── Validate all parts and get real durations ──
    log("\n" + "=" * 60)
    log("PHASE A: Validating parts & extracting durations")
    log("=" * 60)

    # First pass: get raw durations and sizes
    raw_sizes = []
    raw_durations = []
    for f, part_num, size in parts:
        dur = get_real_duration(str(f))
        raw_sizes.append(size)
        raw_durations.append(dur)

    # Second pass: correct corrupt durations
    validated_parts = []
    total_expected_duration = 0.0

    for idx, (f, part_num, size) in enumerate(parts):
        part_key = f"part_{part_num:04d}"
        declared_dur = raw_durations[idx]

        # Detect and correct corrupt duration
        real_dur = detect_corrupt_duration(
            str(f), declared_dur, size, raw_sizes, raw_durations
        )

        # Get codec info
        info = ffprobe_json(str(f))
        vc, ac = (None, None)
        if info:
            vc, ac = get_codecs(info)

        if not vc:
            log(f"  Part {part_num}: ✗ no video stream")
            mf["parts"][part_key] = {"valid": False, "error": "no_video"}
            mf["errors"].append(f"part_{part_num}: no_video")
            continue

        if real_dur < 0.5:
            log(f"  Part {part_num}: ✗ duration too short ({real_dur:.1f}s)")
            mf["parts"][part_key] = {"valid": False, "error": "too_short"}
            mf["errors"].append(f"part_{part_num}: too_short")
            continue

        validated_parts.append({
            "path": str(f),
            "part_num": part_num,
            "size": size,
            "duration": real_dur,
            "video_codec": vc,
            "audio_codec": ac,
        })
        total_expected_duration += real_dur

        dur_note = ""
        if abs(real_dur - declared_dur) > 1.0:
            dur_note = f" [corrected from {declared_dur:.0f}s]"

        log(f"  Part {part_num}: ✓ {real_dur:.1f}s, {human_size(size)}, "
            f"v={vc}, a={ac or 'NONE'}{dur_note}")

        mf["parts"][part_key] = {
            "valid": True,
            "path": str(f),
            "size": size,
            "duration": round(real_dur, 2),
            "video_codec": vc,
            "audio_codec": ac,
        }

    save_manifest(STREAM_ID, mf)

    if not validated_parts:
        log("✗ No valid parts — ABORTING")
        mf["state"] = "FAILED_NO_VALID_PARTS"
        save_manifest(STREAM_ID, mf)
        update_worker_final_state(STREAM_ID, "failed", {"error": "no_valid_parts"})
        return

    log(f"\n✓ {len(validated_parts)} valid parts")
    log(f"  Total expected duration: {format_ts(total_expected_duration)}")

    # ── Convert each part to MP4 ──
    log("\n" + "=" * 60)
    log("PHASE B: Converting parts to MP4")
    log("=" * 60)

    TEMP_DIR.mkdir(parents=True, exist_ok=True)
    mp4_dir = TEMP_DIR / f"mp4_{safe_id(STREAM_ID)}"
    mp4_dir.mkdir(parents=True, exist_ok=True)

    display_name = STREAM_NAME or f"recording_{STREAM_ID}"
    total_sent_duration = 0.0
    sent_count = 0
    failed_count = 0
    convert_failed = 0
    verify_failed = 0
    total = len(validated_parts)

    # Stats for final report
    stats = {
        "total_mkv_parts": len(parts),
        "valid_parts": len(validated_parts),
        "converted_ok": 0,
        "convert_failed": 0,
        "verify_ok": 0,
        "verify_failed": 0,
        "size_ok": 0,
        "size_failed": 0,
        "sent_ok": 0,
        "sent_failed": 0,
        "total_duration": 0.0,
        "failed_parts": [],
    }

    for idx, part_info in enumerate(validated_parts):
        part_num = part_info["part_num"]
        part_path = part_info["path"]
        part_size = part_info["size"]
        part_duration = part_info["duration"]
        part_key = f"part_{part_num:04d}"

        # Skip already sent
        prev = mf["parts"].get(part_key, {})
        if prev.get("sent") and prev.get("message_id"):
            log(f"  [{idx+1}/{total}] Part {part_num}: Already sent — skip")
            sent_count += 1
            total_sent_duration += prev.get("duration", 0)
            stats["sent_ok"] += 1
            continue

        log(f"  [{idx+1}/{total}] Part {part_num}: "
            f"{human_size(part_size)}, {part_duration:.1f}s")

        # Final MP4 path
        mp4_path = str(mp4_dir / f"part_{part_num:04d}.mp4")

        # Check if already converted and verified
        need_convert = True
        if os.path.exists(mp4_path) and os.path.getsize(mp4_path) > 0:
            existing_check = verify_mp4_full(mp4_path)
            if existing_check["ok"]:
                log(f"    ✓ MP4 exists and verified — skipping conversion")
                need_convert = False
                stats["converted_ok"] += 1
                stats["verify_ok"] += 1
                stats["size_ok"] += 1

        if need_convert:
            # FIX: Temp file uses .tmp.mp4 extension (not .mp4.tmp)
            tmp_mp4 = str(mp4_dir / f"part_{part_num:04d}.tmp.mp4")

            if os.path.exists(tmp_mp4):
                try:
                    os.unlink(tmp_mp4)
                except OSError:
                    pass

            if not convert_part_to_mp4(part_path, tmp_mp4):
                log(f"    ✗ Conversion FAILED")
                mf["parts"][part_key]["sent"] = False
                mf["parts"][part_key]["error"] = "conversion_failed"
                mf["errors"].append(f"part_{part_num}: conversion_failed")
                failed_count += 1
                stats["convert_failed"] += 1
                stats["failed_parts"].append(part_num)
                save_manifest(STREAM_ID, mf)
                continue

            # Verify the temp file BEFORE accepting
            tmp_check = verify_mp4_full(tmp_mp4)
            if not tmp_check["ok"]:
                log(f"    ✗ Verification failed: {tmp_check['reason']}")
                try:
                    os.unlink(tmp_mp4)
                except OSError:
                    pass
                mf["parts"][part_key]["sent"] = False
                mf["parts"][part_key]["error"] = f"verify_failed: {tmp_check['reason']}"
                mf["errors"].append(f"part_{part_num}: verify_failed")
                failed_count += 1
                stats["verify_failed"] += 1
                stats["failed_parts"].append(part_num)
                save_manifest(STREAM_ID, mf)
                continue

            stats["converted_ok"] += 1
            stats["verify_ok"] += 1

            # Size check
            if tmp_check["size"] > TG_HARD_LIMIT:
                log(f"    ✗ Too large: {human_size(tmp_check['size'])}")
                try:
                    os.unlink(tmp_mp4)
                except OSError:
                    pass
                mf["parts"][part_key]["sent"] = False
                mf["parts"][part_key]["error"] = "exceeds_size_limit"
                failed_count += 1
                stats["size_failed"] += 1
                stats["failed_parts"].append(part_num)
                save_manifest(STREAM_ID, mf)
                continue

            stats["size_ok"] += 1

            # Atomic rename: temp → final
            if os.path.exists(mp4_path):
                try:
                    os.unlink(mp4_path)
                except OSError:
                    pass
            os.rename(tmp_mp4, mp4_path)
            log(f"    ✓ Converted and verified: {human_size(tmp_check['size'])}, "
                f"{tmp_check['duration']:.1f}s, "
                f"audio={'YES' if tmp_check['has_audio'] else 'NO'}")

        # Final verification of the accepted file
        final_check = verify_mp4_full(mp4_path)
        if not final_check["ok"]:
            log(f"    ✗ Final check failed: {final_check['reason']}")
            mf["parts"][part_key]["sent"] = False
            mf["parts"][part_key]["error"] = final_check["reason"]
            failed_count += 1
            stats["failed_parts"].append(part_num)
            save_manifest(STREAM_ID, mf)
            continue

        # Use verified duration (not metadata from source)
        actual_mp4_duration = final_check["duration"]

        # Send to Telegram
        audio_note = "\n⚠️ بدون صوت" if not final_check["has_audio"] else ""
        caption = (
            f"🎬 {display_name}\n"
            f"🆔 {STREAM_ID}\n"
            f"📎 المقطع {idx+1} من {total}\n"
            f"⏱️ {format_ts(actual_mp4_duration)}"
            f"{audio_note}"
        )

        tg_res = tg_send_video(
            mp4_path, caption,
            int(actual_mp4_duration),
            final_check["width"], final_check["height"]
        )

        if tg_res["ok"]:
            log(f"    📤 SENT ✅ (msg={tg_res['message_id']})")
            sent_count += 1
            total_sent_duration += actual_mp4_duration
            stats["sent_ok"] += 1
            mf["parts"][part_key].update({
                "sent": True,
                "message_id": tg_res["message_id"],
                "mp4_path": mp4_path,
                "mp4_size": final_check["size"],
                "mp4_duration": round(actual_mp4_duration, 1),
                "has_audio": final_check["has_audio"],
            })
        else:
            log(f"    ✗ Send failed: {tg_res['error']}")
            mf["parts"][part_key]["sent"] = False
            mf["parts"][part_key]["send_error"] = tg_res["error"]
            failed_count += 1
            stats["sent_failed"] += 1
            stats["failed_parts"].append(part_num)

        save_manifest(STREAM_ID, mf)

        if idx < total - 1:
            time.sleep(SEND_DELAY_SECONDS)

    # ── Final Report ──
    log("\n" + "=" * 60)
    log("FINAL REPORT")
    log("=" * 60)

    coverage = min(total_sent_duration / total_expected_duration, 1.0) if total_expected_duration > 0 else 0

    log(f"  MKV parts found:        {stats['total_mkv_parts']}")
    log(f"  Valid parts:            {stats['valid_parts']}")
    log(f"  Converted to MP4:       {stats['converted_ok']}")
    log(f"  Conversion failed:      {stats['convert_failed']}")
    log(f"  Verification passed:    {stats['verify_ok']}")
    log(f"  Verification failed:    {stats['verify_failed']}")
    log(f"  Size check passed:      {stats['size_ok']}")
    log(f"  Size check failed:      {stats['size_failed']}")
    log(f"  Sent to Telegram:       {stats['sent_ok']}")
    log(f"  Send failed:            {stats['sent_failed']}")
    log(f"  Total duration sent:    {format_ts(total_sent_duration)}")
    log(f"  Expected duration:      {format_ts(total_expected_duration)}")
    log(f"  Coverage:               {coverage*100:.1f}%")

    if stats["failed_parts"]:
        log(f"  Failed part numbers:    {stats['failed_parts']}")

    log("=" * 60)

    if coverage >= 0.95 and failed_count == 0:
        mf["state"] = "SENT"
        mf["coverage"] = round(coverage, 3)
        save_manifest(STREAM_ID, mf)
        log("✅ ALL parts converted and sent successfully")
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
        log(f"⚠️ PARTIAL: {sent_count}/{total} sent. Re-run to retry failed parts.")
    else:
        mf["state"] = "FAILED"
        save_manifest(STREAM_ID, mf)
        log("✗ No parts sent.")
        update_worker_final_state(STREAM_ID, "failed", {
            "failed_at": datetime.now(timezone.utc).isoformat(),
            "error": "no_parts_sent",
        })

    log("Source MKV files NEVER deleted by this pipeline.")


if __name__ == "__main__":
    main()
