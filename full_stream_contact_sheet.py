#!/usr/bin/env python3
"""
full_stream_contact_sheet.py — Full Stream Contact Sheet Generator (Portrait)

Creates ONE portrait JPG with exactly 12 frames from the ORIGINAL recording,
distributed evenly across the full duration. Grid: 2 columns x 6 rows.

Independent module: does NOT modify record_once.py or any recording logic.
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
# CONTACT SHEET CONFIGURATION — PORTRAIT 2x6 = 12 frames
# ═══════════════════════════════════════════════════════════
GRID_COLS      = int(os.environ.get("CS_COLS", "2"))
GRID_ROWS      = int(os.environ.get("CS_ROWS", "6"))
TOTAL_FRAMES   = GRID_COLS * GRID_ROWS
CELL_W         = int(os.environ.get("CS_CELL_W", "320"))
CELL_H         = int(os.environ.get("CS_CELL_H", "180"))
GAP            = int(os.environ.get("CS_GAP", "4"))
MARGIN         = int(os.environ.get("CS_MARGIN", "8"))
JPEG_QUALITY   = int(os.environ.get("CS_QUALITY", "85"))
FONT_SIZE      = int(os.environ.get("CS_FONT_SIZE", "16"))
BG_COLOR       = os.environ.get("CS_BG_COLOR", "black")
MAX_TG_PHOTO   = 10 * 1024 * 1024


def log(msg: str, level: str = "INFO") -> None:
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"[CONTACT_SHEET {ts}] [{level}] {msg}", flush=True)


# ═══════════════════════════════════════════════════════════
# STATE MANAGEMENT (prevent duplicate creation/sending)
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
    r = run_cmd(f'ffprobe -v quiet -print_format json -show_format "{path}"', 30)
    if r and r.returncode == 0:
        try:
            data = json.loads(r.stdout)
            return float(data.get("format", {}).get("duration", 0))
        except (json.JSONDecodeError, ValueError):
            pass
    return 0.0


def validate_source(path: str) -> bool:
    try:
        p = Path(path)
        if not p.exists():
            return False
        if p.stat().st_size == 0:
            return False
        dur = get_duration(path)
        if dur <= 0:
            return False
        r = run_cmd(f'ffprobe -v quiet -select_streams v:0 -show_entries stream=codec_type -of csv=p=0 "{path}"', 30)
        if r and r.returncode == 0 and 'video' in r.stdout:
            return True
        return False
    except OSError:
        return False


def format_timestamp(seconds: float) -> str:
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


# ═══════════════════════════════════════════════════════════
# FILE DISCOVERY — only original _av.mkv, exclude parts/encoded
# ═══════════════════════════════════════════════════════════
def find_source_mkv() -> Path | None:
    """Find the ORIGINAL complete MKV, excluding _part_ and _encoded files."""
    search_dirs = [PRESERVED_DIR, RECORDINGS_DIR, RECORDINGS_DIR / "_temp"]
    candidates = []

    for d in search_dirs:
        if not d.exists():
            continue
        for f in d.glob("*.mkv"):
            name = f.name
            if "_part_" in name:
                continue
            if "_encoded" in name:
                continue
            if "contact_sheet" in name:
                continue
            try:
                if f.stat().st_size > 0:
                    candidates.append(f)
            except OSError:
                continue

    if not candidates:
        return None

    candidates.sort(key=lambda f: f.stat().st_size, reverse=True)
    return candidates[0]


# ═══════════════════════════════════════════════════════════
# FRAME EXTRACTION & TIMESTAMP CALCULATION
# ═══════════════════════════════════════════════════════════
def calculate_timestamps(duration: float, count: int) -> list:
    if duration <= 0 or count <= 0:
        return []
    if count == 1:
        return [duration / 2]
    margin = min(1.0, duration * 0.01)
    usable = max(duration - 2 * margin, 0.1)
    timestamps = []
    for i in range(count):
        ts = margin + usable * i / (count - 1)
        ts = max(0, min(ts, duration - 0.1))
        timestamps.append(ts)
    return timestamps


def extract_frames(source: str, timestamps: list, tmpdir: str) -> list:
    """Extract frames using simple FFmpeg (no drawtext, no complex filters)."""
    frames = []
    for i, ts in enumerate(timestamps):
        out = os.path.join(tmpdir, f"frame_{i:03d}.jpg")
        label = format_timestamp(ts)
        cmd = (
            f'ffmpeg -y -ss {ts:.3f} -i "{source}" '
            f'-frames:v 1 -q:v 2 '
            f'"{out}"'
        )
        r = run_cmd(cmd, timeout=30)
        if r and r.returncode == 0 and os.path.exists(out) and os.path.getsize(out) > 0:
            frames.append((out, ts, label))
        else:
            stderr_snippet = (r.stderr or "")[:300] if r else "timeout/no response"
            rc = r.returncode if r else "N/A"
            log(f"Frame {i+1}/{len(timestamps)} at {label} FAILED (rc={rc}): {stderr_snippet}", "ERROR")
    return frames


# ═══════════════════════════════════════════════════════════
# PORTRAIT CONTACT SHEET BUILDING (Pillow)
# ═══════════════════════════════════════════════════════════
def build_contact_sheet(frames: list, output_path: str) -> bool:
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        log("Pillow is NOT installed. Cannot build contact sheet.", "ERROR")
        return False

    n = len(frames)
    if n == 0:
        return False

    n = min(n, GRID_COLS * GRID_ROWS)
    frames = frames[:n]

    text_h = FONT_SIZE + 8
    img_w = MARGIN * 2 + GRID_COLS * CELL_W + (GRID_COLS - 1) * GAP
    img_h = MARGIN * 2 + GRID_ROWS * (CELL_H + text_h) + (GRID_ROWS - 1) * GAP

    # ── PORTRAIT VALIDATION ──
    if img_h <= img_w:
        log(f"Canvas {img_w}x{img_h} is NOT portrait. Aborting.", "ERROR")
        return False

    grid_img = Image.new('RGB', (img_w, img_h), color=BG_COLOR)
    draw = ImageDraw.Draw(grid_img)

    font = None
    font_paths = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/freefont/FreeSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        "/usr/share/fonts/TTF/DejaVuSans.ttf",
    ]
    for fp in font_paths:
        try:
            font = ImageFont.truetype(fp, FONT_SIZE)
            break
        except (IOError, OSError):
            continue
    if font is None:
        try:
            font = ImageFont.load_default()
        except Exception:
            font = None

    for i, (frame_path, ts, label) in enumerate(frames):
        row = i // GRID_COLS
        col = i % GRID_COLS
        x = MARGIN + col * (CELL_W + GAP)
        y = MARGIN + row * (CELL_H + text_h + GAP)

        try:
            with Image.open(frame_path) as img:
                if img.mode != 'RGB':
                    img = img.convert('RGB')
                img.thumbnail((CELL_W, CELL_H), Image.LANCZOS)
                offset_x = x + (CELL_W - img.width) // 2
                offset_y = y + (CELL_H - img.height) // 2
                grid_img.paste(img, (offset_x, offset_y))
        except Exception as e:
            log(f"Failed to open frame {frame_path}: {e}", "WARN")
            continue

        if font:
            try:
                text_bbox = draw.textbbox((0, 0), label, font=font)
                text_w = text_bbox[2] - text_bbox[0]
            except AttributeError:
                text_w = len(label) * FONT_SIZE // 2
            text_x = x + (CELL_W - text_w) // 2
            text_y = y + CELL_H + 2
            draw.text((text_x, text_y), label, fill="white", font=font)

    grid_img.save(output_path, "JPEG", quality=JPEG_QUALITY)
    if os.path.exists(output_path) and os.path.getsize(output_path) > 0:
        with Image.open(output_path) as final_img:
            fw, fh = final_img.size
            log(f"Final image: {fw}x{fh} (portrait={fh > fw})")
            return fh > fw
    return False


# ═══════════════════════════════════════════════════════════
# TELEGRAM SEND
# ═══════════════════════════════════════════════════════════
def send_photo(photo_path: str, caption: str) -> dict:
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
def main():
    log("=" * 60)
    log("Full Stream Contact Sheet Generator (PORTRAIT)")
    log(f"  STREAM_ID   : {STREAM_ID}")
    log(f"  STREAM_NAME : {STREAM_NAME or '(not set)'}")
    log(f"  GRID        : {GRID_COLS}x{GRID_ROWS} = {TOTAL_FRAMES} frames")
    log(f"  CELL SIZE   : {CELL_W}x{CELL_H}")
    log("=" * 60)

    st = load_state()
    if st.get("state") == "SENT":
        log("⏭️  Contact sheet already sent — skipping (idempotent)")
        return

    source = find_source_mkv()
    if source is None:
        log("✗ No valid original MKV source found", "WARN")
        st["state"] = "NO_SOURCE"
        st["error"] = "no_source_mkv_found"
        save_state(st)
        return

    log(f"📂 Source: {source.name} ({source.stat().st_size / 1024 / 1024:.1f} MB)")

    if not validate_source(str(source)):
        log("✗ Source file failed validation", "ERROR")
        st["state"] = "INVALID_SOURCE"
        save_state(st)
        return

    safe_id = "".join(c if c.isalnum() or c in "-_" else "_" for c in str(STREAM_ID))
    PRESERVED_DIR.mkdir(parents=True, exist_ok=True)
    output_path = str(PRESERVED_DIR / f"full_stream_contact_sheet_{safe_id}.jpg")

    if st.get("state") == "CREATED" and os.path.exists(output_path) and os.path.getsize(output_path) > 0:
        log("📷 Contact sheet exists from previous run — retrying send only")
        duration = st.get("duration", 0)
    else:
        duration = get_duration(str(source))
        if duration <= 0:
            log("✗ Cannot determine video duration", "ERROR")
            st["state"] = "FAILED"
            st["error"] = "duration_read_failed"
            save_state(st)
            return

        log(f"⏱️  Duration: {format_timestamp(duration)} ({duration:.0f}s)")
        timestamps = calculate_timestamps(duration, TOTAL_FRAMES)
        if not timestamps:
            log("✗ No valid timestamps calculated", "ERROR")
            st["state"] = "FAILED"
            save_state(st)
            return

        log(f"📐 Timestamps: {len(timestamps)} frames across {format_timestamp(duration)}")
        st["state"] = "CREATING"
        st["source_file"] = str(source)
        save_state(st)

        with tempfile.TemporaryDirectory(prefix="cs_frames_") as tmpdir:
            frames = extract_frames(str(source), timestamps, tmpdir)
            log(f"🖼️  Extracted {len(frames)}/{TOTAL_FRAMES} frames")

            if len(frames) < 4:
                log(f"✗ Not enough frames extracted ({len(frames)}/4 minimum)", "ERROR")
                st["state"] = "FAILED"
                st["error"] = f"only_{len(frames)}_frames_extracted"
                save_state(st)
                return

            if build_contact_sheet(frames, output_path):
                fsize = os.path.getsize(output_path)
                log(f"✅ Portrait contact sheet created: {os.path.basename(output_path)} ({fsize / 1024:.0f} KB)")
            else:
                log("✗ Failed to build portrait contact sheet", "ERROR")
                st["state"] = "FAILED"
                st["error"] = "grid_build_failed"
                save_state(st)
                return

        st["state"] = "CREATED"
        st["duration"] = round(duration, 1)
        st["frames_extracted"] = len(frames)
        st["output_file"] = output_path
        st["created_at"] = datetime.now(timezone.utc).isoformat()
        save_state(st)

    if not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
        log("✗ Output file missing or empty at send time", "ERROR")
        st["state"] = "FAILED"
        save_state(st)
        return

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
        st["state"] = "CREATED"
        st["last_send_error"] = tg_res["error"]

    save_state(st)
    log("Contact sheet pipeline finished")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log(f"❌ Fatal error (will not break workflow): {e}", "ERROR")
        import traceback
        traceback.print_exc()
        sys.exit(0)
