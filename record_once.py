import asyncio
import base64
import json
import os
import signal
import sys
import time
from pathlib import Path

from playwright.async_api import async_playwright


# ============================================================
# Configuration
# ============================================================

RECORDINGS_DIR = Path(
    os.getenv("RECORDINGS_DIR", "./recordings")
).resolve()

TEMP_DIR = RECORDINGS_DIR / "_temp"

SEGMENT_SECONDS = int(
    os.getenv("SEGMENT_SECONDS", "60")
)

RECORD_DURATION_SECONDS = int(
    os.getenv("RECORD_DURATION_SECONDS", "0")
)

VIDEO_BITRATE = int(
    os.getenv("VIDEO_BITRATE", "4000000")
)

AUDIO_BITRATE = int(
    os.getenv("AUDIO_BITRATE", "128000")
)

PAGE_TIMEOUT_MS = int(
    os.getenv("PAGE_TIMEOUT_MS", "60000")
)

VIDEO_WAIT_SECONDS = int(
    os.getenv("VIDEO_WAIT_SECONDS", "120")
)

CHUNK_TRANSFER_TIMEOUT = int(
    os.getenv("CHUNK_TRANSFER_TIMEOUT", "30")
)


# ============================================================
# Global stop handling
# ============================================================

_stop_requested = False


def request_stop(signum=None, frame=None):
    global _stop_requested

    if not _stop_requested:
        print("")
        print("[STOP] Stop requested.")
        _stop_requested = True


signal.signal(signal.SIGINT, request_stop)

if hasattr(signal, "SIGTERM"):
    signal.signal(signal.SIGTERM, request_stop)


# ============================================================
# WebRTC / media hook
# ============================================================

WEBRTC_HOOK = r"""
(() => {
    if (window.__superliveHookInstalled) {
        return;
    }

    window.__superliveHookInstalled = true;

    window.__superliveState = {
        peerConnections: [],
        tracks: [],
        streams: [],
        videos: [],
        errors: [],
        logs: []
    };

    const state = window.__superliveState;

    function log(message) {
        try {
            state.logs.push(String(message));
            if (state.logs.length > 200) {
                state.logs.shift();
            }
        } catch (_) {}
    }

    function rememberTrack(track) {
        if (!track) return;

        try {
            if (!state.tracks.includes(track)) {
                state.tracks.push(track);
            }

            log(
                "TRACK " +
                track.kind +
                " id=" +
                track.id +
                " readyState=" +
                track.readyState +
                " enabled=" +
                track.enabled +
                " muted=" +
                track.muted
            );
        } catch (_) {}
    }

    function rememberStream(stream) {
        if (!stream) return;

        try {
            if (!state.streams.includes(stream)) {
                state.streams.push(stream);
            }

            for (const track of stream.getTracks()) {
                rememberTrack(track);
            }
        } catch (_) {}
    }

    // --------------------------------------------------------
    // RTCPeerConnection interception
    // --------------------------------------------------------

    const OriginalRTCPeerConnection =
        window.RTCPeerConnection ||
        window.webkitRTCPeerConnection;

    if (OriginalRTCPeerConnection) {

        class SuperLiveRTCPeerConnection
            extends OriginalRTCPeerConnection {

            constructor(...args) {
                super(...args);

                try {
                    state.peerConnections.push(this);
                } catch (_) {}

                const rememberEvent = (event) => {
                    try {
                        if (event && event.track) {
                            rememberTrack(event.track);
                        }

                        if (event && event.streams) {
                            for (const stream of event.streams) {
                                rememberStream(stream);
                            }
                        }

                        log(
                            "ONTRACK " +
                            (
                                event &&
                                event.track &&
                                event.track.kind
                            )
                        );
                    } catch (_) {}
                };

                try {
                    this.addEventListener(
                        "track",
                        rememberEvent
                    );
                } catch (_) {}

                try {
                    const originalOnTrack =
                        Object.getOwnPropertyDescriptor(
                            Object.getPrototypeOf(this),
                            "ontrack"
                        );

                    if (originalOnTrack) {
                        Object.defineProperty(
                            this,
                            "ontrack",
                            {
                                configurable: true,
                                get() {
                                    return originalOnTrack.get.call(this);
                                },
                                set(fn) {
                                    const wrapped = function(event) {
                                        rememberEvent(event);

                                        if (typeof fn === "function") {
                                            return fn.call(
                                                this,
                                                event
                                            );
                                        }
                                    };

                                    return originalOnTrack.set.call(
                                        this,
                                        wrapped
                                    );
                                }
                            }
                        );
                    }
                } catch (_) {}
            }
        }

        window.RTCPeerConnection =
            SuperLiveRTCPeerConnection;

        window.webkitRTCPeerConnection =
            SuperLiveRTCPeerConnection;
    }

    // --------------------------------------------------------
    // HTMLMediaElement.srcObject interception
    // --------------------------------------------------------

    try {
        const mediaPrototype =
            HTMLMediaElement.prototype;

        const srcObjectDescriptor =
            Object.getOwnPropertyDescriptor(
                mediaPrototype,
                "srcObject"
            );

        if (
            srcObjectDescriptor &&
            srcObjectDescriptor.set &&
            srcObjectDescriptor.get
        ) {
            Object.defineProperty(
                mediaPrototype,
                "srcObject",
                {
                    configurable: true,

                    get() {
                        return srcObjectDescriptor.get.call(this);
                    },

                    set(value) {
                        try {
                            if (value) {
                                rememberStream(value);
                            }
                        } catch (_) {}

                        return srcObjectDescriptor.set.call(
                            this,
                            value
                        );
                    }
                }
            );
        }
    } catch (_) {}

    // --------------------------------------------------------
    // Video discovery
    // --------------------------------------------------------

    function rememberVideo(video) {
        try {
            if (!state.videos.includes(video)) {
                state.videos.push(video);
            }

            if (video.srcObject) {
                rememberStream(video.srcObject);
            }
        } catch (_) {}
    }

    function scanVideos() {
        try {
            document
                .querySelectorAll("video")
                .forEach(rememberVideo);
        } catch (_) {}
    }

    try {
        const observer =
            new MutationObserver(() => {
                scanVideos();
            });

        observer.observe(
            document.documentElement || document,
            {
                subtree: true,
                childList: true
            }
        );
    } catch (_) {}

    setInterval(scanVideos, 1000);

    // --------------------------------------------------------
    // Exposed diagnostics
    // --------------------------------------------------------

    window.__superliveGetState = () => {
        const videos = [];

        try {
            document
                .querySelectorAll("video")
                .forEach((video) => {
                    let tracks = [];

                    try {
                        if (video.srcObject) {
                            tracks =
                                video.srcObject
                                    .getTracks()
                                    .map(t => ({
                                        kind: t.kind,
                                        id: t.id,
                                        readyState: t.readyState,
                                        enabled: t.enabled,
                                        muted: t.muted
                                    }));
                        }
                    } catch (_) {}

                    videos.push({
                        readyState: video.readyState,
                        paused: video.paused,
                        ended: video.ended,
                        muted: video.muted,
                        autoplay: video.autoplay,
                        width: video.videoWidth,
                        height: video.videoHeight,
                        currentTime: video.currentTime,
                        hasSrcObject: !!video.srcObject,
                        tracks
                    });
                });
        } catch (_) {}

        return {
            peerConnections: state.peerConnections.length,

            tracks: state.tracks.map(track => ({
                kind: track.kind,
                id: track.id,
                readyState: track.readyState,
                enabled: track.enabled,
                muted: track.muted
            })),

            streams: state.streams.length,

            videos,

            logs: state.logs.slice(-100),

            errors: state.errors.slice(-100)
        };
    };

    // --------------------------------------------------------
    // Find / prepare recording stream
    // --------------------------------------------------------

    window.__superlivePrepareRecordingStream = () => {

        scanVideos();

        let videoTrack = null;
        let audioTrack = null;

        // First: inspect all video elements.
        for (const video of state.videos) {

            try {
                if (video.srcObject) {

                    const tracks =
                        video.srcObject.getTracks();

                    for (const track of tracks) {

                        if (
                            track.kind === "video" &&
                            track.readyState === "live"
                        ) {
                            videoTrack = track;
                        }

                        if (
                            track.kind === "audio" &&
                            track.readyState === "live" &&
                            !track.muted
                        ) {
                            audioTrack = track;
                        }
                    }
                }
            } catch (_) {}
        }

        // Second: inspect remembered WebRTC tracks.
        if (!videoTrack) {
            videoTrack =
                state.tracks.find(
                    track =>
                        track.kind === "video" &&
                        track.readyState === "live"
                ) || null;
        }

        if (!audioTrack) {
            audioTrack =
                state.tracks.find(
                    track =>
                        track.kind === "audio" &&
                        track.readyState === "live" &&
                        !track.muted
                ) || null;
        }

        // Third: inspect all remembered streams.
        if (!videoTrack || !audioTrack) {

            for (const stream of state.streams) {

                try {
                    if (!videoTrack) {
                        videoTrack =
                            stream
                                .getVideoTracks()
                                .find(
                                    t =>
                                        t.readyState === "live"
                                ) || null;
                    }

                    if (!audioTrack) {
                        audioTrack =
                            stream
                                .getAudioTracks()
                                .find(
                                    t =>
                                        t.readyState === "live" &&
                                        !t.muted
                                ) || null;
                    }
                } catch (_) {}
            }
        }

        if (!videoTrack) {
            return {
                ok: false,
                reason: "No live video WebRTC track found."
            };
        }

        const stream =
            new MediaStream();

        stream.addTrack(videoTrack);

        if (audioTrack) {
            stream.addTrack(audioTrack);
        }

        window.__superliveRecordingStream = stream;

        return {
            ok: true,

            hasVideo: true,

            hasAudio: !!audioTrack,

            videoTrack: {
                id: videoTrack.id,
                readyState: videoTrack.readyState
            },

            audioTrack: audioTrack
                ? {
                    id: audioTrack.id,
                    readyState: audioTrack.readyState
                }
                : null
        };
    };


    // --------------------------------------------------------
    // Recorder
    // --------------------------------------------------------

    window.__superliveRecorder = null;

    window.__superliveRecorderQueue = [];

    window.__superliveRecorderDone = false;

    window.__superliveRecorderError = null;

    window.__superliveRecorderBytes = 0;

    window.__superliveStartRecorder = (videoBits, audioBits) => {

        const stream =
            window.__superliveRecordingStream;

        if (!stream) {
            throw new Error(
                "Recording stream does not exist."
            );
        }

        const mimeCandidates = [
            "video/webm;codecs=vp9,opus",
            "video/webm;codecs=vp8,opus",
            "video/webm"
        ];

        let mimeType = "";

        for (const candidate of mimeCandidates) {

            try {
                if (
                    MediaRecorder.isTypeSupported(
                        candidate
                    )
                ) {
                    mimeType = candidate;
                    break;
                }
            } catch (_) {}
        }

        const options = {
            videoBitsPerSecond: videoBits,
            audioBitsPerSecond: audioBits
        };

        if (mimeType) {
            options.mimeType = mimeType;
        }

        const recorder =
            new MediaRecorder(
                stream,
                options
            );

        window.__superliveRecorder =
            recorder;

        window.__superliveRecorderQueue = [];

        window.__superliveRecorderDone = false;

        window.__superliveRecorderError = null;

        window.__superliveRecorderBytes = 0;

        recorder.ondataavailable = (event) => {

            try {
                if (
                    event.data &&
                    event.data.size > 0
                ) {
                    window
                        .__superliveRecorderQueue
                        .push(event.data);

                    window
                        .__superliveRecorderBytes +=
                        event.data.size;
                }
            } catch (error) {

                window
                    .__superliveRecorderError =
                    String(error);
            }
        };

        recorder.onerror = (event) => {

            try {
                window
                    .__superliveRecorderError =
                    event.error
                        ? String(event.error)
                        : "MediaRecorder error";
            } catch (_) {
                window
                    .__superliveRecorderError =
                    "MediaRecorder error";
            }
        };

        recorder.onstop = () => {
            window.__superliveRecorderDone = true;
        };

        recorder.start(1000);

        return {
            mimeType:
                recorder.mimeType || mimeType,

            state:
                recorder.state
        };
    };


    window.__superliveTakeRecorderChunk = async () => {

        const queue =
            window.__superliveRecorderQueue;

        if (!queue || queue.length === 0) {
            return null;
        }

        const blob =
            queue.shift();

        if (!blob) {
            return null;
        }

        const buffer =
            await blob.arrayBuffer();

        const bytes =
            new Uint8Array(buffer);

        let binary = "";

        const step = 0x8000;

        for (
            let i = 0;
            i < bytes.length;
            i += step
        ) {
            binary += String.fromCharCode(
                ...bytes.subarray(
                    i,
                    Math.min(
                        i + step,
                        bytes.length
                    )
                )
            );
        }

        return {
            base64:
                btoa(binary),

            size:
                bytes.length
        };
    };


    window.__superliveRecorderStatus = () => {

        const recorder =
            window.__superliveRecorder;

        return {
            state:
                recorder
                    ? recorder.state
                    : "none",

            queued:
                window
                    .__superliveRecorderQueue
                    .length,

            bytes:
                window
                    .__superliveRecorderBytes,

            done:
                window
                    .__superliveRecorderDone,

            error:
                window
                    .__superliveRecorderError
        };
    };


    window.__superliveStopRecorder = () => {

        const recorder =
            window.__superliveRecorder;

        if (!recorder) {
            return false;
        }

        if (
            recorder.state === "recording"
        ) {

            try {
                recorder.requestData();
            } catch (_) {}

            setTimeout(() => {

                try {
                    if (
                        recorder.state !==
                        "inactive"
                    ) {
                        recorder.stop();
                    }
                } catch (error) {

                    window
                        .__superliveRecorderError =
                        String(error);

                    window
                        .__superliveRecorderDone =
                        true;
                }

            }, 500);

            return true;
        }

        return false;
    };

})();
"""


# ============================================================
# Browser helpers
# ============================================================

async def install_diagnostics(page):
    messages = []

    def on_console(msg):
        try:
            text = msg.text

            if len(text) > 1000:
                text = text[:1000]

            messages.append(
                f"[CONSOLE:{msg.type}] {text}"
            )

            if len(messages) > 200:
                messages.pop(0)

        except Exception:
            pass

    page.on("console", on_console)

    page.on(
        "pageerror",
        lambda error:
            messages.append(
                f"[PAGEERROR] {error}"
            )
    )

    return messages


async def get_all_frames(page):
    frames = []

    try:
        frames = page.frames
    except Exception:
        pass

    return frames


async def inspect_frame(frame):
    try:
        return await frame.evaluate(
            """
            () => {
                const videos =
                    Array.from(
                        document.querySelectorAll("video")
                    ).map(v => ({
                        readyState: v.readyState,
                        paused: v.paused,
                        ended: v.ended,
                        muted: v.muted,
                        autoplay: v.autoplay,
                        width: v.videoWidth,
                        height: v.videoHeight,
                        currentTime: v.currentTime,
                        hasSrcObject: !!v.srcObject,
                        src: v.currentSrc || v.src || "",
                        tracks: v.srcObject
                            ? v.srcObject.getTracks().map(t => ({
                                kind: t.kind,
                                id: t.id,
                                readyState: t.readyState,
                                enabled: t.enabled,
                                muted: t.muted
                            }))
                            : []
                    }));

                return {
                    url: location.href,
                    title: document.title,
                    videoCount: videos.length,
                    videos,
                    hookState:
                        window.__superliveGetState
                            ? window.__superliveGetState()
                            : null
                };
            }
            """
        )
    except Exception as exc:
        return {
            "error": str(exc)
        }


async def find_best_media_frame(page):
    """
    Search every frame for actual media.

    We deliberately do not require a <video> element to be
    already playable. A frame containing WebRTC tracks is enough.
    """

    best = None

    for frame in await get_all_frames(page):

        try:
            result = await inspect_frame(frame)

            if result.get("error"):
                continue

            score = 0

            if result.get("videoCount", 0) > 0:
                score += 10

            hook_state = result.get(
                "hookState"
            ) or {}

            tracks = hook_state.get(
                "tracks"
            ) or []

            if any(
                t.get("kind") == "video" and
                t.get("readyState") == "live"
                for t in tracks
            ):
                score += 100

            if any(
                t.get("kind") == "audio" and
                t.get("readyState") == "live"
                for t in tracks
            ):
                score += 50

            for video in result.get(
                "videos",
                []
            ):
                if video.get("width", 0) > 0:
                    score += 25

                if video.get("height", 0) > 0:
                    score += 25

                if video.get(
                    "hasSrcObject"
                ):
                    score += 30

            if best is None or score > best[0]:
                best = (
                    score,
                    frame,
                    result
                )

        except Exception:
            continue

    return best


async def force_media_play(frame):
    try:
        await frame.evaluate(
            """
            async () => {

                const videos =
                    Array.from(
                        document.querySelectorAll("video")
                    );

                for (const video of videos) {

                    try {
                        video.autoplay = true;
                        video.playsInline = true;

                        await video.play().catch(() => {});
                    } catch (_) {}
                }

                try {
                    document.body.click();
                } catch (_) {}

                return videos.length;
            }
            """
        )
    except Exception:
        pass


async def wait_for_media(page):
    print(
        f"[2/6] Waiting for live WebRTC media "
        f"(up to {VIDEO_WAIT_SECONDS}s)..."
    )

    deadline = (
        time.monotonic()
        + VIDEO_WAIT_SECONDS
    )

    last_report = 0

    while (
        time.monotonic() < deadline
        and not _stop_requested
    ):

        best = await find_best_media_frame(page)

        if best:

            score, frame, result = best

            try:
                await force_media_play(frame)
            except Exception:
                pass

            try:
                prepared =
                    await frame.evaluate(
                        """
                        () =>
                            window.__superlivePrepareRecordingStream
                                ? window.__superlivePrepareRecordingStream()
                                : {
                                    ok: false,
                                    reason:
                                        "WebRTC hook unavailable"
                                }
                        """
                    )

                if prepared.get("ok"):
                    print(
                        "[MEDIA] WebRTC recording "
                        "stream prepared."
                    )

                    print(
                        "[MEDIA] Video:",
                        prepared.get(
                            "videoTrack"
                        )
                    )

                    print(
                        "[MEDIA] Audio:",
                        prepared.get(
                            "audioTrack"
                        )
                    )

                    return frame

            except Exception as exc:
                if time.monotonic() - last_report > 5:
                    print(
                        "[MEDIA] Preparation error:",
                        exc
                    )
                    last_report = time.monotonic()

        await asyncio.sleep(2)

        if time.monotonic() - last_report > 10:

            print(
                "[MEDIA] Still waiting for WebRTC..."
            )

            last_report = time.monotonic()

    return None


# ============================================================
# Recorder helpers
# ============================================================

async def start_recorder(frame):
    result = await frame.evaluate(
        """
        ({videoBits, audioBits}) =>
            window.__superliveStartRecorder(
                videoBits,
                audioBits
            )
        """,
        {
            "videoBits": VIDEO_BITRATE,
            "audioBits": AUDIO_BITRATE
        }
    )

    print(
        "[RECORDER] MIME:",
        result.get("mimeType")
    )

    print(
        "[RECORDER] State:",
        result.get("state")
    )

    return result


async def drain_recorder_queue(
    frame,
    ffmpeg_stdin
):
    transferred = 0

    while True:

        item = await frame.evaluate(
            """
            () =>
                window.__superliveTakeRecorderChunk
                    ? window.__superliveTakeRecorderChunk()
                    : null
            """
        )

        if not item:
            break

        data = base64.b64decode(
            item["base64"]
        )

        await ffmpeg_stdin.write(data)

        transferred += len(data)

    return transferred


async def get_recorder_status(frame):
    try:
        return await frame.evaluate(
            """
            () =>
                window.__superliveRecorderStatus
                    ? window.__superliveRecorderStatus()
                    : {
                        state: "missing"
                    }
            """
        )
    except Exception as exc:
        return {
            "state": "error",
            "error": str(exc)
        }


async def stop_recorder(frame):
    try:
        await frame.evaluate(
            """
            () =>
                window.__superliveStopRecorder
                    ? window.__superliveStopRecorder()
                    : false
            """
        )
    except Exception as exc:
        print(
            "[RECORDER] Stop request error:",
            exc
        )


async def wait_recorder_done(
    frame,
    ffmpeg_stdin
):
    deadline = (
        time.monotonic()
        + 30
    )

    while time.monotonic() < deadline:

        await drain_recorder_queue(
            frame,
            ffmpeg_stdin
        )

        status = await get_recorder_status(
            frame
        )

        if status.get("error"):
            print(
                "[RECORDER] Browser error:",
                status.get("error")
            )

        if status.get("done"):
            await drain_recorder_queue(
                frame,
                ffmpeg_stdin
            )
            return

        await asyncio.sleep(0.2)

    print(
        "[WARN] Recorder finalization timeout."
    )


# ============================================================
# FFmpeg
# ============================================================

async def start_ffmpeg(output_pattern):
    """
    Convert browser WebM stream to MP4 segments.

    The output video bitrate is deliberately capped so that
    60-second segments remain comfortably below Telegram's
    ordinary 50 MB Bot API upload limit.
    """

    cmd = [
        "ffmpeg",

        "-hide_banner",
        "-loglevel", "warning",

        "-fflags", "+genpts",

        "-i", "pipe:0",

        "-map", "0:v:0",
        "-map", "0:a:0?",

        "-c:v", "libx264",
        "-preset", "veryfast",

        # Hard bitrate cap for predictable segment sizes.
        "-b:v", "3500k",
        "-maxrate", "3500k",
        "-bufsize", "7000k",

        "-pix_fmt", "yuv420p",

        "-c:a", "aac",
        "-b:a", "96k",

        "-movflags", "+faststart",

        "-f", "segment",
        "-segment_time",
        str(SEGMENT_SECONDS),

        "-reset_timestamps", "1",

        "-segment_format", "mp4",

        str(output_pattern)
    ]

    print(
        "[FFMPEG]",
        " ".join(cmd)
    )

    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE
    )

    return process


# ============================================================
# Validation
# ============================================================

async def validate_file(path):
    if not path.exists():
        return False

    if path.stat().st_size < 10000:
        return False

    cmd = [
        "ffprobe",
        "-v", "error",
        "-show_entries",
        "format=duration,size,format_name",
        "-of",
        "json",
        str(path)
    ]

    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )

    stdout, stderr = await process.communicate()

    if process.returncode != 0:
        print(
            "[FFPROBE ERROR]",
            stderr.decode(
                "utf-8",
                errors="replace"
            )
        )
        return False

    try:
        data = json.loads(
            stdout.decode("utf-8")
        )

        fmt = data.get(
            "format",
            {}
        )

        print(
            f"[VALID] {path.name} | "
            f"{fmt.get('format_name')} | "
            f"{fmt.get('duration')} sec | "
            f"{fmt.get('size')} bytes"
        )

        return True

    except Exception as exc:
        print(
            "[FFPROBE PARSE ERROR]",
            exc
        )
        return False


# ============================================================
# Diagnostics
# ============================================================

async def save_diagnostics(page):
    try:
        RECORDINGS_DIR.mkdir(
            parents=True,
            exist_ok=True
        )

        screenshot =
            RECORDINGS_DIR / "_diagnostic.png"

        html =
            RECORDINGS_DIR / "_diagnostic.html"

        await page.screenshot(
            path=str(screenshot),
            full_page=True
        )

        html_text =
            await page.content()

        html.write_text(
            html_text,
            encoding="utf-8"
        )

        diagnostics = []

        for frame in page.frames:

            try:
                data =
                    await inspect_frame(frame)

                diagnostics.append(data)

            except Exception as exc:
                diagnostics.append({
                    "error": str(exc)
                })

        json_path =
            RECORDINGS_DIR / "_diagnostic.json"

        json_path.write_text(
            json.dumps(
                diagnostics,
                indent=2,
                ensure_ascii=False
            ),
            encoding="utf-8"
        )

        print(
            "[DIAGNOSTICS] Saved:"
        )

        print(
            screenshot
        )

        print(
            html
        )

        print(
            json_path
        )

    except Exception as exc:
        print(
            "[DIAGNOSTICS] Failed:",
            exc
        )


# ============================================================
# Main recording routine
# ============================================================

async def record(url):
    RECORDINGS_DIR.mkdir(
        parents=True,
        exist_ok=True
    )

    TEMP_DIR.mkdir(
        parents=True,
        exist_ok=True
    )

    print("")
    print("=" * 60)
    print("SUPERLIVE RECORDER")
    print("=" * 60)
    print("URL:", url)
    print("Output:", RECORDINGS_DIR)
    print("Segment:", SEGMENT_SECONDS, "s")
    print(
        "Duration limit:",
        RECORD_DURATION_SECONDS,
        "s"
    )
    print(
        "Browser video bitrate:",
        VIDEO_BITRATE
    )
    print(
        "Browser audio bitrate:",
        AUDIO_BITRATE
    )
    print("=" * 60)

    async with async_playwright() as p:

        print("[1/6] Launching full Chromium...")

        browser = await p.chromium.launch(
            channel="chromium",
            headless=True,

            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",

                "--autoplay-policy=no-user-gesture-required",

                "--use-fake-ui-for-media-stream",

                "--enable-features=WebRtcHideLocalIpsWithMdns",

                "--disable-background-timer-throttling",
                "--disable-backgrounding-occluded-windows",
                "--disable-renderer-backgrounding",

                "--disable-blink-features=AutomationControlled"
            ]
        )

        context = await browser.new_context(
            viewport={
                "width": 1920,
                "height": 1080
            },

            user_agent=(
                "Mozilla/5.0 "
                "(X11; Linux x86_64) "
                "AppleWebKit/537.36 "
                "(KHTML, like Gecko) "
                "Chrome/140.0.0.0 "
                "Safari/537.36"
            ),

            locale="fr-FR",

            timezone_id="Africa/Casablanca",

            permissions=[
                "microphone",
                "camera"
            ]
        )

        # CRITICAL:
        # Install WebRTC hook before navigation.
        await context.add_init_script(
            WEBRTC_HOOK
        )

        page = await context.new_page()

        diagnostics = await install_diagnostics(
            page
        )

        page.set_default_timeout(
            PAGE_TIMEOUT_MS
        )

        print("[1/6] Opening page...")

        try:
            await page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=PAGE_TIMEOUT_MS
            )
        except Exception as exc:
            print(
                "[WARN] page.goto:",
                exc
            )

        await asyncio.sleep(5)

        print(
            "[PAGE] URL:",
            page.url
        )

        print(
            "[PAGE] Title:",
            await page.title()
        )

        # Give dynamically-created frames time to appear.
        await asyncio.sleep(5)

        frame = await wait_for_media(
            page
        )

        if frame is None:

            print(
                "[FATAL] No live WebRTC "
                "video track could be prepared."
            )

            print(
                "[DIAGNOSTIC] Frames:",
                len(page.frames)
            )

            for index, current_frame in enumerate(
                page.frames
            ):

                try:
                    info =
                        await inspect_frame(
                            current_frame
                        )

                    print(
                        "[FRAME]",
                        index,
                        json.dumps(
                            info,
                            ensure_ascii=False
                        )[:5000]
                    )

                except Exception as exc:
                    print(
                        "[FRAME ERROR]",
                        index,
                        exc
                    )

            print(
                "[CONSOLE]"
            )

            for message in diagnostics[-100:]:
                print(message)

            await save_diagnostics(page)

            await browser.close()

            raise RuntimeError(
                "No playable WebRTC video "
                "could be detected."
            )

        print(
            "[3/6] Starting MediaRecorder..."
        )

        recorder_info =
            await start_recorder(
                frame
            )

        print(
            "[RECORDER]",
            recorder_info
        )

        timestamp =
            time.strftime(
                "%Y%m%d_%H%M%S"
            )

        output_pattern =
            TEMP_DIR / (
                f"recording_{timestamp}_%03d.mp4"
            )

        print(
            "[4/6] Starting FFmpeg..."
        )

        ffmpeg =
            await start_ffmpeg(
                output_pattern
            )

        started_at =
            time.monotonic()

        transferred_total = 0

        try:

            while not _stop_requested:

                elapsed =
                    time.monotonic() - started_at

                if (
                    RECORD_DURATION_SECONDS > 0
                    and elapsed >=
                    RECORD_DURATION_SECONDS
                ):
                    print(
                        "[TIME] Recording duration reached."
                    )
                    break

                transferred =
                    await drain_recorder_queue(
                        frame,
                        ffmpeg.stdin
                    )

                transferred_total += transferred

                status =
                    await get_recorder_status(
                        frame
                    )

                if status.get("error"):
                    raise RuntimeError(
                        "MediaRecorder error: "
                        + str(
                            status.get("error")
                        )
                    )

                if int(elapsed) % 10 == 0:
                    print(
                        f"[RECORDING] "
                        f"{int(elapsed)}s | "
                        f"browser bytes="
                        f"{status.get('bytes', 0)} | "
                        f"transferred="
                        f"{transferred_total}"
                    )

                # Check WebRTC track health.
                try:
                    health =
                        await frame.evaluate(
                            """
                            () => {
                                const s =
                                    window.__superliveRecordingStream;

                                if (!s) {
                                    return {
                                        ok: false,
                                        reason: "stream missing"
                                    };
                                }

                                const video =
                                    s.getVideoTracks()[0];

                                const audio =
                                    s.getAudioTracks()[0];

                                return {
                                    ok: !!video &&
                                        video.readyState === "live",

                                    video: video
                                        ? {
                                            readyState:
                                                video.readyState,
                                            muted:
                                                video.muted,
                                            enabled:
                                                video.enabled
                                        }
                                        : null,

                                    audio: audio
                                        ? {
                                            readyState:
                                                audio.readyState,
                                            muted:
                                                audio.muted,
                                            enabled:
                                                audio.enabled
                                        }
                                        : null
                                };
                            }
                            """
                        )

                    if not health.get("ok"):
                        print(
                            "[WARN] Video track is "
                            "no longer live."
                        )

                        print(
                            json.dumps(
                                health,
                                ensure_ascii=False
                            )
                        )

                        break

                except Exception as exc:
                    print(
                        "[HEALTH] Check failed:",
                        exc
                    )

                await asyncio.sleep(1)

        finally:

            print(
                "[5/6] Stopping MediaRecorder..."
            )

            await stop_recorder(
                frame
            )

            await wait_recorder_done(
                frame,
                ffmpeg.stdin
            )

            try:
                await ffmpeg.stdin.close()
            except Exception:
                pass

            try:
                await ffmpeg.stdin.wait_closed()
            except Exception:
                pass

            try:
                stderr =
                    await asyncio.wait_for(
                        ffmpeg.stderr.read(),
                        timeout=30
                    )
            except asyncio.TimeoutError:
                stderr = b""

            return_code =
                await ffmpeg.wait()

            if stderr:
                print(
                    "[FFMPEG]",
                    stderr.decode(
                        "utf-8",
                        errors="replace"
                    )
                )

            print(
                "[FFMPEG] Exit code:",
                return_code
            )

        print(
            "[6/6] Validating MP4 segments..."
        )

        valid_count = 0

        for path in sorted(
            TEMP_DIR.glob("*.mp4")
        ):

            if await validate_file(path):

                destination =
                    RECORDINGS_DIR / path.name

                path.replace(
                    destination
                )

                valid_count += 1

        print(
            "[RESULT] Valid segments:",
            valid_count
        )

        if valid_count == 0:
            await save_diagnostics(page)

            await browser.close()

            raise RuntimeError(
                "FFmpeg produced no valid MP4 segments."
            )

        print(
            "[RESULT] Total browser bytes:",
            transferred_total
        )

        await browser.close()


# ============================================================
# Entry point
# ============================================================

def main():
    if len(sys.argv) < 2:
        print(
            "Usage: python record_once.py <URL>"
        )
        sys.exit(2)

    url = sys.argv[1].strip()

    if not url:
        print("URL is empty.")
        sys.exit(2)

    try:
        asyncio.run(
            record(url)
        )

    except KeyboardInterrupt:
        print(
            "[STOP] Interrupted."
        )
        sys.exit(130)

    except Exception as exc:
        print("")
        print(
            "[FATAL]",
            exc
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
