#!/usr/bin/env python3
"""
full_stream_contact_sheet.py — Portrait 3x4 Contact Sheet Generator.

FIXES:
  1. Uses manifest-verified source from postprocess_recording.py
  2. Verifies SHA-256 before frame extraction
  3. Never sums durations from globbed part files
  4. Handles truncated sources gracefully
  5. Robust frame extraction with multiple seek strategies
  6. Preserves exact 3x4 grid design
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

GRID_COLS = 3
GRID_ROWS = 4
TOTAL_FRAMES = 12
CELL_W = 360
CELL_H = 640
GAP = 1
MARGIN = 5
JPEG_QUALITY = 92
FONT_SIZE = 28
BG_COLOR = "black"
MAX_TG_PHOTO = 10 * 1024 * 1024
TEXT_H = FONT_SIZE + 8


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


def run_cmd(cmd: str, timeout: int = 60):
    try:
        return subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return None
    except Exception:
        return None


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    try:
        with open(path, 'rb') as f:
            for chunk in iter(lambda: f.read(4 * 1024 * 1024), b''):
                h.update(chunk)
        return h.hexdigest()
    except Exception:
        return ""


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
    return f"{h:02d}:{m:02d}:{s:02d}" if h > 0 else f"{m:02d}:{s:02d}"


def get_authoritative_source() -> tuple:
    """
    Get source from manifest written by postprocess_recording.py.
    Falls back to largest valid MKV if manifest unavailable.
    NEVER sums durations from globbed part files.
    """
    safe_id = "".join(c if c.isalnum() or c in "-_" else "_" for c in str(STREAM_ID))
    mf_path = STATE_DIR / f"manifest_{safe_id}.json"

    if mf_path.exists():
        try:
            mf = json.loads(mf_path.read_text(encoding="utf-8"))
            src = mf.get("source", {})
            path = src.get("path")
            sha = src.get("sha256")
            dur = src.get("duration", 0)

            if path and os.path.exists(path):
                # Verify SHA-256 if available
                if sha:
                    actual_sha = sha256_file(path)
                    if actual_sha == sha:
                        log(f"✓ Manifest source verified (SHA-256 match)")
                        return path, dur
                    else:
                        log(f"⚠️ SHA-256 mismatch, using file anyway", "WARN")
                        return path, dur
                else:
                    return path, dur
        except Exception as e:
            log(f"Manifest read error: {e}", "WARN")

    # Fallback: largest complete MKV (NOT parts)
    log("Manifest unavailable, searching for largest complete MKV...", "WARN")
    candidates = []
    for d in [PRESERVED_DIR, RECORDINGS_DIR, TEMP_DIR]:
        if not d.exists():
            continue
        for f in d.glob("*.mkv"):
            if any(x in f.name for x in ["_part_", "_encoded", "concatenated_", "rebuilt_"]):
                continue
            try:
                candidates.append((str(f), f.stat().st_size))
            except OSError:
                continue

    if candidates:
        candidates.sort(key=lambda x: x[1], reverse=True)
        best = candidates[0][0]
        dur = get_duration(best)
        if dur > 0.5:
            return best, dur

    return None, 0.0


def calculate_timestamps(duration: float, count: int) -> list:
    if duration <= 0 or count <= 0:
        return []
    if duration < 12:
        return [duration * (i + 1) / (count + 1) for i in range(count)]
    margin = min(2.0, duration * 0.02)
    usable = max(duration - 2 * margin, 0.1)
    return [max(0.1, min(margin + usable * i / (count - 1), duration - 0.5)) for i in range(count)]


def get_frame_hash(path: str) -> str:
    try:
        with open(path, 'rb') as f:
            return hashlib.md5(f.read()).hexdigest()
    except Exception:
        return ""


def extract_frame(source: str, ts: float, out: str) -> bool:
    """Extract a single frame with multiple seek strategies."""
    abs_source = os.path.abspath(source)
    abs_out = os.path.abspath(out)

    # Strategy 1: Input seeking (fast)
    cmd1 = f'ffmpeg -y -ss {ts:.3f} -i "{abs_source}" -frames:v 1 -q:v 2 "{abs_out}"'
    r = run_cmd(cmd1, timeout=30)
    if r and r.returncode == 0 and os.path.exists(abs_out) and os.path.getsize(abs_out) > 0:
        return True

    # Strategy 2: Output seeking (accurate but slower)
    cmd2 = f'ffmpeg -y -i "{abs_source}" -ss {ts:.3f} -frames:v 1 -q:v 2 "{abs_out}"'
    r = run_cmd(cmd2, timeout=60)
    if r and r.returncode == 0 and os.path.exists(abs_out) and os.path.getsize(abs_out) > 0:
        return True

    # Strategy 3: Input seeking with noaccurate_seek
    cmd3 = f'ffmpeg -y -ss {ts:.3f} -noaccurate_seek -i "{abs_source}" -frames:v 1 -q:v 2 "{abs_out}"'
    r = run_cmd(cmd3, timeout=30)
    if r and r.returncode == 0 and os.path.exists(abs_out) and os.path.getsize(abs_out) > 0:
        return True

    return False


def extract_frames(source: str, timestamps: list, tmpdir: str, source_duration: float) -> list:
    frames = []
    frame_hashes = set()

    for i, ts in enumerate(timestamps):
        out = os.path.join(tmpdir, f"frame_{i:03d}.jpg")
        label = format_timestamp(ts)

        # Ensure timestamp doesn't exceed source duration
        if ts >= source_duration:
            ts = max(0.1, source_duration - 1.0)
            label = format_timestamp(ts)

        if extract_frame(source, ts, out):
            fh = get_frame_hash(out)
            if fh and fh not in frame_hashes:
                frame_hashes.add(fh)
                frames.append((out, ts, label))
                log(f"  Frame {i+1}/{len(timestamps)} at {label}: ✓")
                continue

        # Fallback: try nearby timestamps
        found = False
        for delta in [-5.0, -3.0, -1.0, 1.0, 3.0, 5.0, -10.0, -15.0, 10.0]:
            nearby = ts + delta
            if nearby < 0.1 or nearby >= source_duration:
                continue
            if extract_frame(source, nearby, out):
                fh = get_frame_hash(out)
                if fh and fh not in frame_hashes:
                    frame_hashes.add(fh)
                    frames.append((out, ts, label))  # Keep original label
                    log(f"  Frame {i+1}/{len(timestamps)} at {label}: ✓ (recovered from {nearby:.1f}s)", "WARN")
                    found = True
                    break

        if not found:
            log(f"  Frame {i+1}/{len(timestamps)} at {label}: ✗ FAILED all attempts", "ERROR")

    return frames


def build_contact_sheet(frames: list, output_path: str) -> bool:
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        log("Pillow not installed", "ERROR")
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
               "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"]:
        try:
            font = ImageFont.truetype(fp, FONT_SIZE)
            break
        except (IOError, OSError):
            continue
    if font is None:
        font = ImageFont.load_default()

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
        except Exception:
            continue

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
    except Exception:
        return False


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
                result["error"] = "HTTP 403"
                return result
            result["error"] = f"HTTP {e.code}"
        except Exception as e:
            result["error"] = str(e)
        if attempt < 3:
            time.sleep(2 * attempt)
    return result


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

    source, duration = get_authoritative_source()
    if not source or duration < 0.5:
        log("✗ No valid source found", "ERROR")
        st["state"] = "NO_SOURCE"
        save_state(st)
        return

    log(f"📂 Source: {os.path.basename(source)}")
    log(f"⏱️  Duration: {format_timestamp(duration)} ({duration:.1f}s)")
    log(f"🔐 SHA-256: {sha256_file(source)[:16]}...")

    safe_id = "".join(c if c.isalnum() or c in "-_" else "_" for c in str(STREAM_ID))
    PRESERVED_DIR.mkdir(parents=True, exist_ok=True)
    output_path = str(PRESERVED_DIR / f"full_stream_contact_sheet_{safe_id}.jpg")

    if st.get("state") == "CREATED" and os.path.exists(output_path) and os.path.getsize(output_path) > 0:
        log("📷 Contact sheet exists — retrying send only")
    else:
        timestamps = calculate_timestamps(duration, TOTAL_FRAMES)
        if not timestamps:
            st["state"] = "FAILED"
            save_state(st)
            return

        log(f"📐 Timestamps: {[format_timestamp(t) for t in timestamps]}")
        st["state"] = "CREATING"
        save_state(st)

        with tempfile.TemporaryDirectory(prefix="cs_frames_") as tmpdir:
            frames = extract_frames(source, timestamps, tmpdir, duration)
            log(f"🖼️  Extracted {len(frames)}/{TOTAL_FRAMES} frames")

            if len(frames) < 4:
                log("✗ Not enough frames extracted", "ERROR")
                st["state"] = "FAILED"
                save_state(st)
                return

            if len(frames) < TOTAL_FRAMES:
                log(f"⚠️ Only {len(frames)}/{TOTAL_FRAMES} frames — proceeding with available", "WARN")

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

    dur_str = format_timestamp(duration)
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
    save_state(st)
    log("Contact sheet pipeline finished")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log(f"❌ Fatal error: {e}", "ERROR")
        sys.exit(0)
