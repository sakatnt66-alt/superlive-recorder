#!/usr/bin/env python3
"""
full_stream_contact_sheet.py — Full Stream Contact Sheet Generator

Creates ONE JPG contact sheet from the ORIGINAL complete recording,
distributing frames evenly across the full duration.

Design principles:
  - Completely independent: does NOT modify any existing project file
  - Reads from recordings/_preserved/ (where FilePreserverThread saves MKV)
  - Fails gracefully: never breaks recording, splitting, or video sending
  - Idempotent: uses state file to prevent duplicate creation/sending
  - No secrets in code: all credentials from environment variables

Dependencies: Python 3.10+ stdlib, ffmpeg, ffprobe (system).
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
from datetime import datetime, timezone
from pathlib import Path

# ═══════════════════════════════════════════════════════════
# CONFIGURATION (from environment — no hardcoded secrets)
# ═══════════════════════════════════════════════════════════
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "")
RECORDINGS_DIR     = Path(os.environ.get("RECORDINGS_DIR", "recordings"))
PRESERVED_DIR      = RECORDINGS_DIR / "_preserved"
STATE_DIR          = RECORDINGS_DIR / "_state"
STREAM_ID          = os.environ.get("STREAM_ID", "unknown")
STREAM_NAME        = os.environ.get("STREAM_NAME", "")

# ═══════════════════════════════════════════════════════════
# CONTACT SHEET CONFIGURATION (customizable via env vars)
# ═══════════════════════════════════════════════════════════
GRID_COLS      = int(os.environ.get("CS_COLS", "4"))
GRID_ROWS      = int(os.environ.get("CS_ROWS", "6"))
TOTAL_FRAMES   = GRID_COLS * GRID_ROWS
CELL_W         = int(os.environ.get("CS_CELL_W", "320"))
CELL_H         = int(os.environ.get("CS_CELL_H", "180"))
GAP            = int(os.environ.get("CS_GAP", "4"))
MARGIN         = int(os.environ.get("CS_MARGIN", "8"))
JPEG_QUALITY   = int(os.environ.get("CS_QUALITY", "2"))
FONT_SIZE      = int(os.environ.get("CS_FONT_SIZE", "16"))
BG_COLOR       = os.environ.get("CS_BG_COLOR", "black")
MAX_TG_PHOTO   = 10 * 1024 * 1024  # 10 MB Telegram photo limit


# ═══════════════════════════════════════════════════════════
# LOGGING
# ═══════════════════════════════════════════════════════════
def log(msg: str, level: str = "INFO") -> None:
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"[CONTACT_SHEET {ts}] [{level}] {msg}", flush=True)


# ═══════════════════════════════════════════════════════════
# STATE MANAGEMENT (prevent duplicates)
# ═══════════════════════════════════════════════════════════
def state_file() -> Path:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    safe_id = "".join(c if c.isalnum() or c in "-_" else "_" for c in str(STREAM_ID))
    return STATE_DIR / f"contact_sheet_{safe_id}.json"


def load_state() -> dict:
    p = state_file()
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"stream_id": STREAM_ID, "state": "NOT_STARTED"}


def save_state(st: dict) -> None:
    p = state_file()
    tmp = p.with_suffix(".tmp")
    st["updated_at"] = datetime.now(timezone.utc).isoformat()
    tmp.write_text(json.dumps(st, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(p)


# ═══════════════════════════════════════════════════════════
# FFPROBE / FFMPEG HELPERS
# ═══════════════════════════════════════════════════════════
def run_cmd(cmd: str, timeout: int = 300):
    try:
        return subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        log(f"Timeout ({timeout}s): {cmd[:100]}", "WARN")
        return None
    except Exception as e:
        log(f"Command error: {e}", "ERROR")
        return None


def get_duration(path: str) -> float:
    """Get video duration in seconds using ffprobe."""
    r = run_cmd(
        f'ffprobe -v quiet -print_format json -show_format "{path}"', 30
    )
    if r and r.returncode == 0:
        try:
            data = json.loads(r.stdout)
            return float(data.get("format", {}).get("duration", 0))
        except (json.JSONDecodeError, ValueError):
            pass
    return 0.0


def validate_source(path: str) -> bool:
    """Validate that the source file exists, is non-empty, and readable."""
    try:
        p = Path(path)
        if not p.exists():
            return False
        if p.stat().st_size == 0:
            return False
        # Quick ffprobe check
        info_dur = get_duration(path)
        return info_dur > 0
    except OSError:
        return False


def format_timestamp(seconds: float) -> str:
    """Format seconds as HH:MM:SS."""
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


# ═══════════════════════════════════════════════════════════
# FRAME EXTRACTION & CONTACT SHEET CREATION
# ═══════════════════════════════════════════════════════════
def calculate_timestamps(duration: float, count: int) -> list:
    """
    Calculate evenly distributed timestamps across the full duration.
    Avoids the very start (first 1s) and very end (last 1s).
    """
    if duration <= 0:
        return []
    if count <= 0:
        return []
    if count == 1:
        return [duration / 2]

    margin = min(1.0, duration * 0.01)
    usable = duration - 2 * margin
    if usable <= 0:
        usable = duration
        margin = 0

    timestamps = []
    for i in range(count):
        ts = margin + usable * i / (count - 1)
        ts = max(0, min(ts, duration - 0.1))
        timestamps.append(ts)
    return timestamps


def extract_frames(source: str, timestamps: list, tmpdir: str) -> list:
    """
    Extract frames at specified timestamps from the source video.
    Each frame gets a timestamp label overlaid.
    Returns list of (frame_path, timestamp) tuples.
    """
    frames = []
    for i, ts in enumerate(timestamps):
        out = os.path.join(tmpdir, f"frame_{i:03d}.jpg")
        label = format_timestamp(ts)

        cmd = (
            f'ffmpeg -y -ss {ts:.3f} -i "{source}" '
            f'-frames:v 1 -q:v {JPEG_QUALITY} '
            f'-vf "'
            f'scale={CELL_W}:{CELL_H}:force_original_aspect_ratio=decrease,'
            f'pad={CELL_W}:{CELL_H}:(ow-iw)/2:(oh-ih)/2:{BG_COLOR},'
            f'drawtext=text=\'{label}\':'
            f'fontsize={FONT_SIZE}:fontcolor=white:'
            f'borderw=1:bordercolor=black:'
            f'x=(w-text_w)/2:y=h-th-4'
            f'" '
            f'"{out}"'
        )
        r = run_cmd(cmd, timeout=30)
        if r and r.returncode == 0 and os.path.exists(out) and os.path.getsize(out) > 0:
            frames.append((out, ts))
        else:
            log(f"Frame {i + 1}/{len(timestamps)} at {label} failed", "WARN")

    return frames


def build_contact_sheet(frames: list, output_path: str) -> bool:
    """
    Combine extracted frames into a single grid contact sheet JPG.
    Uses ffmpeg concat + tile filter.
    """
    n = len(frames)
    if n == 0:
        return False

    # Build input args
    inputs = " ".join(f'-i "{p}"' for p, _ in frames)

    # Build filter: concat all frames then tile into grid
    concat_inputs = "".join(f"[{i}:v]" for i in range(n))
    filter_complex = (
        f"{concat_inputs}concat=n={n}:v=1:a=0,"
        f"tile={GRID_COLS}x{GRID_ROWS}:"
        f"padding={GAP}:margin={MARGIN}:"
        f"color={BG_COLOR}"
    )

    cmd = (
        f'ffmpeg -y {inputs} '
        f'-filter_complex "{filter_complex}" '
        f'-map "[out]" -q:v {JPEG_QUALITY} '
        f'"{output_path}"'
    )

    # Actually, tile output doesn't use [out] label with concat. Fix:
    filter_complex = (
        f"{concat_inputs}concat=n={n}:v=1:a=0,"
        f"tile={GRID_COLS}x{GRID_ROWS}:"
        f"padding={GAP}:margin={MARGIN}:"
        f"color={BG_COLOR}"
    )
    cmd = (
        f'ffmpeg -y {inputs} '
        f'-filter_complex "{filter_complex}" '
        f'-frames:v 1 -q:v {JPEG_QUALITY} '
        f'"{output_path}"'
    )

    r = run_cmd(cmd, timeout=120)
    if r and r.returncode == 0 and os.path.exists(output_path) and os.path.getsize(output_path) > 0:
        return True

    # Fallback: try without tile filter, using hstack/vstack approach
    log("Tile filter failed, trying hstack/vstack fallback...", "WARN")
    return build_contact_sheet_manual(frames, output_path)


def build_contact_sheet_manual(frames: list, output_path: str) -> bool:
    """Fallback: build grid using hstack/vstack filters."""
    n = len(frames)
    if n == 0:
        return False

    inputs = " ".join(f'-i "{p}"' for p, _ in frames)
    parts = []

    # Scale and pad each frame
    for i in range(n):
        parts.append(
            f"[{i}:v]scale={CELL_W}:{CELL_H}:"
            f"force_original_aspect_ratio=decrease,"
            f"pad={CELL_W}:{CELL_H}:(ow-iw)/2:(oh-ih)/2:{BG_COLOR},"
            f"setsar=1[v{i}]"
        )

    # Build rows
    rows = []
    for r in range(GRID_ROWS):
        cols = []
        for c in range(GRID_COLS):
            idx = r * GRID_COLS + c
            if idx < n:
                cols.append(f"[v{idx}]")
        if cols:
            if len(cols) > 1:
                rows.append(f"{''.join(cols)}hstack=inputs={len(cols)}[row{r}]")
            else:
                rows.append(f"{cols[0]}copy[row{r}]")

    if not rows:
        return False

    # Stack rows vertically
    if len(rows) > 1:
        row_refs = "".join(f"[row{r}]" for r in range(len(rows)))
        parts.append(f"{row_refs}vstack=inputs={len(rows)}[out]")
    else:
        parts.append("[row0]copy[out]")

    fc = ";".join(parts)
    cmd = (
        f'ffmpeg -y {inputs} '
        f'-filter_complex "{fc}" '
        f'-map "[out]" -q:v {JPEG_QUALITY} '
        f'"{output_path}"'
    )
    r = run_cmd(cmd, timeout=120)
    return (
        r is not None
        and r.returncode == 0
        and os.path.exists(output_path)
        and os.path.getsize(output_path) > 0
    )


# ═══════════════════════════════════════════════════════════
# TELEGRAM SEND (photo)
# ═══════════════════════════════════════════════════════════
def send_photo(photo_path: str, caption: str) -> dict:
    """Send photo to Telegram. Returns {ok, message_id, error}."""
    result = {"ok": False, "message_id": None, "error": ""}

    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        result["error"] = "Telegram credentials not configured"
        return result

    fsize = os.path.getsize(photo_path)
    if fsize > MAX_TG_PHOTO:
        result["error"] = f"Photo too large: {fsize / 1024 / 1024:.1f} MB > 10 MB"
        return result

    boundary = uuid.uuid4().hex
    fname = os.path.basename(photo_path)

    with open(photo_path, "rb") as f:
        pdata = f.read()

    body = b""
    body += f"--{boundary}\r\nContent-Disposition: form-data; name=\"chat_id\"\r\n\r\n{TELEGRAM_CHAT_ID}\r\n".encode()
    body += f"--{boundary}\r\nContent-Disposition: form-data; name=\"caption\"\r\n\r\n".encode()
    body += caption.encode("utf-8") + b"\r\n"
    body += f"--{boundary}\r\nContent-Disposition: form-data; name=\"parse_mode\"\r\n\r\nHTML\r\n".encode()
    body += f"--{boundary}\r\nContent-Disposition: form-data; name=\"photo\"; filename=\"{fname}\"\r\nContent-Type: image/jpeg\r\n\r\n".encode()
    body += pdata
    body += f"\r\n--{boundary}--\r\n".encode()

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"

    for attempt in range(1, 4):
        try:
            req = urllib.request.Request(url, data=body, headers={
                "Content-Type": f"multipart/form-data; boundary={boundary}"
            })
            with urllib.request.urlopen(req, timeout=120) as resp:
                if resp.status == 200:
                    data = json.loads(resp.read().decode("utf-8"))
                    result["ok"] = True
                    result["message_id"] = data.get("result", {}).get("message_id")
                    return result
        except urllib.error.HTTPError as e:
            if e.code == 403:
                result["error"] = "HTTP 403 — NOT retrying"
                return result
            result["error"] = f"HTTP {e.code}"
            log(f"sendPhoto attempt {attempt}/3: HTTP {e.code}", "WARN")
        except Exception as e:
            result["error"] = str(e)
            log(f"sendPhoto attempt {attempt}/3: {e}", "WARN")
        if attempt < 3:
            time.sleep(2 * attempt)

    return result


# ═══════════════════════════════════════════════════════════
# MAIN PIPELINE
# ═══════════════════════════════════════════════════════════
def find_source_mkv() -> Path | None:
    """Find the preserved MKV source file."""
    search_dirs = [PRESERVED_DIR, RECORDINGS_DIR, RECORDINGS_DIR / "_temp"]
    for d in search_dirs:
        if d.exists():
            mkvs = sorted(d.glob("*.mkv"), key=lambda f: f.stat().st_size, reverse=True)
            if mkvs:
                return mkvs[0]
    return None


def main():
    log("=" * 60)
    log("Full Stream Contact Sheet Generator")
    log(f"  STREAM_ID   : {STREAM_ID}")
    log(f"  STREAM_NAME : {STREAM_NAME or '(not set)'}")
    log(f"  GRID        : {GRID_COLS}x{GRID_ROWS} = {TOTAL_FRAMES} frames")
    log(f"  CELL SIZE   : {CELL_W}x{CELL_H}")
    log("=" * 60)

    # ── Check state for idempotency ──
    st = load_state()
    if st.get("state") == "SENT":
        log("⏭️ Contact sheet already sent — skipping")
        return

    # ── Find source MKV ──
    source = find_source_mkv()
    if source is None:
        log("✗ No MKV source found in any directory", "WARN")
        log("  This is expected if record_once.py deleted the file")
        log("  and FilePreserverThread did not preserve it.")
        st["state"] = "NO_SOURCE"
        save_state(st)
        return

    log(f"📂 Source: {source.name} ({source.stat().st_size / 1024 / 1024:.1f} MB)")

    # ── Validate source ──
    if not validate_source(str(source)):
        log("✗ Source file is invalid or unreadable", "ERROR")
        st["state"] = "INVALID_SOURCE"
        save_state(st)
        return

    # ── Check if image already exists (retry send only) ──
    safe_id = "".join(c if c.isalnum() or c in "-_" else "_" for c in str(STREAM_ID))
    output_path = str(PRESERVED_DIR / f"full_stream_contact_sheet_{safe_id}.jpg")

    if st.get("state") == "CREATED" and os.path.exists(output_path):
        log("📷 Contact sheet exists — retrying send only")
    else:
        # ── Get duration ──
        duration = get_duration(str(source))
        if duration <= 0:
            log("✗ Cannot determine duration", "ERROR")
            st["state"] = "FAILED"
            st["error"] = "duration_read_failed"
            save_state(st)
            return

        log(f"⏱️  Duration: {format_timestamp(duration)} ({duration:.0f}s)")

        # ── Calculate timestamps ──
        timestamps = calculate_timestamps(duration, TOTAL_FRAMES)
        if not timestamps:
            log("✗ No valid timestamps calculated", "ERROR")
            st["state"] = "FAILED"
            save_state(st)
            return

        log(f"📐 Timestamps: {len(timestamps)} frames across {format_timestamp(duration)}")

        # ── Extract frames and build contact sheet ──
        st["state"] = "CREATING"
        save_state(st)

        with tempfile.TemporaryDirectory(prefix="cs_frames_") as tmpdir:
            frames = extract_frames(str(source), timestamps, tmpdir)
            log(f"🖼️  Extracted {len(frames)}/{TOTAL_FRAMES} frames")

            if len(frames) < 4:
                log("✗ Not enough frames for contact sheet", "ERROR")
                st["state"] = "FAILED"
                st["error"] = f"only_{len(frames)}_frames"
                save_state(st)
                return

            if build_contact_sheet(frames, output_path):
                log(f"✅ Contact sheet created: {os.path.basename(output_path)}")
                log(f"   Size: {os.path.getsize(output_path) / 1024:.0f} KB")
            else:
                log("✗ Failed to build contact sheet", "ERROR")
                st["state"] = "FAILED"
                st["error"] = "grid_build_failed"
                save_state(st)
                return

        st["state"] = "CREATED"
        st["source_file"] = str(source)
        st["output_file"] = output_path
        st["duration"] = round(duration, 1)
        st["frames_used"] = len(frames)
        st["created_at"] = datetime.now(timezone.utc).isoformat()
        save_state(st)

    # ── Send to Telegram ──
    if not os.path.exists(output_path):
        log("✗ Output file missing at send time", "ERROR")
        st["state"] = "FAILED"
        save_state(st)
        return

    duration = st.get("duration", 0)
    dur_str = format_timestamp(duration) if duration else "غير معروف"

    caption = (
        f"🖼️ <b>معاينة البث الكامل</b>\n"
        f"👤 {STREAM_NAME or STREAM_ID}\n"
        f"🆔 <code>{STREAM_ID}</code>\n"
        f"⏱️ المدة: {dur_str}\n"
        f"📐 {GRID_COLS}×{GRID_ROWS} = {TOTAL_FRAMES} لقطة"
    )

    tg_res = send_photo(output_path, caption)
    if tg_res["ok"]:
        log(f"📤 Contact sheet sent ✅ (message_id={tg_res['message_id']})")
        st["state"] = "SENT"
        st["sent_at"] = datetime.now(timezone.utc).isoformat()
        st["message_id"] = tg_res["message_id"]
    else:
        log(f"⚠️ Send failed: {tg_res['error']}", "WARN")
        st["state"] = "CREATED"  # Keep as CREATED for retry
        st["last_send_error"] = tg_res["error"]

    save_state(st)
    log("Contact sheet pipeline finished")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log(f"❌ Fatal error: {e}", "ERROR")
        import traceback
        traceback.print_exc()
        # Exit with 0 to not break the workflow
        # Contact sheet is optional — must not fail the recording
        sys.exit(0)
