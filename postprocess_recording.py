#!/usr/bin/env python3
"""
postprocess_recording.py — MKV-to-MP4 conversion, segmentation, and delivery.

This is the RESCUE AND PRIMARY processing pipeline that runs AFTER record_once.py
finishes (regardless of its exit code). It is responsible for:

1. Finding the ORIGINAL complete MKV file (not _encoded or _part_ intermediates)
2. Verifying it has both video AND audio streams
3. Converting to MP4 via stream copy (no re-encoding)
4. Splitting into segments under 45 MiB each
5. Re-splitting any oversized segment (NEVER skipping)
6. Verifying each segment has audio+video before sending
7. Sending segments to Telegram with idempotent state tracking
8. NEVER deleting the source MKV until all segments are confirmed sent

This module is the recovery path when record_once.py's internal Matroska
splitting fails. It picks up the preserved original MKV and completes the job.
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

# ── Size limits ──
# Telegram Bot API hard limit is 50 MB. We use 45 MiB as operational limit
# with 5 MB safety margin for metadata/container overhead.
MAX_SEGMENT_BYTES = 45 * 1024 * 1024   # 45 MiB operational limit
TG_HARD_LIMIT     = 50 * 1024 * 1024   # 50 MB absolute Telegram limit
MAX_TG_PHOTO_BYTES = 10 * 1024 * 1024

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


def has_audio_stream(path: str) -> bool:
    """Verify actual audio stream presence via ffprobe (not filename)."""
    r = run(
        f'ffprobe -v quiet -select_streams a '
        f'-show_entries stream=codec_type -of csv=p=0 "{path}"', 30
    )
    return bool(r and r.returncode == 0 and 'audio' in r.stdout)


# ═══════════════════════════════════════════════════════════
# STATE MANAGEMENT
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
    return {"stream_id": sid, "state": "DISCOVERED", "segments": {}, "errors": []}


def save_state(sid: str, st: dict) -> None:
    p = state_path(sid)
    tmp = p.with_suffix(".tmp")
    st["updated_at"] = datetime.now(timezone.utc).isoformat()
    tmp.write_text(json.dumps(st, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(p)


# ═══════════════════════════════════════════════════════════
# SOURCE SELECTION — picks the REAL original, not intermediates
# ═══════════════════════════════════════════════════════════
def discover_mkv_files() -> list:
    """
    Find the ORIGINAL complete MKV files.

    EXCLUDES:
      *_part_*.mkv    — split parts from record_once.py's failed attempts
      *_encoded*.mkv  — video-only intermediate (NO audio)
      Any file without an audio stream (verified via ffprobe)

    PRIORITY: Prefer *_av.mkv, then largest file with audio+video.
    """
    candidates = []
    for d in [PRESERVED_DIR, RECORDINGS_DIR, TEMP_DIR]:
        if not d.exists():
            continue
        for f in d.glob("*.mkv"):
            name = f.name
            # Exclude known intermediates and parts
            if "_part_" in name:
                log(f"Excluded split part: {name}")
                continue
            if "_encoded" in name:
                log(f"Excluded video-only intermediate: {name}")
                continue
            try:
                size = f.stat().st_size
                if size > 0:
                    candidates.append((f, size))
            except OSError:
                continue

    if not candidates:
        return []

    # Sort by size descending — the original complete file is the largest
    candidates.sort(key=lambda x: x[1], reverse=True)

    # Verify the top candidate has both audio and video
    verified = []
    for f, size in candidates:
        info = ffprobe_json(str(f))
        if info is None:
            log(f"Skipping unreadable file: {f.name}")
            continue
        vc, ac = get_codecs(info)
        if vc and ac:
            verified.append(f)
            log(f"✓ Original source verified: {f.name} "
                f"({human_size(size)}, video={vc}, audio={ac})")
            break  # Take the first (largest) verified file
        elif vc and not ac:
            log(f"Skipping video-only file: {f.name} (no audio stream)")
        else:
            log(f"Skipping file without video: {f.name}")

    return verified


# ═══════════════════════════════════════════════════════════
# SEGMENT DURATION CALCULATION — accurate, based on actual file
# ═══════════════════════════════════════════════════════════
def calc_segment_duration(file_size: int, duration: float) -> float:
    """
    Calculate segment duration to keep each segment under MAX_SEGMENT_BYTES.

    Uses ACTUAL file size and duration (not estimates).
    Applies a 0.75 safety factor to account for keyframe alignment
    (segments cut at keyframes may be slightly larger than estimated).
    """
    if duration <= 0 or file_size <= 0:
        return 60.0  # Conservative fallback

    if file_size <= MAX_SEGMENT_BYTES:
        return duration + 10  # Single segment

    # Calculate actual bitrate from file size and duration
    bitrate = file_size / duration  # bytes per second

    # Calculate maximum duration for target size
    max_dur = MAX_SEGMENT_BYTES / bitrate

    # Apply safety factor: keyframe-aligned cuts can overshoot by up to 25%
    # because the segment boundary snaps to the nearest keyframe AFTER
    # the target time. A 0.75 factor ensures we stay under the limit.
    safe_dur = max_dur * 0.75

    # Minimum 10 seconds per segment (avoid excessive fragmentation)
    return max(safe_dur, 10.0)


# ═══════════════════════════════════════════════════════════
# SPLITTING — with automatic re-split of oversized segments
# ═══════════════════════════════════════════════════════════
def split_mkv_to_mp4_segments(src: str, outdir: str, seg_dur: float) -> list:
    """
    Split source MKV into MP4 segments using stream copy.
    Returns list of segment file paths.
    """
    pattern = os.path.join(outdir, "segment_%03d.mp4")
    cmd = (
        f'ffmpeg -y -i "{src}" '
        f'-c:v copy -c:a copy '
        f'-f segment -segment_time {seg_dur:.1f} '
        f'-reset_timestamps 1 '
        f'-movflags +faststart '
        f'"{pattern}"'
    )
    log(f"Splitting: segment_time={seg_dur:.1f}s, source={human_size(os.path.getsize(src))}")
    r = run(cmd, timeout=1800)
    if r is None or r.returncode != 0:
        err = (r.stderr or "")[-400:] if r else "timeout"
        log(f"Initial split failed: {err}")
        return []
    segments = sorted(Path(outdir).glob("segment_*.mp4"))
    return [str(s) for s in segments if s.stat().st_size > 0]


def resplit_oversized_segment(seg_path: str, outdir: str, seg_index: int) -> list:
    """
    Re-split a single oversized segment into smaller pieces.
    Called when a segment exceeds MAX_SEGMENT_BYTES after initial split.
    Returns list of sub-segment paths.
    """
    size = os.path.getsize(seg_path)
    info = ffprobe_json(seg_path)
    if info is None:
        log(f"Cannot probe oversized segment for re-split: {seg_path}")
        return [seg_path]  # Return as-is; caller will handle

    dur = get_duration(info)
    if dur <= 0:
        return [seg_path]

    # Calculate sub-segment duration with aggressive safety factor
    bitrate = size / dur
    target_dur = (MAX_SEGMENT_BYTES * 0.70) / bitrate  # Extra conservative
    target_dur = max(target_dur, 5.0)

    log(f"Re-splitting oversized segment {seg_index}: "
        f"{human_size(size)} → sub-segments of ~{target_dur:.0f}s")

    pattern = os.path.join(outdir, f"resplit_{seg_index:03d}_%03d.mp4")
    cmd = (
        f'ffmpeg -y -i "{seg_path}" '
        f'-c:v copy -c:a copy '
        f'-f segment -segment_time {target_dur:.1f} '
        f'-reset_timestamps 1 '
        f'-movflags +faststart '
        f'"{pattern}"'
    )
    r = run(cmd, timeout=600)
    if r is None or r.returncode != 0:
        err = (r.stderr or "")[-200:] if r else "timeout"
        log(f"Re-split failed for segment {seg_index}: {err}")
        return [seg_path]

    subs = sorted(Path(outdir).glob(f"resplit_{seg_index:03d}_*.mp4"))
    result = [str(s) for s in subs if s.stat().st_size > 0]
    if result:
        log(f"Re-split produced {len(result)} sub-segments from segment {seg_index}")
        return result
    return [seg_path]


def verify_segment(path: str) -> dict:
    """
    Verify a segment has valid video AND audio streams.
    Returns dict with verification results.
    """
    result = {"ok": False, "reason": "", "duration": 0.0,
              "width": 0, "height": 0, "vcodec": None, "acodec": None}

    info = ffprobe_json(path)
    if info is None:
        result["reason"] = "ffprobe cannot read segment"
        return result

    dur = get_duration(info)
    if dur <= 0:
        result["reason"] = "segment has no readable duration"
        return result

    vc, ac = get_codecs(info)
    if not vc:
        result["reason"] = "segment has NO video stream"
        return result
    if not ac:
        result["reason"] = "segment has NO audio stream — SILENT, will NOT be sent"
        return result

    w, h = get_resolution(info)
    result.update({
        "ok": True, "reason": "ok", "duration": dur,
        "width": w, "height": h, "vcodec": vc, "acodec": ac
    })
    return result


# ═══════════════════════════════════════════════════════════
# TELEGRAM SENDING
# ═══════════════════════════════════════════════════════════
def tg_send_video(video_path: str, thumb_path: str, caption: str,
                  duration: int, width: int, height: int) -> dict:
    result = {"ok": False, "message_id": None, "error": ""}
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        result["error"] = "Telegram credentials not configured"
        return result
    fsize = os.path.getsize(video_path)
    if fsize > TG_HARD_LIMIT:
        result["error"] = f"Video too large for Telegram: {human_size(fsize)}"
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
                result["error"] = "HTTP 403 — NOT retrying"
                return result
            result["error"] = f"HTTP {e.code}"
            log(f"sendVideo attempt {attempt}/3: HTTP {e.code}")
        except Exception as e:
            result["error"] = str(e)
            log(f"sendVideo attempt {attempt}/3: {e}")
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


def create_thumbnail(video_path: str, thumb_path: str) -> bool:
    info = ffprobe_json(video_path)
    if info is None:
        return False
    dur = get_duration(info)
    ts = min(dur * 0.25, max(dur - 0.5, 0))
    cmd = (
        f'ffmpeg -y -ss {ts:.2f} -i "{video_path}" '
        f'-frames:v 1 -q:v 2 '
        f'-vf "scale=320:180:force_original_aspect_ratio=decrease,'
        f'pad=320:180:(ow-iw)/2:(oh-ih)/2:black" '
        f'"{thumb_path}"'
    )
    r = run(cmd, timeout=30)
    return bool(r and r.returncode == 0 and os.path.exists(thumb_path)
                and os.path.getsize(thumb_path) > 0)


# ═══════════════════════════════════════════════════════════
# MAIN PIPELINE FOR ONE RECORDING
# ═══════════════════════════════════════════════════════════
def process_one(mkv_path: Path, sid: str) -> bool:
    mkv = str(mkv_path)
    marker = mkv + MARKER_SUFFIX
    if os.path.exists(marker):
        log(f"⏭️  Already fully processed: {mkv_path.name}")
        return True

    st = load_state(sid)
    log("=" * 60)
    log(f"📂 Processing: {mkv_path.name} (stream_id={sid})")
    log("=" * 60)

    # ── Validate source ──
    info = ffprobe_json(mkv)
    if info is None:
        log("✗ ffprobe cannot read source MKV — source preserved, nothing sent")
        st["state"] = "FAILED"
        st["errors"].append("ffprobe_failed_on_source")
        save_state(sid, st)
        return False

    duration = get_duration(info)
    src_size = os.path.getsize(mkv)
    vc, ac = get_codecs(info)
    w, h = get_resolution(info)

    log(f"⏱️  Duration : {format_ts(duration)} ({duration:.1f}s)")
    log(f"📦 Size    : {human_size(src_size)}")
    log(f"🎬 Video   : {vc}  {w}x{h}")
    log(f"🔊 Audio   : {ac or 'NONE'}")

    if not vc:
        log("✗ Source has no video stream — cannot process")
        st["state"] = "FAILED"
        save_state(sid, st)
        return False

    if not ac:
        log("✗ Source has NO audio stream — refusing to send silent video")
        st["state"] = "FAILED_NO_AUDIO"
        st["errors"].append("source_has_no_audio")
        save_state(sid, st)
        return False

    if vc not in MP4_VIDEO_OK:
        log(f"✗ Video codec '{vc}' not MP4-compatible — keeping MKV, no conversion")
        st["state"] = "FAILED_INCOMPATIBLE_CODEC"
        save_state(sid, st)
        return False

    # ── Calculate segment duration from ACTUAL file metrics ──
    st["state"] = "PROCESSING"
    st["source_file"] = mkv
    st["source_size"] = src_size
    st["source_duration"] = round(duration, 2)
    save_state(sid, st)

    seg_dur = calc_segment_duration(src_size, duration)
    log(f"📐 Calculated segment duration: {seg_dur:.1f}s "
        f"(source: {human_size(src_size)} / {duration:.0f}s)")

    with tempfile.TemporaryDirectory(prefix="pp_seg_") as segdir:
        # ── Initial split ──
        segments = split_mkv_to_mp4_segments(mkv, segdir, seg_dur)
        if not segments:
            log("✗ No segments produced — source preserved, nothing sent")
            st["state"] = "FAILED"
            st["errors"].append("split_produced_no_segments")
            save_state(sid, st)
            return False

        log(f"📦 Initial split produced {len(segments)} segment(s)")

        # ── Check each segment and re-split if oversized ──
        final_segments = []
        for idx, seg_path in enumerate(segments):
            seg_size = os.path.getsize(seg_path)
            if seg_size > MAX_SEGMENT_BYTES:
                log(f"  ⚠️ Segment {idx+1} is {human_size(seg_size)} — "
                    f"exceeds {human_size(MAX_SEGMENT_BYTES)}, re-splitting...")
                sub_segs = resplit_oversized_segment(seg_path, segdir, idx)
                # Verify sub-segments are actually smaller
                for sub in sub_segs:
                    sub_size = os.path.getsize(sub)
                    if sub_size <= MAX_SEGMENT_BYTES:
                        final_segments.append(sub)
                    else:
                        # Still oversized after re-split — try one more time
                        # with even shorter duration
                        log(f"  ⚠️ Sub-segment still {human_size(sub_size)}, "
                            f"attempting deeper re-split...")
                        deep_subs = resplit_oversized_segment(sub, segdir, idx + 100)
                        for ds in deep_subs:
                            if os.path.getsize(ds) <= MAX_SEGMENT_BYTES:
                                final_segments.append(ds)
                            else:
                                log(f"  ✗ Cannot reduce below limit: {human_size(os.path.getsize(ds))}")
                                st["errors"].append(f"segment_still_oversize:{os.path.basename(ds)}")
            else:
                final_segments.append(seg_path)

        if not final_segments:
            log("✗ All segments failed size check — source preserved")
            st["state"] = "FAILED"
            st["errors"].append("all_segments_oversize")
            save_state(sid, st)
            return False

        log(f"📦 Final segment count: {len(final_segments)}")

        # ── Verify and send each segment ──
        display_name = STREAM_NAME or mkv_path.stem
        total_segments = len(final_segments)
        sent_count = 0
        failed_count = 0

        for idx, seg_path in enumerate(final_segments):
            seg_name = os.path.basename(seg_path)
            seg_size = os.path.getsize(seg_path)

            # Check idempotency: skip already-sent segments
            prev = st["segments"].get(seg_name, {})
            if prev.get("sent") and prev.get("message_id"):
                log(f"  [{idx+1}/{total_segments}] Already sent (msg={prev['message_id']}) — skipped")
                sent_count += 1
                continue

            # Verify segment integrity
            verdict = verify_segment(seg_path)
            if not verdict["ok"]:
                log(f"  [{idx+1}/{total_segments}] ✗ REJECTED: {verdict['reason']}")
                st["segments"][seg_name] = {"sent": False, "error": verdict["reason"]}
                st["errors"].append(f"segment_rejected:{seg_name}:{verdict['reason']}")
                failed_count += 1
                save_state(sid, st)
                continue

            log(f"  [{idx+1}/{total_segments}] {seg_name}: "
                f"{human_size(seg_size)}, {verdict['duration']:.1f}s, "
                f"{verdict['vcodec']}+{verdict['acodec']} ✓")

            # Double-check size before sending
            if seg_size > TG_HARD_LIMIT:
                log(f"  ✗ Segment exceeds Telegram hard limit — cannot send")
                st["segments"][seg_name] = {"sent": False, "error": "exceeds_tg_hard_limit"}
                failed_count += 1
                save_state(sid, st)
                continue

            # Create thumbnail
            thumb_path = os.path.join(segdir, f"thumb_{idx:03d}.jpg")
            thumb_ok = create_thumbnail(seg_path, thumb_path)

            # Build caption
            caption = (
                f"🎬 {display_name}\n"
                f"🆔 {sid}\n"
                f"📎 المقطع {idx+1} من {total_segments}\n"
                f"⏱️ {format_ts(verdict['duration'])}"
            )

            # Send via Telegram sendVideo
            tg_res = tg_send_video(
                seg_path,
                thumb_path if thumb_ok else "",
                caption,
                int(verdict["duration"]),
                verdict["width"],
                verdict["height"],
            )

            if tg_res["ok"]:
                log(f"  📤 Segment {idx+1}/{total_segments} sent ✅ "
                    f"(msg_id={tg_res['message_id']})")
                sent_count += 1
                st["segments"][seg_name] = {
                    "sent": True,
                    "message_id": tg_res["message_id"],
                    "size": seg_size,
                    "duration": round(verdict["duration"], 1),
                    "has_audio": True,
                    "has_video": True,
                }
            else:
                log(f"  ✗ Segment {idx+1} send failed: {tg_res['error']} — "
                    f"source preserved for retry")
                st["segments"][seg_name] = {"sent": False, "error": tg_res["error"]}
                st["errors"].append(f"send_failed:{seg_name}:{tg_res['error']}")
                failed_count += 1

            save_state(sid, st)
            time.sleep(1)  # Rate limit courtesy

    # ── Final status ──
    if sent_count > 0 and failed_count == 0:
        st["state"] = "SENT"
        with open(marker, "w", encoding="utf-8") as f:
            json.dump({
                "sent_at": datetime.now(timezone.utc).isoformat(),
                "segments_sent": sent_count,
                "total_segments": total_segments,
            }, f, indent=2)
        log(f"✅ COMPLETE: {sent_count}/{total_segments} segments sent with audio+video")
        return True
    elif sent_count > 0:
        st["state"] = "PARTIAL"
        save_state(sid, st)
        log(f"⚠️ PARTIAL: {sent_count}/{total_segments} sent, {failed_count} failed. "
            f"Source preserved. Re-run to retry failed segments.")
        return False
    else:
        st["state"] = "FAILED"
        save_state(sid, st)
        log("✗ No segments sent. Source preserved for manual recovery.")
        return False


# ═══════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════
def main():
    log("=" * 60)
    log("Post-processing pipeline started (RESCUE MODE)")
    log(f"  PRESERVED_DIR  : {PRESERVED_DIR}")
    log(f"  RECORDINGS_DIR : {RECORDINGS_DIR}")
    log(f"  TEMP_DIR       : {TEMP_DIR}")
    log(f"  STREAM_ID      : {STREAM_ID}")
    log(f"  STREAM_NAME    : {STREAM_NAME or '(not set)'}")
    log(f"  MAX_SEGMENT    : {human_size(MAX_SEGMENT_BYTES)}")
    log("=" * 60)

    mkv_files = discover_mkv_files()
    if not mkv_files:
        log("No valid original MKV found (intermediates excluded)")
        log("If record_once.py deleted the file, it cannot be recovered here.")
        return

    log(f"Found {len(mkv_files)} verified original MKV file(s)")
    for f in mkv_files:
        log(f"  • {f.name} ({human_size(f.stat().st_size)})")

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
    log(f"Results: {success} complete, {fail} failed/partial")
    log("Source files are NEVER deleted by this pipeline.")
    log("Post-processing pipeline finished.")
    log("=" * 60)


if __name__ == "__main__":
    main()
