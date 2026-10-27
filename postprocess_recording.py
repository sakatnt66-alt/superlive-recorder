#!/usr/bin/env python3
"""
postprocess_recording.py — MKV-to-MP4 conversion, segmentation, delivery.

KEY FIXES in this version:
  1. Segment sending loop NEVER stops early — all segments are attempted
  2. Each segment is tracked independently with retry capability
  3. Oversized segments are re-split instead of skipped
  4. Audio verification before every send
  5. Rate limiting between Telegram sends to avoid 429 errors
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
MARKER_SUFFIX      = ".postprocessed"
MP4_VIDEO_OK       = {"h264", "hevc", "mpeg4", "mjpeg"}
MP4_AUDIO_OK       = {"aac", "mp3", "ac3", "eac3", "alac", "mp2", "opus"}
SEND_DELAY_SECONDS = 3  # Delay between Telegram sends to avoid rate limits
MAX_RETRIES        = 3


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
    r = run(f'ffprobe -v quiet -select_streams a -show_entries stream=codec_type -of csv=p=0 "{path}"', 30)
    return bool(r and r.returncode == 0 and 'audio' in r.stdout)


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
    return {"stream_id": sid, "state": "DISCOVERED", "segments": {}, "errors": []}


def save_state(sid: str, st: dict) -> None:
    p = state_path(sid)
    tmp = p.with_suffix(".tmp")
    st["updated_at"] = datetime.now(timezone.utc).isoformat()
    tmp.write_text(json.dumps(st, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(p)


# ═══════════════════════════════════════════════════════════
# SOURCE DISCOVERY
# ═══════════════════════════════════════════════════════════
def discover_mkv_files() -> list:
    candidates = []
    for d in [PRESERVED_DIR, RECORDINGS_DIR, TEMP_DIR]:
        if not d.exists():
            continue
        for f in d.glob("*.mkv"):
            name = f.name
            if "_part_" in name or "_encoded" in name:
                continue
            try:
                if f.stat().st_size > 0:
                    candidates.append((f, f.stat().st_size))
            except OSError:
                continue
    if not candidates:
        return []
    candidates.sort(key=lambda x: x[1], reverse=True)
    verified = []
    for f, size in candidates:
        info = ffprobe_json(str(f))
        if info is None:
            continue
        vc, ac = get_codecs(info)
        if vc and ac:
            verified.append(f)
            log(f"✓ Source: {f.name} ({human_size(size)}, v={vc}, a={ac})")
            break
        elif vc:
            log(f"Skipping video-only: {f.name}")
    return verified


# ═══════════════════════════════════════════════════════════
# SEGMENTATION
# ═══════════════════════════════════════════════════════════
def calc_segment_duration(file_size: int, duration: float) -> float:
    if duration <= 0 or file_size <= 0:
        return 60.0
    if file_size <= MAX_SEGMENT_BYTES:
        return duration + 10
    bitrate = file_size / duration
    max_dur = MAX_SEGMENT_BYTES / bitrate
    safe_dur = max_dur * 0.75
    return max(safe_dur, 10.0)


def split_to_segments(src: str, outdir: str, seg_dur: float) -> list:
    pattern = os.path.join(outdir, "segment_%03d.mp4")
    cmd = (
        f'ffmpeg -y -i "{src}" '
        f'-c:v copy -c:a copy '
        f'-f segment -segment_time {seg_dur:.1f} '
        f'-reset_timestamps 1 -movflags +faststart '
        f'"{pattern}"'
    )
    log(f"Splitting: seg_time={seg_dur:.1f}s, src={human_size(os.path.getsize(src))}")
    r = run(cmd, timeout=1800)
    if r is None or r.returncode != 0:
        log(f"Split failed: {(r.stderr or '')[-300:] if r else 'timeout'}")
        return []
    return [str(s) for s in sorted(Path(outdir).glob("segment_*.mp4")) if s.stat().st_size > 0]


def resplit_oversized(seg_path: str, outdir: str, idx: int) -> list:
    size = os.path.getsize(seg_path)
    info = ffprobe_json(seg_path)
    if not info:
        return [seg_path]
    dur = get_duration(info)
    if dur <= 0:
        return [seg_path]
    bitrate = size / dur
    target_dur = max((MAX_SEGMENT_BYTES * 0.70) / bitrate, 5.0)
    log(f"Re-splitting segment {idx}: {human_size(size)} → ~{target_dur:.0f}s parts")
    pattern = os.path.join(outdir, f"resplit_{idx:03d}_%03d.mp4")
    cmd = (
        f'ffmpeg -y -i "{seg_path}" '
        f'-c:v copy -c:a copy '
        f'-f segment -segment_time {target_dur:.1f} '
        f'-reset_timestamps 1 -movflags +faststart '
        f'"{pattern}"'
    )
    r = run(cmd, timeout=600)
    if r and r.returncode == 0:
        subs = [str(s) for s in sorted(Path(outdir).glob(f"resplit_{idx:03d}_*.mp4")) if s.stat().st_size > 0]
        if subs:
            log(f"Re-split produced {len(subs)} sub-segments")
            return subs
    return [seg_path]


def verify_segment(path: str) -> dict:
    result = {"ok": False, "reason": "", "duration": 0.0, "width": 0, "height": 0}
    info = ffprobe_json(path)
    if not info:
        result["reason"] = "ffprobe failed"
        return result
    dur = get_duration(info)
    if dur <= 0:
        result["reason"] = "no duration"
        return result
    vc, ac = get_codecs(info)
    if not vc:
        result["reason"] = "no video"
        return result
    if not ac:
        result["reason"] = "no audio (silent)"
        return result
    w, h = get_resolution(info)
    result.update({"ok": True, "reason": "ok", "duration": dur, "width": w, "height": h})
    return result


# ═══════════════════════════════════════════════════════════
# TELEGRAM SENDING with retry
# ═══════════════════════════════════════════════════════════
def tg_send_video(video_path: str, caption: str, duration: int, width: int, height: int) -> dict:
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
            log(f"sendVideo attempt {attempt}/{MAX_RETRIES}: HTTP {e.code}")
        except Exception as e:
            result["error"] = str(e)
            log(f"sendVideo attempt {attempt}/{MAX_RETRIES}: {e}")
        if attempt < MAX_RETRIES:
            wait = 5 * attempt
            log(f"Retrying in {wait}s...")
            time.sleep(wait)
    return result


# ═══════════════════════════════════════════════════════════
# MAIN PIPELINE
# ═══════════════════════════════════════════════════════════
def process_one(mkv_path: Path, sid: str) -> bool:
    mkv = str(mkv_path)
    marker = mkv + MARKER_SUFFIX
    if os.path.exists(marker):
        log(f"⏭️ Already processed: {mkv_path.name}")
        return True

    st = load_state(sid)
    log("=" * 60)
    log(f"📂 Processing: {mkv_path.name} (sid={sid})")
    log("=" * 60)

    info = ffprobe_json(mkv)
    if not info:
        log("✗ ffprobe failed on source")
        st["state"] = "FAILED"
        save_state(sid, st)
        return False

    duration = get_duration(info)
    src_size = os.path.getsize(mkv)
    vc, ac = get_codecs(info)
    w, h = get_resolution(info)

    log(f"⏱️ Duration: {format_ts(duration)} ({duration:.1f}s)")
    log(f"📦 Size: {human_size(src_size)}")
    log(f"🎬 Video: {vc} {w}x{h}")
    log(f"🔊 Audio: {ac or 'NONE'}")

    if not ac:
        log("✗ Source has NO audio — refusing to process silent video")
        st["state"] = "FAILED_NO_AUDIO"
        save_state(sid, st)
        return False

    if vc and vc not in MP4_VIDEO_OK:
        log(f"✗ Codec '{vc}' not MP4-compatible")
        st["state"] = "FAILED_CODEC"
        save_state(sid, st)
        return False

    st["state"] = "PROCESSING"
    save_state(sid, st)

    seg_dur = calc_segment_duration(src_size, duration)
    log(f"📐 Segment duration: {seg_dur:.1f}s")

    with tempfile.TemporaryDirectory(prefix="pp_seg_") as segdir:
        segments = split_to_segments(mkv, segdir, seg_dur)
        if not segments:
            log("✗ No segments produced")
            st["state"] = "FAILED"
            save_state(sid, st)
            return False

        # Handle oversized segments
        final_segments = []
        for idx, seg in enumerate(segments):
            if os.path.getsize(seg) > MAX_SEGMENT_BYTES:
                log(f"⚠️ Segment {idx+1} oversized ({human_size(os.path.getsize(seg))}) — re-splitting")
                subs = resplit_oversized(seg, segdir, idx)
                for sub in subs:
                    if os.path.getsize(sub) <= MAX_SEGMENT_BYTES:
                        final_segments.append(sub)
                    else:
                        log(f"✗ Sub-segment still oversized: {human_size(os.path.getsize(sub))}")
                        st["errors"].append(f"oversize:{os.path.basename(sub)}")
            else:
                final_segments.append(seg)

        total = len(final_segments)
        log(f"📦 Final segments: {total}")

        if not final_segments:
            log("✗ All segments failed size check")
            st["state"] = "FAILED"
            save_state(sid, st)
            return False

        display_name = STREAM_NAME or mkv_path.stem
        sent_count = 0
        failed_count = 0

        # ═══════════════════════════════════════════════════
        # SEND ALL SEGMENTS — loop NEVER stops early
        # ═══════════════════════════════════════════════════
        for idx, seg_path in enumerate(final_segments):
            seg_name = os.path.basename(seg_path)
            seg_key = f"seg_{idx:04d}"

            # Skip already-sent segments (idempotency)
            prev = st["segments"].get(seg_key, {})
            if prev.get("sent") and prev.get("message_id"):
                log(f"  [{idx+1}/{total}] Already sent (msg={prev['message_id']}) — skip")
                sent_count += 1
                continue

            # Verify segment
            verdict = verify_segment(seg_path)
            if not verdict["ok"]:
                log(f"  [{idx+1}/{total}] ✗ REJECTED: {verdict['reason']}")
                st["segments"][seg_key] = {"sent": False, "error": verdict["reason"]}
                st["errors"].append(f"{seg_name}:{verdict['reason']}")
                failed_count += 1
                save_state(sid, st)
                continue

            seg_size = os.path.getsize(seg_path)
            log(f"  [{idx+1}/{total}] {seg_name}: {human_size(seg_size)}, {verdict['duration']:.1f}s ✓")

            if seg_size > TG_HARD_LIMIT:
                log(f"  [{idx+1}/{total}] ✗ Exceeds Telegram limit — cannot send")
                st["segments"][seg_key] = {"sent": False, "error": "exceeds_tg_limit"}
                failed_count += 1
                save_state(sid, st)
                continue

            caption = (
                f"🎬 {display_name}\n"
                f"🆔 {sid}\n"
                f"📎 المقطع {idx+1} من {total}\n"
                f"⏱️ {format_ts(verdict['duration'])}"
            )

            # Send with retry
            tg_res = tg_send_video(
                seg_path, caption,
                int(verdict["duration"]),
                verdict["width"], verdict["height"]
            )

            if tg_res["ok"]:
                log(f"  [{idx+1}/{total}] 📤 SENT ✅ (msg={tg_res['message_id']})")
                sent_count += 1
                st["segments"][seg_key] = {
                    "sent": True,
                    "message_id": tg_res["message_id"],
                    "size": seg_size,
                    "duration": round(verdict["duration"], 1),
                }
            else:
                log(f"  [{idx+1}/{total}] ✗ Send failed: {tg_res['error']} — will NOT stop, continuing...")
                st["segments"][seg_key] = {"sent": False, "error": tg_res["error"]}
                st["errors"].append(f"{seg_name}:{tg_res['error']}")
                failed_count += 1

            save_state(sid, st)

            # Rate limit delay between sends
            if idx < total - 1:
                time.sleep(SEND_DELAY_SECONDS)

    # Final status
    log(f"\n{'=' * 60}")
    log(f"RESULT: {sent_count}/{total} sent, {failed_count} failed")
    log(f"{'=' * 60}")

    if sent_count > 0 and failed_count == 0:
        st["state"] = "SENT"
        with open(marker, "w") as f:
            json.dump({"sent_at": datetime.now(timezone.utc).isoformat(), "segments": sent_count}, f)
        log("✅ ALL segments sent successfully")
        return True
    elif sent_count > 0:
        st["state"] = "PARTIAL"
        save_state(sid, st)
        log(f"⚠️ PARTIAL: {sent_count}/{total} sent. Source preserved for retry.")
        return False
    else:
        st["state"] = "FAILED"
        save_state(sid, st)
        log("✗ No segments sent. Source preserved.")
        return False


def main():
    log("=" * 60)
    log("Post-processing pipeline (RESCUE + PRIMARY)")
    log(f"STREAM_ID={STREAM_ID}")
    log("=" * 60)

    mkv_files = discover_mkv_files()
    if not mkv_files:
        log("No valid MKV source found")
        return

    success = fail = 0
    for mkv in mkv_files:
        try:
            if process_one(mkv, STREAM_ID):
                success += 1
            else:
                fail += 1
        except Exception as e:
            log(f"❌ Error on {mkv.name}: {e}")
            import traceback
            traceback.print_exc()
            fail += 1

    log(f"Final: {success} complete, {fail} failed")
    log("Source files NEVER deleted by this pipeline.")


if __name__ == "__main__":
    main()
