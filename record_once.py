#!/usr/bin/env python3
import asyncio
import base64
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

# ============================================================
# CONFIGURATION
# ============================================================
URL = os.environ.get("RECORD_URL", "https://superlivetv.com/fr/livestream/150596097")
RECORDINGS_DIR = Path(os.environ.get("RECORDINGS_DIR", "recordings")).resolve()
TEMP_DIR = Path(os.environ.get("TEMP_DIR", str(RECORDINGS_DIR / "_temp"))).resolve()
SEGMENT_SECONDS = int(os.environ.get("SEGMENT_SECONDS", "60"))
RECORD_DURATION_SECONDS = int(os.environ.get("RECORD_DURATION_SECONDS", "300"))
VIDEO_BITRATE = int(os.environ.get("VIDEO_BITRATE", "4000000"))
AUDIO_BITRATE = int(os.environ.get("AUDIO_BITRATE", "128000"))
VIDEO_WAIT_SECONDS = int(os.environ.get("VIDEO_WAIT_SECONDS", "120"))
PAGE_TIMEOUT_MS = int(os.environ.get("PAGE_TIMEOUT_MS", "60000"))
FIRST_CHUNK_TIMEOUT_SECONDS = int(os.environ.get("FIRST_CHUNK_TIMEOUT_SECONDS", "20"))
GLOBAL_WATCHDOG_SECONDS = int(os.environ.get("GLOBAL_WATCHDOG_SECONDS", str(max(RECORD_DURATION_SECONDS + 180, 420))))

CHUNK_TIMESLICE_MS = 1000
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"

def log(msg=""): print(msg, flush=True)
def log_section(t): log("\n" + "="*70 + f"\n{t}\n" + "="*70)

# ============================================================
# THE ULTIMATE WEBRTC HOOK (Merged from your local code)
# ============================================================
WEBRTC_HOOK = r"""
(() => {
    if (window.__superlive_hook_installed) return;
    window.__superlive_hook_installed = true;
    window.__superlive_audio_tracks = [];
    window.__superlive_video_tracks = [];
    window.__superlive_track_links = [];
    window.__superlive_streams = [];
    let recorder = null, recorderQueue = [], recorderError = null;

    function addUnique(arr, value) { if (value && !arr.includes(value)) arr.push(value); }
    function rememberStream(stream) {
        if (!stream) return;
        try { addUnique(window.__superlive_streams, stream); for (const t of stream.getTracks()) rememberTrack(t, stream); } catch (_) {}
    }
    function rememberTrack(track, stream = null) {
        if (!track) return;
        try {
            if (track.kind === "audio") addUnique(window.__superlive_audio_tracks, track);
            if (track.kind === "video") addUnique(window.__superlive_video_tracks, track);
            if (stream) {
                const existing = window.__superlive_track_links.find(x => x.track === track);
                if (existing) addUnique(existing.streams, stream);
                else window.__superlive_track_links.push({ track, streams: [stream] });
            }
        } catch (_) {}
    }

    const OriginalPC = window.RTCPeerConnection || window.webkitRTCPeerConnection;
    if (OriginalPC) {
        const WrappedPC = function(...args) {
            const pc = new OriginalPC(...args);
            try {
                pc.addEventListener("track", (e) => {
                    if (e.track) rememberTrack(e.track);
                    if (e.streams) for (const s of e.streams) rememberStream(s);
                    if (e.receiver && e.receiver.track) rememberTrack(e.receiver.track);
                });
            } catch (_) {}
            return pc;
        };
        WrappedPC.prototype = OriginalPC.prototype;
        Object.setPrototypeOf(WrappedPC, OriginalPC);
        window.RTCPeerConnection = WrappedPC;
        if (window.webkitRTCPeerConnection) window.webkitRTCPeerConnection = WrappedPC;
    }

    window.__superlivePrepare = () => {
        const videos = Array.from(document.querySelectorAll("video"));
        let video = videos.find(v => v.srcObject && v.readyState >= 2 && v.videoWidth > 0);
        if (!video) video = videos.find(v => v.srcObject) || null;
        if (!video) throw new Error("No video element found.");
        
        const sourceStream = video.srcObject;
        let videoTrack = sourceStream ? sourceStream.getVideoTracks().find(t => t.readyState === "live") : null;
        if (!videoTrack) videoTrack = (window.__superlive_video_tracks || []).find(t => t.readyState === "live");
        if (!videoTrack) throw new Error("No live video track.");

        let audioTrack = sourceStream ? sourceStream.getAudioTracks().find(t => t.readyState === "live") : null;
        if (!audioTrack) {
            const links = window.__superlive_track_links || [];
            const linked = links.filter(l => l.track && l.track.kind === "audio" && l.streams && l.streams.some(s => s.getVideoTracks().some(v => v === videoTrack || v.id === videoTrack.id)));
            audioTrack = linked.find(l => !l.track.muted)?.track || linked[0]?.track || null;
        }
        if (!audioTrack) {
            const liveAudios = (window.__superlive_audio_tracks || []).filter(t => t.readyState === "live");
            audioTrack = liveAudios.find(t => !t.muted) || liveAudios[0] || null;
        }
        if (!audioTrack) throw new Error("No live audio track found.");

        window.__preparedStream = new MediaStream([videoTrack, audioTrack]);
        return { video: videoTrack.id, audio: audioTrack.id };
    };

    window.__superliveStartRec = (vb, ab, ts) => {
        recorder = new MediaRecorder(window.__preparedStream, { mimeType: "video/webm;codecs=vp9,opus", videoBitsPerSecond: vb, audioBitsPerSecond: ab });
        recorder.ondataavailable = async (e) => {
            if (e.data.size > 0) {
                const buf = await e.data.arrayBuffer();
                const bytes = new Uint8Array(buf); let bin = ""; const S = 0x8000;
                for (let i = 0; i < bytes.length; i += S) bin += String.fromCharCode(...bytes.subarray(i, Math.min(i + S, bytes.length)));
                recorderQueue.push(btoa(bin));
            }
        };
        recorder.onerror = (e) => { recorderError = e.error ? String(e.error) : "Error"; };
        recorder.start(ts);
        return { state: recorder.state };
    };

    window.__superliveWaitChunk = async (ms) => {
        const s = Date.now(); while (Date.now() - s < ms) { if (recorderQueue.length > 0) return { ok: true }; await new Promise(r => setTimeout(r, 200)); }
        return { ok: false, error: recorderError || "Timeout" };
    };
    window.__superliveTakeChunk = () => recorderQueue.shift();
    window.__superliveStopRec = () => { if (recorder && recorder.state !== "inactive") recorder.stop(); };
})();
"""

# ============================================================
# PYTHON HELPERS
# ============================================================
async def safe_eval(page, expr, arg=None, timeout=15):
    try:
        if arg is not None: return await asyncio.wait_for(page.evaluate(expr, arg), timeout=timeout)
        return await asyncio.wait_for(page.evaluate(expr), timeout=timeout)
    except Exception as e: raise RuntimeError(f"Eval failed: {e}")

def decode_chunk(enc): return base64.b64decode(enc, validate=False) if enc else b""

def build_ffmpeg_cmd(prefix):
    out = str(prefix) + "_%03d.mp4"
    return ["ffmpeg", "-hide_banner", "-loglevel", "warning", "-i", "pipe:0",
            "-map", "0:v:0", "-map", "0:a:0?", "-vf", "pad=width=ceil(iw/2)*2:height=ceil(ih/2)*2:color=black",
            "-c:v", "libx264", "-preset", "veryfast", "-b:v", str(VIDEO_BITRATE), "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", str(AUDIO_BITRATE), "-reset_timestamps", "1",
            "-segment_time", str(SEGMENT_SECONDS), "-segment_format", "mp4", "-f", "segment", out]

# ============================================================
# WORKFLOW
# ============================================================
async def run_recording(playwright):
    log("[1/4] Launching Chromium (headless=False via Xvfb)...")
    browser = await playwright.chromium.launch(
        headless=False, # CRITICAL: Xvfb will handle the display
        args=[
            "--no-sandbox", "--disable-setuid-sandbox", "--autoplay-policy=no-user-gesture-required",
            "--disable-background-timer-throttling", "--disable-backgrounding-occluded-windows",
            "--disable-renderer-backgrounding", "--window-size=1920,1080"
        ]
    )
    context = await browser.new_context(viewport={"width": 1920, "height": 1080}, locale="fr-FR", user_agent=USER_AGENT)
    await context.add_init_script(WEBRTC_HOOK)
    page = await context.new_page()
    
    page.on("console", lambda m: log(f"[CONSOLE:{m.type}] {m.text}") if m.type in ["error", "warning"] else None)
    
    log(f"[2/4] Navigating to {URL}")
    await page.goto(URL, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS)
    
    log("[*] Waiting for WebRTC Video & Audio...")
    start = time.time()
    while time.time() - start < VIDEO_WAIT_SECONDS:
        try:
            info = await safe_eval(page, """() => {
                const v = document.querySelector('video');
                return {
                    hasVideo: !!(v && v.videoWidth > 0),
                    audioCount: (window.__superlive_audio_tracks || []).filter(t => t.readyState === 'live').length
                };
            }""")
            if info['hasVideo'] and info['audioCount'] > 0:
                log(f"[✓] WebRTC Stream Locked! (Audio Tracks: {info['audioCount']})")
                break
        except: pass
        await asyncio.sleep(2)
    else:
        raise RuntimeError("Timed out waiting for WebRTC stream.")

    log("[3/4] Preparing MediaStream & Starting Recorder...")
    prep = await safe_eval(page, "window.__superlivePrepare()")
    log(f"[✓] Linked Video: {prep['video']} | Linked Audio: {prep['audio']}")
    
    await safe_eval(page, f"window.__superliveStartRec({VIDEO_BITRATE}, {AUDIO_BITRATE}, {CHUNK_TIMESLICE_MS})")
    
    wait_res = await safe_eval(page, f"window.__superliveWaitChunk({FIRST_CHUNK_TIMEOUT_SECONDS * 1000})")
    if not wait_res.get('ok'): raise RuntimeError(f"Recorder failed to produce data: {wait_res.get('error')}")
    log("[✓] MediaRecorder is generating data!")

    TEMP_DIR.mkdir(parents=True, exist_ok=True)
    prefix = TEMP_DIR / f"rec_{time.strftime('%Y%m%d_%H%M%S')}"
    cmd = build_ffmpeg_cmd(prefix)
    
    ffmpeg = await asyncio.create_subprocess_exec(*cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    log(f"[4/4] Recording for {RECORD_DURATION_SECONDS}s via FFmpeg...")
    
    start_rec = time.monotonic()
    chunks = 0
    while time.monotonic() - start_rec < RECORD_DURATION_SECONDS:
        chunk = await safe_eval(page, "window.__superliveTakeChunk()")
        if chunk:
            ffmpeg.stdin.write(decode_chunk(chunk))
            await ffmpeg.stdin.drain()
            chunks += 1
        else:
            await asyncio.sleep(0.2)
            
        if chunks % 50 == 0 and chunks > 0:
            log(f"[*] Progress: {int(time.monotonic() - start_rec)}s | Chunks: {chunks}")

    log("[*] Stopping Recorder & Draining...")
    await safe_eval(page, "window.__superliveStopRec()")
    await asyncio.sleep(2)
    
    while True:
        chunk = await safe_eval(page, "window.__superliveTakeChunk()")
        if not chunk: break
        ffmpeg.stdin.write(decode_chunk(chunk))
        await ffmpeg.stdin.drain()

    ffmpeg.stdin.close()
    await ffmpeg.wait()
    
    RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)
    moved = []
    for f in TEMP_DIR.glob("*.mp4"):
        dest = RECORDINGS_DIR / f.name
        shutil.move(str(f), str(dest))
        moved.append(dest)
        log(f"[✓] Saved: {dest.name} ({dest.stat().st_size / 1024 / 1024:.2f} MB)")
        
    await context.close()
    await browser.close()
    
    if not moved: raise RuntimeError("No MP4 files produced.")
    return moved

async def main():
    log_section("SUPERLIVE ULTIMATE RECORDER (XVFB)")
    from playwright.async_api import async_playwright
    async with async_playwright() as p:
        return await asyncio.wait_for(run_recording(p), timeout=GLOBAL_WATCHDOG_SECONDS)

if __name__ == "__main__":
    try: asyncio.run(main())
    except Exception as e:
        log(f"\n[FATAL] {type(e).__name__}: {e}")
        sys.exit(1)
