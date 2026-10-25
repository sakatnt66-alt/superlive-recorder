#!/usr/bin/env python3
"""
postprocess_recording.py — Post-processing pipeline for completed recordings.

Pipeline:
  1. Validate MKV file integrity (ffprobe)
  2. Remux MKV → MP4 via stream copy (NO re-encoding)
  3. Extract 12 evenly-distributed frames
  4. Build a 4×3 preview grid image
  5. Send MP4 + preview to Telegram

This script is fully independent. It does NOT modify record_once.py
or any recording logic. It runs AFTER the recording is complete.
"""

import json
import mimetypes
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path

# ──────────────────────────────────────────────
# Configuration (from environment variables)
# ──────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "")
RECORDINGS_DIR     = Path(os.environ.get("RECORDINGS_DIR", "recordings"))
TEMP_DIR           = RECORDINGS_DIR / "_temp"
STREAM_ID          = os.environ.get("STREAM_ID", "unknown")
STREAM_NAME        = os.environ.get("STREAM_NAME", "")

# ──────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────
GRID_COLS   = 4
GRID_ROWS   = 3
GRID_COUNT  = GRID_COLS * GRID_ROWS          # 12 frames
CELL_W      = 400                             # width of each cell
CELL_H      = 225                             # height of each cell
GAP         = 4                               # gap between cells (px)
JPEG_Q      = 2                               # ffmpeg JPEG quality (2 = high)

MAX_TG_VIDEO_BYTES = 50 * 1024 * 1024        # 50 MB  (Bot API upload limit)
MAX_TG_PHOTO_BYTES = 10 * 1024 * 1024        # 10 MB

MARKER_SUFFIX = ".postprocessed"              # idempotency marker

# Codecs that can be *copied* into an MP4 container without re-encoding
MP4_VIDEO_OK = {"h264", "hevc", "mpeg4", "mjpeg"}
MP4_AUDIO_OK = {"aac", "mp3", "ac3", "eac3", "alac", "mp2"}

# ──────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────
def log(msg: str) -> None:
    print(f"[POSTPROC] {msg}", flush=True)


def run(cmd: str, timeout: int = 300):
    """Run a shell command; return CompletedProcess or None on timeout/error."""
    try:
        return subprocess.run(
            cmd, shell=True, capture_output=True, text=True, timeout=timeout
        )
    except subprocess.TimeoutExpired:
        log(f"⏰ Timeout ({timeout}s): {cmd[:100]}")
        return None
    except Exception as exc:
        log(f"❌ Command error: {exc}")
        return None


def ffprobe_json(path: str):
    """Return parsed ffprobe JSON or None."""
    r = run(f'ffprobe -v quiet -print_format json -show_format -show_streams "{path}"', 30)
    if r and r.returncode == 0:
        try:
            return json.loads(r.stdout)
        except json.JSONDecodeError:
            pass
    return None


def file_is_stable(path: str, wait: int = 3) -> bool:
    """Check that the file exists, is non-empty, and its size is not changing."""
    try:
        s1 = os.path.getsize(path)
        if s1 == 0:
            return False
        time.sleep(wait)
        return os.path.getsize(path) == s1
    except OSError:
        return False


def get_codecs(info: dict):
    """Return (video_codec, audio_codec) from ffprobe info."""
    vc = ac = None
    for s in info.get("streams", []):
        ct = s.get("codec_type")
        cn = s.get("codec_name", "").lower()
        if ct == "video" and vc is None:
            vc = cn
        elif ct == "audio" and ac is None:
            ac = cn
    return vc, ac


def human_size(nbytes: int) -> str:
    mb = nbytes / (1024 * 1024)
    if mb >= 1024:
        return f"{mb / 1024:.2f} GB"
    return f"{mb:.1f} MB"


def format_duration(seconds: float) -> str:
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


# ──────────────────────────────────────────────
# 1. Remux  MKV → MP4  (stream copy, zero re-encode)
# ──────────────────────────────────────────────
def remux_mkv_to_mp4(src: str, dst: str) -> tuple:
    """
    Copy video and audio streams into an MP4 container.
    Returns (success: bool, error_message: str).
    """
    tmp = dst + ".tmp"
    cmd = (
        f'ffmpeg -y -i "{src}" '
        f'-c:v copy -c:a copy '
        f'-movflags +faststart '
        f'-map 0:v:0 -map 0:a:0? '
        f'"{tmp}"'
    )
    r = run(cmd, timeout=900)

    if r is None:
        if os.path.exists(tmp):
            os.remove(tmp)
        return False, "FFmpeg timed out during remux"

    if r.returncode != 0:
        tail = (r.stderr or "")[-400:]
        if os.path.exists(tmp):
            os.remove(tmp)
        return False, tail

    if not os.path.exists(tmp) or os.path.getsize(tmp) == 0:
        return False, "Output file is empty"

    if ffprobe_json(tmp) is None:
        os.remove(tmp)
        return False, "Output file failed ffprobe validation"

    os.replace(tmp, dst)
    return True, ""


# ──────────────────────────────────────────────
# 2. Extract 12 frames distributed over duration
# ──────────────────────────────────────────────
def extract_frames(src: str, duration: float, tmpdir: str) -> list:
    """
    Extract GRID_COUNT frames evenly spaced across the video.
    Returns list of frame file paths that were successfully created.
    """
    paths = []
    margin = min(2.0, duration * 0.02)
    span   = max(duration - 2 * margin, 0.1)

    for i in range(GRID_COUNT):
        if GRID_COUNT > 1:
            ts = margin + span * i / (GRID_COUNT - 1)
        else:
            ts = margin

        out = os.path.join(tmpdir, f"frame_{i:02d}.jpg")
        cmd = (
            f'ffmpeg -y -ss {ts:.3f} -i "{src}" '
            f'-frames:v 1 -q:v {JPEG_Q} '
            f'-vf "scale={CELL_W}:{CELL_H}:force_original_aspect_ratio=decrease,'
            f'pad={CELL_W}:{CELL_H}:(ow-iw)/2:(oh-ih)/2:black" '
            f'"{out}"'
        )
        r = run(cmd, timeout=30)
        if r and r.returncode == 0 and os.path.exists(out) and os.path.getsize(out) > 0:
            paths.append(out)
        else:
            log(f"⚠️ Frame {i + 1}/{GRID_COUNT} at {ts:.1f}s failed")

    return paths


# ──────────────────────────────────────────────
# 3. Build 4×3 grid preview image
# ──────────────────────────────────────────────
def build_grid(frame_paths: list, out_path: str) -> bool:
    """
    Combine extracted frames into a single grid image using ffmpeg
    hstack / vstack filters.  No PIL dependency.
    """
    n = len(frame_paths)
    if n == 0:
        return False

    inputs = " ".join(f'-i "{p}"' for p in frame_paths)
    parts  = []

    for i in range(n):
        parts.append(
            f"[{i}:v]scale={CELL_W}:{CELL_H}:"
            f"force_original_aspect_ratio=decrease,"
            f"pad={CELL_W}:{CELL_H}:(ow-iw)/2:(oh-ih)/2:black,"
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

    if len(rows) == 0:
        return False

    if len(rows) > 1:
        refs = "".join(f"[row{r}]" for r in range(len(rows)))
        parts.append(f"{refs}vstack=inputs={len(rows)}[out]")
    else:
        parts.append("[row0]copy[out]")

    fc  = ";".join(parts)
    cmd = f'ffmpeg -y {inputs} -filter_complex "{fc}" -map "[out]" -q:v {JPEG_Q} "{out_path}"'
    r   = run(cmd, timeout=120)
    return (
        r is not None
        and r.returncode == 0
        and os.path.exists(out_path)
        and os.path.getsize(out_path) > 0
    )


# ──────────────────────────────────────────────
# 4. Telegram upload  (Bot API, multipart)
# ──────────────────────────────────────────────
def tg_upload(method: str, filepath: str, field: str, caption: str = "") -> bool:
    """
    Upload a file to Telegram using the Bot API.
    Retries up to 3 times on transient errors.
    """
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log("⚠️ Telegram credentials not set — skipping upload")
        return False

    fsize = os.path.getsize(filepath)
    limit = MAX_TG_VIDEO_BYTES if "video" in method.lower() else MAX_TG_PHOTO_BYTES
    if fsize > limit:
        log(f"⚠️ File too large for Telegram: {human_size(fsize)} > {human_size(limit)}")
        return False

    boundary = uuid.uuid4().hex
    fname    = os.path.basename(filepath)
    mime     = mimetypes.guess_type(filepath)[0] or "application/octet-stream"

    with open(filepath, "rb") as fh:
        fdata = fh.read()

    body  = b""
    body += f"--{boundary}\r\n".encode()
    body += b'Content-Disposition: form-data; name="chat_id"\r\n\r\n'
    body += f"{TELEGRAM_CHAT_ID}\r\n".encode()

    if caption:
        body += f"--{boundary}\r\n".encode()
        body += b'Content-Disposition: form-data; name="caption"\r\n\r\n'
        body += caption.encode("utf-8") + b"\r\n"

    body += f"--{boundary}\r\n".encode()
    body += f'Content-Disposition: form-data; name="{field}"; filename="{fname}"\r\n'.encode()
    body += f"Content-Type: {mime}\r\n\r\n".encode()
    body += fdata
    body += f"\r\n--{boundary}--\r\n".encode()

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{method}"

    for attempt in range(1, 4):
        try:
            req = urllib.request.Request(
                url, data=body,
                headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
            )
            with urllib.request.urlopen(req, timeout=120) as resp:
                if resp.status == 200:
                    return True
        except Exception as exc:
            log(f"⚠️ Telegram {method} attempt {attempt}/3: {exc}")
            if attempt < 3:
                time.sleep(3)

    return False


# ──────────────────────────────────────────────
# 5. Process one recording end-to-end
# ──────────────────────────────────────────────
def process_recording(mkv_path: Path) -> bool:
    mkv    = str(mkv_path)
    marker = mkv + MARKER_SUFFIX

    # Idempotency: skip if already processed
    if os.path.exists(marker):
        log(f"⏭️  Already processed: {mkv_path.name}")
        return True

    log(f"📂 Processing: {mkv_path.name}")

    # --- Validate file is complete ---
    if not file_is_stable(mkv):
        log("✗ File is still being written or is empty — skipping")
        return False

    # --- Probe ---
    info = ffprobe_json(mkv)
    if info is None:
        log("✗ ffprobe cannot read this file")
        return False

    duration = float(info.get("format", {}).get("duration", 0))
    if duration <= 0:
        log("✗ No valid duration found")
        return False

    log(f"⏱️  Duration : {format_duration(duration)}  ({duration:.0f}s)")
    log(f"📦 Size    : {human_size(os.path.getsize(mkv))}")

    # --- Codec compatibility check ---
    vc, ac = get_codecs(info)
    log(f"🎬 Video codec: {vc or 'none'}")
    log(f"🔊 Audio codec: {ac or 'none'}")

    if vc and vc not in MP4_VIDEO_OK:
        log(f"✗ Video codec '{vc}' is NOT MP4-compatible.")
        log("  Keeping original MKV. No re-encoding will be attempted.")
        return False

    if ac and ac not in MP4_AUDIO_OK:
        log(f"✗ Audio codec '{ac}' is NOT MP4-compatible.")
        log("  Keeping original MKV. No re-encoding will be attempted.")
        return False

    # --- Remux MKV → MP4 ---
    mp4_path = str(mkv_path.with_suffix(".mp4"))
    log("🔄 Remuxing MKV → MP4 (stream copy, no re-encode)...")

    ok, err = remux_mkv_to_mp4(mkv, mp4_path)
    if not ok:
        log(f"✗ Remux failed: {err}")
        log("  Original MKV is preserved.")
        return False

    mp4_size = os.path.getsize(mp4_path)
    log(f"✅ MP4 created: {os.path.basename(mp4_path)}  ({human_size(mp4_size)})")

    # --- Verify MP4 has video stream ---
    mp4_info = ffprobe_json(mp4_path)
    if mp4_info is None:
        log("✗ MP4 failed validation")
        os.remove(mp4_path)
        return False

    mp4_vc, mp4_ac = get_codecs(mp4_info)
    log(f"✅ MP4 codecs confirmed: video={mp4_vc}, audio={mp4_ac or 'none'}")

    # --- Extract frames & build preview grid ---
    preview_path = str(mkv_path.with_suffix("")) + "_preview.jpg"
    preview_ok   = False

    with tempfile.TemporaryDirectory(prefix="postproc_") as tmpdir:
        frames = extract_frames(mp4_path, duration, tmpdir)
        log(f"🖼️  Extracted {len(frames)}/{GRID_COUNT} frames")

        if len(frames) >= 2:
            if build_grid(frames, preview_path):
                log(f"✅ Preview grid created: {os.path.basename(preview_path)}")
                preview_ok = True
            else:
                log("⚠️ Grid image creation failed")
        else:
            log("⚠️ Not enough frames for a preview grid")

    # --- Send to Telegram ---
    display_name = STREAM_NAME or mkv_path.stem
    dur_str      = format_duration(duration)
    caption      = (
        f"🎬 {display_name}\n"
        f"🆔 {STREAM_ID}\n"
        f"⏱️ {dur_str}\n"
        f"📦 {human_size(mp4_size)}"
    )

    video_sent = False
    if mp4_size <= MAX_TG_VIDEO_BYTES:
        video_sent = tg_upload("sendVideo", mp4_path, "video", caption)
        if video_sent:
            log("📤 Video sent to Telegram ✅")
        else:
            log("⚠️ Video upload to Telegram failed")
    else:
        log(f"⚠️ Video too large for Bot API ({human_size(mp4_size)}). Keeping local file.")

    photo_sent = False
    if preview_ok and os.path.exists(preview_path):
        psize = os.path.getsize(preview_path)
        if psize <= MAX_TG_PHOTO_BYTES:
            photo_sent = tg_upload(
                "sendPhoto", preview_path, "photo",
                f"🖼️ معاينة: {display_name}\n⏱️ {dur_str}"
            )
            if photo_sent:
                log("📤 Preview sent to Telegram ✅")
            else:
                log("⚠️ Preview upload to Telegram failed")
        else:
            log(f"⚠️ Preview too large: {human_size(psize)}")

    # --- Write idempotency marker ---
    with open(marker, "w", encoding="utf-8") as f:
        json.dump(
            {
                "processed_at": datetime.now(timezone.utc).isoformat(),
                "source": os.path.basename(mkv),
                "mp4": os.path.basename(mp4_path),
                "preview": os.path.basename(preview_path) if preview_ok else None,
                "duration_sec": round(duration, 1),
                "video_codec": vc,
                "audio_codec": ac,
                "video_sent": video_sent,
                "preview_sent": photo_sent,
            },
            f,
            indent=2,
            ensure_ascii=False,
        )

    log(f"✅ Done: {mkv_path.name}")
    return video_sent or photo_sent


# ──────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────
def main():
    log("=" * 60)
    log("Post-processing pipeline started")
    log(f"  RECORDINGS_DIR : {RECORDINGS_DIR}")
    log(f"  STREAM_ID      : {STREAM_ID}")
    log(f"  STREAM_NAME    : {STREAM_NAME or '(not set)'}")
    log("=" * 60)

    # Collect MKV files from both directories
    mkv_files = sorted(RECORDINGS_DIR.glob("*.mkv"))
    if TEMP_DIR.exists():
        mkv_files += sorted(TEMP_DIR.glob("*.mkv"))

    if not mkv_files:
        log("No MKV files found — nothing to process.")
        return

    log(f"Found {len(mkv_files)} MKV file(s)")

    success = 0
    fail    = 0
    for mkv in mkv_files:
        try:
            if process_recording(mkv):
                success += 1
            else:
                fail += 1
        except Exception as exc:
            log(f"❌ Unexpected error on {mkv.name}: {exc}")
            import traceback
            traceback.print_exc()
            fail += 1

    log("=" * 60)
    log(f"Results: {success} succeeded, {fail} failed/skipped")
    log("Post-processing pipeline finished")
    log("=" * 60)


if __name__ == "__main__":
    main()
