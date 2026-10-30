#!/usr/bin/env python3
"""
full_stream_contact_sheet.py — Contact Sheet from Per-part sources.
FIXES: Deduplicates parts, corrects corrupt durations, extracts from correct part.
Design: UNCHANGED (3x4 grid, crop-to-fill, timestamp overlay).
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
# PART DISCOVERY — deduplicated, duration-corrected
# ═══════════════════════════════════════════════════════════
def discover_parts_with_durations() -> list:
    """Find deduplicated parts with corrected durations."""
    search_order = [PRESERVED_DIR, RECORDINGS_DIR, TEMP_DIR]
    parts_by_num = {}
    
    for d in search_order:
        if not d.exists():
            continue
        for pattern in ["*_av_part_*.mkv", "*_av_part_*.webm"]:
            for f in d.glob(pattern):
                try:
                    size = f.stat().st_size
                    if size < MIN_PART_SIZE:
                        continue
                    m = re.search(r'_part_(\d+)', f.name)
                    part_num = int(m.group(1)) if m else -1
                    if part_num < 0:
                        continue
                    if part_num in parts_by_num:
                        # Prefer _preserved
                        if "_preserved" in str(f):
                            parts_by_num[part_num] = (str(f), part_num, size)
                    else:
                        parts_by_num[part_num] = (str(f), part_num, size)
                except OSError:
                    continue
    
    if not parts_by_num:
        return []
    
    # Get durations and correct corrupt ones
    raw_parts = []
    for part_num in sorted(parts_by_num.keys()):
        path, num, size = parts_by_num[part_num]
        dur = get_duration(path)
        raw_parts.append((path, num, size, dur))
    
    # Calculate median duration/size ratio for correction
    valid_ratios = [(dur / size) for _, _, size, dur in raw_parts
                    if size > 0 and 0 < dur < 600]
    
    result = []
    cumulative = 0.0
    
    for path, num, size, dur in raw_parts:
        # Detect corrupt duration
        if valid_ratios:
            valid_ratios.sort()
            median_ratio = valid_ratios[len(valid_ratios) // 2]
            estimated = size * median_ratio
            if dur > estimated * 3:
                log(f"Part {num}: correcting duration {dur:.0f}s → {estimated:.0f}s", "WARN")
                dur = estimated
        
        if dur < 0.5:
            dur = max(size * valid_ratios[len(valid_ratios)//2], 1.0) if valid_ratios else 1.0
        
        result.append((path, num, dur, cumulative))
        cumulative += dur
    
    return result

def find_single_source() -> str:
    for d in [PRESERVED_DIR, RECORDINGS_DIR, TEMP_DIR]:
        if not d.exists():
            continue
        for f in d.glob("*.mkv"):
            if "_part_" in f.name or "_encoded" in f.name or "concatenated_" in f.name:
                continue
            try:
                if f.stat().st_size >= 1024 * 1024:
                    dur = get_duration(str(f))
                    if dur >= 5.0:
                        return str(f)
            except OSError:
                continue
    return None

# ═══════════════════════════════════════════════════════════
# TIMESTAMPS
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
        timestamps.append(max(0, min(ts, duration - 0.1)))
    return timestamps

# ═══════════════════════════════════════════════════════════
# FRAME EXTRACTION
# ═══════════════════════════════════════════════════════════
def get_frame_hash(path: str) -> str:
    try:
        with open(path, 'rb') as f:
            return hashlib.md5(f.read()).hexdigest()
    except Exception:
        return ""

def extract_frame_from_part(part_path: str, offset: float, out_path: str) -> bool:
    abs_part = os.path.abspath(part_path)
    abs_out = os.path.abspath(out_path)
    
    cmd = f'ffmpeg -y -ss {offset:.3f} -i "{abs_part}" -frames:v 1 -q:v 2 "{abs_out}"'
    r = run_cmd(cmd, timeout=30)
    if r and r.returncode == 0 and os.path.exists(abs_out) and os.path.getsize(abs_out) > 0:
        return True
    
    cmd2 = f'ffmpeg -y -i "{abs_part}" -ss {offset:.3f} -frames:v 1 -q:v 2 "{abs_out}"'
    r2 = run_cmd(cmd2, timeout=60)
    if r2 and r2.returncode == 0 and os.path.exists(abs_out) and os.path.getsize(abs_out) > 0:
        return True
    
    return False

def extract_frames_from_parts(parts: list, timestamps: list, tmpdir: str) -> list:
    frames = []
    frame_hashes = set()
    
    for i, ts in enumerate(timestamps):
        out = os.path.join(tmpdir, f"frame_{i:03d}.jpg")
        label = format_timestamp(ts)
        
        target_part = None
        offset_in_part = 0.0
        part_duration = 0.0
        
        for part_path, part_num, part_dur, start_offset in parts:
            if start_offset <= ts < start_offset + part_dur:
                target_part = part_path
                offset_in_part = ts - start_offset
                part_duration = part_dur
                break
        
        if target_part is None and parts:
            target_part = parts[-1][0]
            offset_in_part = max(0.1, parts[-1][2] - 1.0)
            part_duration = parts[-1][2]
        
        if target_part is None:
            log(f"Frame {i+1}/{len(timestamps)} at {label}: no part", "ERROR")
            continue
        
        success = extract_frame_from_part(target_part, offset_in_part, out)
        
        if success:
            fh = get_frame_hash(out)
            if fh and fh not in frame_hashes:
                frame_hashes.add(fh)
                frames.append((out, ts, label))
                continue
        
        # Fallback: nearby offsets
        found = False
        for delta in [-3.0, -1.0, 1.0, 3.0, -5.0, 5.0, -10.0, 10.0]:
            nearby = max(0.1, min(offset_in_part + delta, part_duration - 0.5))
            if extract_frame_from_part(target_part, nearby, out):
                fh = get_frame_hash(out)
                if fh and fh not in frame_hashes:
                    frame_hashes.add(fh)
                    frames.append((out, ts, label))
                    log(f"Frame {i+1} at {label}: recovered from offset {nearby:.1f}s", "WARN")
                    found = True
                    break
        
        if not found:
            log(f"Frame {i+1}/{len(timestamps)} at {label}: FAILED all attempts", "ERROR")
    
    return frames

def extract_frames_from_single(source: str, timestamps: list, tmpdir: str) -> list:
    frames = []
    frame_hashes = set()
    source_duration = get_duration(source)
    
    for i, ts in enumerate(timestamps):
        out = os.path.join(tmpdir, f"frame_{i:03d}.jpg")
        label = format_timestamp(ts)
        
        for seek_ts in [ts] + [max(0.1, min(ts + d, source_duration - 0.5))
                                for d in [-3, -1, 1, 3, -5, 5]]:
            cmd = f'ffmpeg -y -ss {seek_ts:.3f} -i "{source}" -frames:v 1 -q:v 2 "{out}"'
            r = run_cmd(cmd, timeout=30)
            if r and r.returncode == 0 and os.path.exists(out) and os.path.getsize(out) > 0:
                fh = get_frame_hash(out)
                if fh and fh not in frame_hashes:
                    frame_hashes.add(fh)
                    frames.append((out, ts, label))
                    break
        else:
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
        result["error"] = "no credentials"
        return result
    if os.path.getsize(photo_path) > MAX_TG_PHOTO:
        result["error"] = "too large"
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

    parts = discover_parts_with_durations()
    single_source = None

    if parts and len(parts) > 0:
        total_duration = sum(dur for _, _, dur, _ in parts)
        log(f"📦 Found {len(parts)} unique parts, total duration: {format_timestamp(total_duration)}")
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
            if parts:
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
                log(f"⚠️ Only {len(frames)}/{TOTAL_FRAMES} frames", "WARN")

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
        log(f"❌ Fatal error: {e}", "ERROR")
        sys.exit(0)
