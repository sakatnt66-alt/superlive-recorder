import asyncio
import base64
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

from playwright.async_api import async_playwright


# ============================================================
# SUPERLIVE RECORDER v6
# WebRTC MediaStream -> MediaRecorder -> FFmpeg -> MP4 segments
#
# Designed for:
# - Linux / GitHub Actions
# - Headless Chromium
# - Long recordings
# - Bounded browser memory
# - Independent MP4 segments
# - Future Telegram / Worker integration
# ============================================================


# ============================================================
# CONFIGURATION
# ============================================================

BASE_DIR = Path(
    os.environ.get(
        "RECORDINGS_DIR",
        str(Path.cwd() / "recordings")
    )
).resolve()

TEMP_DIR = BASE_DIR / "_temp"
SEGMENTS_DIR = BASE_DIR / "segments"

BASE_DIR.mkdir(parents=True, exist_ok=True)
TEMP_DIR.mkdir(parents=True, exist_ok=True)
SEGMENTS_DIR.mkdir(parents=True, exist_ok=True)


FFMPEG = os.environ.get("FFMPEG", "ffmpeg")
FFPROBE = os.environ.get("FFPROBE", "ffprobe")

PAGE_TIMEOUT = int(
    os.environ.get("PAGE_TIMEOUT", "30000")
)

STREAM_TIMEOUT = int(
    os.environ.get("STREAM_TIMEOUT", "90")
)

# MediaRecorder requests data periodically.
# This is NOT used as the recording duration timer.
MEDIARECORDER_TIMESLICE_MS = int(
    os.environ.get("MEDIARECORDER_TIMESLICE_MS", "1000")
)

# How often Python asks the browser for new MediaRecorder chunks.
CHUNK_POLL_INTERVAL = float(
    os.environ.get("CHUNK_POLL_INTERVAL", "0.5")
)

# Recording is split into independent MP4 files.
#
# 120 seconds is intentionally conservative:
# - bounded browser memory
# - small failure domain
# - Telegram-friendly after transcoding
SEGMENT_SECONDS = int(
    os.environ.get("SEGMENT_SECONDS", "120")
)

# 4 Mbps video is a safer starting point than the old 8 Mbps.
# This can be increased later after testing quality/storage/CPU.
VIDEO_BITRATE = os.environ.get(
    "VIDEO_BITRATE",
    "4M"
)

AUDIO_BITRATE = os.environ.get(
    "AUDIO_BITRATE",
    "128k"
)

# Maximum expected size warning for one MP4 segment.
# This is NOT a hard FFmpeg limit.
MAX_SEGMENT_MB = float(
    os.environ.get("MAX_SEGMENT_MB", "45")
)

# If set to 0, record until SIGTERM/SIGINT.
# Otherwise record for this many seconds.
RECORD_SECONDS = int(
    os.environ.get("RECORD_SECONDS", "0")
)

# Optional URL environment variable.
TARGET_URL = os.environ.get(
    "TARGET_URL",
    ""
).strip()


# ============================================================
# GLOBAL STOP EVENT
# ============================================================

STOP_EVENT = asyncio.Event()


def _handle_signal(signum, frame):
    print()
    print(f"[*] Received signal {signum}. Stopping safely...")
    STOP_EVENT.set()


signal.signal(signal.SIGINT, _handle_signal)
signal.signal(signal.SIGTERM, _handle_signal)


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

    function addUnique(arr, value) {
        if (value && !arr.includes(value)) {
            arr.push(value);
        }
    }

    function rememberTrack(track, stream = null) {
        if (!track) return;

        try {
            if (track.kind === "audio") {
                addUnique(window.__superlive_audio_tracks, track);
            }

            if (track.kind === "video") {
                addUnique(window.__superlive_video_tracks, track);
            }

            if (stream) {
                const existing =
                    window.__superlive_track_links.find(
                        x => x.track === track
                    );

                if (existing) {
                    addUnique(existing.streams, stream);
                } else {
                    window.__superlive_track_links.push({
                        track,
                        streams: [stream]
                    });
                }
            }
        } catch (_) {}
    }

    function rememberStream(stream) {
        if (!stream) return;

        try {
            addUnique(
                window.__superlive_streams,
                stream
            );

            for (const track of stream.getTracks()) {
                rememberTrack(track, stream);
            }
        } catch (_) {}
    }

    const OriginalPC = window.RTCPeerConnection;

    if (!OriginalPC) {
        return;
    }

    function SuperLiveRTCPeerConnection(...args) {
        const pc = new OriginalPC(...args);

        try {
            pc.addEventListener("track", (event) => {
                const track = event.track;

                const streams =
                    Array.isArray(event.streams)
                        ? event.streams
                        : [];

                if (track) {
                    rememberTrack(track);
                }

                for (const stream of streams) {
                    rememberStream(stream);
                }

                for (const stream of streams) {
                    try {
                        for (const t of stream.getTracks()) {
                            rememberTrack(t, stream);
                        }
                    } catch (_) {}
                }

                try {
                    const receiver = event.receiver;

                    if (receiver && receiver.track) {
                        rememberTrack(receiver.track);
                    }
                } catch (_) {}
            });
        } catch (_) {}

        return pc;
    }

    SuperLiveRTCPeerConnection.prototype =
        OriginalPC.prototype;

    Object.setPrototypeOf(
        SuperLiveRTCPeerConnection,
        OriginalPC
    );

    window.RTCPeerConnection =
        SuperLiveRTCPeerConnection;
})();
"""


# ============================================================
# UTILITIES
# ============================================================

def check_binary(binary_name):
    return shutil.which(binary_name) is not None


def check_ffmpeg():
    return check_binary(FFMPEG)


def check_ffprobe():
    return check_binary(FFPROBE)


def extract_stream_id(url):
    match = re.search(
        r"/livestream/(\d+)",
        url
    )

    if match:
        return match.group(1)

    return str(int(time.time()))


def sanitize_filename(value):
    value = re.sub(
        r"[^a-zA-Z0-9_.-]+",
        "_",
        value
    )

    return value.strip("._") or "stream"


def now_timestamp():
    return time.strftime(
        "%Y%m%d_%H%M%S"
    )


def format_duration(seconds):
    seconds = max(0, int(seconds))

    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    secs = seconds % 60

    return (
        f"{hours:02d}:"
        f"{minutes:02d}:"
        f"{secs:02d}"
    )


# ============================================================
# VIDEO DISCOVERY
# ============================================================

async def find_video(page):
    """
    Search main document and child frames for a usable video element.
    """

    # Main page first.
    try:
        videos = page.locator("video")
        count = await videos.count()

        for i in range(count):
            video = videos.nth(i)

            try:
                if await video.is_visible():
                    return video
            except Exception:
                pass

        if count:
            return videos.first

    except Exception:
        pass

    # Child frames.
    for frame in page.frames:
        if frame == page.main_frame:
            continue

        try:
            videos = frame.locator("video")
            count = await videos.count()

            for i in range(count):
                video = videos.nth(i)

                try:
                    if await video.is_visible():
                        return video
                except Exception:
                    pass

            if count:
                return videos.first

        except Exception:
            pass

    return None


async def get_video_info(video):
    return await video.evaluate(
        """
        video => ({
            readyState: video.readyState,
            width: video.videoWidth,
            height: video.videoHeight,
            currentTime: video.currentTime,
            paused: video.paused,
            muted: video.muted,
            volume: video.volume,
            hasSrcObject: !!video.srcObject
        })
        """
    )


async def wait_for_video(page):
    print("[*] Searching for WebRTC video...")

    start = time.monotonic()
    last_time = None

    while (
        time.monotonic() - start
        < STREAM_TIMEOUT
    ):
        video = await find_video(page)

        if video:
            try:
                info = await get_video_info(video)

                current_time = float(
                    info["currentTime"]
                )

                print(
                    "    video: "
                    f"{info['width']}x{info['height']} | "
                    f"readyState={info['readyState']} | "
                    f"currentTime={current_time:.2f}",
                    end="\r"
                )

                if (
                    info["readyState"] >= 2
                    and info["width"] > 0
                    and info["height"] > 0
                ):
                    if (
                        last_time is not None
                        and current_time > last_time
                    ):
                        print()

                        print(
                            "[✓] WebRTC video is playing: "
                            f"{info['width']}x"
                            f"{info['height']}"
                        )

                        return video

                    last_time = current_time

            except Exception:
                pass

        await asyncio.sleep(1)

    print()

    return None


# ============================================================
# WEBRTC TRACK INSPECTION
# ============================================================

async def get_track_info(page):
    return await page.evaluate(
        """
        () => ({
            audioCount:
                (window.__superlive_audio_tracks || [])
                .length,

            videoCount:
                (window.__superlive_video_tracks || [])
                .length
        })
        """
    )


async def wait_for_audio(page):
    print(
        "[*] Waiting for live/unmuted WebRTC AudioTrack..."
    )

    start = time.monotonic()

    while (
        time.monotonic() - start
        < STREAM_TIMEOUT
    ):
        info = await page.evaluate(
            """
            () => ({
                videoCount:
                    (window.__superlive_video_tracks || [])
                    .length,

                audioCount:
                    (window.__superlive_audio_tracks || [])
                    .length,

                audio:
                    (window.__superlive_audio_tracks || [])
                    .map(t => ({
                        id: t.id,
                        muted: t.muted,
                        readyState: t.readyState
                    }))
            })
            """
        )

        live_audio = [
            t
            for t in info["audio"]
            if t["readyState"] == "live"
        ]

        usable_audio = [
            t
            for t in live_audio
            if not t["muted"]
        ]

        print(
            "    WebRTC tracks: "
            f"video={info['videoCount']} "
            f"audio={info['audioCount']} | "
            f"live={len(live_audio)} "
            f"usable={len(usable_audio)}",
            end="\r"
        )

        if usable_audio:
            print()

            print(
                f"[✓] Found "
                f"{len(usable_audio)} usable AudioTrack(s)"
            )

            return True

        await asyncio.sleep(0.5)

    print()

    return False


# ============================================================
# BUILD RECORDING MEDIASTREAM
# ============================================================

async def prepare_recording_stream(page):
    """
    Build one MediaStream containing exactly:
      - 1 live video track
      - 1 live/unmuted audio track

    Selection priority:
      1. same video.srcObject
      2. same WebRTC MediaStream relationship
      3. remembered MediaStream containing video
      4. any live/unmuted WebRTC audio
    """

    return await page.evaluate(
        """
        () => {
            const videos =
                Array.from(
                    document.querySelectorAll("video")
                );

            let video =
                videos.find(v =>
                    v.srcObject &&
                    v.readyState >= 2 &&
                    v.videoWidth > 0 &&
                    v.videoHeight > 0
                );

            if (!video) {
                video =
                    videos.find(
                        v =>
                            v.srcObject &&
                            v.videoWidth > 0
                    )
                    ||
                    videos.find(
                        v => v.srcObject
                    )
                    ||
                    null;
            }

            if (!video) {
                throw new Error(
                    "No video element with srcObject."
                );
            }

            const sourceStream =
                video.srcObject || null;

            let videoTrack =
                sourceStream
                    ? sourceStream
                        .getVideoTracks()
                        .find(
                            t =>
                                t.readyState === "live"
                        )
                    : null;

            if (!videoTrack) {
                videoTrack =
                    (
                        window.__superlive_video_tracks
                        || []
                    ).find(
                        t =>
                            t &&
                            t.readyState === "live"
                    ) || null;
            }

            if (!videoTrack) {
                throw new Error(
                    "No live VideoTrack found."
                );
            }

            const audioTracks =
                (
                    window.__superlive_audio_tracks
                    || []
                ).filter(
                    t =>
                        t &&
                        t.readyState === "live"
                );

            const links =
                window.__superlive_track_links
                || [];

            // ------------------------------------------------
            // 1. Audio in the same srcObject.
            // ------------------------------------------------

            let audioTrack =
                sourceStream
                    ? sourceStream
                        .getAudioTracks()
                        .find(
                            t =>
                                t.readyState === "live"
                                &&
                                !t.muted
                        )
                    : null;

            let audioSource =
                audioTrack
                    ? "same-srcObject"
                    : null;

            // If only muted audio is present in srcObject,
            // don't select it yet.
            if (!audioTrack && sourceStream) {
                audioTrack =
                    sourceStream
                        .getAudioTracks()
                        .find(
                            t =>
                                t.readyState === "live"
                        )
                        || null;

                if (audioTrack) {
                    // Continue searching for an unmuted
                    // better candidate.
                    audioTrack = null;
                }
            }

            // ------------------------------------------------
            // 2. Same WebRTC stream relationship.
            // ------------------------------------------------

            const linked = [];

            for (const link of links) {
                if (
                    !link ||
                    !link.track ||
                    link.track.kind !== "audio"
                ) {
                    continue;
                }

                if (
                    !audioTracks.includes(
                        link.track
                    )
                ) {
                    continue;
                }

                for (
                    const stream
                    of (link.streams || [])
                ) {
                    if (!stream) continue;

                    try {
                        const vids =
                            stream.getVideoTracks();

                        if (
                            vids.some(
                                t =>
                                    t === videoTrack
                                    ||
                                    t.id === videoTrack.id
                            )
                        ) {
                            linked.push(
                                link.track
                            );

                            break;
                        }
                    } catch (_) {}
                }
            }

            if (!audioTrack) {
                audioTrack =
                    linked.find(
                        t => !t.muted
                    )
                    ||
                    null;

                if (audioTrack) {
                    audioSource =
                        "same-webrtc-stream";
                }
            }

            // ------------------------------------------------
            // 3. Search remembered MediaStreams.
            // ------------------------------------------------

            if (!audioTrack) {
                for (
                    const stream
                    of (
                        window.__superlive_streams
                        || []
                    )
                ) {
                    try {
                        const vids =
                            stream.getVideoTracks();

                        const containsVideo =
                            vids.some(
                                t =>
                                    t === videoTrack
                                    ||
                                    t.id === videoTrack.id
                            );

                        if (!containsVideo) {
                            continue;
                        }

                        const audios =
                            stream
                                .getAudioTracks()
                                .filter(
                                    t =>
                                        t.readyState
                                        === "live"
                                );

                        const usable =
                            audios.find(
                                t => !t.muted
                            );

                        if (usable) {
                            audioTrack = usable;
                            audioSource =
                                "remembered-mediaStream";
                            break;
                        }
                    } catch (_) {}
                }
            }

            // ------------------------------------------------
            // 4. Last resort: any live/unmuted audio.
            // ------------------------------------------------

            if (!audioTrack) {
                audioTrack =
                    audioTracks.find(
                        t => !t.muted
                    )
                    ||
                    null;

                if (audioTrack) {
                    audioSource =
                        "webrtc-unlinked-unmuted";
                }
            }

            if (!audioTrack) {
                throw new Error(
                    "No live/unmuted AudioTrack " +
                    "could be selected."
                );
            }

            const recordingStream =
                new MediaStream([
                    videoTrack,
                    audioTrack
                ]);

            window.__superlive_recording_stream =
                recordingStream;

            window.__superlive_source_stream =
                sourceStream;

            window.__superlive_source_video =
                video;

            return {
                video: 1,
                audio: 1,

                audioSource,

                sourceStreamId:
                    sourceStream
                        ? sourceStream.id
                        : null,

                sourceTracks:
                    sourceStream
                        ? sourceStream
                            .getTracks()
                            .map(t => ({
                                kind: t.kind,
                                id: t.id,
                                muted: t.muted,
                                readyState:
                                    t.readyState
                            }))
                        : [],

                videoTrack: {
                    id: videoTrack.id,
                    muted: videoTrack.muted,
                    readyState:
                        videoTrack.readyState
                },

                audioTrack: {
                    id: audioTrack.id,
                    muted: audioTrack.muted,
                    readyState:
                        audioTrack.readyState
                }
            };
        }
        """
    )


# ============================================================
# MEDIARECORDER INITIALIZATION
# ============================================================

async def get_supported_mime_type(page):
    return await page.evaluate(
        """
        () => {
            const types = [
                "video/webm;codecs=vp9,opus",
                "video/webm;codecs=vp8,opus",
                "video/webm"
            ];

            for (const type of types) {
                if (
                    MediaRecorder.isTypeSupported(
                        type
                    )
                ) {
                    return type;
                }
            }

            return null;
        }
        """
    )


async def start_recorder(page):
    """
    Start MediaRecorder.

    Instead of keeping the whole recording forever,
    chunks are placed into a small queue which Python
    continuously drains.
    """

    mime_type = (
        await get_supported_mime_type(page)
    )

    if not mime_type:
        raise RuntimeError(
            "Browser does not support WebM MediaRecorder."
        )

    result = await page.evaluate(
        """
        ({mimeType, timeslice}) => {
            const stream =
                window.__superlive_recording_stream;

            if (!stream) {
                throw new Error(
                    "Recording MediaStream not found."
                );
            }

            const audio =
                stream.getAudioTracks();

            const video =
                stream.getVideoTracks();

            if (audio.length !== 1) {
                throw new Error(
                    `Expected 1 audio track, got ${audio.length}.`
                );
            }

            if (video.length !== 1) {
                throw new Error(
                    `Expected 1 video track, got ${video.length}.`
                );
            }

            if (
                audio[0].readyState !== "live"
                ||
                video[0].readyState !== "live"
            ) {
                throw new Error(
                    "AudioTrack or VideoTrack is not live."
                );
            }

            const recorder =
                new MediaRecorder(
                    stream,
                    {
                        mimeType,
                        videoBitsPerSecond: 4000000,
                        audioBitsPerSecond: 128000
                    }
                );

            window.__superliveRecorder =
                recorder;

            window.__superliveChunkQueue =
                [];

            window.__superliveRecorderError =
                null;

            window.__superliveRecorderStartedAt =
                performance.now();

            recorder.ondataavailable =
                (event) => {
                    if (
                        event.data &&
                        event.data.size > 0
                    ) {
                        window
                            .__superliveChunkQueue
                            .push(event.data);
                    }
                };

            recorder.onerror =
                (event) => {
                    window
                        .__superliveRecorderError =
                        event.error
                            ? event.error.message
                            : "MediaRecorder error";
                };

            recorder.start(timeslice);

            return {
                mimeType,
                audio: audio.length,
                video: video.length
            };
        }
        """,
        {
            "mimeType": mime_type,
            "timeslice":
                MEDIARECORDER_TIMESLICE_MS
        }
    )

    print(
        f"[✓] MediaRecorder MIME: "
        f"{result['mimeType']}"
    )

    print(
        f"[✓] Video tracks: "
        f"{result['video']}"
    )

    print(
        f"[✓] Audio tracks: "
        f"{result['audio']}"
    )

    print(
        "[✓] MediaRecorder started."
    )

    return result


# ============================================================
# BROWSER CHUNK QUEUE
# ============================================================

async def browser_queue_status(page):
    return await page.evaluate(
        """
        () => ({
            queueLength:
                (
                    window.__superliveChunkQueue
                    || []
                ).length,

            recorderState:
                window.__superliveRecorder
                    ? window
                        .__superliveRecorder
                        .state
                    : "missing",

            recorderError:
                window.__superliveRecorderError
                    || null
        })
        """
    )


async def pop_browser_chunk(page):
    """
    Pop one Blob from the browser queue and transfer
    it to Python as base64.

    We deliberately transfer one chunk at a time to
    avoid creating a giant base64 string.
    """

    return await page.evaluate(
        """
        async () => {
            const queue =
                window.__superliveChunkQueue
                || [];

            if (!queue.length) {
                return null;
            }

            const blob =
                queue.shift();

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
                        i + step
                    )
                );
            }

            return {
                data: btoa(binary),
                size: bytes.length,
                type: blob.type || ""
            };
        }
        """
    )


# ============================================================
# FFMPEG PIPE
# ============================================================

async def start_ffmpeg_pipe(output_pattern):
    """
    Start FFmpeg with WebM input from stdin and segmented
    MP4 output.

    FFmpeg receives the continuous MediaRecorder WebM stream.
    The segment muxer creates independent MP4 files.
    """

    command = [
        FFMPEG,

        "-hide_banner",
        "-loglevel", "warning",

        # Input is a WebM byte stream.
        "-f", "webm",
        "-i", "pipe:0",

        # Video.
        "-map", "0:v:0",
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-crf", "21",
        "-pix_fmt", "yuv420p",

        # Audio.
        "-map", "0:a:0?",
        "-c:a", "aac",
        "-b:a", AUDIO_BITRATE,

        # Segment output.
        "-f", "segment",
        "-segment_time", str(SEGMENT_SECONDS),
        "-reset_timestamps", "1",

        # Independent MP4 segments.
        "-segment_format", "mp4",

        str(output_pattern)
    ]

    print("[*] Starting FFmpeg:")
    print("    " + " ".join(command))

    process = await asyncio.create_subprocess_exec(
        *command,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )

    return process


async def write_chunk_to_ffmpeg(
    process,
    chunk_bytes
):
    if process.stdin is None:
        raise RuntimeError(
            "FFmpeg stdin is unavailable."
        )

    process.stdin.write(chunk_bytes)

    try:
        await process.stdin.drain()
    except (BrokenPipeError, ConnectionResetError):
        raise RuntimeError(
            "FFmpeg pipe closed unexpectedly."
        )


async def close_ffmpeg_pipe(process):
    if process.stdin:
        try:
            process.stdin.close()
        except Exception:
            pass

        try:
            await process.stdin.wait_closed()
        except Exception:
            pass


# ============================================================
# FFMPEG STDERR READER
# ============================================================

async def read_ffmpeg_stderr(
    process,
    collector
):
    if not process.stderr:
        return

    try:
        while True:
            line = await process.stderr.readline()

            if not line:
                break

            text = (
                line
                .decode(
                    "utf-8",
                    errors="replace"
                )
                .strip()
            )

            if text:
                collector.append(text)

    except asyncio.CancelledError:
        raise

    except Exception as exc:
        collector.append(
            f"stderr reader error: {exc}"
        )


# ============================================================
# RECORDING HEALTH
# ============================================================

async def get_recording_health(page):
    return await page.evaluate(
        """
        () => {
            const recorder =
                window.__superliveRecorder;

            const video =
                window.__superlive_source_video;

            const recordingStream =
                window.__superlive_recording_stream;

            const audio =
                recordingStream
                    ? recordingStream
                        .getAudioTracks()
                        .map(t => ({
                            id: t.id,
                            muted: t.muted,
                            readyState:
                                t.readyState
                        }))
                    : [];

            const videoTracks =
                recordingStream
                    ? recordingStream
                        .getVideoTracks()
                        .map(t => ({
                            id: t.id,
                            muted: t.muted,
                            readyState:
                                t.readyState
                        }))
                    : [];

            return {
                recorderState:
                    recorder
                        ? recorder.state
                        : "missing",

                recorderError:
                    window
                        .__superliveRecorderError
                        || null,

                video: video
                    ? {
                        readyState:
                            video.readyState,

                        width:
                            video.videoWidth,

                        height:
                            video.videoHeight,

                        currentTime:
                            video.currentTime,

                        paused:
                            video.paused
                    }
                    : null,

                audioTracks: audio,
                videoTracks: videoTracks,

                queueLength:
                    (
                        window
                            .__superliveChunkQueue
                        || []
                    ).length
            };
        }
        """
    )


async def print_health(page):
    try:
        health =
            await get_recording_health(page)

        video =
            health.get("video")

        if video:
            print(
                "[health] "
                f"recorder={health['recorderState']} | "
                f"video="
                f"{video['width']}x"
                f"{video['height']} | "
                f"videoTime="
                f"{video['currentTime']:.1f} | "
                f"queue="
                f"{health['queueLength']}"
            )

        else:
            print(
                "[health] "
                f"recorder="
                f"{health['recorderState']} | "
                f"queue="
                f"{health['queueLength']}"
            )

        if health.get("recorderError"):
            print(
                "[❌] MediaRecorder error: "
                f"{health['recorderError']}"
            )

        for track in health["audioTracks"]:
            if (
                track["readyState"]
                != "live"
            ):
                print(
                    "[!] AudioTrack state: "
                    f"{track['readyState']}"
                )

        for track in health["videoTracks"]:
            if (
                track["readyState"]
                != "live"
            ):
                print(
                    "[!] VideoTrack state: "
                    f"{track['readyState']}"
                )

        return health

    except Exception as exc:
        print(
            "[!] Health check failed: "
            f"{exc}"
        )

        return None


# ============================================================
# WAIT FOR FINAL CHUNKS
# ============================================================

async def flush_and_stop_recorder(page):
    """
    Request final data, stop MediaRecorder, then wait for
    all final dataavailable events to arrive.
    """

    result = await page.evaluate(
        """
        async () => {
            const recorder =
                window.__superliveRecorder;

            if (!recorder) {
                throw new Error(
                    "MediaRecorder not found."
                );
            }

            if (
                recorder.state === "recording"
            ) {
                try {
                    recorder.requestData();
                } catch (_) {}
            }

            if (
                recorder.state !== "inactive"
            ) {
                recorder.stop();
            }

            const deadline =
                Date.now() + 10000;

            while (
                recorder.state !== "inactive"
            ) {
                if (
                    Date.now() > deadline
                ) {
                    throw new Error(
                        "MediaRecorder stop timeout."
                    );
                }

                await new Promise(
                    resolve =>
                        setTimeout(
                            resolve,
                            50
                        )
                );
            }

            // Give the last dataavailable event
            // time to enter the queue.
            await new Promise(
                resolve =>
                    setTimeout(
                        resolve,
                        500
                    )
            );

            return {
                state: recorder.state,

                queueLength:
                    (
                        window
                            .__superliveChunkQueue
                        || []
                    ).length,

                error:
                    window
                        .__superliveRecorderError
                        || null
            };
        }
        """
    )

    return result


# ============================================================
# DRAIN BROWSER QUEUE
# ============================================================

async def drain_browser_queue(
    page,
    ffmpeg_process,
    stats
):
    """
    Drain every available MediaRecorder chunk into FFmpeg.
    """

    drained = 0

    while True:
        chunk = await pop_browser_chunk(page)

        if chunk is None:
            break

        encoded = chunk["data"]

        chunk_bytes = base64.b64decode(
            encoded
        )

        await write_chunk_to_ffmpeg(
            ffmpeg_process,
            chunk_bytes
        )

        stats["chunks"] += 1
        stats["bytes"] += len(chunk_bytes)

        drained += 1

    return drained


# ============================================================
# WAIT FOR NEW CHUNKS / RECORDING LOOP
# ============================================================

async def recording_loop(
    page,
    ffmpeg_process,
    duration_seconds,
    stats
):
    """
    Main recording loop.

    Important:
    elapsed time comes from Python's monotonic clock,
    NOT MediaRecorder timeslice/chunk count.
    """

    started = time.monotonic()

    last_health = started
    last_print = started

    while True:
        now = time.monotonic()
        elapsed = now - started

        # Stop requested externally.
        if STOP_EVENT.is_set():
            print(
                "[*] Stop requested."
            )
            break

        # Duration limit.
        if (
            duration_seconds > 0
            and elapsed >= duration_seconds
        ):
            print(
                "[*] Recording duration reached."
            )
            break

        # Drain browser chunks.
        try:
            await drain_browser_queue(
                page,
                ffmpeg_process,
                stats
            )

        except Exception as exc:
            print(
                "[❌] Failed to drain "
                f"MediaRecorder queue: {exc}"
            )
            raise

        # Health check every 10 seconds.
        if (
            now - last_health
            >= 10
        ):
            health =
                await print_health(page)

            last_health = now

            if health:
                if (
                    health["recorderState"]
                    == "inactive"
                ):
                    raise RuntimeError(
                        "MediaRecorder became inactive "
                        "while recording."
                    )

                if health.get(
                    "recorderError"
                ):
                    raise RuntimeError(
                        "MediaRecorder error: "
                        +
                        health[
                            "recorderError"
                        ]
                    )

                audio_ok = any(
                    (
                        t["readyState"]
                        == "live"
                        and
                        not t["muted"]
                    )
                    for t
                    in health["audioTracks"]
                )

                video_ok = any(
                    t["readyState"]
                    == "live"
                    for t
                    in health["videoTracks"]
                )

                if not audio_ok:
                    raise RuntimeError(
                        "Recording AudioTrack "
                        "is no longer live/unmuted."
                    )

                if not video_ok:
                    raise RuntimeError(
                        "Recording VideoTrack "
                        "is no longer live."
                    )

        # Human-readable progress every 5 seconds.
        if (
            now - last_print
            >= 5
        ):
            print(
                "[recording] "
                f"{format_duration(elapsed)} | "
                f"chunks={stats['chunks']} | "
                f"received="
                f"{stats['bytes'] / 1024 / 1024:.2f} MB"
            )

            last_print = now

        await asyncio.sleep(
            CHUNK_POLL_INTERVAL
        )

    # Final flush/stop.
    print(
        "[*] Flushing final MediaRecorder data..."
    )

    final_state =
        await flush_and_stop_recorder(
            page
        )

    print(
        "[✓] MediaRecorder stopped: "
        f"queue={final_state['queueLength']}"
    )

    if final_state.get("error"):
        raise RuntimeError(
            "MediaRecorder error: "
            +
            final_state["error"]
        )

    # Drain final chunks.
    await drain_browser_queue(
        page,
        ffmpeg_process,
        stats
    )


# ============================================================
# STOP FFmpeg
# ============================================================

async def finish_ffmpeg(
    process,
    stderr_lines
):
    print(
        "[*] Closing FFmpeg input..."
    )

    await close_ffmpeg_pipe(
        process
    )

    try:
        return_code =
            await asyncio.wait_for(
                process.wait(),
                timeout=60
            )

    except asyncio.TimeoutError:
        print(
            "[!] FFmpeg did not exit "
            "within 60 seconds."
        )

        process.kill()

        return_code =
            await process.wait()

    if return_code != 0:
        print(
            "[❌] FFmpeg exited with code "
            f"{return_code}"
        )

        if stderr_lines:
            print(
                "[FFmpeg]"
            )

            for line in stderr_lines[-50:]:
                print(
                    "  " + line
                )

        raise RuntimeError(
            "FFmpeg failed."
        )

    print(
        "[✓] FFmpeg finished successfully."
    )


# ============================================================
# FIND OUTPUT SEGMENTS
# ============================================================

def list_segment_files(
    stream_id,
    session_prefix
):
    pattern =
        f"{session_prefix}_part_*.mp4"

    return sorted(
        SEGMENTS_DIR.glob(pattern)
    )


# ============================================================
# FFPROBE VALIDATION
# ============================================================

def probe_file(file_path):
    if not check_ffprobe():
        print(
            "[!] ffprobe not found. "
            "Skipping deep validation."
        )

        return None

    command = [
        FFPROBE,

        "-v", "error",

        "-show_entries",
        "format=duration,size,format_name",

        "-show_streams",

        "-of",
        "json",

        str(file_path)
    ]

    result = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace"
    )

    if result.returncode != 0:
        print(
            f"[❌] ffprobe failed for "
            f"{file_path.name}"
        )

        if result.stderr:
            print(
                result.stderr
            )

        return None

    try:
        return json.loads(
            result.stdout
        )
    except Exception:
        return None


def validate_segment(file_path):
    if (
        not file_path.exists()
        or
        file_path.stat().st_size <= 0
    ):
        print(
            "[❌] Empty/missing segment: "
            f"{file_path}"
        )

        return False

    size_mb =
        file_path.stat().st_size / 1024 / 1024

    print(
        "[*] Validating: "
        f"{file_path.name} "
        f"({size_mb:.2f} MB)"
    )

    data =
        probe_file(file_path)

    if data is None:
        return False

    format_info =
        data.get("format", {})

    streams =
        data.get("streams", [])

    duration =
        float(
            format_info.get(
                "duration",
                0
            )
            or 0
        )

    has_video = any(
        s.get("codec_type")
        == "video"
        for s in streams
    )

    has_audio = any(
        s.get("codec_type")
        == "audio"
        for s in streams
    )

    if not has_video:
        print(
            "[❌] Segment has no video stream."
        )

        return False

    if not has_audio:
        print(
            "[❌] Segment has no audio stream."
        )

        return False

    print(
        "[✓] duration="
        f"{duration:.2f}s | "
        f"video=yes | "
        f"audio=yes"
    )

    if size_mb > MAX_SEGMENT_MB:
        print(
            "[⚠] Segment is above "
            f"{MAX_SEGMENT_MB:.1f} MB: "
            f"{size_mb:.2f} MB"
        )

    return True


# ============================================================
# VALIDATE ALL SEGMENTS
# ============================================================

def validate_all_segments(
    segment_files
):
    if not segment_files:
        print(
            "[❌] No MP4 segments were created."
        )

        return False

    print()
    print(
        "=" * 70
    )
    print(
        " VALIDATING SEGMENTS"
    )
    print(
        "=" * 70
    )

    success = True

    for file_path in segment_files:
        if not validate_segment(
            file_path
        ):
            success = False

    print()

    if success:
        print(
            "[✓] All segments passed validation."
        )
    else:
        print(
            "[❌] One or more segments "
            "failed validation."
        )

    return success


# ============================================================
# MAIN RECORDING FUNCTION
# ============================================================

async def record_stream(
    target_url,
    duration_seconds=0
):
    stream_id =
        sanitize_filename(
            extract_stream_id(
                target_url
            )
        )

    session_prefix = (
        f"superlive_"
        f"{stream_id}_"
        f"{now_timestamp()}"
    )

    output_pattern =
        SEGMENTS_DIR / (
            f"{session_prefix}_part_%03d.mp4"
        )

    print()
    print(
        "=" * 70
    )

    print(
        " SUPERLIVE RECORDER v6"
    )

    print(
        " WebRTC → MediaRecorder → "
        "FFmpeg → MP4 segments"
    )

    print(
        "=" * 70
    )

    print()
    print(
        f"[*] Stream ID : {stream_id}"
    )

    print(
        f"[*] Base dir  : {BASE_DIR}"
    )

    print(
        f"[*] Segments  : {SEGMENTS_DIR}"
    )

    print(
        f"[*] Segment duration: "
        f"{SEGMENT_SECONDS}s"
    )

    print(
        f"[*] Video bitrate: "
        f"{VIDEO_BITRATE}"
    )

    print(
        f"[*] Audio bitrate: "
        f"{AUDIO_BITRATE}"
    )

    if duration_seconds > 0:
        print(
            "[*] Maximum recording duration: "
            f"{format_duration(duration_seconds)}"
        )
    else:
        print(
            "[*] Recording duration: unlimited "
            "(until Stop/signal)"
        )

    print()

    browser = None
    context = None

    ffmpeg_process = None
    stderr_task = None

    stderr_lines = []

    stats = {
        "chunks": 0,
        "bytes": 0
    }

    segment_files = []

    try:
        async with async_playwright() as p:

            print(
                "[*] Launching Chromium..."
            )

            browser =
                await p.chromium.launch(
                    headless=True,
                    args=[
                        "--autoplay-policy="
                        "no-user-gesture-required",

                        "--disable-background-"
                        "timer-throttling",

                        "--disable-backgrounding-"
                        "occluded-windows",

                        "--disable-renderer-"
                        "backgrounding",

                        "--disable-dev-shm-usage",

                        "--no-sandbox",

                        "--disable-gpu",

                        "--use-fake-ui-for-media-stream",

                        "--autoplay-policy="
                        "no-user-gesture-required"
                    ]
                )

            context =
                await browser.new_context(
                    viewport={
                        "width": 1280,
                        "height": 900
                    },

                    locale="fr-FR"
                )

            # IMPORTANT:
            # The WebRTC hook is installed before goto().
            await context.add_init_script(
                WEBRTC_HOOK
            )

            page =
                await context.new_page()

            # ------------------------------------------------
            # Network diagnostics.
            # ------------------------------------------------

            def on_request(request):
                url =
                    request.url.lower()

                if (
                    "agora.io" in url
                    or
                    "sd-rtn.com" in url
                    or
                    "transpond/webrtc" in url
                ):
                    print(
                        "[WebRTC] "
                        f"{request.method} "
                        f"{request.url[:180]}"
                    )

            page.on(
                "request",
                on_request
            )

            print(
                "[*] Opening stream page..."
            )

            try:
                await page.goto(
                    target_url,
                    wait_until="domcontentloaded",
                    timeout=PAGE_TIMEOUT
                )

            except Exception as exc:
                print(
                    "[!] page.goto warning: "
                    f"{exc}"
                )

            # ------------------------------------------------
            # Wait for video.
            # ------------------------------------------------

            video =
                await wait_for_video(
                    page
                )

            if video is None:
                raise RuntimeError(
                    "Could not find a playing WebRTC video."
                )

            dimensions =
                await video.evaluate(
                    """
                    video => ({
                        width: video.videoWidth,
                        height: video.videoHeight
                    })
                    """
                )

            print(
                "[✓] Resolution: "
                f"{dimensions['width']}x"
                f"{dimensions['height']}"
            )

            # ------------------------------------------------
            # Audio.
            # ------------------------------------------------

            audio_found =
                await wait_for_audio(
                    page
                )

            if not audio_found:
                raise RuntimeError(
                    "No live/unmuted AudioTrack found."
                )

            # ------------------------------------------------
            # Track information.
            # ------------------------------------------------

            track_info =
                await get_track_info(
                    page
                )

            print(
                "[✓] Detected tracks: "
                f"video={track_info['videoCount']} "
                f"audio={track_info['audioCount']}"
            )

            # ------------------------------------------------
            # Prepare recording MediaStream.
            # ------------------------------------------------

            tracks =
                await prepare_recording_stream(
                    page
                )

            print(
                "[✓] Final video tracks: "
                f"{tracks['video']}"
            )

            print(
                "[✓] Final audio tracks: "
                f"{tracks['audio']}"
            )

            print(
                "[✓] Audio source: "
                f"{tracks['audioSource']}"
            )

            print(
                "[✓] Source MediaStream: "
                f"{tracks['sourceStreamId']}"
            )

            print(
                "    Source tracks:"
            )

            for source_track in (
                tracks["sourceTracks"]
            ):
                print(
                    "      - "
                    f"{source_track['kind']} | "
                    f"id={source_track['id']} | "
                    f"muted={source_track['muted']} | "
                    f"state={source_track['readyState']}"
                )

            print(
                "    VideoTrack: "
                f"id={tracks['videoTrack']['id']} "
                f"muted={tracks['videoTrack']['muted']} "
                f"state={tracks['videoTrack']['readyState']}"
            )

            print(
                "    AudioTrack: "
                f"id={tracks['audioTrack']['id']} "
                f"muted={tracks['audioTrack']['muted']} "
                f"state={tracks['audioTrack']['readyState']}"
            )

            if (
                tracks["audio"] != 1
                or
                tracks["video"] != 1
            ):
                raise RuntimeError(
                    "Final MediaStream does not "
                    "contain exactly 1 video + 1 audio track."
                )

            # ------------------------------------------------
            # Start FFmpeg.
            # ------------------------------------------------

            ffmpeg_process =
                await start_ffmpeg_pipe(
                    output_pattern
                )

            stderr_task =
                asyncio.create_task(
                    read_ffmpeg_stderr(
                        ffmpeg_process,
                        stderr_lines
                    )
                )

            # ------------------------------------------------
            # Start MediaRecorder.
            # ------------------------------------------------

            await start_recorder(
                page
            )

            print()
            print(
                "=" * 70
            )

            print(
                " 🔴 RECORDING"
            )

            print(
                "=" * 70
            )

            print(
                f"[*] Session: "
                f"{session_prefix}"
            )

            print(
                "[*] FFmpeg is receiving WebM chunks."
            )

            print(
                "[*] MP4 segments are being written continuously."
            )

            print(
                "[*] Send SIGTERM/SIGINT to stop safely."
            )

            print()

            # ------------------------------------------------
            # Main recording.
            # ------------------------------------------------

            await recording_loop(
                page,
                ffmpeg_process,
                duration_seconds,
                stats
            )

            # ------------------------------------------------
            # Close FFmpeg.
            # ------------------------------------------------

            await finish_ffmpeg(
                ffmpeg_process,
                stderr_lines
            )

            ffmpeg_process = None

            # Give filesystem a moment.
            await asyncio.sleep(1)

            # ------------------------------------------------
            # Locate generated segments.
            # ------------------------------------------------

            segment_files =
                list_segment_files(
                    stream_id,
                    session_prefix
                )

            print()
            print(
                f"[✓] Generated "
                f"{len(segment_files)} MP4 segment(s)."
            )

    except Exception as exc:
        print()
        print(
            "[❌] Recording failed: "
            f"{exc}"
        )

        # Try to stop FFmpeg cleanly.
        if ffmpeg_process:
            try:
                await close_ffmpeg_pipe(
                    ffmpeg_process
                )

                await asyncio.wait_for(
                    ffmpeg_process.wait(),
                    timeout=20
                )

            except Exception:
                try:
                    ffmpeg_process.kill()
                except Exception:
                    pass

        raise

    finally:
        # Cancel stderr reader.
        if stderr_task:
            try:
                stderr_task.cancel()
                await stderr_task
            except asyncio.CancelledError:
                pass
            except Exception:
                pass

        # Close browser resources.
        if context:
            try:
                await context.close()
            except Exception:
                pass

        if browser:
            try:
                await browser.close()
            except Exception:
                pass

    # --------------------------------------------------------
    # Validation after browser/FFmpeg shutdown.
    # --------------------------------------------------------

    segment_files =
        list_segment_files(
            stream_id,
            session_prefix
        )

    valid =
        validate_all_segments(
            segment_files
        )

    print()
    print(
        "=" * 70
    )

    if valid:
        print(
            " ✅ RECORDING COMPLETED"
        )
    else:
        print(
            " ⚠ RECORDING COMPLETED "
            "WITH VALIDATION ERRORS"
        )

    print(
        "=" * 70
    )

    print()

    print(
        f"📁 Recording directory:"
    )

    print(
        f"   {SEGMENTS_DIR}"
    )

    print()

    for file_path in segment_files:
        size_mb =
            file_path.stat().st_size / 1024 / 1024

        print(
            f"   - {file_path.name} "
            f"({size_mb:.2f} MB)"
        )

    print()

    print(
        "[stats] MediaRecorder chunks: "
        f"{stats['chunks']}"
    )

    print(
        "[stats] WebM bytes received: "
        f"{stats['bytes'] / 1024 / 1024:.2f} MB"
    )

    print()

    return {
        "stream_id": stream_id,
        "session": session_prefix,
        "segments": [
            str(p)
            for p in segment_files
        ],
        "valid": valid,
        "chunks": stats["chunks"],
        "bytes": stats["bytes"]
    }


# ============================================================
# COMMAND LINE
# ============================================================

def get_target_url():
    # First preference: environment variable.
    if TARGET_URL:
        return TARGET_URL

    # Second: command-line argument.
    if len(sys.argv) >= 2:
        return sys.argv[1].strip()

    # Third: interactive input.
    try:
        value =
            input(
                "👉 Paste livestream URL:\n"
            ).strip()

        return value

    except EOFError:
        return ""


def get_duration():
    # Environment variable.
    if RECORD_SECONDS > 0:
        return RECORD_SECONDS

    # Optional CLI:
    # python record_once.py URL 600
    if len(sys.argv) >= 3:
        try:
            value =
                int(sys.argv[2])

            return max(
                0,
                value
            )

        except ValueError:
            pass

    return 0


async def main():
    print(
        "=" * 70
    )

    print(
        " SUPERLIVE RECORDER v6"
    )

    print(
        " WebRTC → MediaRecorder → FFmpeg → MP4"
    )

    print(
        "=" * 70
    )

    print()

    # --------------------------------------------------------
    # Dependency checks.
    # --------------------------------------------------------

    if not check_ffmpeg():
        print(
            "[❌] FFmpeg not found in PATH."
        )

        print(
            "[*] Test with:"
        )

        print(
            "    ffmpeg -version"
        )

        return 1

    print(
        "[✓] FFmpeg found."
    )

    if check_ffprobe():
        print(
            "[✓] FFprobe found."
        )

    else:
        print(
            "[!] FFprobe not found."
        )

        print(
            "[!] Deep MP4 validation will be limited."
        )

    print()

    # --------------------------------------------------------
    # URL.
    # --------------------------------------------------------

    target_url =
        get_target_url()

    if not target_url:
        print(
            "[❌] No livestream URL provided."
        )

        return 1

    if not target_url.startswith(
        (
            "http://",
            "https://"
        )
    ):
        print(
            "[❌] Invalid URL."
        )

        return 1

    duration =
        get_duration()

    # --------------------------------------------------------
    # Run.
    # --------------------------------------------------------

    try:
        result =
            await record_stream(
                target_url,
                duration
            )

        if result["valid"]:
            return 0

        return 2

    except KeyboardInterrupt:
        print()
        print(
            "[*] Interrupted."
        )

        return 130

    except Exception as exc:
        print()
        print(
            "[❌] Fatal error:"
        )

        print(
            f"    {exc}"
        )

        import traceback

        traceback.print_exc()

        return 1


# ============================================================
# ENTRY POINT
# ============================================================

if __name__ == "__main__":
    exit_code = asyncio.run(
        main()
    )

    sys.exit(
        exit_code
    )
