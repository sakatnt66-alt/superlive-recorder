#!/usr/bin/env python3
import asyncio
import base64
import os
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
VIDEO_BITRATE = int(os.environ.get("VIDEO_BITRATE", "4000000"))
AUDIO_BITRATE = int(os.environ.get("AUDIO_BITRATE", "128000"))
VIDEO_WAIT_SECONDS = int(os.environ.get("VIDEO_WAIT_SECONDS", "120"))
PAGE_TIMEOUT_MS = int(os.environ.get("PAGE_TIMEOUT_MS", "60000"))
FIRST_CHUNK_TIMEOUT_SECONDS = int(os.environ.get("FIRST_CHUNK_TIMEOUT_SECONDS", "20"))

DURATION_MINUTES = float(os.environ.get("DURATION_MINUTES", "5"))
RECORD_DURATION_SECONDS = int(DURATION_MINUTES * 60)

GLOBAL_WATCHDOG_SECONDS = int(max(RECORD_DURATION_SECONDS + 300, 600))

USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

def log(msg=""): print(msg, flush=True)
def log_section(t): log("\n" + "="*70 + f"\n{t}\n" + "="*70)

# ============================================================
# WEBRTC HOOK (IPC Chunking to bypass Playwright string limits)
# ============================================================
WEBRTC_HOOK = r"""
(() => {
    if (window.__superlive_hook_installed) return;
    window.__superlive_hook_installed = true;
    window.__superlive_audio_tracks = [];
    window.__superlive_video_tracks = [];
    window.__superlive_track_links = [];
    window.__superlive_streams = [];
    let recorder = null, recorderError = null, isRecording = false, chunkCount = 0;
    
    // Global queue for IPC chunking
    window.__uploadQueue = [];
    window.__isUploading = false;

    async function processQueue() {
        if (window.__isUploading) return;
        window.__isUploading = true;
        while (window.__uploadQueue.length > 0) {
            const chunk = window.__uploadQueue.shift();
            try {
                // Await ensures strict ordering and prevents interleaving
                await window.uploadVideoChunk(chunk);
            } catch(e) {
                console.error("[JS] Upload error:", e);
            }
        }
        window.__isUploading = false;
    }

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
        let mimeType = "video/webm;codecs=vp9,opus";
        if (!MediaRecorder.isTypeSupported(mimeType)) {
            mimeType = "video/webm;codecs=vp8,opus";
            if (!MediaRecorder.isTypeSupported(mimeType)) mimeType = "video/webm";
        }
        
        recorder = new MediaRecorder(window.__preparedStream, { mimeType, videoBitsPerSecond: vb, audioBitsPerSecond: ab });
        isRecording = true;
        
        recorder.ondataavailable = (e) => {
            if (e.data.size > 0 && isRecording) {
                chunkCount++;
                if (chunkCount % 10 === 0) console.log(`[JS] Generated chunk #${chunkCount}`);
                
                const reader = new FileReader();
                reader.onload = () => {
                    try {
                        const result = reader.result;
                        const commaIndex = result.indexOf(',');
                        if (commaIndex !== -1) {
                            const base64 = result.substring(commaIndex + 1);
                            // CRITICAL FIX: Chunk the string to 64KB to bypass Playwright IPC string truncation
                            const chunkSize = 65536; 
                            for (let i = 0; i < base64.length; i += chunkSize) {
                                window.__uploadQueue.push(base64.substring(i, i + chunkSize));
                            }
                            processQueue();
                        }
                    } catch (err) {
                        console.error("[JS] FileReader error:", err);
                    }
                };
                reader.readAsDataURL(e.data);
            }
        };
        recorder.onerror = (e) => { recorderError = e.error ? String(e.error) : "Error"; };
        recorder.start(ts);
        return { state: recorder.state };
    };

    window.__superliveWaitChunk = async (ms) => {
        const s = Date.now(); while (Date.now() - s < ms) { 
            if (recorder && recorder.state === "recording") return { ok: true }; 
            await new Promise(r => setTimeout(r, 200)); 
        }
        return { ok: false, error: recorderError || "Timeout" };
    };
    
    window.__superliveStopRec = () => { 
        isRecording = false;
        if (recorder && recorder.state !== "inactive") recorder.stop(); 
    };
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

# ============================================================
# WORKFLOW
# ============================================================
async def run_recording(playwright):
    log("[1/5] Launching Chromium (headless=False via Xvfb)...")
    browser = await playwright.chromium.launch(
        headless=False, 
        args=[
            "--no-sandbox", "--disable-setuid-sandbox", "--autoplay-policy=no-user-gesture-required",
            "--disable-background-timer-throttling", "--disable-backgrounding-occluded-windows",
            "--disable-renderer-backgrounding", "--window-size=1920,1080"
        ]
    )
    context = await browser.new_context(viewport={"width": 1920, "height": 1080}, locale="fr-FR", user_agent=USER_AGENT)
    
    TEMP_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = time.strftime('%Y%m%d_%H%M%S')
    webm_path = TEMP_DIR / f"rec_{timestamp}.webm"
    webm_file = open(webm_path, "wb")
    
    async def upload_chunk(base64_str):
        try:
            # validate=False handles missing padding on the very last chunk gracefully
            decoded = base64.b64decode(base64_str, validate=False)
            webm_file.write(decoded)
        except Exception as e:
            log(f"[WARN] Decode error: {e}")
            
    await context.expose_function("uploadVideoChunk", upload_chunk)
    await context.add_init_script(WEBRTC_HOOK)
    
    page = await context.new_page()
    page.on("console", lambda m: log(f"[CONSOLE:{m.type}] {m.text}") if m.type in ["error", "warning", "log"] else None)
    
    log(f"[2/5] Navigating to {URL}")
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
        webm_file.close()
        await page.screenshot(path=str(TEMP_DIR / "timeout_error.png"))
        raise RuntimeError("Timed out waiting for WebRTC stream.")

    log("[3/5] Preparing MediaStream & Starting Recorder...")
    prep = await safe_eval(page, "window.__superlivePrepare()")
    log(f"[✓] Linked Video: {prep['video']} | Linked Audio: {prep['audio']}")
    
    await safe_eval(page, f"window.__superliveStartRec({VIDEO_BITRATE}, {AUDIO_BITRATE}, 1000)")
    
    wait_res = await safe_eval(page, f"window.__superliveWaitChunk({FIRST_CHUNK_TIMEOUT_SECONDS * 1000})")
    if not wait_res.get('ok'): 
        webm_file.close()
        raise RuntimeError(f"Recorder failed to produce data: {wait_res.get('error')}")
    log("[✓] MediaRecorder is generating data! (IPC Chunking Active)")

    log(f"[4/5] Capturing Native WebM for {RECORD_DURATION_SECONDS}s...")
    start_rec = time.monotonic()
    
    while time.monotonic() - start_rec < RECORD_DURATION_SECONDS:
        await asyncio.sleep(1)
        if (time.monotonic() - start_rec) % 30 < 1:
            size_mb = webm_path.stat().st_size / 1024 / 1024
            log(f"[*] Progress: {int(time.monotonic() - start_rec)}s | File Size: {size_mb:.2f} MB")

    log("[*] Stopping Recorder & Flushing Queue...")
    await safe_eval(page, "window.__superliveStopRec()")
    
    # Wait for JS queue to empty completely before closing file
    for _ in range(30):
        is_empty = await safe_eval(page, "() => window.__uploadQueue.length === 0 && !window.__isUploading")
        if is_empty: break
        await asyncio.sleep(0.5)
        
    webm_file.close()
    log(f"[✓] Native Capture Complete. Final File size: {webm_path.stat().st_size / 1024 / 1024:.2f} MB")
    
    # ============================================================
    # POST-PROCESSING (FFmpeg)
    # ============================================================
    log("[5/5] Post-Processing: Converting WebM to MP4 Segments...")
    RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)
    out_pattern = str(RECORDINGS_DIR / f"rec_{timestamp}_%03d.mp4")
    
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-stats",
        "-i", str(webm_path),
        "-map", "0:v:0", "-map", "0:a:0?",
        "-vf", "pad=width=ceil(iw/2)*2:height=ceil(ih/2)*2:color=black",
        "-c:v", "libx264", "-preset", "veryfast", "-b:v", str(VIDEO_BITRATE), "-maxrate", f"{int(VIDEO_BITRATE*1.5)}", "-bufsize", f"{VIDEO_BITRATE*2}", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", str(AUDIO_BITRATE),
        "-f", "segment", "-segment_time", str(SEGMENT_SECONDS), "-segment_format", "mp4",
        "-reset_timestamps", "1", "-movflags", "+faststart",
        out_pattern
    ]
    
    ffmpeg = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    stdout, stderr = await ffmpeg.communicate()
    
    if ffmpeg.returncode != 0:
        log(f"[ERROR] FFmpeg failed:\n{stderr.decode()}")
        raise RuntimeError("FFmpeg post-processing failed")
        
    webm_path.unlink(missing_ok=True)
    
    moved = []
    for f in RECORDINGS_DIR.glob(f"rec_{timestamp}_*.mp4"):
        moved.append(f)
        log(f"[✓] Saved: {f.name} ({f.stat().st_size / 1024 / 1024:.2f} MB)")
        
    await context.close()
    await browser.close()
    
    if not moved: raise RuntimeError("No MP4 files produced.")
    return moved

async def main():
    log_section("SUPERLIVE ULTIMATE RECORDER (IPC CHUNKING EDITION)")
    from playwright.async_api import async_playwright
    async with async_playwright() as p:
        return await asyncio.wait_for(run_recording(p), timeout=GLOBAL_WATCHDOG_SECONDS)

if __name__ == "__main__":
    try: asyncio.run(main())
    except Exception as e:
        log(f"\n[FATAL] {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
