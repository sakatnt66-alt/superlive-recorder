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
# TELEGRAM UPLOAD SETTINGS
# ============================================================
TELEGRAM_MAX_SIZE_MB = 44.9
TELEGRAM_TARGET_SIZE_MB = 42
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# ============================================================
# WORKER API SETTINGS
# ============================================================
WORKER_URL = os.environ.get("WORKER_URL", "")

# ============================================================
# CLOUDFLARE KV STATE MANAGEMENT
# ============================================================

def kv_get_state(stream_id):
    if not stream_id or not WORKER_URL:
        return None
    try:
        url = f"{WORKER_URL.rstrip('/')}/api/check-stop/{stream_id}"
        req = urllib.request.Request(url)
        req.add_header('User-Agent', 'SuperLive-Recorder-Python')
        with urllib.request.urlopen(req, timeout=5) as response:
            return json.loads(response.read().decode())
    except Exception as e:
        return None


def kv_delete_state(stream_id):
    if not stream_id or not WORKER_URL:
        return False
    url = f"{WORKER_URL.rstrip('/')}/api/delete-recording/{stream_id}"
    try:
        req = urllib.request.Request(url, method='DELETE')
        req.add_header('User-Agent', 'SuperLive-Recorder-Python')
        with urllib.request.urlopen(req, timeout=10) as response:
            result = json.loads(response.read().decode())
            return result.get('success', False)
    except:
        return False


def kv_update_state(stream_id, status, file_size_mb=0, error=None, telegram_sent=False):
    if not stream_id or not WORKER_URL:
        return False
    url = f"{WORKER_URL.rstrip('/')}/api/update-state/{stream_id}"
    state = kv_get_state(stream_id) or {}
    state['status'] = status
    if file_size_mb > 0:
        state['file_size_mb'] = file_size_mb
    if error:
        state['error'] = error
    state['telegram_sent'] = telegram_sent
    state['updated_at'] = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
    try:
        req = urllib.request.Request(url, method='POST', data=json.dumps(state).encode())
        req.add_header('Content-Type', 'application/json')
        req.add_header('User-Agent', 'SuperLive-Recorder-Python')
        with urllib.request.urlopen(req, timeout=10) as response:
            return json.loads(response.read().decode()).get('success', False)
    except:
        return False


def check_stop_requested(stream_id):
    data = kv_get_state(stream_id)
    if data and data.get('should_stop'):
        return True
    return False


# ============================================================
# TELEGRAM UPLOAD (IMPROVED WITH BETTER ERROR HANDLING)
# ============================================================

def send_to_telegram(file_path, stream_id, part_number, total_parts):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return False
    
    file_size_mb = file_path.stat().st_size / (1024 * 1024)
    if file_size_mb > TELEGRAM_MAX_SIZE_MB:
        log(f"[TG] ✗ File too large: {file_size_mb:.2f} MB")
        return False
    
    caption = (
        f"📺 <b>البث:</b> <code>{stream_id}</code>\n"
        f"📦 <b>الجزء:</b> {part_number}/{total_parts}\n"
        f"📊 <b>الحجم:</b> {file_size_mb:.2f} MB"
    )
    
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendVideo"
    
    try:
        boundary = f"----WebKitFormBoundary{int(time.time() * 1000)}"
        with open(file_path, 'rb') as f:
            file_data = f.read()
        
        body = []
        body.append(f"--{boundary}".encode())
        body.append(b'Content-Disposition: form-data; name="chat_id"\r\n\r\n')
        body.append(TELEGRAM_CHAT_ID.encode())
        body.append(f"\r\n--{boundary}".encode())
        body.append(b'Content-Disposition: form-data; name="caption"\r\n\r\n')
        body.append(caption.encode('utf-8'))
        body.append(f"\r\n--{boundary}".encode())
        body.append(b'Content-Disposition: form-data; name="parse_mode"\r\n\r\n')
        body.append(b'HTML')
        body.append(f"\r\n--{boundary}".encode())
        body.append(b'Content-Disposition: form-data; name="supports_streaming"\r\n\r\n')
        body.append(b'true')
        body.append(f"\r\n--{boundary}".encode())
        body.append(f'Content-Disposition: form-data; name="video"; filename="{file_path.name}"\r\n'.encode())
        body.append(b'Content-Type: video/mp4\r\n\r\n')
        body.append(file_data)
        body.append(f"\r\n--{boundary}--".encode())
        
        body_bytes = b''.join(body)
        
        req = urllib.request.Request(url, data=body_bytes)
        req.add_header('Content-Type', f'multipart/form-data; boundary={boundary}')
        
        try:
            with urllib.request.urlopen(req, timeout=300) as response:
                result = json.loads(response.read().decode())
                if result.get('ok'):
                    log(f"[TG] ✓ {file_path.name} ({file_size_mb:.2f} MB) [STREAMING]")
                    return True
                else:
                    log(f"[TG] ✗ Telegram API error: {result}")
        except urllib.error.HTTPError as e:
            error_body = e.read().decode('utf-8', errors='replace')
            log(f"[TG] ✗ HTTP {e.code}: {error_body[:300]}")
        except Exception as e:
            log(f"[TG] ✗ Error: {type(e).__name__}: {e}")
    
    return False


# ============================================================
# CONFIGURATION (OPTIMIZED FOR SPEED + COMPATIBILITY)
# ============================================================
URL = os.environ.get("RECORD_URL", "https://superlivetv.com/fr/livestream/150596097")
RECORDINGS_DIR = Path(os.environ.get("RECORDINGS_DIR", "recordings")).resolve()
TEMP_DIR = Path(os.environ.get("TEMP_DIR", str(RECORDINGS_DIR / "_temp"))).resolve()
VIDEO_BITRATE = int(os.environ.get("VIDEO_BITRATE", "8000000"))
AUDIO_BITRATE = int(os.environ.get("AUDIO_BITRATE", "192000"))
VIDEO_WAIT_SECONDS = int(os.environ.get("VIDEO_WAIT_SECONDS", "60"))
PAGE_TIMEOUT_MS = int(os.environ.get("PAGE_TIMEOUT_MS", "30000"))
FIRST_CHUNK_TIMEOUT_SECONDS = int(os.environ.get("FIRST_CHUNK_TIMEOUT_SECONDS", "10"))

STREAM_ID = os.environ.get("STREAM_ID", "")

STOP_CHECK_INTERVAL = 3
STREAM_IDLE_TIMEOUT = 20
MIN_CHUNK_SIZE = 500
MAX_RECORDING_SECONDS = 6 * 3600

GLOBAL_WATCHDOG_SECONDS = int(MAX_RECORDING_SECONDS + 1800)

USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

def log(msg=""):
    try:
        print(msg, flush=True)
    except:
        pass

def log_section(t): log("\n" + "="*70 + f"\n{t}\n" + "="*70)

# ============================================================
# WEBRTC HOOK
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
    let lastChunkTime = Date.now();
    let lastChunkSize = 0;
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
                if (!resp.ok) uploadErrors++;
            } catch(err) { uploadErrors++; }
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
                lastChunkSize = e.data.size;
                lastChunkTime = Date.now();
                if (chunkCount % 10 === 0) console.log(`[JS] Chunk #${chunkCount} | size: ${e.data.size} bytes`);
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
    
    window.__superliveGetStatus = () => {
        const v = document.querySelector('video');
        let videoTrackState = 'no_video';
        if (v && v.srcObject) {
            const vTracks = v.srcObject.getVideoTracks();
            videoTrackState = vTracks.length > 0 ? vTracks[0].readyState : 'no_track';
        }
        return {
            queueLength: uploadQueue.length,
            isUploading,
            chunkCount,
            idleTimeMs: Date.now() - lastChunkTime,
            lastChunkSize: lastChunkSize,
            videoReadyState: v ? v.readyState : 0,
            videoTrackState: videoTrackState
        };
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


def convert_webm_to_mp4(webm_path, mp4_path):
    """Convert WebM to MP4 with Telegram-compatible settings"""
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-fflags", "+genpts", "-i", str(webm_path),
        "-map", "0:v:0", "-map", "0:a:0?",
        "-vf", "fps=30,pad=width=ceil(iw/2)*2:height=ceil(ih/2)*2:color=black",
        "-vsync", "cfr", "-r", "30",
        # TELEGRAM-COMPATIBLE SETTINGS:
        "-c:v", "libx264", 
        "-preset", "fast",           # fast instead of veryfast (better compatibility)
        "-crf", "20", 
        "-profile:v", "main",        # Telegram-compatible profile
        "-level", "3.1",             # Standard level
        "-pix_fmt", "yuv420p", 
        "-threads", "0",
        "-bf", "0",                  # No B-frames (Telegram doesn't like them)
        "-g", "60",                  # GOP size = 2 seconds (good for streaming)
        "-keyint_min", "30",         # Minimum keyframe interval
        "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
        "-movflags", "+faststart",
        str(mp4_path)
    ]
    
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        log(f"[FFmpeg] ✗ Error: {result.stderr[:300]}")
        return False
    return True


# ============================================================
# WORKFLOW
# ============================================================
async def run_recording(playwright):
    log("[1/4] Launching Chromium...")
    log(f"[DEBUG] STREAM_ID = '{STREAM_ID}'")
    
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
    page.on("console", lambda m: log(f"[CONSOLE:{m.type}] {m.text}") if m.type in ["error", "warning"] else None)
    
    TEMP_DIR.mkdir(parents=True, exist_ok=True)
    RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = time.strftime('%Y%m%d_%H%M%S')
    webm_path = TEMP_DIR / f"rec_{timestamp}.webm"
    webm_file = open(webm_path, "wb")
    total_bytes = 0
    chunk_count = 0
    is_recording_active = True
    
    async def handle_chunk(route, request):
        nonlocal total_bytes, chunk_count
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
            log(f"[WARN] Write error: {e}")
        await route.fulfill(status=200, body=b"ok", content_type="text/plain")
    
    await page.route("**/__slr_chunk", handle_chunk)
    
    log(f"[2/4] Navigating to {URL}")
    start_navigate = time.monotonic()
    await page.goto(URL, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS)
    log(f"[✓] Page loaded in {time.monotonic() - start_navigate:.1f}s")
    
    log("[*] Waiting for WebRTC (timeout: 60s)...")
    start_wait = time.monotonic()
    stream_locked = False
    while time.monotonic() - start_wait < VIDEO_WAIT_SECONDS:
        try:
            info = await safe_eval(page, """() => {
                const v = document.querySelector('video');
                return {
                    hasVideo: !!(v && v.videoWidth > 0),
                    audioCount: (window.__superlive_audio_tracks || []).filter(t => t.readyState === 'live').length
                };
            }""")
            if info['hasVideo'] and info['audioCount'] > 0:
                log(f"[✓] WebRTC locked in {time.monotonic() - start_wait:.1f}s")
                stream_locked = True
                break
        except: pass
        await asyncio.sleep(1)
    
    if not stream_locked:
        webm_file.close()
        await page.unroute("**/__slr_chunk")
        kv_update_state(STREAM_ID, 'failed', error='WebRTC timeout')
        kv_delete_state(STREAM_ID)
        raise RuntimeError("WebRTC timeout")

    log("[3/4] Starting recorder...")
    prep = await safe_eval(page, "window.__superlivePrepare()")
    rec_info = await safe_eval(page, f"window.__superliveStartRec({VIDEO_BITRATE}, {AUDIO_BITRATE}, 1000)")
    log(f"[✓] Recorder started: {rec_info.get('mimeType')}")
    
    wait_res = await safe_eval(page, f"window.__superliveWaitChunk({FIRST_CHUNK_TIMEOUT_SECONDS * 1000})")
    if not wait_res.get('ok'): 
        webm_file.close()
        await page.unroute("**/__slr_chunk")
        kv_update_state(STREAM_ID, 'failed', error='No data')
        kv_delete_state(STREAM_ID)
        raise RuntimeError("No data from recorder")
    
    log("[✓] Recording active!")
    total_start = time.monotonic()
    last_stop_check = time.monotonic()
    last_progress_log = time.monotonic()
    stop_reason = "unknown"
    
    kv_update_state(STREAM_ID, 'recording')
    
    while time.monotonic() - total_start < MAX_RECORDING_SECONDS:
        await asyncio.sleep(1)
        elapsed = time.monotonic() - total_start
        
        if time.monotonic() - last_stop_check >= STOP_CHECK_INTERVAL:
            last_stop_check = time.monotonic()
            try:
                if check_stop_requested(STREAM_ID):
                    log(f"[!] STOP requested at {int(elapsed)}s")
                    stop_reason = "stop_requested"
                    break
            except: pass
        
        if time.monotonic() - last_progress_log >= 30:
            last_progress_log = time.monotonic()
            log(f"[*] Progress: {int(elapsed)}s | {chunk_count} chunks | {total_bytes/1024/1024:.2f} MB")
        
        try:
            status = await safe_eval(page, "window.__superliveGetStatus()")
            idle_ms = status.get('idleTimeMs', 0)
            video_track_state = status.get('videoTrackState', 'unknown')
            
            if video_track_state == 'ended' and chunk_count > 5:
                log(f"[!] Stream ended (track state = {video_track_state})")
                stop_reason = "stream_ended_track"
                break
            
            if idle_ms > STREAM_IDLE_TIMEOUT * 1000:
                log(f"[!] Stream ended (no data for {idle_ms/1000:.1f}s)")
                stop_reason = "stream_ended_idle"
                break
        except: pass

    log(f"[*] Stopping (reason: {stop_reason})...")
    await safe_eval(page, "window.__superliveStopRec()")
    
    for _ in range(30):
        try:
            status = await safe_eval(page, "window.__superliveGetStatus()")
            if status['queueLength'] == 0 and not status['isUploading']:
                break
        except: pass
        await asyncio.sleep(0.5)
    
    is_recording_active = False
    await page.unroute("**/__slr_chunk")
    webm_file.close()
    
    final_size = webm_path.stat().st_size
    log(f"[✓] Capture done: {final_size / 1024 / 1024:.2f} MB | {chunk_count} chunks")
    
    if final_size < 1024:
        kv_update_state(STREAM_ID, 'failed', error='Empty file')
        kv_delete_state(STREAM_ID)
        raise RuntimeError("Empty recording")
    
    log("[*] Closing browser...")
    await context.close()
    await browser.close()
    
    # ============================================================
    # FAST POST-PROCESSING WITH TELEGRAM-COMPATIBLE ENCODING
    # ============================================================
    log("[4/4] Converting + Uploading (FAST MODE + TELEGRAM COMPATIBLE)...")
    
    full_mp4_path = RECORDINGS_DIR / f"full_{timestamp}.mp4"
    
    log("[*] FFmpeg converting (fast preset, Telegram-compatible)...")
    convert_start = time.monotonic()
    success = convert_webm_to_mp4(webm_path, full_mp4_path)
    convert_time = time.monotonic() - convert_start
    
    webm_path.unlink(missing_ok=True)
    
    if not success or not full_mp4_path.exists():
        kv_update_state(STREAM_ID, 'failed', error='FFmpeg failed')
        kv_delete_state(STREAM_ID)
        raise RuntimeError("FFmpeg failed")
    
    total_size = full_mp4_path.stat().st_size / 1024 / 1024
    log(f"[✓] Converted in {convert_time:.1f}s: {total_size:.2f} MB")
    
    # Split if needed
    log("[*] Checking if split is needed...")
    target_size_bytes = int(TELEGRAM_TARGET_SIZE_MB * 1024 * 1024)
    
    if full_mp4_path.stat().st_size <= int(TELEGRAM_MAX_SIZE_MB * 1024 * 1024):
        final_files = [full_mp4_path]
        log(f"[✓] No split needed (under 45 MB)")
    else:
        log("[*] Splitting for Telegram...")
        split_start = time.monotonic()
        duration_cmd = ["ffprobe", "-v", "error", "-show_entries", "format=duration",
                       "-of", "default=noprint_wrappers=1:nokey=1", str(full_mp4_path)]
        result = subprocess.run(duration_cmd, capture_output=True, text=True)
        try:
            duration = float(result.stdout.strip())
        except:
            duration = 300
        
        num_parts = max(2, int(total_size / TELEGRAM_TARGET_SIZE_MB) + 1)
        segment_time = max(30, int(duration / num_parts))
        
        output_pattern = str(RECORDINGS_DIR / f"stream_{STREAM_ID}_part%02d.mp4")
        cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-i", str(full_mp4_path),
            "-c", "copy", "-map", "0",
            "-f", "segment",
            "-segment_time", str(segment_time),
            "-reset_timestamps", "1",
            "-movflags", "+faststart",
            output_pattern
        ]
        subprocess.run(cmd, capture_output=True, text=True)
        full_mp4_path.unlink()
        final_files = sorted(RECORDINGS_DIR.glob(f"stream_{STREAM_ID}_part*.mp4"))
        split_time = time.monotonic() - split_start
        log(f"[✓] Split into {len(final_files)} parts in {split_time:.1f}s")
    
    # Upload to Telegram
    log(f"[*] Uploading {len(final_files)} files to Telegram...")
    kv_update_state(STREAM_ID, 'uploading', file_size_mb=total_size)
    
    upload_start = time.monotonic()
    success_count = 0
    total_parts = len(final_files)
    
    for i, file_path in enumerate(final_files, 1):
        if send_to_telegram(file_path, STREAM_ID, i, total_parts):
            success_count += 1
            try:
                file_path.unlink()
            except: pass
        time.sleep(1)
    
    upload_time = time.monotonic() - upload_start
    log(f"[✓] Uploaded in {upload_time:.1f}s: {success_count}/{total_parts}")
    
    kv_update_state(STREAM_ID, 'finished', file_size_mb=total_size, telegram_sent=(success_count > 0))
    
    if success_count > 0:
        kv_delete_state(STREAM_ID)
    
    total_time = time.monotonic() - total_start
    log(f"\n[✓✓✓] DONE! Total: {total_time:.1f}s | Convert: {convert_time:.1f}s | Upload: {upload_time:.1f}s")
    return []


async def main():
    log_section("SUPERLIVE RECORDER (FAST + TELEGRAM COMPATIBLE)")
    from playwright.async_api import async_playwright
    async with async_playwright() as p:
        return await asyncio.wait_for(run_recording(p), timeout=GLOBAL_WATCHDOG_SECONDS)


if __name__ == "__main__":
    try: asyncio.run(main())
    except Exception as e:
        log(f"\n[FATAL] {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()
        kv_update_state(STREAM_ID, 'failed', error=str(e))
        kv_delete_state(STREAM_ID)
        sys.exit(1)
