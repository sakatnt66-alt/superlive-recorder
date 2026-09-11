import asyncio
import base64
import json
import os
import shutil
import signal
import subprocess
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
    os.getenv("SEGMENT_SECONDS", "90")
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

HEALTH_INTERVAL = 10

FFMPEG = os.getenv(
    "FFMPEG",
    "ffmpeg"
)

FFPROBE = os.getenv(
    "FFPROBE",
    "ffprobe"
)


# ============================================================
# Global stop flag
# ============================================================

_stop_requested = False


# ============================================================
# WebRTC hook
# IMPORTANT:
# This must be installed before page navigation.
# ============================================================

WEBRTC_HOOK = r"""
(() => {
    if (window.__superliveWebRTCHookInstalled) {
        return;
    }

    window.__superliveWebRTCHookInstalled = true;

    window.__superliveTracks = {
        audio: new Set(),
        video: new Set(),
        all: new Set()
    };

    window.__superliveStreams = new Set();

    const OriginalPC =
        window.RTCPeerConnection ||
        window.webkitRTCPeerConnection;

    if (!OriginalPC) {
        return;
    }

    function rememberTrack(track) {
        try {
            window.__superliveTracks.all.add(track);

            if (track.kind === "video") {
                window.__superliveTracks.video.add(track);
            }

            if (track.kind === "audio") {
                window.__superliveTracks.audio.add(track);
            }
        } catch (_) {}
    }

    function rememberStream(stream) {
        try {
            window.__superliveStreams.add(stream);

            for (const track of stream.getTracks()) {
                rememberTrack(track);
            }
        } catch (_) {}
    }

    function HookedPC(...args) {
        const pc = new OriginalPC(...args);

        try {
            pc.addEventListener(
                "track",
                event => {
                    try {
                        rememberTrack(event.track);

                        if (event.streams) {
                            for (const stream of event.streams) {
                                rememberStream(stream);
                            }
                        }
                    } catch (_) {}
                }
            );
        } catch (_) {}

        return pc;
    }

    HookedPC.prototype = OriginalPC.prototype;

    try {
        for (const key of Object.keys(OriginalPC)) {
            try {
                HookedPC[key] = OriginalPC[key];
            } catch (_) {}
        }
    } catch (_) {}

    window.RTCPeerConnection = HookedPC;

    window.__superliveGetTracks = () => {
        return {
            audio: Array.from(
                window.__superliveTracks.audio
            ),

            video: Array.from(
                window.__superliveTracks.video
            ),

            all: Array.from(
                window.__superliveTracks.all
            )
        };
    };
})();
"""


# ============================================================
# Signal handling
# ============================================================

def request_stop(signum, frame):
    global _stop_requested

    if not _stop_requested:
        _stop_requested = True

        print(
            "\n[INFO] Stop requested."
        )

        print(
            "[INFO] Finishing the current recording safely..."
        )


# ============================================================
# Utilities
# ============================================================

def ensure_directories():
    RECORDINGS_DIR.mkdir(
        parents=True,
        exist_ok=True
    )

    TEMP_DIR.mkdir(
        parents=True,
        exist_ok=True
    )


def check_program(name):
    path = shutil.which(name)

    if not path:
        raise RuntimeError(
            f"Required program not found: {name}"
        )

    return path


def timestamp():
    return time.strftime(
        "%Y%m%d_%H%M%S"
    )


def safe_prefix():
    return (
        f"recording_"
        f"{timestamp()}_"
        f"{int(time.time())}"
    )


# ============================================================
# Video discovery
# ============================================================

async def find_video(page):
    """
    Search the main page and child frames
    for a usable video element.
    """

    try:
        videos = await page.locator(
            "video"
        ).all()

        if videos:
            for video in videos:
                try:
                    box = await video.bounding_box()

                    if (
                        box
                        and box["width"] > 0
                        and box["height"] > 0
                    ):
                        return video

                except Exception:
                    continue

            return videos[0]

    except Exception:
        pass

    for frame in page.frames:
        if frame == page.main_frame:
            continue

        try:
            videos = await frame.locator(
                "video"
            ).all()

            if videos:
                for video in videos:
                    try:
                        box = await video.bounding_box()

                        if (
                            box
                            and box["width"] > 0
                            and box["height"] > 0
                        ):
                            return video

                    except Exception:
                        continue

                return videos[0]

        except Exception:
            continue

    return None


async def wait_for_video(
    page,
    timeout=120
):
    """
    Wait until a video element exists,
    has dimensions, and its currentTime advances.
    """

    deadline = (
        time.monotonic()
        + timeout
    )

    while time.monotonic() < deadline:

        video = await find_video(page)

        if video:
            try:
                state = await video.evaluate(
                    """
                    v => ({
                        readyState: v.readyState,
                        width: v.videoWidth,
                        height: v.videoHeight,
                        currentTime: v.currentTime,
                        paused: v.paused
                    })
                    """
                )

                if (
                    state["readyState"] >= 2
                    and state["width"] > 0
                    and state["height"] > 0
                ):
                    first_time = (
                        state["currentTime"]
                    )

                    await asyncio.sleep(2)

                    second_time = (
                        await video.evaluate(
                            "v => v.currentTime"
                        )
                    )

                    if second_time > first_time:
                        return video

            except Exception:
                pass

        await asyncio.sleep(2)

    raise RuntimeError(
        "A playable video could not be detected."
    )


# ============================================================
# Track preparation
# ============================================================

async def prepare_recording_stream(page):
    """
    Select the live WebRTC video and audio tracks
    and create a dedicated MediaStream.
    """

    video = await find_video(page)

    if not video:
        raise RuntimeError(
            "Video element not found."
        )

    result = await video.evaluate(
        """
        async video => {

            function live(track) {
                return (
                    track &&
                    track.readyState === "live"
                );
            }

            const videoTracks = [];
            const audioTracks = [];

            const src = video.srcObject;

            if (src instanceof MediaStream) {

                for (const track of src.getTracks()) {

                    if (
                        track.kind === "video"
                        && live(track)
                    ) {
                        videoTracks.push(track);
                    }

                    if (
                        track.kind === "audio"
                        && live(track)
                    ) {
                        audioTracks.push(track);
                    }
                }
            }

            if (!videoTracks.length) {

                const remembered =
                    window.__superliveGetTracks
                        ? window.__superliveGetTracks()
                        : {
                            video: [],
                            audio: []
                        };

                for (
                    const track
                    of remembered.video || []
                ) {
                    if (live(track)) {
                        videoTracks.push(track);
                    }
                }
            }

            if (!audioTracks.length) {

                const remembered =
                    window.__superliveGetTracks
                        ? window.__superliveGetTracks()
                        : {
                            video: [],
                            audio: []
                        };

                for (
                    const track
                    of remembered.audio || []
                ) {
                    if (live(track)) {
                        audioTracks.push(track);
                    }
                }
            }

            if (!videoTracks.length) {
                return {
                    ok: false,
                    reason:
                        "No live video track found."
                };
            }

            if (!audioTracks.length) {
                return {
                    ok: false,
                    reason:
                        "No live audio track found."
                };
            }

            const selectedVideo =
                videoTracks[0];

            let selectedAudio = null;

            // Prefer audio from the same MediaStream.
            if (
                src instanceof MediaStream
            ) {
                selectedAudio =
                    src
                        .getAudioTracks()
                        .find(
                            track => live(track)
                        ) || null;
            }

            // Fallback to remembered audio.
            if (!selectedAudio) {
                selectedAudio =
                    audioTracks[0];
            }

            if (!selectedAudio) {
                return {
                    ok: false,
                    reason:
                        "Could not select audio track."
                };
            }

            const stream =
                new MediaStream([
                    selectedVideo,
                    selectedAudio
                ]);

            window.__superliveRecordingStream =
                stream;

            return {
                ok: true,

                videoTrack: {
                    id: selectedVideo.id,
                    readyState:
                        selectedVideo.readyState
                },

                audioTrack: {
                    id: selectedAudio.id,
                    readyState:
                        selectedAudio.readyState
                }
            };
        }
        """
    )

    if not result.get("ok"):
        raise RuntimeError(
            result.get(
                "reason",
                "Track selection failed."
            )
        )

    return result


# ============================================================
# MediaRecorder
# ============================================================

async def start_recorder(page):
    """
    Start MediaRecorder.

    Chunks are placed into a browser-side queue.
    They are transferred incrementally to FFmpeg.
    """

    result = await page.evaluate(
        f"""
        () => {{

            const stream =
                window.__superliveRecordingStream;

            if (!stream) {{
                throw new Error(
                    "Recording stream does not exist."
                );
            }}

            if (!window.MediaRecorder) {{
                throw new Error(
                    "MediaRecorder is not supported."
                );
            }}

            const candidates = [
                "video/webm;codecs=vp9,opus",
                "video/webm;codecs=vp8,opus",
                "video/webm"
            ];

            let mimeType = null;

            for (
                const candidate
                of candidates
            ) {{
                try {{
                    if (
                        MediaRecorder.isTypeSupported(
                            candidate
                        )
                    ) {{
                        mimeType = candidate;
                        break;
                    }}
                }} catch (_) {{}}
            }}

            if (!mimeType) {{
                throw new Error(
                    "No supported WebM MediaRecorder MIME type."
                );
            }}

            window.__superliveRecorderQueue =
                [];

            window.__superliveRecorderDone =
                false;

            window.__superliveRecorderError =
                null;

            window.__superliveRecorderBytes =
                0;

            const recorder =
                new MediaRecorder(
                    stream,
                    {{
                        mimeType,

                        videoBitsPerSecond:
                            {VIDEO_BITRATE},

                        audioBitsPerSecond:
                            {AUDIO_BITRATE}
                    }}
                );

            recorder.addEventListener(
                "dataavailable",
                event => {{

                    if (
                        event.data
                        && event.data.size > 0
                    ) {{
                        window
                            .__superliveRecorderQueue
                            .push(event.data);

                        window
                            .__superliveRecorderBytes
                            += event.data.size;
                    }}
                }}
            );

            recorder.addEventListener(
                "error",
                event => {{

                    window
                        .__superliveRecorderError =
                        event.error
                            ? String(event.error)
                            : "MediaRecorder error";
                }}
            );

            recorder.addEventListener(
                "stop",
                () => {{
                    window
                        .__superliveRecorderDone =
                        true;
                }}
            );

            window.__superliveRecorder =
                recorder;

            recorder.start(1000);

            return {{
                mimeType,

                videoBitsPerSecond:
                    recorder.videoBitsPerSecond,

                audioBitsPerSecond:
                    recorder.audioBitsPerSecond
            }};
        }}
        """
    )

    return result


# ============================================================
# Blob transfer
# ============================================================

async def blob_to_bytes(
    page,
    blob
):
    """
    Transfer a browser Blob to Python
    through base64 in manageable pieces.
    """

    data = await page.evaluate(
        """
        async blob => {

            const buffer =
                await blob.arrayBuffer();

            const bytes =
                new Uint8Array(buffer);

            let binary = "";

            const step =
                1024 * 1024;

            for (
                let i = 0;
                i < bytes.length;
                i += step
            ) {

                const slice =
                    bytes.subarray(
                        i,
                        Math.min(
                            i + step,
                            bytes.length
                        )
                    );

                binary += String.fromCharCode(
                    ...slice
                );
            }

            return btoa(binary);
        }
        """,
        blob
    )

    return base64.b64decode(
        data
    )


# ============================================================
# Queue drain
# ============================================================

async def drain_recorder_queue(
    page,
    ffmpeg_process
):
    """
    Move all currently available MediaRecorder
    chunks to FFmpeg.
    """

    while True:

        error = await page.evaluate(
            """
            () =>
                window.__superliveRecorderError
            """
        )

        if error:
            raise RuntimeError(
                error
            )

        chunks = await page.evaluate(
            """
            () => {

                const q =
                    window.__superliveRecorderQueue
                    || [];

                if (!q.length) {
                    return [];
                }

                window.__superliveRecorderQueue =
                    [];

                return q;
            }
            """
        )

        if chunks:

            for chunk in chunks:

                raw = await blob_to_bytes(
                    page,
                    chunk
                )

                try:
                    ffmpeg_process.stdin.write(
                        raw
                    )

                    await ffmpeg_process.stdin.drain()

                except (
                    BrokenPipeError,
                    ConnectionResetError
                ):
                    raise RuntimeError(
                        "FFmpeg pipe closed unexpectedly."
                    )

        done = await page.evaluate(
            """
            () =>
                window.__superliveRecorderDone
            """
        )

        if done and not chunks:
            break

        await asyncio.sleep(
            0.2
        )


# ============================================================
# Stop MediaRecorder
# ============================================================

async def stop_recorder(page):
    """
    Request final data and then stop MediaRecorder.
    """

    await page.evaluate(
        """
        async () => {

            const recorder =
                window.__superliveRecorder;

            if (!recorder) {
                return;
            }

            if (
                recorder.state !==
                "inactive"
            ) {

                try {
                    recorder.requestData();
                } catch (_) {}

                await new Promise(
                    resolve =>
                        setTimeout(
                            resolve,
                            500
                        )
                );

                if (
                    recorder.state !==
                    "inactive"
                ) {
                    recorder.stop();
                }
            }
        }
        """
    )


# ============================================================
# FFmpeg
# ============================================================

def create_ffmpeg_process(
    prefix
):
    """
    FFmpeg receives WebM through stdin
    and creates MP4 segments.
    """

    output_pattern = str(
        TEMP_DIR
        / f"{prefix}_part_%03d.mp4"
    )

    command = [
        FFMPEG,

        "-hide_banner",
        "-loglevel", "warning",

        "-fflags", "+genpts",

        "-i", "pipe:0",

        "-map", "0:v:0",
        "-map", "0:a:0?",

        "-c:v", "libx264",
        "-preset", "veryfast",
        "-crf", "21",
        "-pix_fmt", "yuv420p",

        "-c:a", "aac",
        "-b:a", "128k",

        "-movflags", "+faststart",

        "-f", "segment",
        "-segment_time",
        str(SEGMENT_SECONDS),

        "-reset_timestamps", "1",

        "-segment_format", "mp4",

        output_pattern
    ]

    return subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE
    )


async def finish_ffmpeg(
    process
):
    """
    Close FFmpeg stdin and wait for FFmpeg
    to finalize all MP4 files.
    """

    if process.stdin:

        try:
            process.stdin.close()
        except Exception:
            pass

    try:
        return_code = await asyncio.to_thread(
            process.wait,
            120
        )

    except subprocess.TimeoutExpired:

        print(
            "[WARN] FFmpeg did not finish in time."
        )

        process.kill()

        return_code = await asyncio.to_thread(
            process.wait
        )

    stderr = b""

    try:
        if process.stderr:
            stderr = process.stderr.read()
    except Exception:
        pass

    if return_code != 0:

        message = stderr.decode(
            "utf-8",
            errors="replace"
        )

        raise RuntimeError(
            "FFmpeg failed with exit code "
            f"{return_code}:\n{message}"
        )


# ============================================================
# FFprobe
# ============================================================

def probe_file(path):
    command = [
        FFPROBE,

        "-v", "error",

        "-show_entries",
        "format=duration,size,format_name",

        "-of",
        "json",

        str(path)
    ]

    result = subprocess.run(
        command,
        capture_output=True,
        text=True
    )

    if result.returncode != 0:
        return None

    try:
        data = json.loads(
            result.stdout
        )

        return data.get(
            "format",
            {}
        )

    except Exception:
        return None


def validate_segment(path):
    if not path.exists():
        return (
            False,
            "file does not exist"
        )

    size = path.stat().st_size

    if size < 1024:
        return (
            False,
            "file is too small"
        )

    info = probe_file(
        path
    )

    if not info:
        return (
            False,
            "ffprobe could not read file"
        )

    duration = float(
        info.get("duration") or 0
    )

    if duration <= 0:
        return (
            False,
            "duration is zero"
        )

    return (
        True,
        {
            "size": size,
            "duration": duration,
            "format": info.get(
                "format_name"
            )
        }
    )


# ============================================================
# Finalize segments
# ============================================================

def finalize_segments(
    prefix
):
    files = sorted(
        TEMP_DIR.glob(
            f"{prefix}_part_*.mp4"
        )
    )

    results = []

    for source in files:

        ok, info = validate_segment(
            source
        )

        if not ok:

            print(
                "[WARN] Invalid segment: "
                f"{source.name} -> {info}"
            )

            continue

        destination = (
            RECORDINGS_DIR
            / source.name
        )

        shutil.move(
            str(source),
            str(destination)
        )

        results.append(
            {
                "file": str(
                    destination
                ),

                "size": info["size"],

                "duration":
                    info["duration"]
            }
        )

        size_mb = (
            info["size"]
            / 1024
            / 1024
        )

        print(
            f"[OK] {destination.name} "
            f"{size_mb:.2f} MB "
            f"{info['duration']:.1f}s"
        )

    return results


# ============================================================
# Recording
# ============================================================

async def record_stream(
    url
):
    ensure_directories()

    prefix = safe_prefix()

    print("=" * 60)
    print("SUPERLIVE RECORDER")
    print("=" * 60)

    print(
        f"URL: {url}"
    )

    print(
        f"Output: {RECORDINGS_DIR}"
    )

    print(
        f"Segment: {SEGMENT_SECONDS}s"
    )

    print(
        f"Duration limit: "
        f"{RECORD_DURATION_SECONDS}s"
    )

    print(
        f"Video bitrate: "
        f"{VIDEO_BITRATE}"
    )

    print(
        f"Audio bitrate: "
        f"{AUDIO_BITRATE}"
    )

    print("=" * 60)

    ffmpeg_process = None

    async with async_playwright() as p:

        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",

                "--autoplay-policy="
                "no-user-gesture-required",

                "--disable-background-timer-throttling",
                "--disable-backgrounding-occluded-windows",
                "--disable-renderer-backgrounding"
            ]
        )

        context = await browser.new_context(
            locale="fr-FR",

            viewport={
                "width": 1280,
                "height": 720
            }
        )

        # IMPORTANT:
        # Hook must be installed before navigation.
        await context.add_init_script(
            WEBRTC_HOOK
        )

        page = await context.new_page()

        page.set_default_timeout(
            PAGE_TIMEOUT_MS
        )

        print(
            "[1/6] Opening page..."
        )

        await page.goto(
            url,
            wait_until="domcontentloaded",
            timeout=PAGE_TIMEOUT_MS
        )

        print(
            "[2/6] Waiting for live video..."
        )

        await wait_for_video(
            page,
            timeout=120
        )

        print(
            "[3/6] Preparing WebRTC tracks..."
        )

        track_info = (
            await prepare_recording_stream(
                page
            )
        )

        print(
            "[OK] Tracks:",
            json.dumps(
                track_info,
                ensure_ascii=False
            )
        )

        print(
            "[4/6] Starting MediaRecorder..."
        )

        recorder_info = (
            await start_recorder(
                page
            )
        )

        print(
            "[OK] MediaRecorder:",
            json.dumps(
                recorder_info,
                ensure_ascii=False
            )
        )

        print(
            "[5/6] Starting FFmpeg..."
        )

        ffmpeg_process = (
            create_ffmpeg_process(
                prefix
            )
        )

        start_time = (
            time.monotonic()
        )

        print(
            "[OK] Recording started."
        )

        try:

            while True:

                # ------------------------------------------------
                # Stop requested externally?
                # ------------------------------------------------

                if _stop_requested:
                    print(
                        "[INFO] External stop detected."
                    )
                    break

                # ------------------------------------------------
                # Duration limit reached?
                # ------------------------------------------------

                elapsed = (
                    time.monotonic()
                    - start_time
                )

                if (
                    RECORD_DURATION_SECONDS > 0
                    and elapsed
                    >= RECORD_DURATION_SECONDS
                ):
                    print(
                        "[INFO] Recording duration "
                        "limit reached."
                    )

                    break

                # ------------------------------------------------
                # Drain browser chunks.
                # ------------------------------------------------

                await drain_recorder_queue(
                    page,
                    ffmpeg_process
                )

                # ------------------------------------------------
                # Check track health.
                # ------------------------------------------------

                health = await page.evaluate(
                    """
                    () => {

                        const stream =
                            window.__superliveRecordingStream;

                        if (!stream) {
                            return {
                                ok: false,
                                reason:
                                    "stream missing"
                            };
                        }

                        const tracks =
                            stream.getTracks();

                        return {
                            ok: true,

                            tracks:
                                tracks.map(
                                    t => ({
                                        kind: t.kind,
                                        id: t.id,
                                        readyState:
                                            t.readyState,
                                        enabled:
                                            t.enabled,
                                        muted:
                                            t.muted
                                    })
                                )
                        };
                    }
                    """
                )

                print(
                    f"[HEALTH] "
                    f"{elapsed / 60:.1f} min | "
                    f"{json.dumps(health, ensure_ascii=False)}"
                )

                if not health.get("ok"):

                    raise RuntimeError(
                        health.get(
                            "reason",
                            "recording stream failed"
                        )
                    )

                dead_tracks = [
                    track
                    for track in health["tracks"]
                    if track["readyState"]
                    != "live"
                ]

                if dead_tracks:

                    raise RuntimeError(
                        "A recording track stopped: "
                        + json.dumps(
                            dead_tracks,
                            ensure_ascii=False
                        )
                    )

                await asyncio.sleep(
                    HEALTH_INTERVAL
                )

        except asyncio.CancelledError:

            print(
                "[INFO] Recording cancelled."
            )

        finally:

            print(
                "[6/6] Stopping MediaRecorder..."
            )

            try:
                await stop_recorder(
                    page
                )

            except Exception as exc:

                print(
                    "[WARN] Could not stop "
                    f"MediaRecorder: {exc}"
                )

            # ----------------------------------------------------
            # Drain final dataavailable event.
            # ----------------------------------------------------

            try:

                await drain_recorder_queue(
                    page,
                    ffmpeg_process
                )

            except Exception as exc:

                print(
                    "[WARN] Final drain failed: "
                    f"{exc}"
                )

            print(
                "[INFO] Finalizing FFmpeg..."
            )

            try:

                await finish_ffmpeg(
                    ffmpeg_process
                )

            except Exception as exc:

                print(
                    f"[ERROR] {exc}"
                )

            ffmpeg_process = None

        await context.close()

        await browser.close()

    segments = finalize_segments(
        prefix
    )

    if not segments:

        raise RuntimeError(
            "Recording finished but no valid "
            "MP4 segments were produced."
        )

    total_size = sum(
        segment["size"]
        for segment in segments
    )

    total_duration = sum(
        segment["duration"]
        for segment in segments
    )

    print("=" * 60)
    print("RECORDING COMPLETE")
    print("=" * 60)

    print(
        f"Segments: {len(segments)}"
    )

    print(
        f"Duration: "
        f"{total_duration / 60:.2f} min"
    )

    print(
        f"Size: "
        f"{total_size / 1024 / 1024:.2f} MB"
    )

    print("=" * 60)

    return segments


# ============================================================
# Main
# ============================================================

async def main_async():

    ensure_directories()

    check_program(
        FFMPEG
    )

    check_program(
        FFPROBE
    )

    if len(sys.argv) >= 2:

        url = sys.argv[1].strip()

    else:

        url = input(
            "Enter livestream URL: "
        ).strip()

    if not url:

        raise RuntimeError(
            "No URL provided."
        )

    return await record_stream(
        url
    )


def main():

    signal.signal(
        signal.SIGINT,
        request_stop
    )

    if hasattr(
        signal,
        "SIGTERM"
    ):
        signal.signal(
            signal.SIGTERM,
            request_stop
        )

    try:

        asyncio.run(
            main_async()
        )

    except KeyboardInterrupt:

        print(
            "\n[INFO] Stopped by user."
        )

    except Exception as exc:

        print(
            f"\n[FATAL] {exc}"
        )

        sys.exit(1)


if __name__ == "__main__":
    main()
