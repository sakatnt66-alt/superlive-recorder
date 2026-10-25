#!/usr/bin/env python3
"""
postprocess_recording.py — Independent post-processing pipeline for completed recordings.

Pipeline:
  DISCOVERED → VALIDATING → CONVERTING → VERIFIED → PREVIEW → SENDING → SENT

Design principles:
  - NEVER modifies record_once.py or any recording logic
  - Stream copy only (no re-encoding) unless explicitly impossible
  - Original MKV is NEVER deleted until all outputs are verified and sent
  - Idempotent: safe to re-run without duplicate processing or sending
  - Supports 1–5 concurrent streams via stream_id isolation
  - No secrets in code; all credentials from environment variables

Dependencies: Python 3.10+ standard library, ffmpeg, ffprobe (system)
"""

import json
import mimetypes
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path

# ═══════════════════════════════════════════════════════════
# CONFIGURATION (all from environment — no hardcoded secrets)
# ═══════════════════════════════════════════════════════════
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "")
RECORDINGS_DIR     = Path(os.environ.get("RECORDINGS_DIR", "recordings"))
TEMP_DIR           = RECORDINGS_DIR / "_temp"
STATE_DIR          = RECORDINGS_DIR / "_state"
STREAM_ID          = os.environ.get("STREAM_ID", "unknown")
STREAM_NAME        = os.environ.get("STREAM_NAME", "")

# ═══════════════════════════════════════════════════════════
# CONSTANTS
# ═══════════════════════════════════════════════════════════
GRID_COLS = 4
GRID_ROWS = 3
GRID_COUNT = GRID_COLS * GRID_ROWS  # 12
CELL_W = 400
CELL_H = 225
JPEG_QUALITY = 2

MAX_TG_VIDEO_BYTES = 50 * 1024 * 1024   # 50 MB Bot API limit
MAX_TG_PHOTO_BYTES = 10 * 1024 * 1024   # 10 MB
TG_SEGMENT_SECONDS = 570                 # ~9.5 min per segment (safe under 50MB at 8Mbps)

MARKER_SUFFIX = ".postprocessed"

# Codecs that can be stream-copied into MP4
MP4_VIDEO_OK = {"h264", "hevc", "mpeg4", "mjpeg"}
MP4_AUDIO_OK = {"aac", "mp3", "ac3", "eac3", "alac", "mp2"}
MP4_AUDIO_WARN = {"opus", "vorbis", "flac"}  # Technically possible, limited player support

# State machine
STATES = [
    "DISCOVERED", "VALIDATING", "CONVERTING", "VERIFIED",
    "PREVIEW", "SENDING", "SENT", "FAILED"
]


# ═══════════════════════════════════════════════════════════
# LOGGING
# ═══════════════════════════════════════════════════════════
def log(msg: str, level: str = "INFO") -> None:
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"[POSTPROC {ts}] [{level}] {msg}", flush=True)


# ═══════════════════════════════════════════════════════════
# STATE MANAGEMENT
# ═══════════════════════════════════════════════════════════
def state_path(stream_id: str) -> Path:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    safe_id = "".join(c if c.isalnum() or c in "-_" else "_" for c in str(stream_id))
    return STATE_DIR / f"{safe_id}.json"


def load_state(stream_id: str) -> dict:
    p = state_path(stream_id)
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {
        "stream_id": stream_id,
        "state": "DISCOVERED",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "files": {},
        "telegram": {},
        "errors": [],
    }


def save_state(stream_id: str, state: dict) -> None:
    p = state_path(stream_id)
    tmp = p.with_suffix(".tmp")
    state["updated_at"] = datetime.now(timezone.utc).isoformat()
    tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(p)  # atomic on POSIX


def set_state(stream_id: str, state: dict, new_state: str, **extra) -> dict:
    state["state"] = new_state
    state.update(extra)
    save_state(stream_id, state)
    log(f"State → {new_state}")
    return state


def add_error(stream_id: str, state: dict, error: str) -> None:
    state.setdefault("errors", []).append({
        "time": datetime.now(timezone.utc).isoformat(),
        "error": error,
    })
    save_state(stream_id, state)


# ═══════════════════════════════════════════════════════════
# SHELL / FFPROBE HELPERS
# ═══════════════════════════════════════════════════════════
def run_cmd(cmd: str, timeout: int = 600):
    try:
        return subprocess.run(
            cmd, shell=True, capture_output=True, text=True, timeout=timeout
        )
    except subprocess.TimeoutExpired:
        log(f"Command timed out ({timeout}s): {cmd[:120]}", "WARN")
        return None
    except Exception as exc:
        log(f"Command error: {exc}", "ERROR")
        return None


def ffprobe_json(path: str) -> dict | None:
    r = run_cmd(
        f'ffprobe -v quiet -print_format json -show_format -show_streams "{path}"', 30
    )
    if r and r.returncode == 0:
        try:
            return json.loads(r.stdout)
        except json.JSONDecodeError:
            pass
    return None


def get_duration(info: dict) -> float:
    return float(info.get("format", {}).get("duration", 0))


def get_codecs(info: dict) -> tuple:
    vc = ac = None
    for s in info.get("streams", []):
        ct = s.get("codec_type")
        cn = (s.get("codec_name") or "").lower()
        if ct == "video" and vc is None:
            vc = cn
        elif ct == "audio" and ac is None:
            ac = cn
    return vc, ac


def get_packet_counts(path: str) -> dict:
    """Count video and audio packets using ffprobe."""
    r = run_cmd(
        f'ffprobe -v quiet -count_packets -select_streams v:0 '
        f'-show_entries stream=nb_read_packets -of csv=p=0 "{path}"', 120
    )
    v_pkts = int(r.stdout.strip()) if r and r.returncode == 0 and r.stdout.strip().isdigit() else -1

    r = run_cmd(
        f'ffprobe -v quiet -count_packets -select_streams a:0 '
        f'-show_entries stream=nb_read_packets -of csv=p=0 "{path}"', 120
    )
    a_pkts = int(r.stdout.strip()) if r and r.returncode == 0 and r.stdout.strip().isdigit() else -1

    return {"video_packets": v_pkts, "audio_packets": a_pkts}


def file_is_stable(path: str, wait: int = 3) -> bool:
    try:
        s1 = os.path.getsize(path)
        if s1 == 0:
            return False
        time.sleep(wait)
        return os.path.getsize(path) == s1
    except OSError:
        return False


def human_size(n: int) -> str:
    mb = n / (1024 * 1024)
    return f"{mb / 1024:.2f} GB" if mb >= 1024 else f"{mb:.1f} MB"


def format_ts(seconds: float) -> str:
    h, rem = divmod(int(seconds), 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


# ═══════════════════════════════════════════════════════════
# FILE DISCOVERY
# ═══════════════════════════════════════════════════════════
def discover_mkv_files() -> list:
    """Find all MKV files in recordings directories."""
    files = []
    for d in [RECORDINGS_DIR, TEMP_DIR]:
        if d.exists():
            files.extend(sorted(d.glob("*.mkv")))
    # Exclude files that are clearly still being written (very recent, size 0)
    valid = []
    for f in files:
        try:
            if f.stat().st_size > 0:
                valid.append(f)
        except OSError:
            pass
    return valid


# ═══════════════════════════════════════════════════════════
# MKV → MP4 CONVERSION (STREAM COPY)
# ═══════════════════════════════════════════════════════════
def check_codec_compat(info: dict) -> dict:
    vc, ac = get_codecs(info)
    result = {
        "video_codec": vc,
        "audio_codec": ac,
        "video_ok": vc in MP4_VIDEO_OK if vc else False,
        "audio_ok": ac in MP4_AUDIO_OK if ac else True,
        "audio_warn": ac in MP4_AUDIO_WARN if ac else False,
        "can_copy": False,
        "notes": [],
    }
    if not result["video_ok"]:
        result["notes"].append(f"Video codec '{vc}' is NOT MP4-compatible. Cannot stream-copy.")
    if ac and not result["audio_ok"] and not result["audio_warn"]:
        result["notes"].append(f"Audio codec '{ac}' is NOT MP4-compatible.")
    if result["audio_warn"]:
        result["notes"].append(
            f"Audio codec '{ac}' can be copied to MP4 but has LIMITED player support. "
            f"Original MKV is preserved as the authoritative copy."
        )
    result["can_copy"] = result["video_ok"] and (result["audio_ok"] or result["audio_warn"])
    return result


def remux_to_mp4(src: str, dst: str) -> tuple:
    """
    Remux MKV → MP4 using stream copy. No re-encoding.
    Returns (success, error_message, warnings).
    """
    warnings = []
    tmp = dst + ".tmp.mp4"

    # Build ffmpeg command
    cmd = (
        f'ffmpeg -y -i "{src}" '
        f'-map 0:v:0 -map 0:a? '
        f'-c:v copy '
        f'-c:a copy '
        f'-movflags +faststart '
        f'-fflags +genpts '
        f'"{tmp}"'
    )

    log(f"Remux command: {cmd}")
    r = run_cmd(cmd, timeout=900)

    if r is None:
        if os.path.exists(tmp):
            os.remove(tmp)
        return False, "FFmpeg timed out during remux", warnings

    if r.returncode != 0:
        stderr_tail = (r.stderr or "")[-500:]
        if os.path.exists(tmp):
            os.remove(tmp)

        # Check for specific Opus-in-MP4 issues
        if "opus" in stderr_tail.lower() or "codec not currently supported" in stderr_tail.lower():
            warnings.append("Opus audio may not be fully supported in MP4 container by ffmpeg.")
            # Try without audio
            cmd_no_audio = (
                f'ffmpeg -y -i "{src}" '
                f'-map 0:v:0 '
                f'-c:v copy '
                f'-movflags +faststart '
                f'"{tmp}"'
            )
            log("Retrying remux without audio stream...", "WARN")
            r2 = run_cmd(cmd_no_audio, timeout=900)
            if r2 and r2.returncode == 0 and os.path.exists(tmp) and os.path.getsize(tmp) > 0:
                warnings.append("MP4 created with VIDEO ONLY. Audio preserved in original MKV.")
                os.replace(tmp, dst)
                return True, "", warnings
            if os.path.exists(tmp):
                os.remove(tmp)
            return False, f"Remux failed (with and without audio): {stderr_tail}", warnings

        return False, stderr_tail, warnings

    if not os.path.exists(tmp) or os.path.getsize(tmp) == 0:
        return False, "Output file is empty or missing", warnings

    # Validate output
    out_info = ffprobe_json(tmp)
    if out_info is None:
        os.remove(tmp)
        return False, "Output MP4 failed ffprobe validation", warnings

    out_vc, out_ac = get_codecs(out_info)
    if not out_vc:
        os.remove(tmp)
        return False, "Output MP4 has no video stream", warnings

    os.replace(tmp, dst)
    return True, "", warnings


def verify_mp4(mp4_path: str, src_info: dict) -> dict:
    """Verify MP4 integrity and compare with source."""
    result = {"valid": False, "details": {}}

    info = ffprobe_json(mp4_path)
    if info is None:
        result["details"]["error"] = "ffprobe cannot read MP4"
        return result

    src_dur = get_duration(src_info)
    dst_dur = get_duration(info)
    src_vc, src_ac = get_codecs(src_info)
    dst_vc, dst_ac = get_codecs(info)

    result["details"] = {
        "src_duration": round(src_dur, 2),
        "dst_duration": round(dst_dur, 2),
        "duration_diff": round(abs(src_dur - dst_dur), 2),
        "src_video_codec": src_vc,
        "dst_video_codec": dst_vc,
        "src_audio_codec": src_ac,
        "dst_audio_codec": dst_ac,
        "file_size": os.path.getsize(mp4_path),
    }

    # Duration should be within 2 seconds
    if abs(src_dur - dst_dur) > 2.0:
        result["details"]["warning"] = f"Duration mismatch: {src_dur:.1f}s vs {dst_dur:.1f}s"

    # Video codec must match (stream copy)
    if src_vc and dst_vc and src_vc != dst_vc:
        result["details"]["error"] = f"Video codec changed: {src_vc} → {dst_vc}"
        return result

    result["valid"] = True
    return result


# ═══════════════════════════════════════════════════════════
# PREVIEW GRID (12 frames, 4×3)
# ═══════════════════════════════════════════════════════════
def extract_frames(src: str, duration: float, tmpdir: str) -> list:
    """Extract GRID_COUNT frames evenly distributed across the video."""
    paths = []
    if duration <= 0:
        return paths

    margin = min(2.0, duration * 0.02)
    span = max(duration - 2 * margin, 0.1)

    for i in range(GRID_COUNT):
        ts = margin + (span * i / max(GRID_COUNT - 1, 1))
        out = os.path.join(tmpdir, f"frame_{i:02d}.jpg")

        cmd = (
            f'ffmpeg -y -ss {ts:.3f} -i "{src}" '
            f'-frames:v 1 -q:v {JPEG_QUALITY} '
            f'-vf "scale={CELL_W}:{CELL_H}:force_original_aspect_ratio=decrease,'
            f'pad={CELL_W}:{CELL_H}:(ow-iw)/2:(oh-ih)/2:black" '
            f'"{out}"'
        )
        r = run_cmd(cmd, timeout=30)
        if r and r.returncode == 0 and os.path.exists(out) and os.path.getsize(out) > 0:
            paths.append((out, ts))
        else:
            log(f"Frame {i + 1}/{GRID_COUNT} at {ts:.1f}s failed", "WARN")

    return paths


def build_grid(frames: list, out_path: str) -> bool:
    """Build a 4×3 grid image from extracted frames using ffmpeg."""
    n = len(frames)
    if n == 0:
        return False

    inputs = " ".join(f'-i "{p}"' for p, _ in frames)
    parts = []

    for i in range(n):
        ts = frames[i][1]
        label = format_ts(ts)
        parts.append(
            f"[{i}:v]scale={CELL_W}:{CELL_H}:"
            f"force_original_aspect_ratio=decrease,"
            f"pad={CELL_W}:{CELL_H}:(ow-iw)/2:(oh-ih)/2:black,"
            f"drawtext=text='{label}':fontsize=18:fontcolor=white:"
            f"borderw=1:bordercolor=black:x=4:y=h-24,"
            f"setsar=1[v{i}]"
        )

    rows = []
    for r in range(GRID_ROWS):
        cols = []
        for c in range(GRID_COLS):
            idx = r * GRID_COLS + c
            if idx < n:
                cols.append(f"[v{idx}]")
        if not cols:
            continue
        if len(cols) > 1:
            rows.append(f"{''.join(cols)}hstack=inputs={len(cols)}[row{r}]")
        else:
            rows.append(f"{cols[0]}copy[row{r}]")

    if not rows:
        return False

    if len(rows) > 1:
        refs = "".join(f"[row{r}]" for r in range(len(rows)))
        parts.append(f"{refs}vstack=inputs={len(rows)}[out]")
    else:
        parts.append("[row0]copy[out]")

    fc = ";".join(parts)
    cmd = f'ffmpeg -y {inputs} -filter_complex "{fc}" -map "[out]" -q:v {JPEG_QUALITY} "{out_path}"'
    r = run_cmd(cmd, timeout=120)
    return (
        r is not None
        and r.returncode == 0
        and os.path.exists(out_path)
        and os.path.getsize(out_path) > 0
    )


# ═══════════════════════════════════════════════════════════
# TELEGRAM UPLOAD
# ═══════════════════════════════════════════════════════════
def tg_upload(method: str, filepath: str, field: str, caption: str = "") -> dict:
    """
    Upload a file to Telegram Bot API.
    Returns {"ok": bool, "message_id": int|None, "error": str}.
    Retries up to 3 times on transient errors (NOT on 403).
    """
    result = {"ok": False, "message_id": None, "error": ""}

    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        result["error"] = "Telegram credentials not configured"
        return result

    fsize = os.path.getsize(filepath)
    limit = MAX_TG_VIDEO_BYTES if "video" in method.lower() else MAX_TG_PHOTO_BYTES
    if fsize > limit:
        result["error"] = f"File too large: {human_size(fsize)} > {human_size(limit)}"
        return result

    boundary = uuid.uuid4().hex
    fname = os.path.basename(filepath)
    mime = mimetypes.guess_type(filepath)[0] or "application/octet-stream"

    with open(filepath, "rb") as fh:
        fdata = fh.read()

    body = b""
    body += f"--{boundary}\r\nContent-Disposition: form-data; name=\"chat_id\"\r\n\r\n{TELEGRAM_CHAT_ID}\r\n".encode()
    if caption:
        body += f"--{boundary}\r\nContent-Disposition: form-data; name=\"caption\"\r\n\r\n".encode()
        body += caption.encode("utf-8") + b"\r\n"
    body += f"--{boundary}\r\nContent-Disposition: form-data; name=\"{field}\"; filename=\"{fname}\"\r\nContent-Type: {mime}\r\n\r\n".encode()
    body += fdata
    body += f"\r\n--{boundary}--\r\n".encode()

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{method}"

    for attempt in range(1, 4):
        try:
            req = urllib.request.Request(
                url, data=body,
                headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
            )
            with urllib.request.urlopen(req, timeout=180) as resp:
                if resp.status == 200:
                    resp_data = json.loads(resp.read().decode("utf-8"))
                    msg_id = resp_data.get("result", {}).get("message_id")
                    result["ok"] = True
                    result["message_id"] = msg_id
                    return result
        except urllib.error.HTTPError as e:
            if e.code == 403:
                result["error"] = f"HTTP 403 Forbidden — likely invalid bot token or blocked chat_id. NOT retrying."
                log(result["error"], "ERROR")
                return result  # Do NOT retry 403
            result["error"] = f"HTTP {e.code}: {e.reason}"
            log(f"Telegram {method} attempt {attempt}/3: HTTP {e.code}", "WARN")
        except Exception as exc:
            result["error"] = str(exc)
            log(f"Telegram {method} attempt {attempt}/3: {exc}", "WARN")

        if attempt < 3:
            time.sleep(3 * attempt)

    return result


def tg_send_text(text: str) -> None:
    """Send a plain text message to Telegram (for notifications)."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    payload = json.dumps({
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
    }).encode("utf-8")
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    try:
        urllib.request.urlopen(req, timeout=30)
    except Exception:
        pass


# ═══════════════════════════════════════════════════════════
# SEGMENT SPLITTING (for files > 50MB)
# ═══════════════════════════════════════════════════════════
def split_mp4_at_keyframes(src: str, outdir: str) -> list:
    """
    Split MP4 into segments at keyframe boundaries using stream copy.
    Returns list of segment file paths.
    """
    pattern = os.path.join(outdir, "segment_%03d.mp4")
    cmd = (
        f'ffmpeg -y -i "{src}" '
        f'-c copy -map 0 '
        f'-f segment -segment_time {TG_SEGMENT_SECONDS} '
        f'-reset_timestamps 1 '
        f'"{pattern}"'
    )
    log(f"Splitting MP4 into ~{TG_SEGMENT_SECONDS}s segments at keyframe boundaries...")
    r = run_cmd(cmd, timeout=900)

    if r is None or r.returncode != 0:
        log("Segment splitting failed", "ERROR")
        return []

    segments = sorted(Path(outdir).glob("segment_*.mp4"))
    valid = [s for s in segments if s.stat().st_size > 0]
    log(f"Created {len(valid)} segment(s)")
    return [str(s) for s in valid]


# ═══════════════════════════════════════════════════════════
# MAIN PIPELINE FOR ONE RECORDING
# ═══════════════════════════════════════════════════════════
def process_one(mkv_path: Path, sid: str) -> bool:
    mkv = str(mkv_path)
    marker = mkv + MARKER_SUFFIX

    # ── Idempotency check ──
    if os.path.exists(marker):
        log(f"⏭️  Already processed: {mkv_path.name}")
        return True

    st = load_state(sid)

    # Skip if already fully sent
    if st.get("state") == "SENT" and st.get("telegram", {}).get("video_sent"):
        log(f"⏭️  Already sent (state=SENT): {mkv_path.name}")
        Path(marker).write_text("sent\n")
        return True

    log(f"{'=' * 60}")
    log(f"📂 Processing: {mkv_path.name}  (stream_id={sid})")
    log(f"{'=' * 60}")

    # ── DISCOVERED → VALIDATING ──
    set_state(sid, st, "VALIDATING")

    if not file_is_stable(mkv):
        log("File is still being written or empty — skipping", "WARN")
        set_state(sid, st, "DISCOVERED")
        return False

    info = ffprobe_json(mkv)
    if info is None:
        log("ffprobe cannot read this file", "ERROR")
        add_error(sid, st, "ffprobe failed on source MKV")
        set_state(sid, st, "FAILED")
        return False

    duration = get_duration(info)
    if duration <= 0:
        log("No valid duration", "ERROR")
        set_state(sid, st, "FAILED")
        return False

    src_size = os.path.getsize(mkv)
    log(f"⏱️  Duration : {format_ts(duration)}  ({duration:.0f}s)")
    log(f"📦 Size    : {human_size(src_size)}")

    # Packet counts for diagnostics
    pkts = get_packet_counts(mkv)
    log(f"📊 Packets : video={pkts['video_packets']}, audio={pkts['audio_packets']}")
    st["source_info"] = {
        "duration": round(duration, 2),
        "size_bytes": src_size,
        "video_packets": pkts["video_packets"],
        "audio_packets": pkts["audio_packets"],
    }
    save_state(sid, st)

    # ── Codec compatibility ──
    compat = check_codec_compat(info)
    log(f"🎬 Video: {compat['video_codec']}  |  Audio: {compat['audio_codec']}")
    for note in compat["notes"]:
        log(f"   ⚠️ {note}", "WARN")

    if not compat["can_copy"]:
        log("Cannot stream-copy to MP4. Keeping original MKV.", "ERROR")
        add_error(sid, st, f"Incompatible codecs: v={compat['video_codec']}, a={compat['audio_codec']}")
        set_state(sid, st, "FAILED")
        tg_send_text(
            f"⚠️ <b>تعذر تحويل التسجيل إلى MP4</b>\n"
            f"🆔 {sid}\n"
            f"🎬 الترميزات غير متوافقة مع MP4.\n"
            f"تم الاحتفاظ بملف MKV الأصلي."
        )
        return False

    # ── VALIDATING → CONVERTING ──
    set_state(sid, st, "CONVERTING")

    mp4_path = str(mkv_path.with_suffix(".mp4"))
    ok, err, warnings = remux_to_mp4(mkv, mp4_path)

    if not ok:
        log(f"Remux failed: {err}", "ERROR")
        add_error(sid, st, f"Remux failed: {err}")
        set_state(sid, st, "FAILED")
        tg_send_text(
            f"❌ <b>فشل تحويل التسجيل</b>\n"
            f"🆔 {sid}\n"
            f"⚠️ {err[:200]}\n"
            f"تم الاحتفاظ بملف MKV الأصلي."
        )
        return False

    for w in warnings:
        log(f"⚠️ {w}", "WARN")

    mp4_size = os.path.getsize(mp4_path)
    log(f"✅ MP4 created: {os.path.basename(mp4_path)}  ({human_size(mp4_size)})")

    # ── CONVERTING → VERIFIED ──
    verification = verify_mp4(mp4_path, info)
    if not verification["valid"]:
        err_msg = verification["details"].get("error", "Verification failed")
        log(f"MP4 verification failed: {err_msg}", "ERROR")
        add_error(sid, st, f"Verification: {err_msg}")
        os.remove(mp4_path)
        set_state(sid, st, "FAILED")
        return False

    det = verification["details"]
    log(f"✅ Verified: dur={det['dst_duration']}s (src={det['src_duration']}s), "
        f"codec={det['dst_video_codec']}/{det['dst_audio_codec'] or 'none'}")
    st["mp4_info"] = det
    save_state(sid, st)
    set_state(sid, st, "VERIFIED")

    # ── VERIFIED → PREVIEW ──
    set_state(sid, st, "PREVIEW")

    preview_path = str(mkv_path.with_suffix("")) + "_preview.jpg"
    preview_ok = False

    with tempfile.TemporaryDirectory(prefix="pp_frames_") as tmpdir:
        frames = extract_frames(mkv, duration, tmpdir)  # Extract from ORIGINAL MKV
        log(f"🖼️  Extracted {len(frames)}/{GRID_COUNT} frames from original MKV")

        if len(frames) >= 2:
            if build_grid(frames, preview_path):
                preview_ok = True
                log(f"✅ Preview grid: {os.path.basename(preview_path)}")
            else:
                log("Grid creation failed", "WARN")
        else:
            log("Not enough frames for preview", "WARN")

    st["preview_ok"] = preview_ok
    save_state(sid, st)

    # ── PREVIEW → SENDING ──
    set_state(sid, st, "SENDING")

    display_name = STREAM_NAME or mkv_path.stem
    caption = (
        f"🎬 {display_name}\n"
        f"🆔 {sid}\n"
        f"⏱️ {format_ts(duration)}\n"
        f"📦 {human_size(mp4_size)}"
    )

    # --- Send video ---
    video_sent = False
    video_msg_id = None
    tg_video = st.get("telegram", {}).get("video")

    if tg_video and tg_video.get("ok"):
        log("⏭️  Video already sent (from state)")
        video_sent = True
        video_msg_id = tg_video.get("message_id")
    elif mp4_size <= MAX_TG_VIDEO_BYTES:
        res = tg_upload("sendVideo", mp4_path, "video", caption)
        if res["ok"]:
            video_sent = True
            video_msg_id = res["message_id"]
            log(f"📤 Video sent ✅  (message_id={video_msg_id})")
        else:
            log(f"Video upload failed: {res['error']}", "ERROR")
            add_error(sid, st, f"Video upload: {res['error']}")
    else:
        log(f"Video too large ({human_size(mp4_size)}). Splitting into segments...", "WARN")
        with tempfile.TemporaryDirectory(prefix="pp_seg_") as segdir:
            segments = split_mp4_at_keyframes(mp4_path, segdir)
            if segments:
                all_ok = True
                for idx, seg in enumerate(segments):
                    seg_cap = f"{caption}\n📎 الجزء {idx + 1}/{len(segments)}"
                    res = tg_upload("sendVideo", seg, "video", seg_cap)
                    if res["ok"]:
                        log(f"📤 Segment {idx + 1}/{len(segments)} sent ✅")
                    else:
                        log(f"Segment {idx + 1} failed: {res['error']}", "ERROR")
                        all_ok = False
                        add_error(sid, st, f"Segment {idx + 1}: {res['error']}")
                        break
                    time.sleep(1)
                if all_ok:
                    video_sent = True
                    log(f"📤 All {len(segments)} segments sent ✅")
            else:
                log("Segment splitting failed — video not sent", "ERROR")
                add_error(sid, st, "Segment splitting failed")

    # --- Send preview ---
    preview_sent = False
    tg_preview = st.get("telegram", {}).get("preview")

    if tg_preview and tg_preview.get("ok"):
        log("⏭️  Preview already sent (from state)")
        preview_sent = True
    elif preview_ok and os.path.exists(preview_path):
        psize = os.path.getsize(preview_path)
        if psize <= MAX_TG_PHOTO_BYTES:
            res = tg_upload("sendPhoto", preview_path, "photo", f"🖼️ معاينة: {display_name}")
            if res["ok"]:
                preview_sent = True
                log(f"📤 Preview sent ✅  (message_id={res['message_id']})")
            else:
                log(f"Preview upload failed: {res['error']}", "ERROR")
                add_error(sid, st, f"Preview upload: {res['error']}")
        else:
            log(f"Preview too large: {human_size(psize)}", "WARN")

    # Update Telegram state
    st["telegram"] = {
        "video": {"ok": video_sent, "message_id": video_msg_id},
        "preview": {"ok": preview_sent},
        "video_sent": video_sent,
        "preview_sent": preview_sent,
    }
    save_state(sid, st)

    # ── SENDING → SENT or FAILED ──
    if video_sent:
        set_state(sid, st, "SENT")
        Path(marker).write_text(json.dumps({
            "sent_at": datetime.now(timezone.utc).isoformat(),
            "video_msg_id": video_msg_id,
        }, indent=2))
        log(f"✅ COMPLETE: {mkv_path.name}")
        return True
    else:
        set_state(sid, st, "FAILED")
        add_error(sid, st, "Video was not sent successfully")
        log("Video not sent — original files preserved for retry", "ERROR")
        return False


# ═══════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════
def main():
    log("=" * 60)
    log("Post-processing pipeline started")
    log(f"  RECORDINGS_DIR : {RECORDINGS_DIR}")
    log(f"  STREAM_ID      : {STREAM_ID}")
    log(f"  STREAM_NAME    : {STREAM_NAME or '(not set)'}")
    log(f"  TELEGRAM       : {'configured' if TELEGRAM_BOT_TOKEN else 'NOT configured'}")
    log("=" * 60)

    mkv_files = discover_mkv_files()
    if not mkv_files:
        log("No MKV files found — nothing to process.")
        return

    log(f"Found {len(mkv_files)} MKV file(s)")

    success = 0
    fail = 0
    for mkv in mkv_files:
        try:
            if process_one(mkv, STREAM_ID):
                success += 1
            else:
                fail += 1
        except Exception as exc:
            log(f"Unexpected error on {mkv.name}: {exc}", "ERROR")
            import traceback
            traceback.print_exc()
            fail += 1

    log("=" * 60)
    log(f"Results: {success} succeeded, {fail} failed/skipped")
    log("Post-processing pipeline finished")
    log("=" * 60)


if __name__ == "__main__":
    main()
