#!/usr/bin/env python3
"""
postprocess_recording.py — Post-processing pipeline.
Searches recordings/, recordings/_temp/, AND recordings/_preserved/
for completed MKV files.
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

GRID_COLS, GRID_ROWS = 4, 3
GRID_COUNT = GRID_COLS * GRID_ROWS
CELL_W, CELL_H = 400, 225
JPEG_Q = 2
MAX_TG_VIDEO = 50 * 1024 * 1024
MAX_TG_PHOTO = 10 * 1024 * 1024
MARKER = ".postprocessed"
MP4_V_OK = {"h264", "hevc", "mpeg4"}
MP4_A_OK = {"aac", "mp3", "ac3", "eac3", "alac", "mp2", "opus"}

def log(m): print(f"[POSTPROC] {m}", flush=True)

def run(cmd, t=600):
    try: return subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=t)
    except: return None

def probe(p):
    r = run(f'ffprobe -v quiet -print_format json -show_format -show_streams "{p}"', 30)
    if r and r.returncode == 0:
        try: return json.loads(r.stdout)
        except: pass
    return None

def stable(p, w=3):
    try:
        s1 = os.path.getsize(p)
        if s1 == 0: return False
        time.sleep(w)
        return os.path.getsize(p) == s1
    except: return False

def codecs(info):
    v = a = None
    for s in info.get("streams", []):
        if s.get("codec_type") == "video" and not v: v = (s.get("codec_name") or "").lower()
        elif s.get("codec_type") == "audio" and not a: a = (s.get("codec_name") or "").lower()
    return v, a

def remux(src, dst):
    tmp = dst + ".tmp"
    r = run(f'ffmpeg -y -i "{src}" -c:v copy -c:a copy -movflags +faststart "{tmp}"', 900)
    if r is None or r.returncode != 0:
        if os.path.exists(tmp): os.remove(tmp)
        return False, (r.stderr[-300:] if r and r.stderr else "timeout")
    if not os.path.exists(tmp) or os.path.getsize(tmp) == 0:
        return False, "empty output"
    if probe(tmp) is None:
        os.remove(tmp)
        return False, "invalid output"
    os.replace(tmp, dst)
    return True, ""

def extract_frames(src, dur, td):
    paths = []
    margin = min(2.0, dur * 0.02)
    span = max(dur - 2 * margin, 0.1)
    for i in range(GRID_COUNT):
        ts = margin + span * i / max(GRID_COUNT - 1, 1)
        out = os.path.join(td, f"f{i:02d}.jpg")
        r = run(f'ffmpeg -y -ss {ts:.3f} -i "{src}" -frames:v 1 -q:v {JPEG_Q} '
                f'-vf "scale={CELL_W}:{CELL_H}:force_original_aspect_ratio=decrease,'
                f'pad={CELL_W}:{CELL_H}:(ow-iw)/2:(oh-ih)/2:black" "{out}"', 30)
        if r and r.returncode == 0 and os.path.exists(out) and os.path.getsize(out) > 0:
            paths.append((out, ts))
    return paths

def grid(frames, out):
    n = len(frames)
    if n == 0: return False
    inp = " ".join(f'-i "{p}"' for p, _ in frames)
    parts = []
    for i in range(n):
        h, m, s = int(frames[i][1]//3600), int(frames[i][1]%3600//60), int(frames[i][1]%60)
        lbl = f"{h:02d}:{m:02d}:{s:02d}"
        parts.append(f"[{i}:v]scale={CELL_W}:{CELL_H}:force_original_aspect_ratio=decrease,"
                     f"pad={CELL_W}:{CELL_H}:(ow-iw)/2:(oh-ih)/2:black,"
                     f"drawtext=text='{lbl}':fontsize=18:fontcolor=white:borderw=1:bordercolor=black:x=4:y=h-24,"
                     f"setsar=1[v{i}]")
    rows = []
    for r in range(GRID_ROWS):
        cols = [f"[v{r*GRID_COLS+c}]" for c in range(GRID_COLS) if r*GRID_COLS+c < n]
        if cols:
            rows.append(f"{''.join(cols)}hstack=inputs={len(cols)}[row{r}]" if len(cols)>1 else f"{cols[0]}copy[row{r}]")
    if not rows: return False
    if len(rows) > 1:
        parts.append(f"{''.join(f'[row{r}]' for r in range(len(rows)))}vstack=inputs={len(rows)}[out]")
    else:
        parts.append("[row0]copy[out]")
    r = run(f'ffmpeg -y {inp} -filter_complex "{";".join(parts)}" -map "[out]" -q:v {JPEG_Q} "{out}"', 120)
    return r is not None and r.returncode == 0 and os.path.exists(out)

def tg(method, fp, field, cap=""):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID: return False
    sz = os.path.getsize(fp)
    lim = MAX_TG_VIDEO if "video" in method.lower() else MAX_TG_PHOTO
    if sz > lim:
        log(f"⚠️ Too large for Telegram: {sz/1024/1024:.0f}MB")
        return False
    b = uuid.uuid4().hex
    fn = os.path.basename(fp)
    mime = mimetypes.guess_type(fp)[0] or "application/octet-stream"
    with open(fp, "rb") as f: fd = f.read()
    body = f"--{b}\r\nContent-Disposition: form-data; name=\"chat_id\"\r\n\r\n{TELEGRAM_CHAT_ID}\r\n".encode()
    if cap: body += f"--{b}\r\nContent-Disposition: form-data; name=\"caption\"\r\n\r\n".encode() + cap.encode() + b"\r\n"
    body += f"--{b}\r\nContent-Disposition: form-data; name=\"{field}\"; filename=\"{fn}\"\r\nContent-Type: {mime}\r\n\r\n".encode()
    body += fd + f"\r\n--{b}--\r\n".encode()
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{method}"
    for a in range(3):
        try:
            req = urllib.request.Request(url, data=body, headers={"Content-Type": f"multipart/form-data; boundary={b}"})
            with urllib.request.urlopen(req, timeout=180) as resp:
                if resp.status == 200: return True
        except urllib.error.HTTPError as e:
            if e.code == 403:
                log("⚠️ HTTP 403 — NOT retrying")
                return False
            log(f"⚠️ TG {method} attempt {a+1}: HTTP {e.code}")
        except Exception as e:
            log(f"⚠️ TG {method} attempt {a+1}: {e}")
        time.sleep(3)
    return False

def process(mkv):
    mkv = str(mkv)
    if os.path.exists(mkv + MARKER):
        log(f"⏭️ Already processed: {os.path.basename(mkv)}")
        return True
    log(f"📂 Processing: {os.path.basename(mkv)}")
    if not stable(mkv):
        log("✗ File not stable"); return False
    info = probe(mkv)
    if not info:
        log("✗ ffprobe failed"); return False
    dur = float(info.get("format", {}).get("duration", 0))
    if dur <= 0:
        log("✗ No duration"); return False
    log(f"⏱️ Duration: {dur:.0f}s")
    vc, ac = codecs(info)
    log(f"🎬 {vc} / 🔊 {ac}")
    if vc and vc not in MP4_V_OK:
        log(f"✗ Video '{vc}' not MP4-compatible. Keeping MKV."); return False
    mp4 = mkv.rsplit(".", 1)[0] + ".mp4"
    ok, err = remux(mkv, mp4)
    if not ok:
        log(f"✗ Remux failed: {err}"); return False
    log(f"✅ MP4: {os.path.basename(mp4)} ({os.path.getsize(mp4)/1024/1024:.1f}MB)")
    preview = mkv.rsplit(".", 1)[0] + "_preview.jpg"
    pok = False
    with tempfile.TemporaryDirectory() as td:
        fr = extract_frames(mkv, dur, td)
        log(f"🖼️ {len(fr)}/{GRID_COUNT} frames")
        if len(fr) >= 2 and grid(fr, preview):
            pok = True; log("✅ Preview grid created")
    name = STREAM_NAME or os.path.basename(mkv)
    cap = f"🎬 {name}\n🆔 {STREAM_ID}\n⏱️ {dur/60:.1f} min"
    v_ok = tg("sendVideo", mp4, "video", cap) if os.path.getsize(mp4) <= MAX_TG_VIDEO else False
    p_ok = tg("sendPhoto", preview, "photo", f"🖼️ {name}") if pok and os.path.exists(preview) else False
    with open(mkv + MARKER, "w") as f:
        json.dump({"at": datetime.now(timezone.utc).isoformat(), "v": v_ok, "p": p_ok}, f)
    log(f"✅ Done: video={'✓' if v_ok else '✗'} preview={'✓' if p_ok else '✗'}")
    return v_ok or p_ok

def main():
    log("=" * 50)
    log("Post-processing pipeline started")
    log(f"  RECORDINGS_DIR : {RECORDINGS_DIR}")
    log(f"  PRESERVED_DIR  : {PRESERVED_DIR}")
    log(f"  STREAM_ID      : {STREAM_ID}")
    log("=" * 50)
    mkvs = []
    for d in [RECORDINGS_DIR, TEMP_DIR, PRESERVED_DIR]:
        if d.exists():
            mkvs.extend(sorted(d.glob("*.mkv")))
    if not mkvs:
        log("No MKV files found in any directory")
        return
    log(f"Found {len(mkvs)} MKV file(s)")
    s = f = 0
    for m in mkvs:
        try:
            if process(m): s += 1
            else: f += 1
        except Exception as e:
            log(f"❌ Error: {e}"); f += 1
    log(f"Results: {s} ok, {f} failed")

if __name__ == "__main__":
    main()
