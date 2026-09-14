#!/usr/bin/env python3
import asyncio
import os
import subprocess
import sys
import time
import json
import urllib.request
from pathlib import Path

# ============================================================
# CLOUDFLARE KV STATE MANAGEMENT
# ============================================================

def kv_update_state(stream_id, status, file_size_mb=0, error=None, telegram_sent=False):
    """Update recording state in Cloudflare KV"""
    account_id = os.environ.get("CLOUDFLARE_ACCOUNT_ID", "")
    namespace_id = os.environ.get("CF_KV_NAMESPACE_ID", "")
    api_token = os.environ.get("CLOUDFLARE_API_TOKEN", "")
    
    if not stream_id or not all([account_id, namespace_id, api_token]):
        log("[KV] No stream_id or Cloudflare credentials, skipping state update")
        return False
    
    url = f"https://api.cloudflare.com/client/v4/accounts/{account_id}/storage/kv/namespaces/{namespace_id}/values/recording:{stream_id}"
    
    try:
        # Read existing state
        state = {}
        try:
            req = urllib.request.Request(url)
            req.add_header('Authorization', f'Bearer {api_token}')
            with urllib.request.urlopen(req, timeout=10) as response:
                state = json.loads(response.read().decode())
        except Exception as e:
            log(f"[KV] Could not read existing state: {e}")
        
        # Update state
        state['status'] = status
        if file_size_mb > 0:
            state['file_size_mb'] = file_size_mb
        if error:
            state['error'] = error
        state['telegram_sent'] = telegram_sent
        state['updated_at'] = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
        
        # Write state back
        req = urllib.request.Request(url, method='PUT')
        req.add_header('Authorization', f'Bearer {api_token}')
        req.add_header('Content-Type', 'application/json')
        with urllib.request.urlopen(req, data=json.dumps(state).encode(), timeout=10) as response:
            log(f"[KV] State updated: {stream_id} -> {status}")
            
        # Update active_count when finished/failed/stopped
        if status in ['finished', 'failed', 'stopped', 'completed']:
            kv_decrement_active_count(account_id, namespace_id, api_token)
            
        return True
    except Exception as e:
        log(f"[KV] Failed to update state: {e}")
        return False


def kv_decrement_active_count(account_id, namespace_id, api_token):
    """Decrement the active recordings counter"""
    try:
        url = f"https://api.cloudflare.com/client/v4/accounts/{account_id}/storage/kv/namespaces/{namespace_id}/values/active_count"
        
        # Read current count
        req = urllib.request.Request(url)
        req.add_header('Authorization', f'Bearer {api_token}')
        try:
            with urllib.request.urlopen(req, timeout=10) as response:
                count = int(response.read().decode())
        except:
            count = 0
        
        # Decrement and write back
        new_count = max(0, count - 1)
        req = urllib.request.Request(url, method='PUT')
        req.add_header('Authorization', f'Bearer {api_token}')
        req.add_header('Content-Type', 'text/plain')
        with urllib.request.urlopen(req, data=str(new_count).encode(), timeout=10) as response:
            log(f"[KV] Active count decremented: {count} -> {new_count}")
    except Exception as e:
        log(f"[KV] Failed to decrement active count: {e}")


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

STREAM_ID = os.environ.get("STREAM_ID", "")

GLOBAL_WATCHDOG_SECONDS = int(max(RECORD_DURATION_SECONDS + 1800, 2400))

USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

def log(msg=""):
    """Safe logging that handles BlockingIOError gracefully"""
    try:
        print(msg, flush=True)
    except BlockingIOError:
        time.sleep(0.1)
        try:
            print(msg, flush=True)
        except:
            pass

def log_section(t): log("\n" + "="*70 + f"\n{t}\n" + "="*70)

# ============================================================
# WEBRTC HOOK (Direct Binary Transfer via fetch POST)
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
    
    let uploadQueue = [];
    let isUploading = false;
    let uploadErrors = 0;

    async function processQueue() {
        if (isUploading) return;
        isUploading = true;
        while (uploadQueue.length > 0) {
            const buffer = uploadQueue.shift();
            try {
                const resp = await fetch('/__slr_chunk', {
                    method: 'POST',
                    body: buffer,
                    headers: { 'Content-Type': 'application/octet-stream' }
                });
                if (!resp.ok) {
                    uploadErrors++;
                    console.error('[JS] Upload HTTP error:', resp.status);
                }
            } catch(err) {
                uploadErrors++;
                console.error('[JS] Upload fetch error:', err);
            }
        }
        isUploading = false;
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
                if (chunkCount % 10 === 0) console.log(`[JS] Chunk #${chunkCount} | size: ${e.data.size} bytes | queue: ${uploadQueue.length} | errors: ${uploadErrors}`);
                e.data.arrayBuffer().then(buf => {
                    uploadQueue.push(buf);
                    processQueue();
                });
            }
        };
        recorder.onerror = (e) => { recorderError = e.error ? String(e.error) : "Error"; };
        recorder.start(ts);
        return { state: recorder.state, mimeType };
    };

    window.__superliveWaitChunk = async (ms) => {
        const s = Date.now(); while (Date.now() - s < ms) { 
            if (chunkCount > 0) return { ok: true }; 
            await new Promise(r => setTimeout(r, 200)); 
        }
        return { ok: false, error: recorderError || "Timeout" };
    };
    
    window.__superliveGetStatus = () => ({
        queueLength: uploadQueue.length,
        isUploading,
        chunkCount,
        uploadErrors,
        recorderState: recorder ? recorder.state : 'none'
    });
    
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
    await context.add_init_script(WEBRTC_HOOK)
    
    page = await context.new_page()
    page.on("console", lambda m: log(f"[CONSOLE:{m.type}] {m.text}") if m.type in ["error", "warning", "log"] else None)
    
    # ============================================================
    # ROUTE INTERCEPTION: Direct binary transfer (Zero Encoding)
    # ============================================================
    TEMP_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = time.strftime('%Y%m%d_%H%M%S')
    webm_path = TEMP_DIR / f"rec_{timestamp}.webm"
    webm_file = open(webm_path, "wb")
    total_bytes = 0
    chunk_count = 0
    write_errors = 0
    is_recording_active = True
    
    async def handle_chunk(route, request):
        nonlocal total_bytes, chunk_count, write_errors
        if not is_recording_active:
            await route.fulfill(status=200, body=b"ok", content_type="text/plain")
            return
        try:
            body = request.post_data_buffer
            if body:
                webm_file.write(body)
                total_bytes += len(body)
                chunk_count += 1
        except Exception as e:
            write_errors += 1
            log(f"[WARN] Write error #{write_errors}: {e}")
        await route.fulfill(status=200, body=b"ok", content_type="text/plain")
    
    await page.route("**/__slr_chunk", handle_chunk)
    
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
        is_recording_active = False
        await page.unroute("**/__slr_chunk")
        webm_file.close()
        await page.screenshot(path=str(TEMP_DIR / "timeout_error.png"))
        kv_update_state(STREAM_ID, 'failed', error='Timed out waiting for WebRTC stream')
        raise RuntimeError("Timed out waiting for WebRTC stream.")

    log("[3/5] Preparing MediaStream & Starting Recorder...")
    prep = await safe_eval(page, "window.__superlivePrepare()")
    log(f"[✓] Linked Video: {prep['video']} | Linked Audio: {prep['audio']}")
    
    rec_info = await safe_eval(page, f"window.__superliveStartRec({VIDEO_BITRATE}, {AUDIO_BITRATE}, 1000)")
    log(f"[✓] MediaRecorder started: state={rec_info.get('state')}, mimeType={rec_info.get('mimeType')}")
    
    wait_res = await safe_eval(page, f"window.__superliveWaitChunk({FIRST_CHUNK_TIMEOUT_SECONDS * 1000})")
    if not wait_res.get('ok'): 
        is_recording_active = False
        await page.unroute("**/__slr_chunk")
        webm_file.close()
        kv_update_state(STREAM_ID, 'failed', error=f"Recorder failed to produce data: {wait_res.get('error')}")
        raise RuntimeError(f"Recorder failed to produce data: {wait_res.get('error')}")
    log("[✓] MediaRecorder is generating data! (Direct Binary Transfer Active)")

    log(f"[4/5] Capturing WebM for {RECORD_DURATION_SECONDS}s (Zero Encoding Overhead)...")
    start_rec = time.monotonic()
    
    while time.monotonic() - start_rec < RECORD_DURATION_SECONDS:
        await asyncio.sleep(1)
        elapsed = int(time.monotonic() - start_rec)
        if elapsed % 15 == 0 and elapsed > 0:
            size_mb = total_bytes / 1024 / 1024
            log(f"[*] Progress: {elapsed}s | Chunks: {chunk_count} | Size: {size_mb:.2f} MB | Errors: {write_errors}")

    log("[*] Stopping Recorder & Flushing Queue...")
    await safe_eval(page, "window.__superliveStopRec()")
    
    # Wait for JS queue to empty completely
    for i in range(60):
        try:
            status = await safe_eval(page, "window.__superliveGetStatus()")
            if status['queueLength'] == 0 and not status['isUploading']:
                log(f"[✓] Queue flushed. Total JS chunks: {status['chunkCount']}, Upload errors: {status['uploadErrors']}")
                break
        except: pass
        await asyncio.sleep(0.5)
    
    is_recording_active = False
    await page.unroute("**/__slr_chunk")
    webm_file.close()
    
    final_size = webm_path.stat().st_size
    log(f"[✓] Capture Complete. File: {final_size / 1024 / 1024:.2f} MB | Python chunks: {chunk_count} | Write errors: {write_errors}")
    
    if final_size < 1024:
        kv_update_state(STREAM_ID, 'failed', error=f"WebM file too small ({final_size} bytes)")
        raise RuntimeError(f"WebM file too small ({final_size} bytes). Recording likely failed.")
    
    log("[*] Closing browser to allocate maximum resources for FFmpeg...")
    await context.close()
    await browser.close()
    
    # ============================================================
    # POST-PROCESSING (FFmpeg) - FIXED FOR VFR STUTTERING
    # ============================================================
    log("[5/5] Post-Processing: Converting WebM to MP4 Segments (CFR Mode)...")
    RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)
    out_pattern = str(RECORDINGS_DIR / f"rec_{timestamp}_%03d.mp4")
    
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "warning", "-stats",
        "-fflags", "+genpts", "-i", str(webm_path),
        "-map", "0:v:0", "-map", "0:a:0?",
        "-vf", "fps=30,pad=width=ceil(iw/2)*2:height=ceil(ih/2)*2:color=black",
        "-vsync", "cfr", "-r", "30",
        "-c:v", "libx264", "-preset", "superfast", "-crf", "23", "-pix_fmt", "yuv420p", "-threads", "0",
        "-c:a", "aac", "-b:a", str(AUDIO_BITRATE), "-ar", "44100",
        "-f", "segment", "-segment_time", str(SEGMENT_SECONDS), "-segment_format", "mp4",
        "-reset_timestamps", "1", "-movflags", "+faststart",
        out_pattern
    ]
    
    log("[*] Running FFmpeg conversion (Synchronous Mode)...")
    result = subprocess.run(cmd, capture_output=True, text=True)
    
    if result.stderr:
        lines = result.stderr.strip().split('\n')
        for line in lines[-15:]:
            if line.strip():
                log(f"[FFmpeg] {line}")
                
    mp4_files = list(sorted(RECORDINGS_DIR.glob(f"rec_{timestamp}_*.mp4")))
    
    if result.returncode != 0 and len(mp4_files) == 0:
        log(f"[ERROR] FFmpeg failed with exit code {result.returncode} and no MP4 files created")
        webm_path.unlink(missing_ok=True)
        kv_update_state(STREAM_ID, 'failed', error=f"FFmpeg failed with exit code {result.returncode}")
        raise RuntimeError("FFmpeg post-processing failed")
    elif result.returncode != 0:
        log(f"[WARN] FFmpeg exited with code {result.returncode} but {len(mp4_files)} MP4 file(s) were created")
        
    webm_path.unlink(missing_ok=True)
    
    if not mp4_files:
        kv_update_state(STREAM_ID, 'failed', error="No MP4 files produced")
        raise RuntimeError("No MP4 files produced.")
    
    for f in mp4_files:
        log(f"[✓] Saved: {f.name} ({f.stat().st_size / 1024 / 1024:.2f} MB)")
    
    # Update state to finished
    total_size = sum(f.stat().st_size for f in mp4_files) / 1024 / 1024
    kv_update_state(STREAM_ID, 'finished', file_size_mb=total_size)
        
    log(f"\n[✓✓✓] SUCCESS! {len(mp4_files)} MP4 file(s) created.")
    return mp4_files

async def main():
    log_section("SUPERLIVE RECORDER (DIRECT BINARY TRANSFER + KV STATE)")
    from playwright.async_api import async_playwright
    async with async_playwright() as p:
        return await asyncio.wait_for(run_recording(p), timeout=GLOBAL_WATCHDOG_SECONDS)

if __name__ == "__main__":
    try: asyncio.run(main())
    except Exception as e:
        log(f"\n[FATAL] {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()
        # Update state to failed
        kv_update_state(STREAM_ID, 'failed', error=str(e))
        sys.exit(1)
