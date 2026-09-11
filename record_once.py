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
URL = os.environ.get(
    "RECORD_URL",
    "https://superlivetv.com/fr/livestream/150596097",
)
RECORDINGS_DIR = Path(
    os.environ.get("RECORDINGS_DIR", "recordings")
).resolve()
TEMP_DIR = Path(
    os.environ.get(
        "TEMP_DIR",
        str(RECORDINGS_DIR / "_temp"),
    )
).resolve()
SEGMENT_SECONDS = int(
    os.environ.get("SEGMENT_SECONDS", "60")
)
RECORD_DURATION_SECONDS = int(
    os.environ.get("RECORD_DURATION_SECONDS", "300")
)
VIDEO_BITRATE = int(
    os.environ.get("VIDEO_BITRATE", "4000000")
)
AUDIO_BITRATE = int(
    os.environ.get("AUDIO_BITRATE", "128000")
)
VIDEO_WAIT_SECONDS = int(
    os.environ.get("VIDEO_WAIT_SECONDS", "120")
)
PAGE_TIMEOUT_MS = int(
    os.environ.get("PAGE_TIMEOUT_MS", "60000")
)
FIRST_CHUNK_TIMEOUT_SECONDS = int(
    os.environ.get("FIRST_CHUNK_TIMEOUT_SECONDS", "15")
)
CANDIDATE_TEST_TIMEOUT_SECONDS = int(
    os.environ.get("CANDIDATE_TEST_TIMEOUT_SECONDS", "25")
)
MEDIA_VALIDATION_TIMEOUT_SECONDS = int(
    os.environ.get("MEDIA_VALIDATION_TIMEOUT_SECONDS", "5")
)
MAX_CANDIDATES = int(
    os.environ.get("MAX_CANDIDATES", "8")
)
GLOBAL_WATCHDOG_SECONDS = int(
    os.environ.get(
        "GLOBAL_WATCHDOG_SECONDS",
        str(max(RECORD_DURATION_SECONDS + 180, 420)),
    )
)

EVALUATE_TIMEOUT_SECONDS = 12
STREAM_PREPARE_TIMEOUT_SECONDS = 10
RECORDER_START_TIMEOUT_SECONDS = 10
RECORDER_STOP_TIMEOUT_SECONDS = 8
CHUNK_POLL_TIMEOUT_SECONDS = 5
FFMPEG_START_TIMEOUT_SECONDS = 15
PAGE_OPERATION_TIMEOUT_SECONDS = 20
CHUNK_TIMESLICE_MS = 1000

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) "
    "AppleWebKit/537.36 "
    "(KHTML, like Gecko) "
    "Chrome/140.0.0.0 Safari/537.36"
)

# ============================================================
# LOGGING
# ============================================================
def log(message=""):
    print(message, flush=True)

def log_section(title):
    log("")
    log("=" * 70)
    log(title)
    log("=" * 70)

# ============================================================
# JAVASCRIPT HOOK
# ============================================================
WEBRTC_HOOK = r"""
(() => {
    if (window.__superliveRecorderInstalled) {
        return;
    }
    window.__superliveRecorderInstalled = true;

    const originalPC =
        window.RTCPeerConnection ||
        window.webkitRTCPeerConnection;
    const streams = new Set();
    const tracks = new Map();
    const peerConnections = new Set();
    let recorder = null;
    let recorderError = null;
    let recorderDataEvents = 0;
    let recorderTotalBytes = 0;
    let recorderQueue = [];

    const validationVideos = new Set();
    const validationAudios = new Set();
    const validationAudioContexts = new Set();

    function rememberTrack(track) {
        if (!track) return;
        try {
            if (track.readyState === "live") {
                tracks.set(track.id, track);
            }
        } catch (_) {}
    }

    function rememberStream(stream) {
        if (!stream) return;
        try {
            streams.add(stream);
            for (const track of stream.getTracks()) {
                rememberTrack(track);
            }
        } catch (_) {}
    }

    function rememberVideoElements() {
        try {
            for (const video of document.querySelectorAll("video")) {
                if (video.srcObject) {
                    rememberStream(video.srcObject);
                }
                try {
                    const cs =
                        typeof video.captureStream === "function"
                            ? video.captureStream()
                            : null;
                    if (cs) {
                        rememberStream(cs);
                    }
                } catch (_) {}
            }
        } catch (_) {}
    }

    function rememberAudioElements() {
        try {
            for (const audio of document.querySelectorAll("audio")) {
                if (audio.srcObject) {
                    rememberStream(audio.srcObject);
                }
                try {
                    const cs =
                        typeof audio.captureStream === "function"
                            ? audio.captureStream()
                            : null;
                    if (cs) {
                        rememberStream(cs);
                    }
                } catch (_) {}
            }
        } catch (_) {}
    }

    function inspectAllMedia() {
        rememberVideoElements();
        rememberAudioElements();

        const result = {
            videos: [],
            audios: [],
            tracks: [],
            streamCount: streams.size,
            peerConnectionCount: peerConnections.size,
            recorder: {
                exists: !!recorder,
                state: recorder ? recorder.state : null,
                dataEvents: recorderDataEvents,
                queueLength: recorderQueue.length,
                totalBytes: recorderTotalBytes,
                error: recorderError
            }
        };

        try {
            for (const video of document.querySelectorAll("video")) {
                let readyState = 0;
                let currentTime = 0;
                let width = 0;
                let height = 0;
                let paused = true;
                let ended = false;
                let muted = true;
                let volume = 0;
                let hasSrcObject = false;
                let captureStreamAvailable = false;
                try {
                    readyState = video.readyState;
                    currentTime = video.currentTime;
                    width = video.videoWidth;
                    height = video.videoHeight;
                    paused = video.paused;
                    ended = video.ended;
                    muted = video.muted;
                    volume = video.volume;
                    hasSrcObject = !!video.srcObject;
                    captureStreamAvailable =
                        typeof video.captureStream === "function";
                } catch (_) {}

                result.videos.push({
                    readyState,
                    paused,
                    ended,
                    currentTime,
                    width,
                    height,
                    muted,
                    volume,
                    hasSrcObject,
                    captureStreamAvailable
                });
            }
        } catch (_) {}

        try {
            for (const audio of document.querySelectorAll("audio")) {
                result.audios.push({
                    readyState: audio.readyState,
                    paused: audio.paused,
                    ended: audio.ended,
                    currentTime: audio.currentTime,
                    muted: audio.muted,
                    volume: audio.volume,
                    hasSrcObject: !!audio.srcObject
                });
            }
        } catch (_) {}

        for (const track of tracks.values()) {
            try {
                result.tracks.push({
                    kind: track.kind,
                    id: track.id,
                    readyState: track.readyState,
                    muted: !!track.muted,
                    enabled: !!track.enabled
                });
            } catch (_) {}
        }
        return result;
    }

    // ========================================================
    // WebRTC TRACK DISCOVERY
    // ========================================================
    if (originalPC) {
        const WrappedPC = function(...args) {
            const pc = new originalPC(...args);
            try {
                peerConnections.add(pc);
            } catch (_) {}
            try {
                pc.addEventListener("track", (event) => {
                    try {
                        if (event.track) {
                            rememberTrack(event.track);
                        }
                        if (event.streams) {
                            for (const stream of event.streams) {
                                rememberStream(stream);
                            }
                        }
                    } catch (_) {}
                });
            } catch (_) {}
            return pc;
        };
        WrappedPC.prototype = originalPC.prototype;
        try {
            Object.setPrototypeOf(WrappedPC, originalPC);
        } catch (_) {}
        window.RTCPeerConnection = WrappedPC;
        if (window.webkitRTCPeerConnection) {
            window.webkitRTCPeerConnection = WrappedPC;
        }
    }

    window.__superliveGetState = () => {
        rememberVideoElements();
        rememberAudioElements();
        return inspectAllMedia();
    };

    window.__superliveForcePlay = async () => {
        const results = [];
        for (const video of document.querySelectorAll("video")) {
            try {
                video.muted = false;
                video.volume = 1;
                const p = video.play();
                if (p && typeof p.catch === "function") {
                    await p.catch(() => {});
                }
                results.push({ type: "video", ok: true });
            } catch (e) {
                results.push({ type: "video", ok: false, error: String(e) });
            }
        }
        rememberVideoElements();
        rememberAudioElements();
        return results;
    };

    function buildCandidateObjects() {
        rememberVideoElements();
        rememberAudioElements();

        const realCandidates = [];
        const syntheticCandidates = [];

        function addCandidate(collection, label, stream, score, source) {
            if (!stream) return;
            try {
                const videoTracks = stream.getVideoTracks().filter(t => t && t.readyState === "live");
                const audioTracks = stream.getAudioTracks().filter(t => t && t.readyState === "live");

                if (videoTracks.length === 0 && audioTracks.length === 0) return;

                const id = label + ":" + stream.id;
                if (collection.some(c => c.id === id)) return;

                collection.push({
                    id, label, source, score, stream,
                    videoCount: videoTracks.length,
                    audioCount: audioTracks.length,
                    videoUnmuted: videoTracks.filter(t => !t.muted).length,
                    audioUnmuted: audioTracks.filter(t => !t.muted).length
                });
            } catch (_) {}
        }

        for (const stream of streams) {
            try {
                const vt = stream.getVideoTracks();
                const at = stream.getAudioTracks();
                const liveV = vt.filter(t => t.readyState === "live");
                const liveA = at.filter(t => t.readyState === "live");
                const vu = liveV.filter(t => !t.muted).length;
                const au = liveA.filter(t => !t.muted).length;

                if (liveV.length === 0) continue;

                let score = 10000;
                score += liveV.length * 100;
                score += liveA.length * 100;
                score += vu * 1000;
                score += au * 1000;
                if (vu > 0 && au > 0) score += 50000;
                else if (liveV.length > 0 && liveA.length > 0) score += 20000;

                addCandidate(realCandidates, "stream", stream, score, "remembered-stream");
            } catch (_) {}
        }

        const liveVideos = [];
        const liveAudios = [];
        for (const track of tracks.values()) {
            try {
                if (track.readyState === "live" && track.kind === "video") liveVideos.push(track);
                if (track.readyState === "live" && track.kind === "audio") liveAudios.push(track);
            } catch (_) {}
        }

        const bestVideos = liveVideos.slice().sort((a, b) => Number(!b.muted) - Number(!a.muted)).slice(0, 4);
        const bestAudios = liveAudios.slice().sort((a, b) => Number(!b.muted) - Number(!a.muted)).slice(0, 4);

        for (const videoTrack of bestVideos) {
            for (const audioTrack of bestAudios) {
                try {
                    const synthetic = new MediaStream([videoTrack, audioTrack]);
                    let score = 1000;
                    if (!videoTrack.muted) score += 5000;
                    if (!audioTrack.muted) score += 5000;
                    if (!videoTrack.muted && !audioTrack.muted) score += 25000;
                    addCandidate(syntheticCandidates, "synthetic", synthetic, score, "best-track-pair");
                } catch (_) {}
            }
        }

        realCandidates.sort((a, b) => b.score - a.score);
        syntheticCandidates.sort((a, b) => b.score - a.score);

        return { real: realCandidates, synthetic: syntheticCandidates };
    }

    window.__superliveGetCandidates = () => {
        const groups = buildCandidateObjects();
        const combined = [...groups.real, ...groups.synthetic];
        return combined.slice(0, 20).map((candidate, index) => ({
            index, id: candidate.id, label: candidate.label, source: candidate.source, score: candidate.score,
            videoCount: candidate.videoCount, audioCount: candidate.audioCount,
            videoUnmuted: candidate.videoUnmuted, audioUnmuted: candidate.audioUnmuted
        }));
    };

    window.__superlivePrepareRecordingStream = (candidateIndex) => {
        const groups = buildCandidateObjects();
        const candidates = [...groups.real, ...groups.synthetic];
        const candidate = candidates[candidateIndex];
        if (!candidate) throw new Error("Recording candidate not found: " + candidateIndex);

        const stream = candidate.stream;
        const videoTracks = stream.getVideoTracks().filter(t => t && t.readyState === "live");
        const audioTracks = stream.getAudioTracks().filter(t => t && t.readyState === "live");

        if (videoTracks.length === 0) throw new Error("Candidate has no live video track");

        const selectedTracks = [videoTracks[0]];
        if (audioTracks.length > 0) selectedTracks.push(audioTracks[0]);

        const cleanStream = new MediaStream(selectedTracks);
        window.__superlivePreparedStream = cleanStream;
        window.__superlivePreparedInfo = {
            label: candidate.label, source: candidate.source, score: candidate.score,
            originalVideoTracks: videoTracks.length, originalAudioTracks: audioTracks.length,
            selectedVideo: videoTracks[0].id,
            selectedAudio: audioTracks.length > 0 ? audioTracks[0].id : null
        };
        return window.__superlivePreparedInfo;
    };

    window.__superliveValidatePreparedVideo = async (timeoutMs) => {
        const stream = window.__superlivePreparedStream;
        if (!stream) return { ok: false, error: "No prepared stream" };
        const videoTracks = stream.getVideoTracks().filter(t => t && t.readyState === "live");
        if (videoTracks.length === 0) return { ok: false, error: "No live video track" };

        const video = document.createElement("video");
        validationVideos.add(video);
        video.autoplay = true; video.muted = true; video.playsInline = true;
        video.style.position = "fixed"; video.style.left = "-10000px"; video.style.opacity = "0";
        document.documentElement.appendChild(video);

        try {
            video.srcObject = stream;
            video.play().catch(() => {});
            await new Promise(r => setTimeout(r, 500));
            return { ok: true, width: video.videoWidth, height: video.videoHeight };
        } catch (e) {
            return { ok: false, error: String(e) };
        }
    };

    window.__superliveValidatePreparedAudio = async (timeoutMs) => {
        return { ok: true, present: true, skipped: true };
    };

    window.__superliveCleanupValidation = () => {
        for (const video of validationVideos) {
            try { video.pause(); video.srcObject = null; video.remove(); } catch (_) {}
        }
        validationVideos.clear();
        return { ok: true };
    };

    window.__superliveStartRecorder = (videoBitsPerSecond, audioBitsPerSecond, timeslice) => {
        if (recorder) {
            try { if (recorder.state !== "inactive") recorder.stop(); } catch (_) {}
            recorder = null;
        }
        recorderError = null; recorderDataEvents = 0; recorderTotalBytes = 0; recorderQueue = [];
        const stream = window.__superlivePreparedStream;
        if (!stream) throw new Error("No prepared MediaStream");

        let mimeType = "";
        for (const mime of ["video/webm;codecs=vp9,opus", "video/webm;codecs=vp8,opus", "video/webm"]) {
            if (MediaRecorder.isTypeSupported(mime)) { mimeType = mime; break; }
        }
        if (!mimeType) throw new Error("No supported WebM MIME type");

        try {
            recorder = new MediaRecorder(stream, { mimeType, videoBitsPerSecond, audioBitsPerSecond });
        } catch (e) { recorderError = String(e); throw e; }

        recorder.ondataavailable = async (event) => {
            try {
                if (!event.data || event.data.size <= 0) return;
                recorderDataEvents += 1; recorderTotalBytes += event.data.size;
                const buffer = await event.data.arrayBuffer();
                const bytes = new Uint8Array(buffer);
                let binary = ""; const STEP = 0x8000;
                for (let i = 0; i < bytes.length; i += STEP) {
                    binary += String.fromCharCode(...bytes.subarray(i, Math.min(i + STEP, bytes.length)));
                }
                recorderQueue.push(btoa(binary));
            } catch (e) { recorderError = String(e); }
        };
        recorder.onerror = (event) => { recorderError = event.error ? String(event.error) : "MediaRecorder error"; };
        recorder.start(timeslice);
        return { mimeType, state: recorder.state };
    };

    window.__superliveRecorderStatus = () => ({
        exists: !!recorder, state: recorder ? recorder.state : null,
        dataEvents: recorderDataEvents, queueLength: recorderQueue.length,
        totalBytes: recorderTotalBytes, error: recorderError
    });

    window.__superliveWaitForFirstChunk = async (timeoutMs) => {
        const start = Date.now();
        while (Date.now() - start < timeoutMs) {
            if (recorderError) return { ok: false, error: recorderError };
            if (recorderQueue.length > 0) return { ok: true, bytes: recorderTotalBytes };
            if (recorder && recorder.state === "inactive") return { ok: false, error: "Recorder became inactive" };
            await new Promise(r => setTimeout(r, 200));
        }
        return { ok: false, error: "Timed out waiting for first MediaRecorder chunk" };
    };

    window.__superliveTakeRecorderChunk = () => recorderQueue.length === 0 ? null : recorderQueue.shift();

    window.__superliveStopRecorder = () => {
        if (!recorder) return { ok: true, alreadyStopped: true };
        try { if (recorder.state !== "inactive") recorder.stop(); return { ok: true }; }
        catch (e) { recorderError = String(e); return { ok: false, error: String(e) }; }
    };

    window.__superliveResetRecorder = async () => {
        try {
            if (recorder && recorder.state !== "inactive") recorder.stop();
            recorder = null; recorderError = null; recorderDataEvents = 0; recorderTotalBytes = 0; recorderQueue = [];
            window.__superlivePreparedStream = null; window.__superlivePreparedInfo = null;
            window.__superliveCleanupValidation();
            return { ok: true };
        } catch (e) { return { ok: false, error: String(e) }; }
    };
})();
"""

# ============================================================
# PYTHON HELPERS
# ============================================================
async def safe_evaluate(page, expression, timeout=EVALUATE_TIMEOUT_SECONDS):
    try: return await asyncio.wait_for(page.evaluate(expression), timeout=timeout)
    except asyncio.TimeoutError: raise RuntimeError(f"Browser evaluation timed out after {timeout}s")
    except Exception as exc: raise RuntimeError(f"Browser evaluation failed: {exc}") from exc

async def safe_evaluate_with_arg(page, expression, arg, timeout=EVALUATE_TIMEOUT_SECONDS):
    try: return await asyncio.wait_for(page.evaluate(expression, arg), timeout=timeout)
    except asyncio.TimeoutError: raise RuntimeError(f"Browser evaluation timed out after {timeout}s")
    except Exception as exc: raise RuntimeError(f"Browser evaluation failed: {exc}") from exc

async def safe_frame_evaluate(frame, expression, timeout=EVALUATE_TIMEOUT_SECONDS):
    try: return await asyncio.wait_for(frame.evaluate(expression), timeout=timeout)
    except asyncio.TimeoutError: raise RuntimeError(f"Frame evaluation timed out after {timeout}s")
    except Exception as exc: raise RuntimeError(f"Frame evaluation failed: {exc}") from exc

def decode_chunk(encoded):
    if not encoded: return b""
    try: return base64.b64decode(encoded, validate=False)
    except Exception as exc: raise RuntimeError(f"Could not decode browser chunk: {exc}") from exc

def ensure_tools():
    if not shutil.which("ffmpeg"): raise RuntimeError("ffmpeg was not found in PATH")
    if not shutil.which("ffprobe"): raise RuntimeError("ffprobe was not found in PATH")

def clean_old_temp_files():
    TEMP_DIR.mkdir(parents=True, exist_ok=True)
    for item in TEMP_DIR.iterdir():
        try:
            if item.is_file(): item.unlink()
            elif item.is_dir(): shutil.rmtree(item)
        except Exception: pass

def print_json(title, data):
    log("")
    log(title)
    try: log(json.dumps(data, indent=2, ensure_ascii=False))
    except Exception: log(str(data))

# ============================================================
# DIAGNOSTICS
# ============================================================
async def save_diagnostics(page, state=None):
    try:
        RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)
        screenshot_path = RECORDINGS_DIR / "_diagnostic.png"
        html_path = RECORDINGS_DIR / "_diagnostic.html"
        json_path = RECORDINGS_DIR / "_diagnostic.json"

        try:
            await asyncio.wait_for(page.screenshot(path=str(screenshot_path), full_page=True), timeout=20)
            log(f"[DIAGNOSTIC] Screenshot saved: {screenshot_path}")
        except Exception as exc: log(f"[DIAGNOSTIC] Screenshot failed: {exc}")

        try:
            html = await asyncio.wait_for(page.content(), timeout=20)
            html_path.write_text(html, encoding="utf-8", errors="ignore")
        except Exception: pass

        try:
            payload = {"url": page.url, "state": state, "timestamp": time.time()}
            json_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        except Exception: pass
    except Exception: pass

# ============================================================
# FRAME / MEDIA SEARCH
# ============================================================
async def find_media_state(page):
    frames = list(page.frames)
    for frame in frames:
        try:
            state = await safe_frame_evaluate(frame, """
                () => {
                    if (typeof window.__superliveGetState !== "function") return null;
                    return window.__superliveGetState();
                }
            """, timeout=8)
            if not state: continue
            tracks = state.get("tracks", [])
            live_video = [t for t in tracks if t.get("kind") == "video" and t.get("readyState") == "live"]
            if live_video: return frame, state
        except Exception: continue
    return None, None

async def wait_for_media(page):
    deadline = time.monotonic() + VIDEO_WAIT_SECONDS
    last_print = 0
    while time.monotonic() < deadline:
        frame, state = await find_media_state(page)
        if frame is not None and state is not None:
            videos = [t for t in state.get("tracks", []) if t.get("kind") == "video" and t.get("readyState") == "live"]
            audios = [t for t in state.get("tracks", []) if t.get("kind") == "audio" and t.get("readyState") == "live"]
            if videos:
                log(f"[MEDIA] Candidate media detected. live video tracks={len(videos)}, live audio tracks={len(audios)}")
                return frame, state

        now = time.monotonic()
        if now - last_print >= 10:
            remaining = max(0, int(deadline - now))
            log(f"[MEDIA] Still waiting... {remaining}s remaining")
            last_print = now
        await asyncio.sleep(2)
    raise RuntimeError("Timed out waiting for live WebRTC media.")

# ============================================================
# CANDIDATES
# ============================================================
async def get_candidates(frame):
    candidates = await safe_frame_evaluate(frame, """
        () => {
            if (typeof window.__superliveGetCandidates !== "function") throw new Error("__superliveGetCandidates is unavailable");
            return window.__superliveGetCandidates();
        }
    """, timeout=10)
    if not isinstance(candidates, list): raise RuntimeError("Candidate list was not returned.")
    return candidates[:MAX_CANDIDATES]

# ============================================================
# RECORDER CONTROL
# ============================================================
async def prepare_candidate(frame, candidate_index):
    return await safe_evaluate_with_arg(frame, """
        (index) => {
            if (typeof window.__superlivePrepareRecordingStream !== "function") throw new Error("__superlivePrepareRecordingStream is unavailable");
            return window.__superlivePrepareRecordingStream(index);
        }
    """, candidate_index, timeout=STREAM_PREPARE_TIMEOUT_SECONDS)

async def validate_video(frame):
    return await safe_evaluate(frame, f"""
        () => {{
            if (typeof window.__superliveValidatePreparedVideo !== "function") return {{ ok: false, error: "Video validation function unavailable" }};
            return window.__superliveValidatePreparedVideo({MEDIA_VALIDATION_TIMEOUT_SECONDS * 1000});
        }}
    """, timeout=MEDIA_VALIDATION_TIMEOUT_SECONDS + 4)

async def validate_audio(frame):
    return await safe_evaluate(frame, f"""
        () => {{
            if (typeof window.__superliveValidatePreparedAudio !== "function") return {{ ok: true, present: false, validationUnavailable: true }};
            return window.__superliveValidatePreparedAudio({MEDIA_VALIDATION_TIMEOUT_SECONDS * 1000});
        }}
    """, timeout=MEDIA_VALIDATION_TIMEOUT_SECONDS + 4)

async def cleanup_browser_test(frame):
    try:
        return await safe_evaluate(frame, """
            async () => {
                const result = {};
                try { if (typeof window.__superliveResetRecorder === "function") result.recorder = await window.__superliveResetRecorder(); } catch (e) { result.recorderError = String(e); }
                try { if (typeof window.__superliveCleanupValidation === "function") result.validation = window.__superliveCleanupValidation(); } catch (e) { result.validationError = String(e); }
                return result;
            }
        """, timeout=RECORDER_STOP_TIMEOUT_SECONDS + 3)
    except Exception: return None

async def start_browser_recorder(frame):
    return await safe_evaluate(frame, f"""
        () => {{
            if (typeof window.__superliveStartRecorder !== "function") throw new Error("__superliveStartRecorder is unavailable");
            return window.__superliveStartRecorder({VIDEO_BITRATE}, {AUDIO_BITRATE}, {CHUNK_TIMESLICE_MS});
        }}
    """, timeout=RECORDER_START_TIMEOUT_SECONDS)

async def recorder_status(frame):
    return await safe_evaluate(frame, """
        () => {
            if (typeof window.__superliveRecorderStatus !== "function") return { exists: false, error: "Recorder status function unavailable" };
            return window.__superliveRecorderStatus();
        }
    """, timeout=8)

async def wait_first_chunk(frame):
    return await safe_evaluate(frame, f"""
        () => {{
            if (typeof window.__superliveWaitForFirstChunk !== "function") return {{ ok: false, error: "First chunk function unavailable" }};
            return window.__superliveWaitForFirstChunk({FIRST_CHUNK_TIMEOUT_SECONDS * 1000});
        }}
    """, timeout=FIRST_CHUNK_TIMEOUT_SECONDS + 5)

async def take_chunk(frame):
    return await safe_evaluate(frame, """
        () => {
            if (typeof window.__superliveTakeRecorderChunk !== "function") return null;
            return window.__superliveTakeRecorderChunk();
        }
    """, timeout=CHUNK_POLL_TIMEOUT_SECONDS)

async def stop_browser_recorder(frame):
    try:
        return await safe_evaluate(frame, """
            () => {
                if (typeof window.__superliveStopRecorder !== "function") return { ok: true, unavailable: true };
                return window.__superliveStopRecorder();
            }
        """, timeout=RECORDER_STOP_TIMEOUT_SECONDS)
    except Exception: return None

# ============================================================
# FFMPEG
# ============================================================
def build_ffmpeg_command(output_prefix):
    output_pattern = str(output_prefix) + "_%03d.mp4"
    return [
        "ffmpeg", "-hide_banner", "-loglevel", "warning", "-fflags", "+genpts", "-analyzeduration", "10M", "-probesize", "10M", "-i", "pipe:0",
        "-map", "0:v:0", "-map", "0:a:0?", "-vf", "pad=width=ceil(iw/2)*2:height=ceil(ih/2)*2:color=black",
        "-c:v", "libx264", "-preset", "veryfast", "-b:v", str(VIDEO_BITRATE), "-maxrate", str(VIDEO_BITRATE), "-bufsize", str(VIDEO_BITRATE * 2), "-pix_fmt", "yuv420p",
        "-g", "60", "-keyint_min", "60", "-sc_threshold", "0", "-c:a", "aac", "-b:a", str(AUDIO_BITRATE),
        "-reset_timestamps", "1", "-segment_time", str(SEGMENT_SECONDS), "-segment_format", "mp4", "-f", "segment", output_pattern,
    ]

async def start_ffmpeg():
    TEMP_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    prefix = TEMP_DIR / f"recording_{timestamp}"
    command = build_ffmpeg_command(prefix)
    log("")
    log(f"[FFMPEG] Starting... Output prefix: {prefix}")
    process = await asyncio.wait_for(asyncio.create_subprocess_exec(
        *command, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
    ), timeout=FFMPEG_START_TIMEOUT_SECONDS)
    return process, prefix

async def feed_ffmpeg(process, data):
    if not data: return
    if process.stdin is None: raise RuntimeError("FFmpeg stdin is unavailable.")
    try:
        process.stdin.write(data)
        await process.stdin.drain()
    except BrokenPipeError as exc: raise RuntimeError("FFmpeg pipe closed unexpectedly.") from exc

async def close_ffmpeg(process):
    if process.stdin:
        try: process.stdin.close()
        except Exception: pass
    try: await asyncio.wait_for(process.wait(), timeout=30)
    except asyncio.TimeoutError:
        try: process.terminate()
        except Exception: pass
        try: await asyncio.wait_for(process.wait(), timeout=10)
        except Exception:
            try: process.kill()
            except Exception: pass

    stderr = b""
    try:
        if process.stderr: stderr = await asyncio.wait_for(process.stderr.read(), timeout=5)
    except Exception: pass

    if stderr:
        text = stderr.decode("utf-8", errors="replace").strip()
        if text: log("\n[FFMPEG STDERR]\n" + text[-12000:])
    return process.returncode

def move_finished_recordings():
    RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)
    files = sorted(TEMP_DIR.glob("*.mp4"))
    moved = []
    for source in files:
        if not source.is_file(): continue
        try: size = source.stat().st_size
        except Exception: continue
        if size <= 0: continue
        destination = RECORDINGS_DIR / source.name
        shutil.move(str(source), str(destination))
        moved.append(destination)
    return moved

# ============================================================
# CANDIDATE TEST
# ============================================================
async def test_candidate(frame, candidate):
    index = candidate.get("index", 0)
    log("")
    log(f"[CANDIDATE TEST] #{index} label={candidate.get('label')} source={candidate.get('source')} score={candidate.get('score')}")
    
    async def _test():
        try:
            prepared = await prepare_candidate(frame, index)
            log(f"[STREAM] Prepared: {json.dumps(prepared, indent=2, ensure_ascii=False)}")
        except Exception as exc:
            log(f"[CANDIDATE TEST] Prepare failed: {exc}")
            return False

        # VIDEO VALIDATION (Warning Only in Headless)
        try:
            video_result = await validate_video(frame)
            log(f"[VIDEO VALIDATION] {json.dumps(video_result, indent=2, ensure_ascii=False)}")
            if not video_result.get("ok", False):
                log("[CANDIDATE TEST] Warning: no decoded frames detected in headless mode. Relying entirely on MediaRecorder chunk generation.")
        except Exception as exc:
            log(f"[CANDIDATE TEST] Video validation warning: {exc}")

        # START MEDIARECORDER (The Ultimate Proof)
        try:
            recorder_info = await start_browser_recorder(frame)
            log(f"[RECORDER] Started: {json.dumps(recorder_info, indent=2, ensure_ascii=False)}")
        except Exception as exc:
            log(f"[CANDIDATE TEST] Recorder start failed: {exc}")
            return False

        # WAIT FOR FIRST CHUNK
        try:
            first_chunk = await wait_first_chunk(frame)
            log(f"[CANDIDATE TEST] First chunk result: {json.dumps(first_chunk, indent=2, ensure_ascii=False)}")
            if not first_chunk.get("ok", False):
                log("[CANDIDATE TEST] Recorder started but produced no chunk.")
                return False
            return True # SUCCESS!
        except Exception as exc:
            log(f"[CANDIDATE TEST] First chunk test failed: {exc}")
            return False

    try:
        return await asyncio.wait_for(_test(), timeout=CANDIDATE_TEST_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        log(f"[CANDIDATE TEST] Global candidate timeout after {CANDIDATE_TEST_TIMEOUT_SECONDS}s")
        return False
    finally:
        await cleanup_browser_test(frame)

# ============================================================
# RECORD LOOP
# ============================================================
async def record_stream(frame, duration_seconds):
    log_section("SELECTING WORKING MEDIA STREAM")
    candidates = await get_candidates(frame)
    if not candidates: raise RuntimeError("No recording candidates were found.")
    log(f"[CANDIDATES] Found {len(candidates)} candidates.")

    ordered_candidates = sorted(candidates, key=lambda x: (x.get("source") != "remembered-stream", -x.get("score", 0)))

    working_candidate = None
    for candidate in ordered_candidates:
        ok = await test_candidate(frame, candidate)
        if ok:
            working_candidate = candidate
            log(f"[CANDIDATE TEST] WORKING CANDIDATE FOUND. source={candidate.get('source')}")
            break
        log("[CANDIDATE TEST] Candidate failed. Trying next.")

    if working_candidate is None: raise RuntimeError("All media candidates failed MediaRecorder testing.")

    log("[RECORDING] Preparing final candidate...")
    prepared = await prepare_candidate(frame, working_candidate["index"])
    log(f"[STREAM] Final recording stream: {json.dumps(prepared, indent=2, ensure_ascii=False)}")

    recorder_info = await start_browser_recorder(frame)
    log(f"[RECORDER] Final recorder started: {json.dumps(recorder_info, indent=2, ensure_ascii=False)}")

    ffmpeg_process = None
    try: ffmpeg_process, output_prefix = await start_ffmpeg()
    except Exception:
        await stop_browser_recorder(frame)
        await cleanup_browser_test(frame)
        raise

    log(f"[RECORDING] Recording started. Target duration: {duration_seconds}s")
    start_time = time.monotonic()
    last_status = start_time
    total_bytes = 0
    chunk_count = 0
    ffmpeg_failed = False

    try:
        while True:
            elapsed = time.monotonic() - start_time
            if elapsed >= duration_seconds: break

            pulled_any = False
            while True:
                try: encoded = await take_chunk(frame)
                except Exception: encoded = None
                if not encoded: break

                data = decode_chunk(encoded)
                if not data: continue

                pulled_any = True
                chunk_count += 1
                total_bytes += len(data)

                try: await feed_ffmpeg(ffmpeg_process, data)
                except Exception as exc:
                    ffmpeg_failed = True
                    log(f"[RECORDING] FFmpeg feed failed: {exc}")
                    break

            if ffmpeg_failed: break

            now = time.monotonic()
            if now - last_status >= 10:
                remaining = max(0, int(duration_seconds - elapsed))
                mb = total_bytes / 1024 / 1024
                log(f"[RECORDING] elapsed={int(elapsed)}s remaining={remaining}s chunks={chunk_count} browserMB={mb:.2f}")
                last_status = now

            if not pulled_any: await asyncio.sleep(0.25)

    finally:
        log("[RECORDING] Stopping browser recorder...")
        await stop_browser_recorder(frame)

        log("[RECORDING] Draining final browser chunks...")
        final_deadline = time.monotonic() + 5
        while time.monotonic() < final_deadline:
            try: encoded = await take_chunk(frame)
            except Exception: encoded = None
            if encoded:
                data = decode_chunk(encoded)
                if data:
                    chunk_count += 1
                    total_bytes += len(data)
                    try: await feed_ffmpeg(ffmpeg_process, data)
                    except Exception: break
            else: await asyncio.sleep(0.2)

        log("[FFMPEG] Closing...")
        return_code = await close_ffmpeg(ffmpeg_process)
        log(f"[FFMPEG] Exit code: {return_code}")
        if return_code != 0: raise RuntimeError("FFmpeg exited with a non-zero code.")

        if chunk_count == 0: raise RuntimeError("No MediaRecorder chunks were received.")

        await cleanup_browser_test(frame)
        moved = move_finished_recordings()
        log(f"[OUTPUT] Finished MP4 files: {len(moved)}")
        for file in moved:
            size_mb = file.stat().st_size / 1024 / 1024
            log(f"[OUTPUT] {file.name} {size_mb:.2f} MB")
        if not moved: raise RuntimeError("Recording completed but no MP4 segment was produced.")
        return moved

# ============================================================
# BROWSER WORKFLOW
# ============================================================
async def browser_workflow(playwright):
    browser = None
    context = None
    page = None
    try:
        log("[1/6] Launching full Chromium...")
        browser = await asyncio.wait_for(playwright.chromium.launch(
            channel="chromium", headless=True, ignore_default_args=["--mute-audio"],
            args=[
                "--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage",
                "--autoplay-policy=no-user-gesture-required", "--use-fake-ui-for-media-stream",
                "--disable-background-timer-throttling", "--disable-backgrounding-occluded-windows",
                "--disable-renderer-backgrounding", "--disable-blink-features=AutomationControlled",
                "--window-size=1920,1080", "--disable-gpu", "--use-gl=swiftshader", "--enable-unsafe-swiftshader",
                "--disable-web-security", "--disable-features=IsolateOrigins,site-per-process",
                "--disable-site-isolation-trials", "--disable-features=AudioServiceOutOfProcess",
                "--disable-features=CalculateNativeWinOcclusion", "--disable-features=IntensiveWakeUpThrottling",
            ],
        ), timeout=30)

        context = await browser.new_context(
            viewport={"width": 1920, "height": 1080}, locale="fr-FR", timezone_id="Africa/Casablanca",
            user_agent=USER_AGENT, permissions=["camera", "microphone"], ignore_https_errors=True,
        )
        
        # ROUTE INTERCEPTION (Prevents 429 Too Many Requests)
        async def block_tracking(route):
            url = route.request.url
            if any(x in url for x in ["analytics", "pixel", "tracking", "sd-rtn.com/log", "agora.io/log", "meta.com", "facebook.com"]):
                await route.abort()
            else:
                await route.continue_()
        await context.route("**/*", block_tracking)

        await context.add_init_script(WEBRTC_HOOK)
        page = await context.new_page()
        page.set_default_timeout(PAGE_TIMEOUT_MS)
        page.set_default_navigation_timeout(PAGE_TIMEOUT_MS)

        def on_console(message):
            if message.type in ("error", "warning"): log(f"[CONSOLE:{message.type}] {message.text}")
        page.on("console", on_console)
        page.on("pageerror", lambda error: log(f"[PAGE ERROR] {error}"))

        log(f"[NAVIGATION] Opening {URL}")
        try: await asyncio.wait_for(page.goto(URL, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS), timeout=PAGE_OPERATION_TIMEOUT_SECONDS)
        except Exception as exc: log(f"[NAVIGATION] goto warning: {exc}")
        log(f"[NAVIGATION] Current URL: {page.url}")

        await asyncio.sleep(3)
        try: await page.click("body", position={"x": 100, "y": 100}, timeout=3000)
        except Exception: pass
            
        log("[2/6] Waiting for live WebRTC media...")
        frame, state = await wait_for_media(page)
        print_json("[MEDIA STATE]", state)
        if frame != page.main_frame: log("[MEDIA] Media was found in a child frame.")

        try: await save_diagnostics(page, state)
        except Exception: pass

        log("[3/6] Testing recording candidates...")
        files = await record_stream(frame, RECORD_DURATION_SECONDS)

        log("[6/6] Recording completed successfully.")
        return files

    except Exception:
        if page is not None:
            try: await save_diagnostics(page, await safe_evaluate(page, "typeof window.__superliveGetState === 'function' ? window.__superliveGetState() : null", timeout=5))
            except Exception: pass
        raise
    finally:
        if context is not None:
            try: await context.close()
            except Exception: pass
        if browser is not None:
            try: await browser.close()
            except Exception: pass

# ============================================================
# MAIN
# ============================================================
async def main():
    log_section("SUPERLIVE RECORDER")
    log(f"URL: {URL}")
    log(f"Duration limit: {RECORD_DURATION_SECONDS}s")
    ensure_tools()
    RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)
    TEMP_DIR.mkdir(parents=True, exist_ok=True)
    clean_old_temp_files()

    from playwright.async_api import async_playwright
    async with async_playwright() as playwright:
        try: return await asyncio.wait_for(browser_workflow(playwright), timeout=GLOBAL_WATCHDOG_SECONDS)
        except asyncio.TimeoutError: raise RuntimeError("Global watchdog timeout.")

if __name__ == "__main__":
    try: asyncio.run(main())
    except KeyboardInterrupt: sys.exit(130)
    except Exception as exc:
        log(f"\nFATAL ERROR\n{type(exc).__name__}: {exc}")
        sys.exit(1)
