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
        log(f"[KV] Read error: {type(e).__name__}: {e}")
        return None


def kv_delete_state(stream_id):
    account_id = os.environ.get("CLOUDFLARE_ACCOUNT_ID", "")
    namespace_id = os.environ.get("CF_KV_NAMESPACE_ID", "")
    api_token = os.environ.get("CLOUDFLARE_API_TOKEN", "")
    
    if not stream_id or not all([account_id, namespace_id, api_token]):
        log("[KV] Missing credentials for cleanup")
        return False
    
    url = f"https://api.cloudflare.com/client/v4/accounts/{account_id}/storage/kv/namespaces/{namespace_id}/values/recording:{stream_id}"
    
    try:
        req = urllib.request.Request(url, method='DELETE')
        req.add_header('Authorization', f'Bearer {api_token}')
        with urllib.request.urlopen(req, timeout=10) as response:
            log(f"[KV] ✓ Deleted state for {stream_id}")
            return True
    except Exception as e:
        log(f"[KV] Delete failed: {type(e).__name__}: {e}")
        return False


def kv_update_state(stream_id, status, file_size_mb=0, error=None, telegram_sent=False):
    account_id = os.environ.get("CLOUDFLARE_ACCOUNT_ID", "")
    namespace_id = os.environ.get("CF_KV_NAMESPACE_ID", "")
    api_token = os.environ.get("CLOUDFLARE_API_TOKEN", "")
    
    if not stream_id or not all([account_id, namespace_id, api_token]):
        log("[KV] Missing credentials, skipping state update")
        return False
    
    url = f"https://api.cloudflare.com/client/v4/accounts/{account_id}/storage/kv/namespaces/{namespace_id}/values/recording:{stream_id}"
    
    try:
        state = {}
        try:
            req = urllib.request.Request(url)
            req.add_header('Authorization', f'Bearer {api_token}')
            with urllib.request.urlopen(req, timeout=10) as response:
                state = json.loads(response.read().decode())
        except Exception as e:
            log(f"[KV] Could not read existing state: {type(e).__name__}")
        
        state['status'] = status
        if file_size_mb > 0:
            state['file_size_mb'] = file_size_mb
        if error:
            state['error'] = error
        state['telegram_sent'] = telegram_sent
        state['updated_at'] = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
        
        req = urllib.request.Request(url, method='PUT')
        req.add_header('Authorization', f'Bearer {api_token}')
        req.add_header('Content-Type', 'application/json')
        with urllib.request.urlopen(req, data=json.dumps(state).encode(), timeout=10) as response:
            log(f"[KV] ✓ State updated: {stream_id} -> {status}")
        return True
    except Exception as e:
        log(f"[KV] ✗ Update failed: {type(e).__name__}: {e}")
        return False


def check_stop_requested(stream_id):
    if not stream_id or not WORKER_URL:
        return False
    
    try:
        url = f"{WORKER_URL.rstrip('/')}/api/check-stop/{stream_id}"
        req = urllib.request.Request(url)
        req.add_header('User-Agent', 'SuperLive-Recorder-Python')
        
        with urllib.request.urlopen(req, timeout=5) as response:
            data = json.loads(response.read().decode())
            should_stop = data.get('should_stop', False)
            status = data.get('status', 'unknown')
            
            if should_stop:
                log(f"[STOP-CHECK] ✓✓✓ STOP DETECTED! Status = '{status}'")
                return True
            return False
            
    except Exception as e:
        log(f"[STOP-CHECK] API error: {type(e).__name__}: {e}")
        return False


# ============================================================
# TELEGRAM UPLOAD FUNCTIONS
# ============================================================

def merge_segments_to_target(segments, target_size_bytes, stream_id, output_dir):
    merged_files = []
    current_batch = []
    current_size = 0
    batch_number = 1
    
    log(f"[MERGE] Starting smart merge: {len(segments)} segments → ~{TELEGRAM_TARGET_SIZE_MB} MB each")
    
    for segment in segments:
        seg_size = segment.stat().st_size
        current_batch.append(segment)
        current_size += seg_size
        
        if current_size >= target_size_bytes and len(current_batch) > 0:
            merged_name = f"stream_{stream_id}_part{batch_number:02d}.mp4"
            merged_path = output_dir / merged_name
            
            if len(current_batch) == 1:
                current_batch[0].rename(merged_path)
                log(f"[MERGE] ✓ Part {batch_number}: Single segment → {merged_path.name} ({current_size/1024/1024:.2f} MB)")
            else:
                concat_list = output_dir / f"concat_list_{batch_number}.txt"
                with open(concat_list, 'w') as f:
                    for seg in current_batch:
                        abs_path = seg.resolve().as_posix()
                        f.write(f"file '{abs_path}'\n")
                
                cmd = [
                    "ffmpeg", "-hide_banner", "-loglevel", "error",
                    "-f", "concat", "-safe", "0",
                    "-i", str(concat_list),
                    "-c", "copy",
                    "-movflags", "+faststart",
                    str(merged_path)
                ]
                
                result = subprocess.run(cmd, capture_output=True, text=True)
                
                if result.returncode == 0:
                    for seg in current_batch:
                        seg.unlink()
                    concat_list.unlink()
                    log(f"[MERGE] ✓ Part {batch_number}: Merged {len(current_batch)} segments → {merged_path.name} ({current_size/1024/1024:.2f} MB)")
                else:
                    log(f"[MERGE] ✗ Merge failed: {result.stderr[:200]}")
                    merged_files.extend(current_batch)
                    current_batch = []
                    current_size = 0
                    continue
            
            merged_files.append(merged_path)
            batch_number += 1
            current_batch = []
            current_size = 0
    
    if current_batch:
        merged_name = f"stream_{stream_id}_part{batch_number:02d}.mp4"
        merged_path = output_dir / merged_name
        
        if len(current_batch) == 1:
            current_batch[0].rename(merged_path)
            log(f"[MERGE] ✓ Part {batch_number}: Single segment → {merged_path.name} ({current_size/1024/1024:.2f} MB)")
            merged_files.append(merged_path)
        else:
            concat_list = output_dir / f"concat_list_{batch_number}.txt"
            with open(concat_list, 'w') as f:
                for seg in current_batch:
                    abs_path = seg.resolve().as_posix()
                    f.write(f"file '{abs_path}'\n")
            
            cmd = [
                "ffmpeg", "-hide_banner", "-loglevel", "error",
                "-f", "concat", "-safe", "0",
                "-i", str(concat_list),
                "-c", "copy",
                "-movflags", "+faststart",
                str(merged_path)
            ]
            
            result = subprocess.run(cmd, capture_output=True, text=True)
            
            if result.returncode == 0:
                for seg in current_batch:
                    seg.unlink()
                concat_list.unlink()
                log(f"[MERGE] ✓ Part {batch_number}: Merged {len(current_batch)} segments → {merged_path.name} ({current_size/1024/1024:.2f} MB)")
                merged_files.append(merged_path)
            else:
                log(f"[MERGE] ✗ Final merge failed, keeping segments")
                merged_files.extend(current_batch)
    
    log(f"[MERGE] Complete: {len(segments)} segments → {len(merged_files)} files")
    return merged_files


def split_oversized_files(files):
    final_files = []
    max_size_bytes = int(TELEGRAM_MAX_SIZE_MB * 1024 * 1024)
    
    for file_path in files:
        file_size = file_path.stat().st_size
        
        if file_size <= max_size_bytes:
            final_files.append(file_path)
        else:
            log(f"[SPLIT] {file_path.name} too large ({file_size/1024/1024:.2f} MB), splitting...")
            
            stem = file_path.stem
            parent = file_path.parent
            output_pattern = str(parent / f"{stem}_sub%02d.mp4")
            
            cmd = [
                "ffmpeg", "-hide_banner", "-loglevel", "error",
                "-i", str(file_path),
                "-c", "copy", "-map", "0",
                "-f", "segment",
                "-segment_size", str(max_size_bytes - 500000),
                "-reset_timestamps", "1",
                "-movflags", "+faststart",
                output_pattern
            ]
            
            result = subprocess.run(cmd, capture_output=True, text=True)
            
            if result.returncode == 0:
                parts = sorted(parent.glob(f"{stem}_sub*.mp4"))
                if parts:
                    final_files.extend(parts)
                    file_path.unlink()
                    log(f"[SPLIT] ✓ Created {len(parts)} sub-parts")
                else:
                    final_files.append(file_path)
            else:
                log(f"[SPLIT] ✗ Failed, keeping original")
                final_files.append(file_path)
    
    return final_files


def send_to_telegram(file_path, stream_id, part_number, total_parts):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log("[TG] ✗ No Telegram credentials!")
        return False
    
    file_size_mb = file_path.stat().st_size / (1024 * 1024)
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
        body.append(b'Content-Disposition: form-data; name="chat_id"')
        body.append(b'')
        body.append(TELEGRAM_CHAT_ID.encode())
        body.append(f"--{boundary}".encode())
        body.append(b'Content-Disposition: form-data; name="caption"')
        body.append(b'')
        body.append(caption.encode('utf-8'))
        body.append(f"--{boundary}".encode())
        body.append(b'Content-Disposition: form-data; name="parse_mode"')
        body.append(b'')
        body.append(b'HTML')
        body.append(f"--{boundary}".encode())
        body.append(f'Content-Disposition: form-data; name="video"; filename="{file_path.name}"'.encode())
        body.append(b'Content-Type: video/mp4')
        body.append(b'')
        body.append(file_data)
        body.append(f"--{boundary}--".encode())
        
        body_bytes = b'\r\n'.join(body)
        
        req = urllib.request.Request(url, data=body_bytes)
        req.add_header('Content-Type', f'multipart/form-data; boundary={boundary}')
        
        with urllib.request.urlopen(req, timeout=300) as response:
            result = json.loads(response.read().decode())
            if result.get('ok'):
                log(f"[TG] ✓ Uploaded {file_path.name} ({file_size_mb:.2f} MB)")
                return True
            else:
                log(f"[TG] ✗ API error: {result}")
                return False
                
    except Exception as e:
        log(f"[TG] ✗ Upload error: {type(e).__name__}: {e}")
        return False


def upload_all_to_telegram(mp4_files, stream_id):
    if not mp4_files:
        log("[TG] ✗ No files to upload!")
        return False
    
    total_original_size = sum(f.stat().st_size for f in mp4_files) / 1024 / 1024
    log(f"[TG] Processing {len(mp4_files)} files (total {total_original_size:.2f} MB)...")
    
    target_size_bytes = int(TELEGRAM_TARGET_SIZE_MB * 1024 * 1024)
    merged_files = merge_segments_to_target(mp4_files, target_size_bytes, stream_id, mp4_files[0].parent)
    
    final_files = split_oversized_files(merged_files)
    
    total_final_size = sum(f.stat().st_size for f in final_files) / 1024 / 1024
    log(f"[TG] Final: {len(final_files)} files (total {total_final_size:.2f} MB)")
    
    success_count = 0
    total_parts = len(final_files)
    
    for i, file_path in enumerate(final_files, 1):
        log(f"[TG] Uploading {i}/{total_parts}: {file_path.name}")
        if send_to_telegram(file_path, stream_id, i, total_parts):
            success_count += 1
        time.sleep(2)
    
    log(f"[TG] Upload result: {success_count}/{total_parts} files sent")
    return success_count > 0


# ============================================================
# CONFIGURATION
# ============================================================
URL = os.environ.get("RECORD_URL", "https://superlivetv.com/fr/livestream/150596097")
RECORDINGS_DIR = Path(os.environ.get("RECORDINGS_DIR", "recordings")).resolve()
TEMP_DIR = Path(os.environ.get("TEMP_DIR", str(RECORDINGS_DIR / "_temp"))).resolve()
SEGMENT_SECONDS = int(os.environ.get("SEGMENT_SECONDS", "60"))
VIDEO_BITRATE = int(os.environ.get("VIDEO_BITRATE", "8000000"))  # Increased from 4M to 8M
AUDIO_BITRATE = int(os.environ.get("AUDIO_BITRATE", "192000"))
VIDEO_WAIT_SECONDS = int(os.environ.get("VIDEO_WAIT_SECONDS", "120"))
PAGE_TIMEOUT_MS = int(os.environ.get("PAGE_TIMEOUT_MS", "60000"))
FIRST_CHUNK_TIMEOUT_SECONDS = int(os.environ.get("FIRST_CHUNK_TIMEOUT_SECONDS", "20"))

STREAM_ID = os.environ.get("STREAM_ID", "")

STOP_CHECK_INTERVAL = 3
STREAM_IDLE_TIMEOUT = 30
MIN_CHUNK_SIZE = 1000  # Minimum chunk size in bytes (1 KB)
MAX_RECORDING_SECONDS = 6 * 3600

GLOBAL_WATCHDOG_SECONDS = int(MAX_RECORDING_SECONDS + 3600)

USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

def log(msg=""):
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
# WEBRTC HOOK (IMPROVED: Track chunk sizes)
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
    let lastChunkSize = 0;  // NEW: Track last chunk size
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
                lastChunkSize = e.data.size;  // NEW: Track chunk size
                lastChunkTime = Date.now();
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
        recorderState: recorder ? recorder.state : 'none',
        idleTimeMs: Date.now() - lastChunkTime,
        lastChunkSize: lastChunkSize,  // NEW: Return last chunk size
        videoReadyState: (() => {
            const v = document.querySelector('video');
            return v ? v.readyState : 0;
        })()
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
    log("[1/6] Launching Chromium (headless=False via Xvfb)...")
    log(f"[DEBUG] STREAM_ID = '{STREAM_ID}'")
    log(f"[DEBUG] WORKER_URL = '{WORKER_URL}'")
    
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
    
    log(f"[2/6] Navigating to {URL}")
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
        kv_delete_state(STREAM_ID)
        raise RuntimeError("Timed out waiting for WebRTC stream.")

    log("[3/6] Preparing MediaStream & Starting Recorder...")
    prep = await safe_eval(page, "window.__superlivePrepare()")
    log(f"[✓] Linked Video: {prep['video']} | Linked Audio: {prep['audio']}")
    
    rec_info = await safe_eval(page, f"window.__superliveStartRec({VIDEO_BITRATE}, {AUDIO_BITRATE}, 1000)")
    log(f"[✓] MediaRecorder started: state={rec_info.get('state')}, mimeType={rec_info.get('mimeType')}")
    
    wait_res = await safe_eval(page, f"window.__superliveWaitChunk({FIRST_CHUNK_TIMEOUT_SECONDS * 1000})")
    if not wait_res.get('ok'): 
        is_recording_active = False
        await page.unroute("**/__slr_chunk")
        webm_file.close()
        kv_update_state(STREAM_ID, 'failed', error=f"Recorder failed: {wait_res.get('error')}")
        kv_delete_state(STREAM_ID)
        raise RuntimeError(f"Recorder failed: {wait_res.get('error')}")
    log("[✓] MediaRecorder is generating data!")

    log(f"[4/6] Recording continuously (max {MAX_RECORDING_SECONDS//3600}h)...")
    start_rec = time.monotonic()
    last_stop_check = time.monotonic()
    last_log_time = time.monotonic()
    stop_reason = "unknown"
    
    while time.monotonic() - start_rec < MAX_RECORDING_SECONDS:
        await asyncio.sleep(1)
        elapsed = time.monotonic() - start_rec
        
        # Check for manual stop request
        if time.monotonic() - last_stop_check >= STOP_CHECK_INTERVAL:
            last_stop_check = time.monotonic()
            try:
                if check_stop_requested(STREAM_ID):
                    log(f"[!] ✓✓✓ STOP REQUESTED at {int(elapsed)}s")
                    stop_reason = "stop_requested"
                    break
            except Exception as e:
                log(f"[WARN] Stop check error: {e}")
        
        # Log progress every 15 seconds
        if time.monotonic() - last_log_time >= 15:
            last_log_time = time.monotonic()
            size_mb = total_bytes / 1024 / 1024
            log(f"[*] Progress: {int(elapsed)}s | Chunks: {chunk_count} | Size: {size_mb:.2f} MB | Errors: {write_errors}")
        
        # IMPROVED: Check stream health with multiple conditions
        try:
            status = await safe_eval(page, "window.__superliveGetStatus()")
            idle_ms = status.get('idleTimeMs', 0)
            video_state = status.get('videoReadyState', 0)
            last_chunk_size = status.get('lastChunkSize', 0)
            
            # Condition 1: No chunks for 30 seconds
            if idle_ms > STREAM_IDLE_TIMEOUT * 1000:
                log(f"[!] Stream ended: No chunks for {idle_ms/1000:.1f}s")
                stop_reason = "stream_ended_idle"
                break
            
            # Condition 2: Video stopped (readyState < 2) after recording started
            if video_state < 2 and chunk_count > 5:
                log(f"[!] Stream ended: Video stopped (readyState={video_state})")
                stop_reason = "stream_ended_video"
                break
            
            # Condition 3: Last chunk too small (< 1KB) after recording started
            if last_chunk_size < MIN_CHUNK_SIZE and chunk_count > 5:
                log(f"[!] Stream ended: Last chunk too small ({last_chunk_size} bytes)")
                stop_reason = "stream_ended_small_chunk"
                break
                
        except Exception as e:
            log(f"[WARN] Status check failed: {e}")

    log(f"[*] Stopping recording (reason: {stop_reason})...")
    await safe_eval(page, "window.__superliveStopRec()")
    
    # Wait for queue to flush
    for i in range(60):
        try:
            status = await safe_eval(page, "window.__superliveGetStatus()")
            if status['queueLength'] == 0 and not status['isUploading']:
                log(f"[✓] Queue flushed. Total JS chunks: {status['chunkCount']}")
                break
        except: pass
        await asyncio.sleep(0.5)
    
    is_recording_active = False
    await page.unroute("**/__slr_chunk")
    webm_file.close()
    
    final_size = webm_path.stat().st_size
    log(f"[✓] Capture Complete. File: {final_size / 1024 / 1024:.2f} MB | Chunks: {chunk_count}")
    
    if final_size < 1024:
        kv_update_state(STREAM_ID, 'failed', error=f"WebM too small ({final_size} bytes)")
        kv_delete_state(STREAM_ID)
        raise RuntimeError(f"WebM too small ({final_size} bytes).")
    
    log("[*] Closing browser...")
    await context.close()
    await browser.close()
    
    # ============================================================
    # POST-PROCESSING (FFmpeg)
    # ============================================================
    log("[5/6] Converting WebM to MP4 (HIGH QUALITY)...")
    RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)
    out_pattern = str(RECORDINGS_DIR / f"seg_{timestamp}_%03d.mp4")
    
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "warning", "-stats",
        "-fflags", "+genpts", "-i", str(webm_path),
        "-map", "0:v:0", "-map", "0:a:0?",
        "-vf", "fps=30,pad=width=ceil(iw/2)*2:height=ceil(ih/2)*2:color=black",
        "-vsync", "cfr", "-r", "30",
        "-c:v", "libx264", "-preset", "medium", "-crf", "18", 
        "-pix_fmt", "yuv420p", "-threads", "0",
        "-tune", "film",
        "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
        "-f", "segment", "-segment_time", str(SEGMENT_SECONDS), "-segment_format", "mp4",
        "-reset_timestamps", "1", "-movflags", "+faststart",
        out_pattern
    ]
    
    result = subprocess.run(cmd, capture_output=True, text=True)
    
    if result.stderr:
        lines = result.stderr.strip().split('\n')
        for line in lines[-15:]:
            if line.strip():
                log(f"[FFmpeg] {line}")
                
    mp4_files = list(sorted(RECORDINGS_DIR.glob(f"seg_{timestamp}_*.mp4")))
    
    if result.returncode != 0 and len(mp4_files) == 0:
        log(f"[ERROR] FFmpeg failed: {result.returncode}")
        webm_path.unlink(missing_ok=True)
        kv_update_state(STREAM_ID, 'failed', error=f"FFmpeg failed")
        kv_delete_state(STREAM_ID)
        raise RuntimeError("FFmpeg failed")
        
    webm_path.unlink(missing_ok=True)
    
    if not mp4_files:
        kv_update_state(STREAM_ID, 'failed', error="No MP4 files")
        kv_delete_state(STREAM_ID)
        raise RuntimeError("No MP4 files")
    
    total_raw_size = sum(f.stat().st_size for f in mp4_files) / 1024 / 1024
    log(f"[✓] Created {len(mp4_files)} raw segments (total {total_raw_size:.2f} MB)")
    
    # ============================================================
    # UPLOAD TO TELEGRAM
    # ============================================================
    log("[6/6] Uploading to Telegram (Smart Merge)...")
    kv_update_state(STREAM_ID, 'uploading', file_size_mb=total_raw_size)
    
    upload_success = upload_all_to_telegram(mp4_files, STREAM_ID)
    
    kv_update_state(STREAM_ID, 'finished', file_size_mb=total_raw_size, telegram_sent=upload_success)
    
    if upload_success:
        kv_delete_state(STREAM_ID)
        
    log(f"\n[✓✓✓] SUCCESS! Telegram upload: {'SUCCESS' if upload_success else 'FAILED'}")
    return mp4_files

async def main():
    log_section("SUPERLIVE RECORDER (IMPROVED STREAM DETECTION)")
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
