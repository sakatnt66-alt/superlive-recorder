#!/usr/bin/env python3
"""
full_stream_contact_sheet.py — Contact Sheet from Per-part sources.

Creates ONE portrait JPG with 12 frames from the FULL recording.
Extracts frames directly from individual parts (no massive concat needed).

Grid: 3 columns x 4 rows = 12 frames.
Design: UNCHANGED from previous version.
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
PRESERVED_DIR      = RECORDINGS_DIR / "_preserved"
TEMP_DIR           = RECORDINGS_DIR / "_temp"
STATE_DIR          = RECORDINGS_DIR / "_state"
STREAM_ID          = os.environ.get("STREAM_ID", "unknown")
STREAM_NAME        = os.environ.get("STREAM_NAME", "")

GRID_COLS = int(os.environ.get("CS_COLS", "3"))
GRID_ROWS = int(os.environ.get("CS_ROWS", "4"))
if GRID_COLS * GRID_ROWS != 12:
    GRID_COLS, GRID_ROWS = 3, 4
TOTAL_FRAMES = GRID_COLS * GRID_ROWS

CELL_W       = int(os.environ.get("CS_CELL_W", "360"))
CELL_H       = int(os.environ.get("CS_CELL_H", "640"))
GAP          = int(os.environ.get("CS_GAP", "1"))
MARGIN       = int(os.environ.get("CS_MARGIN", "5"))
JPEG_QUALITY = int(os.environ.get("CS_QUALITY", "92"))
FONT_SIZE    = int(os.environ.get("CS_FONT_SIZE", "28"))
BG_COLOR     = os.environ.get("CS_BG_COLOR", "black")
MAX_TG_PHOTO = 10 * 1024 * 1024
TEXT_H       = FONT_SIZE + 8

MIN_PART_SIZE = 100 * 1024

def log(msg: str, level: str = "INFO") -> None:
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"[CONTACT_SHEET {ts}] [{level}] {msg}", flush=True)

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

def run_cmd(cmd: str, timeout: int = 300):
    try:
        return subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return None
    except Exception:
        return None

def get_duration(path: str) -> float:
    r = run_cmd(f'ffprobe -v quiet -print_format json -show_format "{path}"', 30)
    if r and r.returncode == 0:
        try:
            return float(json.loads(r.stdout).get("format", {}).get("duration", 0))
        except Exception:
            pass
    return 0.0

def format_timestamp(seconds: float) -> str:
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    if h > 0:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"

# ═══════════════════════════════════════════════════════════
# PART DISCOVERY — same logic as postprocess_recording.py
# ═══════════════════════════════════════════════════════════
def discover_parts_with_durations() -> list:
    """
    Find all parts and their durations.
    Returns list of (path, part_num, duration, start_offset) sorted by part_num.
    """
    parts = []
    search_dirs = [PRESERVED_DIR, RECORDINGS_DIR, TEMP_DIR]
    
    for d in search_dirs:
        if not d.exists():
            continue
        for pattern in ["*_av_part_*.mkv", "*_av_part_*.webm", "*_part_*.mkv", "*_part_*.webm"]:
            for f in d.glob(pattern):
                try:
                    size = f.stat().st_size
                    if size < MIN_PART_SIZE:
                        continue
                    m = re.search(r'_part_(\d+)', f.name)
                    part_num = int(m.group(1)) if m else 0
                    dur = get_duration(str(f))
                    if dur > 0:
                        parts.append((str(f), part_num, dur))
                except OSError:
                    continue
    
    if not parts:
        return []
    
    # Sort by part number
    parts.sort(key=lambda x: x[1])
    
    # Calculate start offsets
    result = []
    cumulative = 0.0
    for path, part_num, dur in parts:
        result.append((path, part_num, dur, cumulative))
        cumulative += dur
    
    return result

def find_single_source() -> str:
    """Find a single complete MKV source."""
    search_dirs = [PRESERVED_DIR, RECORDINGS_DIR, TEMP_DIR]
    candidates = []
    for d in search_dirs:
        if not d.exists():
            continue
        for f in d.glob("*.mkv"):
            name = f.name
            if "_part_" in name or "_encoded" in name or "concatenated_" in name:
                continue
            try:
                size = f.stat().st_size
                if size >= 1024 * 1024:
                    candidates.append((f, size))
            except OSError:
                continue
    if not candidates:
        return None
    candidates.sort(key=lambda x: x[1], reverse=True)
    for f, size in candidates:
        dur = get_duration(str(f))
        if dur >= 5.0:
            return str(f)
    return None

# ═══════════════════════════════════════════════════════════
# TIMESTAMP CALCULATION
# ═══════════════════════════════════════════════════════════
def calculate_timestamps(duration: float, count: int) -> list:
    if duration <= 0 or count <= 0:
        return []
    if duration < 12:
        return [duration * (i + 1) / (count + 1) for i in range(count)]
    margin = min(2.0, duration * 0.02)
    usable = max(duration - 2 * margin, 0.1)
    timestamps = []
    for i in range(count):
        ts = margin + usable * i / (count - 1)
        ts = max(0, min(ts, duration - 0.1))
        timestamps.append(ts)
    return timestamps

# ═══════════════════════════════════════════════════════════
# FRAME EXTRACTION — from individual parts (no concat needed)
# ═══════════════════════════════════════════════════════════
def get_frame_hash(path: str) -> str:
    try:
        with open(path, 'rb') as f:
            data = f.read()
        return hashlib.md5(data).hexdigest()
    except Exception:
        return ""

def extract_frame_from_part(part_path: str, offset_in_part: float, out_path: str) -> bool:
    """Extract a single frame from a part at the given offset."""
    # Strategy 1: Fast seek
    cmd = f'ffmpeg -y -ss {offset_in_part:.3f} -i "{part_path}" -frames:v 1 -q:v 2 "{out_path}"'
    r = run_cmd(cmd, timeout=30)
    if r and r.returncode == 0 and os.path.exists(out_path) and os.path.getsize(out_path) > 0:
        return True
    
    # Strategy 2: Accurate seek
    cmd2 = f'ffmpeg -y -i "{part_path}" -ss {offset_in_part:.3f} -frames:v 1 -q:v 2 "{out_path}"'
    r2 = run_cmd(cmd2, timeout=60)
    if r2 and r2.returncode == 0 and os.path.exists(out_path) and os.path.getsize(out_path) > 0:
        return True
    
    return False

def extract_frames_from_parts(parts: list, timestamps: list, tmpdir: str) -> list:
    """
    Extract frames from individual parts.
    Each timestamp is mapped to the correct part based on cumulative offsets.
    """
    frames = []
    frame_hashes = set()
    total_duration = sum(dur for _, _, dur, _ in parts)
    
    for i, ts in enumerate(timestamps):
        out = os.path.join(tmpdir, f"frame_{i:03d}.jpg")
        label = format_timestamp(ts)
        
        # Find which part contains this timestamp
        target_part = None
        offset_in_part = 0.0
        
        for part_path, part_num, part_dur, start_offset in parts:
            if start_offset <= ts < start_offset + part_dur:
                target_part = part_path
                offset_in_part = ts - start_offset
                break
        
        if target_part is None:
            # Timestamp beyond last part, use last part
            if parts:
                target_part = parts[-1][0]
                offset_in_part = max(0, parts[-1][2] - 1.0)
            else:
                log(f"Frame {i+1}/{len(timestamps)} at {label}: no part available", "ERROR")
                continue
        
        # Try to extract
        success = extract_frame_from_part(target_part, offset_in_part, out)
        
        if success:
            frame_hash = get_frame_hash(out)
            if frame_hash and frame_hash not in frame_hashes:
                frame_hashes.add(frame_hash)
                frames.append((out, ts, label))
                continue
            else:
                log(f"Frame {i+1} at {label}: duplicate detected, trying nearby", "WARN")
        
        # Fallback: try nearby offsets within the same part
        found = False
        for offset_delta in [-3.0, -1.0, 1.0, 3.0, -5.0, 5.0]:
            nearby_offset = max(0.1, min(offset_in_part + offset_delta, parts[-1][2] - 0.5))
            if extract_frame_from_part(target_part, nearby_offset, out):
                frame_hash = get_frame_hash(out)
                if frame_hash and frame_hash not in frame_hashes:
                    frame_hashes.add(frame_hash)
                    frames.append((out, ts, label))
                    log(f"Frame {i+1}/{len(timestamps)} at {label}: recovered from nearby offset", "WARN")
                    found = True
                    break
        
        if not found:
            log(f"Frame {i+1}/{len(timestamps)} at {label}: FAILED all attempts", "ERROR")
    
    return frames

def extract_frames_from_single(source: str, timestamps: list, tmpdir: str) -> list:
    """Extract frames from a single source file."""
    frames = []
    frame_hashes = set()
    source_duration = get_duration(source)
    
    for i, ts in enumerate(timestamps):
        out = os.path.join(tmpdir, f"frame_{i:03d}.jpg")
        label = format_timestamp(ts)
        
        cmd = f'ffmpeg -y -ss {ts:.3f} -i "{source}" -frames:v 1 -q:v 2 "{out}"'
        r = run_cmd(cmd, timeout=30)
        if r and r.returncode == 0 and os.path.exists(out) and os.path.getsize(out) > 0:
            frame_hash = get_frame_hash(out)
            if frame_hash and frame_hash not in frame_hashes:
                frame_hashes.add(frame_hash)
                frames.append((out, ts, label))
                continue
        
        cmd2 = f'ffmpeg -y -i "{source}" -ss {ts:.3f} -frames:v 1 -q:v 2 "{out}"'
        r2 = run_cmd(cmd2, timeout=60)
        if r2 and r2.returncode == 0 and os.path.exists(out) and os.path.getsize(out) > 0:
            frame_hash = get_frame_hash(out)
            if frame_hash and frame_hash not in frame_hashes:
                frame_hashes.add(frame_hash)
                frames.append((out, ts, label))
                continue
        
        # Nearby fallback
        found = False
        for offset in [-5.0, -3.0, -1.0, 1.0, 3.0, 5.0]:
            nearby_ts = max(0.1, min(ts + offset, source_duration - 0.5))
            cmd3 = f'ffmpeg -y -ss {nearby_ts:.3f} -i "{source}" -frames:v 1 -q:v 2 "{out}"'
            r3 = run_cmd(cmd3, timeout=30)
            if r3 and r3.returncode == 0 and os.path.exists(out) and os.path.getsize(out) > 0:
                frame_hash = get_frame_hash(out)
                if frame_hash and frame_hash not in frame_hashes:
                    frame_hashes.add(frame_hash)
                    frames.append((out, nearby_ts, label))
                    found = True
                    break
        
        if not found:
            log(f"Frame {i+1}/{len(timestamps)} at {label}: FAILED", "ERROR")
    
    return frames

# ═══════════════════════════════════════════════════════════
# GRID BUILDING — Design UNCHANGED
# ═══════════════════════════════════════════════════════════
def build_contact_sheet(frames: list, output_path: str) -> bool:
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        log("Pillow is NOT installed.", "ERROR")
        return False

    n = min(len(frames), TOTAL_FRAMES)
    frames = frames[:n]
    if n == 0:
        return False

    img_w = MARGIN * 2 + GRID_COLS * CELL_W + (GRID_COLS - 1) * GAP
    img_h = MARGIN * 2 + GRID_ROWS * (CELL_H + TEXT_H) + (GRID_ROWS - 1) * GAP

    grid_img = Image.new('RGB', (img_w, img_h), color=BG_COLOR)
    draw = ImageDraw.Draw(grid_img)

    font = None
    for fp in ["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
               "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
               "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
               "/usr/share/fonts/truetype/freefont/FreeSans.ttf"]:
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
        row, col = divmod(i, GRID_COLS)
        x = MARGIN + col * (CELL_W + GAP)
        y = MARGIN + row * (CELL_H + TEXT_H + GAP)
        try:
            with Image.open(frame_path) as img:
                if img.mode != 'RGB':
                    img = img.convert('RGB')
                target_ratio = CELL_W / CELL_H
                img_ratio = img.width / img.height
                if img_ratio > target_ratio:
                    new_width = int(img.height * target_ratio)
                    left = (img.width - new_width) // 2
                    img = img.crop((left, 0, left + new_width, img.height))
                elif img_ratio < target_ratio:
                    new_height = int(img.width / target_ratio)
                    top = (img.height - new_height) // 2
                    img = img.crop((0, top, img.width, top + new_height))
                img = img.resize((CELL_W, CELL_H), Image.LANCZOS)
                grid_img.paste(img, (x, y))
        except Exception as e:
            log(f"Failed to paste frame {i}: {e}", "WARN")
            continue
        if font:
            try:
                bbox = draw.textbbox((0, 0), label, font=font)
                tw = bbox[2] - bbox[0]
            except AttributeError:
                tw = len(label) * (FONT_SIZE // 2)
            text_x = x + (CELL_W - tw) // 2
            text_y = y + CELL_H + 3
            draw.text((text_x + 2, text_y + 2), label, fill="black", font=font)
            draw.text((text_x, text_y), label, fill="white", font=font)

    try:
        grid_img.save(output_path, "JPEG", quality=JPEG_QUALITY)
        return os.path.exists(output_path) and os.path.getsize(output_path) > 0
    except Exception as e:
        log(f"Save failed: {e}", "ERROR")
        return False

# ═══════════════════════════════════════════════════════════
# TELEGRAM SEND
# ═══════════════════════════════════════════════════════════
def send_photo(photo_path: str, caption: str) -> dict:
    result = {"ok": False, "message_id": None, "error": ""}
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        result["error"] = "Telegram credentials not configured"
        return result
    if os.path.getsize(photo_path) > MAX_TG_PHOTO:
        result["error"] = "Photo too large"
        return result

    boundary = uuid.uuid4().hex
    with open(photo_path, "rb") as f:
        pdata = f.read()

    body = b""
    body += f"--{boundary}\r\nContent-Disposition: form-data; name=\"chat_id\"\r\n\r\n{TELEGRAM_CHAT_ID}\r\n".encode()
    body += f"--{boundary}\r\nContent-Disposition: form-data; name=\"caption\"\r\n\r\n".encode()
    body += caption.encode("utf-8") + b"\r\n"
    body += f"--{boundary}\r\nContent-Disposition: form-data; name=\"parse_mode\"\r\n\r\nHTML\r\n".encode()
    body += f"--{boundary}\r\nContent-Disposition: form-data; name=\"photo\"; filename=\"contact_sheet.jpg\"\r\nContent-Type: image/jpeg\r\n\r\n".encode()
    body += pdata
    body += f"\r\n--{boundary}--\r\n".encode()

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
    for attempt in range(1, 4):
        try:
            req = urllib.request.Request(url, data=body, headers={
                "Content-Type": f"multipart/form-data; boundary={boundary}"})
            with urllib.request.urlopen(req, timeout=120) as resp:
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
        if attempt < 3:
            time.sleep(2 * attempt)
    return result

# ═══════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════
def main():
    log("=" * 60)
    log("Full Stream Contact Sheet Generator (PORTRAIT 3x4)")
    log(f"  STREAM_ID   : {STREAM_ID}")
    log(f"  GRID        : {GRID_COLS}x{GRID_ROWS} = {TOTAL_FRAMES} frames")
    log(f"  CELL SIZE   : {CELL_W}x{CELL_H}")
    log("=" * 60)

    st = load_state()
    if st.get("state") == "SENT":
        log("⏭️  Already sent — skipping")
        return

    # Try per-part extraction first
    parts = discover_parts_with_durations()
    single_source = None
    
    if parts and len(parts) > 0:
        total_duration = sum(dur for _, _, dur, _ in parts)
        log(f"📦 Found {len(parts)} parts, total duration: {format_timestamp(total_duration)}")
        
        if total_duration < 5.0:
            log("✗ Total duration too short", "ERROR")
            st["state"] = "INVALID_SOURCE"
            save_state(st)
            return
    else:
        single_source = find_single_source()
        if single_source:
            total_duration = get_duration(single_source)
            log(f"📂 Single source: {os.path.basename(single_source)} ({total_duration:.1f}s)")
        else:
            log("✗ No valid source found", "ERROR")
            st["state"] = "NO_SOURCE"
            save_state(st)
            return

    safe_id = "".join(c if c.isalnum() or c in "-_" else "_" for c in str(STREAM_ID))
    PRESERVED_DIR.mkdir(parents=True, exist_ok=True)
    output_path = str(PRESERVED_DIR / f"full_stream_contact_sheet_{safe_id}.jpg")

    if st.get("state") == "CREATED" and os.path.exists(output_path) and os.path.getsize(output_path) > 0:
        log("📷 Contact sheet exists — retrying send only")
        duration = st.get("duration", 0)
    else:
        duration = total_duration
        log(f"⏱️  Duration: {format_timestamp(duration)} ({duration:.0f}s)")
        timestamps = calculate_timestamps(duration, TOTAL_FRAMES)
        if not timestamps:
            st["state"] = "FAILED"
            save_state(st)
            return

        log(f"📐 Timestamps: {len(timestamps)} frames across {format_timestamp(duration)}")
        st["state"] = "CREATING"
        save_state(st)

        with tempfile.TemporaryDirectory(prefix="cs_frames_") as tmpdir:
            if parts and len(parts) > 0:
                frames = extract_frames_from_parts(parts, timestamps, tmpdir)
            else:
                frames = extract_frames_from_single(single_source, timestamps, tmpdir)
            
            log(f"🖼️  Extracted {len(frames)}/{TOTAL_FRAMES} frames")
            
            if len(frames) < 4:
                log("✗ Not enough frames extracted", "ERROR")
                st["state"] = "FAILED"
                save_state(st)
                return
            
            if len(frames) < TOTAL_FRAMES:
                log(f"⚠️ Only {len(frames)}/{TOTAL_FRAMES} frames, proceeding", "WARN")
            
            if build_contact_sheet(frames, output_path):
                log(f"✅ Contact sheet created: {os.path.basename(output_path)}")
            else:
                log("✗ Grid build failed", "ERROR")
                st["state"] = "FAILED"
                save_state(st)
                return

        st["state"] = "CREATED"
        st["duration"] = round(duration, 1)
        st["frames_extracted"] = len(frames)
        save_state(st)

    if not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
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
        log(f"📤 Sent ✅ (message_id={tg_res['message_id']})")
        st["state"] = "SENT"
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
        log(f"❌ Fatal error (isolated): {e}", "ERROR")
        sys.exit(0)
