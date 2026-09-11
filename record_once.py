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

SEGMENT_SECONDS = int(os.getenv("SEGMENT_SECONDS", "90"))

# Browser MediaRecorder bitrate.
# 4 Mbps video + 128 kbps audio gives a reasonable starting point.
VIDEO_BITRATE = int(os.getenv("VIDEO_BITRATE", "4000000"))
AUDIO_BITRATE = int(os.getenv("AUDIO_BITRATE", "128000"))

PAGE_TIMEOUT_MS = int(os.getenv("PAGE_TIMEOUT_MS", "60000"))

HEALTH_INTERVAL = 10

# We stop a segment before it becomes dangerously large.
# The final Telegram safety check is done outside this recorder.
MAX_EXPECTED_MB = 45

FFMPEG = os.getenv("FFMPEG", "ffmpeg")
FFPROBE = os.getenv("FFPROBE", "ffprobe")


# ============================================================
# WebRTC hook
# Must be installed BEFORE page navigation.
# ============================================================

WEBRTC_HOOK = r"""
(() => {
    if (window.__superliveWebRTCHookInstalled) return;
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

    if (!OriginalPC) return;

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
            pc.addEventListener("track", event => {
                try {
                    rememberTrack(event.track);

                    if (event.streams) {
                        for (const stream of event.streams) {
                            rememberStream(stream);
                        }
                    }
                } catch (_) {}
            });
        } catch (_) {}

        return pc;
    }

    HookedPC.prototype = OriginalPC.prototype;

    for (const key of Object.keys(OriginalPC)) {
        try {
            HookedPC[key] = OriginalPC[key];
        } catch (_) {}
    }

    window.RTCPeerConnection = HookedPC;

    window.__superliveGetTracks = () => {
        return {
            audio: Array.from(window.__superliveTracks.audio),
            video: Array.from(window.__superliveTracks.video),
            all: Array.from(window.__superliveTracks.all)
        };
    };
})();
"""


# ============================================================
# Utilities
# ============================================================

def ensure_directories():
    RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)
    TEMP_DIR.mkdir(parents=True, exist_ok=True)


def check_program(name: str):
    path = shutil.which(name)

    if not path:
        raise RuntimeError(
            f"Required program not found: {name}"
        )

    return path


def timestamp():
    return time.strftime("%Y%m%d_%H%M%S")


def safe_prefix():
    return f"recording_{timestamp()}_{int(time.time())}"


# ============================================================
# Video discovery
# ============================================================

async def find_video(page):
    """
    Search main document and child frames for a usable video.
    """

    pages = [page]

    for frame in page.frames:
        if frame != page.main_frame:
            try:
                # Frame is handled below.
                pass
            except Exception:
                pass

    for target in pages:
        try:
            videos = await target.locator("video").all()

            if videos:
                for video in videos:
                    try:
                        box = await video.bounding_box()

                        if box and box["width"] > 0 and box["height"] > 0:
                            return video
                    except Exception:
                        continue

                return videos[0]

        except Exception:
            pass

    for frame in page.frames:
        try:
            videos = await frame.locator("video").all()

            if videos:
                for video in videos:
                    try:
                        box = await video.bounding_box()

                        if box and box["width"] > 0 and box["height"] > 0:
                            return video
                    except Exception:
                        continue

                return videos[0]

        except Exception:
            continue

    return None


async def wait_for_video(page, timeout=120):
    """
    Wait until the video actually advances.
    """

    deadline = time.monotonic() + timeout

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
                    first_time = state["currentTime"]

                    await asyncio.sleep(2)

                    state2 = await video.evaluate(
                        "v => v.currentTime"
                    )

                    if state2 > first_time:
                        return video

            except Exception:
                pass

        await asyncio.sleep(2)

    raise RuntimeError(
        "A playable video could not be detected."
    )


# ============================================================
# Audio/video track selection
# ============================================================

async def prepare_recording_stream(page):
    """
    Create a MediaStream containing the actual WebRTC video track
    and the best available live audio track.
    """

    video = await find_video(page)

    if not video:
        raise RuntimeError("Video element not found.")

    result = await video.evaluate(
        """
        async (video) => {

            function live(track) {
                return track &&
                       track.readyState === "live";
            }

            const videoTracks = [];
            const audioTracks = [];

            const src = video.srcObject;

            if (src instanceof MediaStream) {
                for (const track of src.getTracks()) {
                    if (track.kind === "video" && live(track)) {
                        videoTracks.push(track);
                    }

                    if (track.kind === "audio" && live(track)) {
                        audioTracks.push(track);
                    }
                }
            }

            if (!videoTracks.length) {
                const remembered =
                    window.__superliveGetTracks
                        ? window.__superliveGetTracks()
                        : {video: [], audio: []};

                for (const track of remembered.video || []) {
                    if (live(track)) {
                        videoTracks.push(track);
                    }
                }
            }

            if (!audioTracks.length) {
                const remembered =
                    window.__superliveGetTracks
                        ? window.__superliveGetTracks()
                        : {video: [], audio: []};

                for (const track of remembered.audio || []) {
                    if (live(track)) {
                        audioTracks.push(track);
                    }
                }
            }

            if (!videoTracks.length) {
                return {
                    ok: false,
                    reason: "No live video track found."
                };
            }

            if (!audioTracks.length) {
                return {
                    ok: false,
                    reason: "No live audio track found."
                };
            }

            const selectedVideo = videoTracks[0];

            let selectedAudio = null;

            // Prefer audio from the same srcObject.
            if (src instanceof MediaStream) {
                selectedAudio =
                    src.getAudioTracks()
                       .find(track => live(track)) || null;
            }

            // Otherwise choose remembered live audio.
            if (!selectedAudio) {
                selectedAudio = audioTracks[0];
            }

            if (!selectedAudio) {
                return {
                    ok: false,
                    reason: "Could not select audio track."
                };
            }

            const stream =
                new MediaStream([
                    selectedVideo,
                    selectedAudio
                ]);

            window.__superliveRecordingStream = stream;

            return {
                ok: true,
                videoTrack: {
                    id: selectedVideo.id,
                    readyState: selectedVideo.readyState
                },
                audioTrack: {
                    id: selectedAudio.id,
                    readyState: selectedAudio.readyState
                }
            };
        }
        """
    )

    if not result.get("ok"):
        raise RuntimeError(result.get("reason", "Track selection failed."))

    return result


# ============================================================
# MediaRecorder
# ============================================================

async def start_recorder(page):
    """
    Start MediaRecorder and expose chunks through a small queue.
    """

    result = await page.evaluate(
        f"""
        () => {{
            const stream = window.__superliveRecordingStream;

            if (!stream) {{
                throw new Error("Recording stream does not exist.");
            }}

            if (!window.MediaRecorder) {{
                throw new Error("MediaRecorder is not supported.");
            }}

            const candidates = [
                "video/webm;codecs=vp9,opus",
                "video/webm;codecs=vp8,opus",
                "video/webm"
            ];

            let mimeType = null;

            for (const candidate of candidates) {{
                try {{
                    if (MediaRecorder.isTypeSupported(candidate)) {{
                        mimeType = candidate;
                        break;
                    }}
                }} catch (_) {{}}
            }}

            if (!mimeType) {{
                throw new Error("No supported WebM MediaRecorder MIME type.");
            }}

            window.__superliveRecorderQueue = [];
            window.__superliveRecorderDone = false;
            window.__superliveRecorderError = null;
            window.__superliveRecorderBytes = 0;

            const recorder = new MediaRecorder(
                stream,
                {{
                    mimeType,
                    videoBitsPerSecond: {VIDEO_BITRATE},
                    audioBitsPerSecond: {AUDIO_BITRATE}
                }}
            );

            recorder.addEventListener("dataavailable", event => {{
                if (event.data && event.data.size > 0) {{
                    window.__superliveRecorderQueue.push(event.data);
                    window.__superliveRecorderBytes += event.data.size;
                }}
            }});

            recorder.addEventListener("error", event => {{
                window.__superliveRecorderError =
                    event.error
                        ? String(event.error)
                        : "MediaRecorder error";
            }});

            recorder.addEventListener("stop", () => {{
                window.__superliveRecorderDone = true;
            }});

            window.__superliveRecorder = recorder;

            recorder.start(1000);

            return {{
                mimeType,
                videoBitsPerSecond: recorder.videoBitsPerSecond,
                audioBitsPerSecond: recorder.audioBitsPerSecond
            }};
        }}
        """
    )

    return result


async def drain_recorder_queue(page, ffmpeg_process):
    """
    Move MediaRecorder chunks from browser to FFmpeg.
    """

    while True:

        try:
            error = await page.evaluate(
                "() => window.__superliveRecorderError"
            )

            if error:
                raise RuntimeError(error)

        except Exception:
            raise

        chunks = await page.evaluate(
            """
            () => {
                const q = window.__superliveRecorderQueue || [];

                if (!q.length) {
                    return [];
                }

                window.__superliveRecorderQueue = [];

                return q;
            }
            """
        )

        if chunks:
            for chunk in chunks:
                # Transfer Blob through base64 in manageable pieces.
                data = await page.evaluate(
                    """
                    async blob => {
                        const buffer = await blob.arrayBuffer();
                        const bytes = new Uint8Array(buffer);

                        let binary = "";

                        const step = 1024 * 1024;

                        for (let i = 0; i < bytes.length; i += step) {
                            const slice =
                                bytes.subarray(
                                    i,
                                    Math.min(i + step, bytes.length)
                                );

                            binary += String.fromCharCode(...slice);
                        }

                        return btoa(binary);
                    }
                    """,
                    chunk
                )

                raw = base64.b64decode(data)

                try:
                    ffmpeg_process.stdin.write(raw)

                    await ffmpeg_process.stdin.drain()

                except (BrokenPipeError, ConnectionResetError):
                    raise RuntimeError(
                        "FFmpeg pipe closed unexpectedly."
                    )

        done = await page.evaluate(
            "() => window.__superliveRecorderDone"
        )

        if done and not chunks:
            break

        await asyncio.sleep(0.2)


async def stop_recorder(page):
    """
    Stop MediaRecorder cleanly.
    """

    await page.evaluate(
        """
        async () => {
            const recorder = window.__superliveRecorder;

            if (!recorder) return;

            if (recorder.state !== "inactive") {
                try {
                    recorder.requestData();
                } catch (_) {}

                await new Promise(resolve => setTimeout(resolve, 500));

                recorder.stop();
            }
        }
        """
    )


# ============================================================
# FFmpeg
# ============================================================

def create_ffmpeg_process(prefix):
    """
    FFmpeg receives WebM from stdin and creates MP4 segments.
    """

    output_pattern = str(
        TEMP_DIR / f"{prefix}_part_%03d.mp4"
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
        "-segment_time", str(SEGMENT_SECONDS),
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


async def finish_ffmpeg(process):
    """
    Close FFmpeg input and wait for completion.
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
            f"FFmpeg failed with exit code {return_code}:\n{message}"
        )


# ============================================================
# FFprobe validation
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
        data = json.loads(result.stdout)
        return data.get("format", {})
    except Exception:
        return None


def validate_segment(path):
    if not path.exists():
        return False, "file does not exist"

    size = path.stat().st_size

    if size < 1024:
        return False, "file is too small"

    info = probe_file(path)

    if not info:
        return False, "ffprobe could not read file"

    duration = float(
        info.get("duration") or 0
    )

    if duration <= 0:
        return False, "duration is zero"

    return True, {
        "size": size,
        "duration": duration,
        "format": info.get("format_name")
    }


# ============================================================
# Move completed segments to recordings/
# ============================================================

def finalize_segments(prefix):
    files = sorted(
        TEMP_DIR.glob(
            f"{prefix}_part_*.mp4"
        )
    )

    results = []

    for source in files:

        ok, info = validate_segment(source)

        if not ok:
            print(
                f"[WARN] Invalid segment: "
                f"{source.name} -> {info}"
            )
            continue

        destination = RECORDINGS_DIR / source.name

        shutil.move(
            str(source),
            str(destination)
        )

        results.append(
            {
                "file": str(destination),
                "size": info["size"],
                "duration": info["duration"]
            }
        )

        size_mb = info["size"] / 1024 / 1024

        print(
            f"[OK] {destination.name} "
            f"{size_mb:.2f} MB "
            f"{info['duration']:.1f}s"
        )

    return results


# ============================================================
# Recording
# ============================================================

async def record_stream(url):
    ensure_directories()

    prefix = safe_prefix()

    print("=" * 60)
    print("SUPERLIVE RECORDER")
    print("=" * 60)
    print(f"URL: {url}")
    print(f"Output: {RECORDINGS_DIR}")
    print(f"Segment: {SEGMENT_SECONDS}s")
    print(f"Video bitrate: {VIDEO_BITRATE}")
    print(f"Audio bitrate: {AUDIO_BITRATE}")
    print("=" * 60)

    ffmpeg_process = None

    async with async_playwright() as p:

        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",

                # WebRTC-related stability options.
                "--autoplay-policy=no-user-gesture-required",

                # Avoid background throttling.
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
        # Install before goto/navigation.
        await context.add_init_script(
            WEBRTC_HOOK
        )

        page = await context.new_page()

        page.set_default_timeout(
            PAGE_TIMEOUT_MS
        )

        print("[1/6] Opening page...")

        await page.goto(
            url,
            wait_until="domcontentloaded",
            timeout=PAGE_TIMEOUT_MS
        )

        print("[2/6] Waiting for live video...")

        await wait_for_video(
            page,
            timeout=120
        )

        print("[3/6] Preparing WebRTC tracks...")

        track_info = await prepare_recording_stream(
            page
        )

        print(
            "[OK] Tracks:",
            json.dumps(
                track_info,
                ensure_ascii=False
            )
        )

        print("[4/6] Starting MediaRecorder...")

        recorder_info = await start_recorder(
            page
        )

        print(
            "[OK] MediaRecorder:",
            json.dumps(
                recorder_info,
                ensure_ascii=False
            )
        )

        print("[5/6] Starting FFmpeg...")

        ffmpeg_process = create_ffmpeg_process(
            prefix
        )

        start_time = time.monotonic()

        print(
            "[OK] Recording started."
        )

        print(
            "Recording continues until the process is stopped."
        )

        try:

            while True:

                # Drain browser chunks.
                await drain_recorder_queue(
                    page,
                    ffmpeg_process
                )

                elapsed = (
                    time.monotonic()
                    - start_time
                )

                # Check track health.
                health = await page.evaluate(
                    """
                    () => {
                        const stream =
                            window.__superliveRecordingStream;

                        if (!stream) {
                            return {
                                ok: false,
                                reason: "stream missing"
                            };
                        }

                        const tracks =
                            stream.getTracks();

                        return {
                            ok: true,
                            tracks: tracks.map(t => ({
                                kind: t.kind,
                                id: t.id,
                                readyState: t.readyState,
                                enabled: t.enabled,
                                muted: t.muted
                            }))
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
                    t for t in health["tracks"]
                    if t["readyState"] != "live"
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
            print("[INFO] Recording cancelled.")

        finally:

            print("[6/6] Stopping recorder...")

            try:
                await stop_recorder(page)
            except Exception as exc:
                print(
                    f"[WARN] Could not stop MediaRecorder: {exc}"
                )

            # Drain the final dataavailable event.
            try:
                await drain_recorder_queue(
                    page,
                    ffmpeg_process
                )
            except Exception as exc:
                print(
                    f"[WARN] Final drain failed: {exc}"
                )

            print("[INFO] Finalizing FFmpeg...")

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
            "Recording finished but no valid MP4 segments were produced."
        )

    total_size = sum(
        x["size"]
        for x in segments
    )

    total_duration = sum(
        x["duration"]
        for x in segments
    )

    print("=" * 60)
    print("RECORDING COMPLETE")
    print(
        f"Segments: {len(segments)}"
    )
    print(
        f"Duration: {total_duration / 60:.2f} min"
    )
    print(
        f"Size: {total_size / 1024 / 1024:.2f} MB"
    )
    print("=" * 60)

    return segments


# ============================================================
# Signal handling
# ============================================================

_stop_requested = False


def request_stop(signum, frame):
    global _stop_requested

    _stop_requested = True

    print(
        "\n[INFO] Stop requested. "
        "Finishing current recording safely..."
    )


# ============================================================
# Main
# ============================================================

async def main_async():
    ensure_directories()

    check_program(FFMPEG)
    check_program(FFPROBE)

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

    if hasattr(signal, "SIGTERM"):
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
