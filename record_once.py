import asyncio
import base64
import json
import os
import signal
import time
from pathlib import Path

from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError


# ============================================================
# Configuration
# ============================================================

URL = os.environ.get(
    "RECORD_URL",
    "https://superlivetv.com/fr/livestream/150596097",
)

RECORDINGS_DIR = Path(
    os.environ.get("RECORDINGS_DIR", "recordings")
).resolve()

TEMP_DIR = Path(
    os.environ.get("TEMP_DIR", str(RECORDINGS_DIR / "_temp"))
).resolve()

SEGMENT_SECONDS = int(
    os.environ.get("SEGMENT_SECONDS", "60")
)

RECORD_DURATION_SECONDS = int(
    os.environ.get("RECORD_DURATION_SECONDS", "300")
)

VIDEO_BITRATE = int(
    os.environ.get("VIDEO_BITRATE", "3500000")
)

AUDIO_BITRATE = int(
    os.environ.get("AUDIO_BITRATE", "96000")
)

VIDEO_WAIT_SECONDS = int(
    os.environ.get("VIDEO_WAIT_SECONDS", "120")
)

PAGE_TIMEOUT_MS = int(
    os.environ.get("PAGE_TIMEOUT_MS", "60000")
)


RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)
TEMP_DIR.mkdir(parents=True, exist_ok=True)


# ============================================================
# Global stop flag
# ============================================================

STOP_REQUESTED = False


def request_stop(signum, frame):
    global STOP_REQUESTED
    STOP_REQUESTED = True
    print(f"[SIGNAL] Received signal {signum}. Stopping...", flush=True)


signal.signal(signal.SIGINT, request_stop)
signal.signal(signal.SIGTERM, request_stop)


# ============================================================
# Browser-side WebRTC / MediaRecorder hook
# ============================================================

WEBRTC_HOOK = r"""
(() => {
    if (window.__superliveRecorderInstalled) {
        return;
    }

    window.__superliveRecorderInstalled = true;

    const state = {
        peerConnections: [],
        tracks: [],
        streams: [],
        videos: [],
        audioElements: [],
        logs: []
    };

    window.__superliveState = state;

    function log(message) {
        try {
            state.logs.push({
                time: Date.now(),
                message: String(message)
            });

            if (state.logs.length > 200) {
                state.logs.shift();
            }
        } catch (_) {}
    }

    function rememberTrack(track) {
        if (!track) {
            return;
        }

        if (!state.tracks.includes(track)) {
            state.tracks.push(track);
            log(
                "track: " +
                track.kind +
                " id=" +
                track.id +
                " readyState=" +
                track.readyState
            );
        }
    }

    function rememberStream(stream) {
        if (!stream) {
            return;
        }

        if (!state.streams.includes(stream)) {
            state.streams.push(stream);
            log(
                "stream: " +
                stream.id +
                " video=" +
                stream.getVideoTracks().length +
                " audio=" +
                stream.getAudioTracks().length
            );
        }

        try {
            stream.getTracks().forEach(rememberTrack);
        } catch (_) {}
    }

    function rememberVideo(video) {
        if (!video) {
            return;
        }

        if (!state.videos.includes(video)) {
            state.videos.push(video);
            log("video element discovered");
        }

        try {
            if (video.srcObject) {
                rememberStream(video.srcObject);
            }
        } catch (_) {}
    }

    function rememberAudio(audio) {
        if (!audio) {
            return;
        }

        if (!state.audioElements.includes(audio)) {
            state.audioElements.push(audio);
        }

        try {
            if (audio.srcObject) {
                rememberStream(audio.srcObject);
            }
        } catch (_) {}
    }

    // --------------------------------------------------------
    // Intercept RTCPeerConnection
    // --------------------------------------------------------

    const OriginalRTCPeerConnection = window.RTCPeerConnection;

    if (OriginalRTCPeerConnection) {
        const WrappedRTCPeerConnection =
            class extends OriginalRTCPeerConnection {
                constructor(...args) {
                    super(...args);

                    try {
                        state.peerConnections.push(this);

                        this.addEventListener("track", (event) => {
                            try {
                                if (event.track) {
                                    rememberTrack(event.track);
                                }

                                if (event.streams) {
                                    event.streams.forEach(rememberStream);
                                }

                                log(
                                    "RTCPeerConnection track event: " +
                                    (event.track ? event.track.kind : "unknown")
                                );
                            } catch (_) {}
                        });

                        this.addEventListener("connectionstatechange", () => {
                            try {
                                log(
                                    "connectionState=" +
                                    this.connectionState
                                );
                            } catch (_) {}
                        });

                        this.addEventListener("iceconnectionstatechange", () => {
                            try {
                                log(
                                    "iceConnectionState=" +
                                    this.iceConnectionState
                                );
                            } catch (_) {}
                        });

                        log("RTCPeerConnection created");
                    } catch (_) {}
                }
            };

        try {
            window.RTCPeerConnection = WrappedRTCPeerConnection;
        } catch (_) {
            log("Could not replace RTCPeerConnection");
        }
    }


    // --------------------------------------------------------
    // Intercept HTMLMediaElement.srcObject
    // --------------------------------------------------------

    try {
        const mediaPrototype = HTMLMediaElement.prototype;

        const descriptor = Object.getOwnPropertyDescriptor(
            mediaPrototype,
            "srcObject"
        );

        if (descriptor && descriptor.set && descriptor.get) {
            Object.defineProperty(
                mediaPrototype,
                "srcObject",
                {
                    configurable: descriptor.configurable,
                    enumerable: descriptor.enumerable,

                    get: function() {
                        return descriptor.get.call(this);
                    },

                    set: function(value) {
                        try {
                            if (value) {
                                rememberStream(value);
                            }
                        } catch (_) {}

                        return descriptor.set.call(this, value);
                    }
                }
            );

            log("srcObject interception installed");
        }
    } catch (error) {
        log("srcObject hook error: " + error);
    }


    // --------------------------------------------------------
    // Periodic DOM scan
    // --------------------------------------------------------

    function scanMedia() {
        try {
            document.querySelectorAll("video").forEach((video) => {
                rememberVideo(video);
            });

            document.querySelectorAll("audio").forEach((audio) => {
                rememberAudio(audio);
            });
        } catch (_) {}
    }

    setInterval(scanMedia, 1000);

    try {
        scanMedia();
    } catch (_) {}


    // --------------------------------------------------------
    // State inspection
    // --------------------------------------------------------

    window.__superliveGetState = function() {
        const videos = [];

        try {
            state.videos.forEach((video) => {
                if (!video) {
                    return;
                }

                let srcObject = null;

                try {
                    srcObject = video.srcObject;
                } catch (_) {}

                let captureStreamAvailable = false;

                try {
                    captureStreamAvailable =
                        typeof video.captureStream === "function" ||
                        typeof video.mozCaptureStream === "function";
                } catch (_) {}

                videos.push({
                    readyState: video.readyState,
                    paused: video.paused,
                    ended: video.ended,
                    currentTime: video.currentTime,
                    width: video.videoWidth,
                    height: video.videoHeight,
                    muted: video.muted,
                    volume: video.volume,
                    hasSrcObject: !!srcObject,
                    captureStreamAvailable
                });
            });
        } catch (_) {}

        const tracks = [];

        try {
            state.tracks.forEach((track) => {
                if (!track) {
                    return;
                }

                tracks.push({
                    kind: track.kind,
                    id: track.id,
                    readyState: track.readyState,
                    muted: track.muted,
                    enabled: track.enabled
                });
            });
        } catch (_) {}

        return {
            videos,
            tracks,
            streamCount: state.streams.length,
            peerConnectionCount: state.peerConnections.length,
            logs: state.logs.slice(-50)
        };
    };


    // --------------------------------------------------------
    // Force video playback
    // --------------------------------------------------------

    window.__superliveForcePlay = async function() {
        const results = [];

        try {
            state.videos.forEach((video) => {
                try {
                    video.muted = false;
                } catch (_) {}

                try {
                    video.volume = 1;
                } catch (_) {}

                try {
                    video.click();
                } catch (_) {}

                try {
                    const promise = video.play();

                    if (promise && typeof promise.then === "function") {
                        promise
                            .then(() => {
                                results.push({
                                    ok: true,
                                    message: "play ok"
                                });
                            })
                            .catch((error) => {
                                results.push({
                                    ok: false,
                                    message: String(error)
                                });
                            });
                    }
                } catch (error) {
                    results.push({
                        ok: false,
                        message: String(error)
                    });
                }
            });
        } catch (error) {
            results.push({
                ok: false,
                message: String(error)
            });
        }

        await new Promise((resolve) => setTimeout(resolve, 300));

        return results;
    };


    // --------------------------------------------------------
    // Prepare recording MediaStream
    // --------------------------------------------------------

    window.__superlivePrepareRecordingStream = function() {
        let videoTrack = null;
        let audioTrack = null;

        // 1. Prefer tracks coming from actual video elements.
        for (const video of state.videos) {
            try {
                const src = video.srcObject;

                if (src) {
                    const candidates = src.getVideoTracks();

                    if (candidates.length > 0) {
                        videoTrack = candidates.find(
                            (track) =>
                                track.readyState === "live"
                        ) || candidates[0];
                    }

                    if (!audioTrack) {
                        const audios = src.getAudioTracks();

                        if (audios.length > 0) {
                            audioTrack = audios.find(
                                (track) =>
                                    track.readyState === "live"
                            ) || audios[0];
                        }
                    }
                }
            } catch (_) {}

            if (videoTrack) {
                break;
            }
        }


        // 2. Prefer remembered WebRTC streams.
        if (!videoTrack) {
            for (const stream of state.streams) {
                try {
                    const candidates = stream.getVideoTracks();

                    if (candidates.length > 0) {
                        videoTrack = candidates.find(
                            (track) =>
                                track.readyState === "live"
                        ) || candidates[0];

                        if (videoTrack) {
                            break;
                        }
                    }
                } catch (_) {}
            }
        }


        // 3. Use remembered video tracks.
        if (!videoTrack) {
            for (const track of state.tracks) {
                try {
                    if (
                        track.kind === "video" &&
                        track.readyState === "live"
                    ) {
                        videoTrack = track;
                        break;
                    }
                } catch (_) {}
            }
        }


        // 4. Search for audio in remembered streams.
        if (!audioTrack) {
            for (const stream of state.streams) {
                try {
                    const candidates = stream.getAudioTracks();

                    if (candidates.length > 0) {
                        audioTrack = candidates.find(
                            (track) =>
                                track.readyState === "live"
                        ) || candidates[0];

                        if (audioTrack) {
                            break;
                        }
                    }
                } catch (_) {}
            }
        }


        // 5. Search remembered audio tracks.
        if (!audioTrack) {
            for (const track of state.tracks) {
                try {
                    if (
                        track.kind === "audio" &&
                        track.readyState === "live"
                    ) {
                        audioTrack = track;
                        break;
                    }
                } catch (_) {}
            }
        }


        // 6. Last-resort fallback:
        // capture the rendered video element.
        if (!videoTrack) {
            for (const video of state.videos) {
                try {
                    let captured = null;

                    if (typeof video.captureStream === "function") {
                        captured = video.captureStream();
                    } else if (
                        typeof video.mozCaptureStream === "function"
                    ) {
                        captured = video.mozCaptureStream();
                    }

                    if (captured) {
                        const videos = captured.getVideoTracks();

                        if (videos.length > 0) {
                            videoTrack = videos[0];
                        }

                        if (!audioTrack) {
                            const audios = captured.getAudioTracks();

                            if (audios.length > 0) {
                                audioTrack = audios[0];
                            }
                        }
                    }
                } catch (_) {}
            }
        }


        if (!videoTrack) {
            return {
                ok: false,
                reason: "No video track found"
            };
        }


        const outputStream = new MediaStream();

        try {
            outputStream.addTrack(videoTrack);
        } catch (_) {
            return {
                ok: false,
                reason: "Could not add video track"
            };
        }

        if (audioTrack) {
            try {
                outputStream.addTrack(audioTrack);
            } catch (_) {}
        }

        window.__superliveRecordingStream = outputStream;

        return {
            ok: true,
            hasVideo: outputStream.getVideoTracks().length > 0,
            hasAudio: outputStream.getAudioTracks().length > 0,
            videoTrackId: videoTrack.id,
            audioTrackId: audioTrack ? audioTrack.id : null
        };
    };


    // --------------------------------------------------------
    // MediaRecorder
    // --------------------------------------------------------

    window.__superliveRecorder = null;
    window.__superliveRecorderError = null;
    window.__superliveRecorderStarted = false;
    window.__superliveRecorderStopped = false;


    window.__superliveStartRecorder = function() {
        if (window.__superliveRecorder) {
            return {
                ok: true,
                alreadyStarted: true
            };
        }

        const stream = window.__superliveRecordingStream;

        if (!stream) {
            return {
                ok: false,
                reason: "Recording stream is missing"
            };
        }

        let recorder = null;

        const candidates = [
            "video/webm;codecs=vp9,opus",
            "video/webm;codecs=vp8,opus",
            "video/webm"
        ];

        for (const mime of candidates) {
            try {
                if (MediaRecorder.isTypeSupported(mime)) {
                    recorder = new MediaRecorder(
                        stream,
                        {
                            mimeType: mime,
                            videoBitsPerSecond: 4000000,
                            audioBitsPerSecond: 128000
                        }
                    );

                    break;
                }
            } catch (_) {}
        }

        if (!recorder) {
            try {
                recorder = new MediaRecorder(stream);
            } catch (error) {
                window.__superliveRecorderError = String(error);

                return {
                    ok: false,
                    reason: String(error)
                };
            }
        }


        const queue = [];

        window.__superliveRecorderQueue = queue;


        recorder.ondataavailable = (event) => {
            try {
                if (
                    event.data &&
                    event.data.size > 0
                ) {
                    queue.push(event.data);
                }
            } catch (error) {
                window.__superliveRecorderError = String(error);
            }
        };


        recorder.onerror = (event) => {
            try {
                window.__superliveRecorderError =
                    String(
                        event.error ||
                        event.message ||
                        "MediaRecorder error"
                    );
            } catch (_) {
                window.__superliveRecorderError =
                    "MediaRecorder error";
            }
        };


        recorder.onstop = () => {
            window.__superliveRecorderStopped = true;
        };


        try {
            recorder.start(1000);

            window.__superliveRecorder = recorder;
            window.__superliveRecorderStarted = true;

            return {
                ok: true,
                mimeType: recorder.mimeType
            };
        } catch (error) {
            window.__superliveRecorderError = String(error);

            return {
                ok: false,
                reason: String(error)
            };
        }
    };


    window.__superliveRecorderStatus = function() {
        const recorder = window.__superliveRecorder;

        return {
            exists: !!recorder,
            state: recorder ? recorder.state : null,
            queueLength: window.__superliveRecorderQueue
                ? window.__superliveRecorderQueue.length
                : 0,
            error: window.__superliveRecorderError,
            started: window.__superliveRecorderStarted,
            stopped: window.__superliveRecorderStopped
        };
    };


    window.__superliveTakeRecorderChunk = async function() {
        const queue = window.__superliveRecorderQueue;

        if (!queue || queue.length === 0) {
            return null;
        }

        const blob = queue.shift();

        try {
            const buffer = await blob.arrayBuffer();

            const bytes = new Uint8Array(buffer);

            let binary = "";

            const chunkSize = 0x8000;

            for (
                let offset = 0;
                offset < bytes.length;
                offset += chunkSize
            ) {
                const slice = bytes.subarray(
                    offset,
                    Math.min(
                        offset + chunkSize,
                        bytes.length
                    )
                );

                binary += String.fromCharCode(...slice);
            }

            return {
                base64: btoa(binary),
                size: blob.size,
                type: blob.type
            };
        } catch (error) {
            window.__superliveRecorderError = String(error);

            return null;
        }
    };


    window.__superliveStopRecorder = async function() {
        const recorder = window.__superliveRecorder;

        if (!recorder) {
            return {
                ok: false,
                reason: "Recorder does not exist"
            };
        }

        try {
            if (recorder.state !== "inactive") {
                recorder.stop();
            }
        } catch (error) {
            window.__superliveRecorderError = String(error);
        }

        await new Promise((resolve) => {
            setTimeout(resolve, 1500);
        });

        return {
            ok: true,
            state: recorder.state,
            error: window.__superliveRecorderError
        };
    };


    log("SuperLive recorder hook installed");
})();
"""


# ============================================================
# Python helpers
# ============================================================

def print_header():
    print("=" * 60)
    print("SUPERLIVE RECORDER")
    print("=" * 60)
    print(f"URL: {URL}")
    print(f"Output: {RECORDINGS_DIR}")
    print(f"Temp: {TEMP_DIR}")
    print(f"Segment: {SEGMENT_SECONDS}s")
    print(f"Duration limit: {RECORD_DURATION_SECONDS}s")
    print(f"Video bitrate: {VIDEO_BITRATE}")
    print(f"Audio bitrate: {AUDIO_BITRATE}")
    print("=" * 60, flush=True)


async def get_frame_state(frame):
    try:
        return await frame.evaluate(
            "() => window.__superliveGetState ? "
            "window.__superliveGetState() : null"
        )
    except Exception:
        return None


async def force_play(frame):
    try:
        return await frame.evaluate(
            "() => window.__superliveForcePlay "
            "? window.__superliveForcePlay() : []"
        )
    except Exception:
        return []


def score_state(state):
    if not state:
        return -1

    score = 0

    videos = state.get("videos", [])
    tracks = state.get("tracks", [])

    for video in videos:
        width = int(video.get("width") or 0)
        height = int(video.get("height") or 0)
        ready_state = int(video.get("readyState") or 0)
        current_time = float(video.get("currentTime") or 0)
        has_src = bool(video.get("hasSrcObject"))
        capture_available = bool(
            video.get("captureStreamAvailable")
        )

        if width > 0 and height > 0:
            score += 100

        if ready_state >= 2:
            score += 30

        if current_time > 0:
            score += 20

        if has_src:
            score += 100

        if capture_available:
            score += 20

    for track in tracks:
        if track.get("kind") == "video":
            score += 150

            if track.get("readyState") == "live":
                score += 100

        if track.get("kind") == "audio":
            score += 20

    score += int(state.get("streamCount") or 0) * 20
    score += int(state.get("peerConnectionCount") or 0) * 10

    return score


async def find_best_frame(page):
    best_frame = None
    best_state = None
    best_score = -1

    frames = page.frames

    for frame in frames:
        state = await get_frame_state(frame)

        current_score = score_state(state)

        if current_score > best_score:
            best_score = current_score
            best_frame = frame
            best_state = state

    return best_frame, best_state, best_score


async def wait_for_media(page):
    print(
        f"[2/6] Waiting for live WebRTC media "
        f"(up to {VIDEO_WAIT_SECONDS}s)...",
        flush=True,
    )

    started = time.monotonic()
    last_log = 0

    while not STOP_REQUESTED:
        elapsed = time.monotonic() - started

        frame, state, score = await find_best_frame(page)

        if elapsed - last_log >= 5:
            last_log = elapsed

            if state:
                videos = state.get("videos", [])
                tracks = state.get("tracks", [])

                print(
                    f"[MEDIA] t={int(elapsed)}s "
                    f"score={score} "
                    f"videos={len(videos)} "
                    f"tracks={len(tracks)} "
                    f"streams={state.get('streamCount', 0)} "
                    f"pcs={state.get('peerConnectionCount', 0)}",
                    flush=True,
                )

                if videos:
                    for index, video in enumerate(videos[:5]):
                        print(
                            f"[VIDEO {index}] "
                            f"{video}",
                            flush=True,
                        )

                video_tracks = [
                    track
                    for track in tracks
                    if track.get("kind") == "video"
                ]

                if video_tracks:
                    print(
                        f"[WEBRTC VIDEO TRACKS] "
                        f"{video_tracks}",
                        flush=True,
                    )

        if frame and state:
            videos = state.get("videos", [])
            tracks = state.get("tracks", [])

            has_live_video_track = any(
                track.get("kind") == "video"
                and track.get("readyState") == "live"
                for track in tracks
            )

            has_video_element = any(
                int(video.get("width") or 0) > 0
                and int(video.get("height") or 0) > 0
                for video in videos
            )

            has_capture_fallback = any(
                video.get("captureStreamAvailable")
                for video in videos
            )

            if (
                has_live_video_track
                or has_video_element
                or has_capture_fallback
            ):
                print(
                    "[MEDIA] Candidate media detected.",
                    flush=True,
                )

                await force_play(frame)

                await asyncio.sleep(1)

                refreshed = await get_frame_state(frame)

                if refreshed:
                    refreshed_tracks = refreshed.get(
                        "tracks",
                        []
                    )

                    refreshed_videos = refreshed.get(
                        "videos",
                        []
                    )

                    if (
                        any(
                            track.get("kind") == "video"
                            and track.get("readyState") == "live"
                            for track in refreshed_tracks
                        )
                        or any(
                            int(video.get("width") or 0) > 0
                            and int(video.get("height") or 0) > 0
                            for video in refreshed_videos
                        )
                        or any(
                            video.get("captureStreamAvailable")
                            for video in refreshed_videos
                        )
                    ):
                        return frame, refreshed

        if elapsed >= VIDEO_WAIT_SECONDS:
            break

        await asyncio.sleep(1)

    return None, None


async def prepare_recording_stream(frame):
    print(
        "[3/6] Preparing WebRTC recording stream...",
        flush=True,
    )

    result = await frame.evaluate(
        "() => window.__superlivePrepareRecordingStream "
        "? window.__superlivePrepareRecordingStream() "
        ": {ok:false, reason:'hook missing'}"
    )

    print(
        f"[STREAM] {result}",
        flush=True,
    )

    if not result or not result.get("ok"):
        return False

    return True


async def start_media_recorder(frame):
    print(
        "[4/6] Starting browser MediaRecorder...",
        flush=True,
    )

    result = await frame.evaluate(
        "() => window.__superliveStartRecorder "
        "? window.__superliveStartRecorder() "
        ": {ok:false, reason:'recorder hook missing'}"
    )

    print(
        f"[RECORDER] {result}",
        flush=True,
    )

    if not result or not result.get("ok"):
        return False

    return True


async def start_ffmpeg():
    TEMP_PATTERN = str(
        TEMP_DIR / "segment_%05d.mp4"
    )

    command = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "warning",

        "-fflags",
        "+genpts",

        "-analyzeduration",
        "10M",

        "-probesize",
        "10M",

        "-i",
        "pipe:0",

        "-map",
        "0:v:0",

        "-map",
        "0:a:0?",

        "-c:v",
        "libx264",

        "-preset",
        "veryfast",

        "-b:v",
        str(VIDEO_BITRATE),

        "-maxrate",
        str(VIDEO_BITRATE),

        "-bufsize",
        str(VIDEO_BITRATE * 2),

        "-pix_fmt",
        "yuv420p",

        "-g",
        "60",

        "-keyint_min",
        "60",

        "-sc_threshold",
        "0",

        "-c:a",
        "aac",

        "-b:a",
        str(AUDIO_BITRATE),

        "-movflags",
        "+faststart",

        "-force_key_frames",
        f"expr:gte(t,n_forced*{SEGMENT_SECONDS})",

        "-f",
        "segment",

        "-segment_time",
        str(SEGMENT_SECONDS),

        "-reset_timestamps",
        "1",

        "-segment_format",
        "mp4",

        TEMP_PATTERN,
    ]

    print(
        "[FFMPEG] Starting:",
        " ".join(command),
        flush=True,
    )

    process = await asyncio.create_subprocess_exec(
        *command,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )

    return process


async def feed_ffmpeg_from_browser(
    frame,
    ffmpeg_process,
    duration_seconds,
):
    print(
        "[5/6] Recording and feeding FFmpeg...",
        flush=True,
    )

    started = time.monotonic()
    last_status = 0

    while not STOP_REQUESTED:
        elapsed = time.monotonic() - started

        if elapsed >= duration_seconds:
            break

        status = await frame.evaluate(
            "() => window.__superliveRecorderStatus "
            "? window.__superliveRecorderStatus() "
            ": null"
        )

        if status and status.get("error"):
            print(
                f"[RECORDER ERROR] "
                f"{status.get('error')}",
                flush=True,
            )

        chunk = await frame.evaluate(
            "() => window.__superliveTakeRecorderChunk "
            "? window.__superliveTakeRecorderChunk() "
            ": null"
        )

        if chunk:
            encoded = chunk.get("base64")

            if encoded:
                data = base64.b64decode(encoded)

                try:
                    ffmpeg_process.stdin.write(data)
                    await ffmpeg_process.stdin.drain()
                except (BrokenPipeError, ConnectionResetError):
                    print(
                        "[FFMPEG] Broken pipe.",
                        flush=True,
                    )
                    break

        if elapsed - last_status >= 10:
            last_status = elapsed

            queue_length = (
                status.get("queueLength", 0)
                if status
                else 0
            )

            print(
                f"[RECORDING] "
                f"{int(elapsed)}/{duration_seconds}s "
                f"queue={queue_length}",
                flush=True,
            )

        await asyncio.sleep(0.15)

    print(
        "[RECORDER] Stopping browser recorder...",
        flush=True,
    )

    try:
        await frame.evaluate(
            "() => window.__superliveStopRecorder "
            "? window.__superliveStopRecorder() "
            ": null"
        )
    except Exception as error:
        print(
            f"[RECORDER] Stop error: {error}",
            flush=True,
        )

    # Give MediaRecorder time to produce the final blob.
    await asyncio.sleep(2)

    # Drain remaining chunks.
    drain_started = time.monotonic()

    while time.monotonic() - drain_started < 15:
        chunk = await frame.evaluate(
            "() => window.__superliveTakeRecorderChunk "
            "? window.__superliveTakeRecorderChunk() "
            ": null"
        )

        if not chunk:
            status = await frame.evaluate(
                "() => window.__superliveRecorderStatus "
                "? window.__superliveRecorderStatus() "
                ": null"
            )

            queue_length = (
                status.get("queueLength", 0)
                if status
                else 0
            )

            if queue_length == 0:
                break

            await asyncio.sleep(0.25)
            continue

        encoded = chunk.get("base64")

        if encoded:
            data = base64.b64decode(encoded)

            try:
                ffmpeg_process.stdin.write(data)
                await ffmpeg_process.stdin.drain()
            except (BrokenPipeError, ConnectionResetError):
                break

    try:
        ffmpeg_process.stdin.close()
        await ffmpeg_process.stdin.wait_closed()
    except Exception:
        pass

    print(
        "[FFMPEG] Input stream closed. Waiting for FFmpeg...",
        flush=True,
    )

    try:
        stderr_data = await asyncio.wait_for(
            ffmpeg_process.stderr.read(),
            timeout=60,
        )
    except asyncio.TimeoutError:
        print(
            "[FFMPEG] Timeout waiting for FFmpeg.",
            flush=True,
        )

        try:
            ffmpeg_process.kill()
        except Exception:
            pass

        await ffmpeg_process.wait()

        return False

    return_code = await ffmpeg_process.wait()

    stderr_text = stderr_data.decode(
        "utf-8",
        errors="replace",
    )

    if stderr_text.strip():
        print(
            "[FFMPEG STDERR]",
            flush=True,
        )
        print(
            stderr_text[-10000:],
            flush=True,
        )

    print(
        f"[FFMPEG] Exit code: {return_code}",
        flush=True,
    )

    return return_code == 0


async def validate_recordings():
    print(
        "[6/6] Validating MP4 recordings...",
        flush=True,
    )

    files = sorted(
        TEMP_DIR.glob("segment_*.mp4")
    )

    if not files:
        print(
            "[VALIDATION] No MP4 segments found.",
            flush=True,
        )
        return []

    valid_files = []

    for source in files:
        if not source.is_file():
            continue

        size = source.stat().st_size

        if size < 10000:
            print(
                f"[VALIDATION] Too small: "
                f"{source.name} ({size} bytes)",
                flush=True,
            )
            continue

        output = RECORDINGS_DIR / source.name

        try:
            probe = await asyncio.create_subprocess_exec(
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "stream=index,codec_type,codec_name,width,height,channels",
                "-show_entries",
                "format=duration,size,format_name",
                "-of",
                "json",
                str(source),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            stdout_data, stderr_data = await probe.communicate()

            if probe.returncode != 0:
                print(
                    f"[VALIDATION] ffprobe failed for "
                    f"{source.name}: "
                    f"{stderr_data.decode(errors='replace')}",
                    flush=True,
                )
                continue

            info = json.loads(
                stdout_data.decode(
                    "utf-8",
                    errors="replace",
                )
            )

            streams = info.get("streams", [])

            has_video = any(
                stream.get("codec_type") == "video"
                for stream in streams
            )

            if not has_video:
                print(
                    f"[VALIDATION] No video stream: "
                    f"{source.name}",
                    flush=True,
                )
                continue

            source.replace(output)

            final_size = output.stat().st_size

            print(
                f"[VALIDATION] OK: "
                f"{output.name} "
                f"{final_size / (1024 * 1024):.2f} MB",
                flush=True,
            )

            print(
                json.dumps(
                    info,
                    ensure_ascii=False,
                    indent=2,
                ),
                flush=True,
            )

            valid_files.append(output)

        except Exception as error:
            print(
                f"[VALIDATION] Error for "
                f"{source.name}: {error}",
                flush=True,
            )

    return valid_files


async def write_diagnostics(page):
    print(
        "[DIAGNOSTIC] Writing diagnostic files...",
        flush=True,
    )

    try:
        screenshot = RECORDINGS_DIR / "_diagnostic.png"
        await page.screenshot(
            path=str(screenshot),
            full_page=True,
        )
        print(
            f"[DIAGNOSTIC] Screenshot: {screenshot}",
            flush=True,
        )
    except Exception as error:
        print(
            f"[DIAGNOSTIC] Screenshot failed: {error}",
            flush=True,
        )

    try:
        html = RECORDINGS_DIR / "_diagnostic.html"
        content = await page.content()
        html.write_text(
            content,
            encoding="utf-8",
        )
        print(
            f"[DIAGNOSTIC] HTML: {html}",
            flush=True,
        )
    except Exception as error:
        print(
            f"[DIAGNOSTIC] HTML failed: {error}",
            flush=True,
        )

    try:
        json_path = RECORDINGS_DIR / "_diagnostic.json"

        diagnostic = {
            "url": page.url,
            "timestamp": time.time(),
            "frames": [],
        }

        for frame in page.frames:
            state = await get_frame_state(frame)

            diagnostic["frames"].append(
                {
                    "url": frame.url,
                    "state": state,
                }
            )

        json_path.write_text(
            json.dumps(
                diagnostic,
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

        print(
            f"[DIAGNOSTIC] JSON: {json_path}",
            flush=True,
        )

    except Exception as error:
        print(
            f"[DIAGNOSTIC] JSON failed: {error}",
            flush=True,
        )


# ============================================================
# Main
# ============================================================

async def main():
    print_header()

    # Clean temporary segments from previous failed runs.
    for old_file in TEMP_DIR.glob("segment_*.mp4"):
        try:
            old_file.unlink()
        except Exception:
            pass

    async with async_playwright() as playwright:

        print(
            "[1/6] Launching full Chromium...",
            flush=True,
        )

        browser = await playwright.chromium.launch(
            channel="chromium",
            headless=True,

            # Playwright Chromium normally adds --mute-audio.
            # Remove it because we want the live audio available.
            ignore_default_args=[
                "--mute-audio"
            ],

            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",

                "--autoplay-policy=no-user-gesture-required",

                "--use-fake-ui-for-media-stream",

                "--disable-background-timer-throttling",
                "--disable-backgrounding-occluded-windows",
                "--disable-renderer-backgrounding",

                "--disable-blink-features=AutomationControlled",

                "--window-size=1920,1080",
            ],
        )

        context = await browser.new_context(
            viewport={
                "width": 1920,
                "height": 1080,
            },

            locale="fr-FR",

            timezone_id="Africa/Casablanca",

            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) "
                "AppleWebKit/537.36 "
                "(KHTML, like Gecko) "
                "Chrome/140.0.0.0 "
                "Safari/537.36"
            ),

            permissions=[
                "camera",
                "microphone",
            ],
        )

        # IMPORTANT:
        # Install the hook before creating/navigating the page.
        await context.add_init_script(
            WEBRTC_HOOK
        )

        page = await context.new_page()

        page.set_default_timeout(
            PAGE_TIMEOUT_MS
        )

        page.on(
            "pageerror",
            lambda error: print(
                f"[PAGE ERROR] {error}",
                flush=True,
            ),
        )

        page.on(
            "console",
            lambda message: (
                print(
                    f"[CONSOLE:{message.type}] "
                    f"{message.text}",
                    flush=True,
                )
                if message.type in (
                    "error",
                    "warning",
                )
                else None
            ),
        )

        try:
            print(
                f"[NAVIGATION] Opening {URL}",
                flush=True,
            )

            try:
                await page.goto(
                    URL,
                    wait_until="domcontentloaded",
                    timeout=PAGE_TIMEOUT_MS,
                )
            except PlaywrightTimeoutError:
                print(
                    "[NAVIGATION] domcontentloaded "
                    "timeout; continuing.",
                    flush=True,
                )

            print(
                f"[NAVIGATION] Current URL: {page.url}",
                flush=True,
            )

            # Let the application initialize WebRTC.
            await asyncio.sleep(8)

            frame, state = await wait_for_media(
                page
            )

            if not frame:
                print(
                    "[FATAL] No usable WebRTC/media "
                    "source was detected.",
                    flush=True,
                )

                await write_diagnostics(page)

                await browser.close()

                return 1

            print(
                f"[MEDIA] Selected frame: {frame.url}",
                flush=True,
            )

            print(
                "[MEDIA STATE]",
                json.dumps(
                    state,
                    ensure_ascii=False,
                    indent=2,
                ),
                flush=True,
            )

            prepared = await prepare_recording_stream(
                frame
            )

            if not prepared:
                print(
                    "[FATAL] Could not prepare recording stream.",
                    flush=True,
                )

                await write_diagnostics(page)

                await browser.close()

                return 1

            recorder_started = await start_media_recorder(
                frame
            )

            if not recorder_started:
                print(
                    "[FATAL] MediaRecorder could not start.",
                    flush=True,
                )

                await write_diagnostics(page)

                await browser.close()

                return 1

            ffmpeg_process = await start_ffmpeg()

            ffmpeg_ok = await feed_ffmpeg_from_browser(
                frame,
                ffmpeg_process,
                RECORD_DURATION_SECONDS,
            )

            if not ffmpeg_ok:
                print(
                    "[FATAL] FFmpeg did not finish successfully.",
                    flush=True,
                )

                await write_diagnostics(page)

                await browser.close()

                return 1

            recordings = await validate_recordings()

            if not recordings:
                print(
                    "[FATAL] No valid MP4 recordings were produced.",
                    flush=True,
                )

                await write_diagnostics(page)

                await browser.close()

                return 1

            print("=" * 60)
            print("SUCCESS")
            print("=" * 60)

            for recording in recordings:
                size_mb = (
                    recording.stat().st_size
                    / (1024 * 1024)
                )

                print(
                    f"{recording.name}: "
                    f"{size_mb:.2f} MB",
                    flush=True,
                )

            print("=" * 60, flush=True)

            await browser.close()

            return 0

        except Exception as error:
            print(
                "=" * 60,
                flush=True,
            )
            print(
                "[FATAL EXCEPTION]",
                flush=True,
            )
            print(
                repr(error),
                flush=True,
            )
            print(
                "=" * 60,
                flush=True,
            )

            try:
                await write_diagnostics(page)
            except Exception:
                pass

            try:
                await browser.close()
            except Exception:
                pass

            return 1


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    raise SystemExit(exit_code)
