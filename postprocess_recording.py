#!/usr/bin/env python3
"""
postprocess_recording.py — Post-processing pipeline.
Splits MKV into MP4 segments, creates preview grid, sends to Telegram.
Uses Pillow for grid generation (avoids FFmpeg drawtext failures).
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

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "")
RECORDINGS_DIR     = Path(os.environ.get("RECORDINGS_DIR", "recordings"))
TEMP_DIR           = RECORDINGS_DIR / "_temp"
PRESERVED_DIR      = RECORDINGS_DIR / "_preserved"
STATE_DIR          = RECORDINGS_DIR / "_state"
STREAM_ID          = os.environ.get("STREAM_ID", "unknown")
STREAM_NAME        = os.environ.get("STREAM_NAME", "")

MAX_SEGMENT_BYTES  = 45 * 1024 * 1024
TG_HARD_LIMIT      = 50 * 1024 * 1024
MAX_TG_PHOTO_BYTES = 10 * 1024 * 1024

GRID_COLS, GRID_ROWS = 4, 3
GRID_COUNT = GRID_COLS * GRID_ROWS
CELL_W, CELL_H = 400, 225
JPEG_Q = 2
MARKER_SUFFIX = ".postprocessed"
MP4_VIDEO_OK = {"h264", "hevc", "mpeg4", "mjpeg"}
MP4_AUDIO_OK = {"aac", "mp3", "ac3", "eac3", "alac", "mp2", "opus"}


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
    r = run(f'ffprobe -v quiet -print_format json -show_format -show_streams "{path}"', 30)
    if r and r.returncode == 0:
        try:
            return json.loads(r.stdout)
        except json.JSONDecodeError:
            pass
    return None


def get_duration(info: dict) -> float:
    return float(info.get("format", {}).get("duration", 0))


def get_codecs(info: dict):
    vc = ac = None
    for s in info.get("streams", []):
        ct = s.get("codec_type")
        cn = (s.get("codec_name") or "").lower()
        if ct == "video" and vc is None: vc = cn
        elif ct == "audio" and ac is None: ac = cn
    return vc, ac


def get_resolution(info: dict):
    for s in info.get("streams", []):
        if s.get("codec_type") == "video":
            return s.get("width", 0), s.get("height", 0)
    return 0, 0


def human_size(n: int) -> str:
    mb = n / (1024 * 1024)
    return f"{mb / 1024:.2f} GB" if mb >= 1024 else f"{mb:.1f} MB"


def format_ts(seconds: float) -> str:
    h, rem = divmod(int(seconds), 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def safe_id(sid: str) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in str(sid))


# ═══════════════════════════════════════════════════════════
# STATE
# ═══════════════════════════════════════════════════════════
def state_path(sid: str) -> Path:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    return STATE_DIR / f"{safe_id(sid)}.json"


def load_state(sid: str) -> dict:
    p = state_path(sid)
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"stream_id": sid, "state": "DISCOVERED", "segments": {}, "telegram": {}, "errors": []}


def save_state(sid: str, st: dict) -> None:
    p = state_path(sid)
    tmp = p.with_suffix(".tmp")
    st["updated_at"] = datetime.now(timezone.utc).isoformat()
    tmp.write_text(json.dumps(st, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(p)


# ═══════════════════════════════════════════════════════════
# FILE DISCOVERY — EXCLUDES _part_ AND _encoded FILES
# ═══════════════════════════════════════════════════════════
def discover_mkv_files() -> list:
    """
    Search _preserved/ FIRST, then recordings/ and _temp/.
    EXCLUDES: *_part_*.mkv and *_encoded*.mkv (intermediate files).
    """
    files = []
    seen_names = set()
    for d in [PRESERVED_DIR, RECORDINGS_DIR, TEMP_DIR]:
        if not d.exists():
            continue
        for f in sorted(d.glob("*.mkv")):
            name = f.name
            # ── EXCLUDE intermediate files ──
            if "_part_" in name:
                continue
            if "_encoded" in name:
                continue
            if name in seen_names:
                continue
            try:
                if f.stat().st_size > 0:
                    files.append(f)
                    seen_names.add(name)
            except OSError:
                pass
    return files


# ═══════════════════════════════════════════════════════════
# SPLIT INTO MP4 SEGMENTS
# ═══════════════════════════════════════════════════════════
def calc_segment_duration(src: str, info: dict) -> float:
    fsize = os.path.getsize(src)
    dur = get_duration(info)
    if dur <= 0:
        return 300.0
    if fsize <= MAX_SEGMENT_BYTES:
        return dur + 10
    bitrate = fsize / dur
    seg_dur = MAX_SEGMENT_BYTES / bitrate
    return max(seg_dur * 0.9, 10.0)


def split_to_mp4_segments(src: str, outdir: str, seg_dur: float) -> list:
    pattern = os.path.join(outdir, "segment_%03d.mp4")
    cmd = (
        f'ffmpeg -y -i "{src}" '
        f'-c:v copy -c:a copy '
        f'-f segment -segment_time {seg_dur:.1f} '
        f'-reset_timestamps 1 '
        f'-movflags +faststart '
        f'"{pattern}"'
    )
    log(f"Splitting with segment_time={seg_dur:.1f}s (stream copy)")
    r = run(cmd, timeout=900)
    if r is None or r.returncode != 0:
        err = (r.stderr or "")[-300:] if r else "timeout"
        log(f"Segment splitting failed: {err}")
        return []
    segments = sorted(Path(outdir).glob("segment_*.mp4"))
    return [str(s) for s in segments if s.stat().st_size > 0]


def verify_segment(path: str) -> bool:
    info = ffprobe_json(path)
    if info is None:
        return False
    dur = get_duration(info)
    if dur <= 0:
        return False
    vc, _ = get_codecs(info)
    return vc is not None


# ═══════════════════════════════════════════════════════════
# PREVIEW GRID (12 frames, 4×3) — Pillow-based
# ═══════════════════════════════════════════════════════════
def extract_preview_frames(src: str, duration: float, tmpdir: str) -> list:
    paths = []
    margin = min(2.0, duration * 0.02)
    span = max(duration - 2 * margin, 0.1)
    for i in range(GRID_COUNT):
        ts = margin + span * i / max(GRID_COUNT - 1, 1)
        out = os.path.join(tmpdir, f"prev_{i:02d}.jpg")
        cmd = f'ffmpeg -y -ss {ts:.3f} -i "{src}" -frames:v 1 -q:v {JPEG_Q} "{out}"'
        r = run(cmd, timeout=30)
        if r and r.returncode == 0 and os.path.exists(out) and os.path.getsize(out) > 0:
            paths.append((out, ts))
        else:
            stderr_snippet = (r.stderr or "")[:200] if r else "timeout"
            log(f"Preview frame {i+1}/{GRID_COUNT} at {ts:.1f}s failed: {stderr_snippet}")
    return paths


def build_preview_grid(frames: list, out_path: str) -> bool:
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        log("Pillow not installed — cannot build preview grid")
        return False

    n = len(frames)
    if n == 0:
        return False
    n = min(n, GRID_COLS * GRID_ROWS)
    frames = frames[:n]

    text_h = 20
    gap = 4
    margin = 8
    img_w = margin * 2 + GRID_COLS * CELL_W + (GRID_COLS - 1) * gap
    img_h = margin * 2 + GRID_ROWS * (CELL_H + text_h) + (GRID_ROWS - 1) * gap

    grid_img = Image.new('RGB', (img_w, img_h), color="black")
    draw = ImageDraw.Draw(grid_img)

    font = None
    for fp in ["/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
               "/usr/share/fonts/truetype/freefont/FreeSans.ttf"]:
        try:
            font = ImageFont.truetype(fp, 16)
            break
        except (IOError, OSError):
            continue
    if font is None:
        try:
            font = ImageFont.load_default()
        except Exception:
            font = None

    for i, (frame_path, ts) in enumerate(frames):
        row, col = i // GRID_COLS, i % GRID_COLS
        x = margin + col * (CELL_W + gap)
        y = margin + row * (CELL_H + text_h + gap)

        try:
            with Image.open(frame_path) as img:
                if img.mode != 'RGB':
                    img = img.convert('RGB')
                img.thumbnail((CELL_W, CELL_H), Image.LANCZOS)
                offset_x = x + (CELL_W - img.width) // 2
                offset_y = y + (CELL_H - img.height) // 2
                grid_img.paste(img, (offset_x, offset_y))
        except Exception:
            continue

        if font:
            label = format_ts(ts)
            try:
                text_bbox = draw.textbbox((0, 0), label, font=font)
                text_w = text_bbox[2] - text_bbox[0]
            except AttributeError:
                text_w = len(label) * 8
            draw.text((x + (CELL_W - text_w) // 2, y + CELL_H + 2), label, fill="white", font=font)

    try:
        grid_img.save(out_path, "JPEG", quality=90)
        return os.path.exists(out_path) and os.path.getsize(out_path) > 0
    except Exception:
        return False


# ═══════════════════════════════════════════════════════════
# TELEGRAM
# ═══════════════════════════════════════════════════════════
def tg_send_video(video_path: str, thumb_path: str, caption: str,
                  duration: int, width: int, height: int) -> dict:
    result = {"ok": False, "message_id": None, "error": ""}
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        result["error"] = "Telegram credentials not configured"
        return result
    fsize = os.path.getsize(video_path)
    if fsize > TG_HARD_LIMIT:
        result["error"] = f"Video too large: {human_size(fsize)}"
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

    if thumb_path and os.path.exists(thumb_path):
        with open(thumb_path, "rb") as f:
            tdata = f.read()
        body += f"--{boundary}\r\nContent-Disposition: form-data; name=\"thumbnail\"; filename=\"thumb.jpg\"\r\nContent-Type: image/jpeg\r\n\r\n".encode()
        body += tdata + b"\r\n"

    body += f"--{boundary}\r\nContent-Disposition: form-data; name=\"video\"; filename=\"{fname}\"\r\nContent-Type: video/mp4\r\n\r\n".encode()
    body += vdata
    body += f"\r\n--{boundary}--\r\n".encode()

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendVideo"
    for attempt in range(1, 4):
        try:
            req = urllib.request.Request(url, data=body, headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
            with urllib.request.urlopen(req, timeout=300) as resp:
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
        except Exception as e:
            result["error"] = str(e)
        if attempt < 3:
            time.sleep(3 * attempt)
    return result


def tg_send_photo(photo_path: str, caption: str) -> dict:
    result = {"ok": False, "message_id": None, "error": ""}
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        result["error"] = "Telegram credentials not configured"
        return result
    if os.path.getsize(photo_path) > MAX_TG_PHOTO_BYTES:
        result["error"] = "Photo too large"
        return result

    boundary = uuid.uuid4().hex
    with open(photo_path, "rb") as f:
        pdata = f.read()

    body = b""
    body += f"--{boundary}\r\nContent-Disposition: form-data; name=\"chat_id\"\r\n\r\n{TELEGRAM_CHAT_ID}\r\n".encode()
    body += f"--{boundary}\r\nContent-Disposition: form-data; name=\"caption\"\r\n\r\n".encode()
    body += caption.encode("utf-8") + b"\r\n"
    body += f"--{boundary}\r\nContent-Disposition: form-data; name=\"photo\"; filename=\"preview.jpg\"\r\nContent-Type: image/jpeg\r\n\r\n".encode()
    body += pdata
    body += f"\r\n--{boundary}--\r\n".encode()

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
    for attempt in range(1, 4):
        try:
            req = urllib.request.Request(url, data=body, headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
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


def create_thumbnail(video_path: str, thumb_path: str) -> bool:
    info = ffprobe_json(video_path)
    if info is None:
        return False
    dur = get_duration(info)
    ts = min(dur * 0.25, dur - 0.5) if dur > 1 else 0
    cmd = (
        f'ffmpeg -y -ss {ts:.2f} -i "{video_path}" '
        f'-frames:v 1 -q:v 2 '
        f'-vf "scale=320:180:force_original_aspect_ratio=decrease,'
        f'pad=320:180:(ow-iw)/2:(oh-ih)/2:black" '
        f'"{thumb_path}"'
    )
    r = run(cmd, timeout=30)
    return r is not None and r.returncode == 0 and os.path.exists(thumb_path) and os.path.getsize(thumb_path) > 0


# ═══════════════════════════════════════════════════════════
# MAIN PIPELINE FOR ONE RECORDING
# ═══════════════════════════════════════════════════════════
def process_one(mkv_path: Path, sid: str) -> bool:
    mkv = str(mkv_path)
    marker = mkv + MARKER_SUFFIX
    if os.path.exists(marker):
        log(f"⏭️  Already processed: {mkv_path.name}")
        return True

    st = load_state(sid)
    log(f"{'=' * 60}")
    log(f"📂 Processing: {mkv_path.name}  (stream_id={sid})")
    log(f"{'=' * 60}")

    info = ffprobe_json(mkv)
    if info is None:
        log("✗ ffprobe cannot read source MKV")
        st["state"] = "FAILED"
        save_state(sid, st)
        return False

    duration = get_duration(info)
    src_size = os.path.getsize(mkv)
    vc, ac = get_codecs(info)
    w, h = get_resolution(info)

    log(f"⏱️  Duration : {format_ts(duration)}")
    log(f"📦 Size    : {human_size(src_size)}")
    log(f"🎬 Video   : {vc}  {w}x{h}")
    log(f"🔊 Audio   : {ac or 'none'}")

    if vc and vc not in MP4_VIDEO_OK:
        log(f"✗ Video codec '{vc}' not MP4-compatible. Keeping MKV.")
        st["state"] = "FAILED"
        save_state(sid, st)
        return False

    st["state"] = "PROCESSING"
    save_state(sid, st)

    seg_dur = calc_segment_duration(mkv, info)
    log(f"📐 Segment duration: {seg_dur:.1f}s")

    with tempfile.TemporaryDirectory(prefix="pp_seg_") as segdir:
        segments = split_to_mp4_segments(mkv, segdir, seg_dur)
        if not segments:
            log("✗ No segments created")
            st["state"] = "FAILED"
            save_state(sid, st)
            return False

        log(f"📦 Created {len(segments)} segment(s)")
        display_name = STREAM_NAME or mkv_path.stem
        total_segments = len(segments)
        sent_count = 0

        for idx, seg_path in enumerate(segments):
            seg_name = os.path.basename(seg_path)
            seg_size = os.path.getsize(seg_path)
            seg_info = ffprobe_json(seg_path)
            seg_dur_actual = get_duration(seg_info) if seg_info else 0
            seg_w, seg_h = get_resolution(seg_info) if seg_info else (0, 0)

            log(f"  [{idx+1}/{total_segments}] {seg_name}: {human_size(seg_size)}, {seg_dur_actual:.1f}s")

            if not verify_segment(seg_path):
                log(f"  ✗ Segment {idx+1} failed verification")
                continue

            thumb_path = os.path.join(segdir, f"thumb_{idx:03d}.jpg")
            thumb_ok = create_thumbnail(seg_path, thumb_path)

            if seg_size > MAX_SEGMENT_BYTES:
                log(f"  ⚠️ Segment {idx+1} exceeds 45MiB. Skipping send.")
                continue

            caption = (
                f"🎬 {display_name}\n"
                f"🆔 {sid}\n"
                f"📎 المقطع {idx+1} من {total_segments}\n"
                f"⏱️ {format_ts(seg_dur_actual)}"
            )

            tg_res = tg_send_video(seg_path, thumb_path if thumb_ok else "", caption, int(seg_dur_actual), seg_w, seg_h)
            if tg_res["ok"]:
                log(f"  📤 Segment {idx+1} sent ✅ (msg_id={tg_res['message_id']})")
                sent_count += 1
                st["segments"][seg_name] = {"sent": True, "message_id": tg_res["message_id"], "size": seg_size}
            else:
                log(f"  ✗ Segment {idx+1} send failed: {tg_res['error']}")
                st["segments"][seg_name] = {"sent": False, "error": tg_res["error"]}
                st["errors"].append(f"Segment {idx+1}: {tg_res['error']}")

            save_state(sid, st)
            time.sleep(1)

        # ── 12-frame preview grid (Pillow-based) ──
        preview_path = os.path.join(segdir, "preview_grid.jpg")
        preview_sent = False
        frames = extract_preview_frames(mkv, duration, segdir)
        log(f"🖼️  Extracted {len(frames)}/{GRID_COUNT} frames for preview")

        if len(frames) >= 2:
            if build_preview_grid(frames, preview_path):
                log("✅ Preview grid created")
                prev_cap = f"🖼️ معاينة: {display_name}\n🆔 {sid}\n⏱️ {format_ts(duration)}"
                prev_res = tg_send_photo(preview_path, prev_cap)
                if prev_res["ok"]:
                    log(f"📤 Preview sent ✅ (msg_id={prev_res['message_id']})")
                    preview_sent = True
                    st["telegram"]["preview"] = {"ok": True, "message_id": prev_res["message_id"]}
                else:
                    log(f"⚠️ Preview send failed: {prev_res['error']}")
            else:
                log("⚠️ Grid creation failed")
        else:
            log("⚠️ Not enough frames for preview")

        save_state(sid, st)

    if sent_count > 0:
        st["state"] = "SENT"
        with open(marker, "w") as f:
            json.dump({"sent_at": datetime.now(timezone.utc).isoformat(), "segments_sent": sent_count, "preview_sent": preview_sent}, f, indent=2)
        log(f"✅ COMPLETE: {sent_count}/{total_segments} segments sent")
        return True
    else:
        st["state"] = "FAILED"
        save_state(sid, st)
        log("✗ No segments sent successfully")
        return False


# ═══════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════
def main():
    log("=" * 60)
    log("Post-processing pipeline started")
    log(f"  PRESERVED_DIR  : {PRESERVED_DIR}")
    log(f"  RECORDINGS_DIR : {RECORDINGS_DIR}")
    log(f"  STREAM_ID      : {STREAM_ID}")
    log("=" * 60)

    mkv_files = discover_mkv_files()
    if not mkv_files:
        log("No MKV files found (excluding _part_ and _encoded)")
        return

    log(f"Found {len(mkv_files)} original MKV file(s)")
    for f in mkv_files:
        log(f"  • {f.name}  ({human_size(f.stat().st_size)})")

    success = fail = 0
    for mkv in mkv_files:
        try:
            if process_one(mkv, STREAM_ID):
                success += 1
            else:
                fail += 1
        except Exception as e:
            log(f"❌ Unexpected error on {mkv.name}: {e}")
            import traceback
            traceback.print_exc()
            fail += 1

    log("=" * 60)
    log(f"Results: {success} succeeded, {fail} failed/skipped")
    log("Post-processing pipeline finished")
    log("=" * 60)


if __name__ == "__main__":
    main()
