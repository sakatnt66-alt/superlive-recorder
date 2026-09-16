import asyncio
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path


# ============================================================
# TELEGRAM
# ============================================================

TELEGRAM_MAX_SIZE_MB = 44.9
TELEGRAM_TARGET_SIZE_MB = 36.0

UPLOAD_MAX_RETRIES = 3
UPLOAD_RETRY_DELAY_SECONDS = 5

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")


# ============================================================
# WORKER / KV
# ============================================================

WORKER_URL = os.environ.get("WORKER_URL", "").rstrip("/")


def kv_get_state(stream_id):
    if not WORKER_URL or not stream_id:
        return None

    url = f"{WORKER_URL}/api/check-stop/{stream_id}"

    try:
        req = urllib.request.Request(
            url,
            method="GET",
            headers={"User-Agent": "SuperLiveRecorder/1.0"},
        )

        with urllib.request.urlopen(req, timeout=15) as response:
            raw = response.read().decode("utf-8", errors="replace")

        try:
            return json.loads(raw)
        except Exception:
            return raw

    except Exception as e:
        log(f"KV GET error: {e}")
        return None


def kv_delete_state(stream_id):
    if not WORKER_URL or not stream_id:
        return False

    url = f"{WORKER_URL}/api/delete-recording/{stream_id}"

    try:
        req = urllib.request.Request(
            url,
            method="DELETE",
            headers={"User-Agent": "SuperLiveRecorder/1.0"},
        )

        with urllib.request.urlopen(req, timeout=15) as response:
            response.read()

        return True

    except Exception as e:
        log(f"KV DELETE error: {e}")
        return False


def kv_update_state(stream_id, state, **extra):
    if not WORKER_URL or not stream_id:
        return False

    url = f"{WORKER_URL}/api/update-state/{stream_id}"

    payload = {
        "state": state,
        **extra,
    }

    try:
        data = json.dumps(payload).encode("utf-8")

        req = urllib.request.Request(
            url,
            data=data,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "User-Agent": "SuperLiveRecorder/1.0",
            },
        )

        with urllib.request.urlopen(req, timeout=15) as response:
            response.read()

        return True

    except Exception as e:
        log(f"KV UPDATE error: {e}")
        return False


def check_stop_requested(stream_id):
    state = kv_get_state(stream_id)

    if state is None:
        return False

    if isinstance(state, dict):
        for key in (
            "stop",
            "stopRequested",
            "stop_requested",
            "shouldStop",
            "should_stop",
        ):
            value = state.get(key)

            if value is True:
                return True

            if isinstance(value, str) and value.lower() in (
                "true",
                "1",
                "yes",
                "stop",
            ):
                return True

    elif isinstance(state, str):
        value = state.lower()

        if value in ("true", "1", "stop", "stopped"):
            return True

    return False


# ============================================================
# TELEGRAM NOTIFICATION
# ============================================================

def send_telegram_notification(text):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return False

    url = (
        f"https://api.telegram.org/bot"
        f"{TELEGRAM_BOT_TOKEN}/sendMessage"
    )

    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
    }

    try:
        data = json.dumps(payload).encode("utf-8")

        req = urllib.request.Request(
            url,
            data=data,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "User-Agent": "SuperLiveRecorder/1.0",
            },
        )

        with urllib.request.urlopen(req, timeout=20) as response:
            response.read()

        return True

    except Exception as e:
        log(f"Telegram notification error: {e}")
        return False


# ============================================================
# LOGGING
# ============================================================

def log(message):
    print(
        f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] "
        f"{message}",
        flush=True,
    )


def log_section(title):
    print()
    print("=" * 70, flush=True)
    print(title, flush=True)
    print("=" * 70, flush=True)


# ============================================================
# CONFIG
# ============================================================

URL = os.environ.get("RECORD_URL", "")

RECORDING_DIR = Path(
    os.environ.get("RECORDING_DIR", "recordings")
)

TEMP_DIR = Path(
    os.environ.get("TEMP_DIR", "tmp_recordings")
)

RECORDING_DIR.mkdir(parents=True, exist_ok=True)
TEMP_DIR.mkdir(parents=True, exist_ok=True)

VIDEO_BITRATE = 8_000_000
AUDIO_BITRATE = 192_000

VIDEO_WAIT_SECONDS = 60
PAGE_TIMEOUT_MS = 30_000
FIRST_CHUNK_TIMEOUT_SECONDS = 10

STREAM_ID = os.environ.get("STREAM_ID", "")

STOP_CHECK_INTERVAL = 3
STREAM_IDLE_TIMEOUT = 20

MIN_CHUNK_SIZE = 500

MAX_RECORDING_SECONDS = 6 * 3600

GLOBAL_WATCHDOG_SECONDS = (
    MAX_RECORDING_SECONDS + 1800
)

# ------------------------------------------------------------
# IMPORTANT:
#
# The local upload queue can legitimately become large if
# Playwright/Node-side request handling is temporarily slower
# than MediaRecorder.
#
# We must NOT close the browser while chunks remain queued.
#
# This timeout is only a safety limit. The global watchdog
# remains the ultimate process limit.
# ------------------------------------------------------------

FINAL_QUEUE_DRAIN_TIMEOUT_SECONDS = 15 * 60

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)


# ============================================================
# VIDEO HELPERS
# ============================================================

def run_command(command, timeout=None):
    log("Running command:")
    log(" ".join(str(x) for x in command))

    result = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=timeout,
    )

    if result.stdout:
        log(result.stdout.strip())

    if result.stderr:
        log(result.stderr.strip())

    return result


def verify_video_file(path, allow_zero_duration=False):
    path = Path(path)

    if not path.exists():
        log(
            f"Video verification failed: "
            f"file does not exist: {path}"
        )
        return False

    size = path.stat().st_size

    if size <= 10 * 1024:
        log(
            f"Video verification failed: "
            f"file too small ({size} bytes)"
        )
        return False

    command = [
        "ffprobe",
        "-hide_banner",
        "-v",
        "error",
        "-count_packets",
        "-show_entries",
        "format=format_name,duration,size",
        "-show_entries",
        "stream=index,codec_type,codec_name,width,height,"
        "r_frame_rate,avg_frame_rate,time_base,start_time,"
        "duration,nb_read_packets",
        "-of",
        "json",
        str(path),
    ]

    try:
        result = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=60,
        )

        if result.returncode != 0:
            log(
                "ffprobe verification failed: "
                + result.stderr.strip()
            )
            return False

        info = json.loads(result.stdout)

        fmt = info.get("format", {})
        streams = info.get("streams", [])

        try:
            duration = float(
                fmt.get("duration") or 0
            )
        except Exception:
            duration = 0.0

        video_streams = [
            s
            for s in streams
            if s.get("codec_type") == "video"
        ]

        if not video_streams:
            log(
                "Video verification failed: "
                "no video stream"
            )
            return False

        video_stream = video_streams[0]

        try:
            packet_count = int(
                video_stream.get("nb_read_packets") or 0
            )
        except Exception:
            packet_count = 0

        codec_name = video_stream.get("codec_name")

        width = int(
            video_stream.get("width") or 0
        )

        height = int(
            video_stream.get("height") or 0
        )

        if not codec_name:
            log(
                "Video verification failed: "
                "video codec is missing"
            )
            return False

        if width <= 0 or height <= 0:
            log(
                "Video verification failed: "
                f"invalid video dimensions "
                f"{width}x{height}"
            )
            return False

        if packet_count <= 0:
            log(
                "Video verification failed: "
                "video stream contains no readable packets"
            )
            return False

        if duration <= 1:
            if allow_zero_duration:
                log(
                    "WARNING: WebM container duration is "
                    f"{duration}, but video stream is valid "
                    f"({codec_name}, {width}x{height}, "
                    f"packets={packet_count})."
                )
            else:
                log(
                    f"Video verification failed: "
                    f"duration={duration}"
                )
                return False

        log(
            f"Verified video: "
            f"{size / 1024 / 1024:.2f} MB, "
            f"duration={duration:.3f}s, "
            f"codec={codec_name}, "
            f"resolution={width}x{height}, "
            f"video_packets={packet_count}"
        )

        return True

    except Exception as e:
        log(
            f"Video verification exception: {e}"
        )
        return False


def get_video_info(path):
    command = [
        "ffprobe",
        "-hide_banner",
        "-v",
        "error",
        "-show_entries",
        "stream=index,codec_type,codec_name,width,height,"
        "r_frame_rate,avg_frame_rate,time_base,start_time,duration,"
        "nb_frames",
        "-show_entries",
        "format=format_name,duration,size",
        "-of",
        "json",
        str(path),
    ]

    try:
        result = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=60,
        )

        if result.returncode != 0:
            log(
                "ffprobe failed: "
                + result.stderr.strip()
            )
            return None

        return json.loads(result.stdout)

    except Exception as e:
        log(
            f"get_video_info error: {e}"
        )
        return None


def get_duration(info):
    if not info:
        return 0.0

    try:
        return float(
            info.get("format", {}).get("duration")
            or 0
        )
    except Exception:
        return 0.0


def get_video_packet_count(info):
    if not info:
        return 0

    for stream in info.get("streams", []):
        if stream.get("codec_type") != "video":
            continue

        try:
            return int(
                stream.get("nb_frames")
                or 0
            )
        except Exception:
            return 0

    return 0


def log_video_info(label, path):
    info = get_video_info(path)

    if not info:
        log(
            f"{label}: "
            "ffprobe information unavailable"
        )
        return None

    fmt = info.get("format", {})

    log(
        f"{label}: "
        f"format={fmt.get('format_name')} "
        f"duration={fmt.get('duration')} "
        f"size={fmt.get('size')}"
    )

    for stream in info.get("streams", []):
        if stream.get("codec_type") == "video":
            log(
                f"{label} video: "
                f"codec={stream.get('codec_name')} "
                f"resolution="
                f"{stream.get('width')}x"
                f"{stream.get('height')} "
                f"r_frame_rate="
                f"{stream.get('r_frame_rate')} "
                f"avg_frame_rate="
                f"{stream.get('avg_frame_rate')} "
                f"time_base="
                f"{stream.get('time_base')} "
                f"start_time="
                f"{stream.get('start_time')} "
                f"duration="
                f"{stream.get('duration')} "
                f"nb_frames="
                f"{stream.get('nb_frames')}"
            )

        elif stream.get("codec_type") == "audio":
            log(
                f"{label} audio: "
                f"codec={stream.get('codec_name')} "
                f"time_base="
                f"{stream.get('time_base')} "
                f"start_time="
                f"{stream.get('start_time')} "
                f"duration="
                f"{stream.get('duration')}"
            )

    return info


# ============================================================
# TELEGRAM UPLOAD
# ============================================================

def send_to_telegram(path, caption=""):
    path = Path(path)

    if not path.exists():
        log(
            f"Telegram upload failed: "
            f"{path} does not exist"
        )
        return False

    if (
        not TELEGRAM_BOT_TOKEN
        or not TELEGRAM_CHAT_ID
    ):
        log(
            "Telegram credentials are missing"
        )
        return False

    info = get_video_info(path)

    width = 0
    height = 0
    duration = 0

    if info:
        duration = int(
            max(
                0,
                round(
                    get_duration(info)
                ),
            )
        )

        for stream in info.get("streams", []):
            if stream.get("codec_type") == "video":
                width = int(
                    stream.get("width") or 0
                )

                height = int(
                    stream.get("height") or 0
                )

                break

    url = (
        f"https://api.telegram.org/bot"
        f"{TELEGRAM_BOT_TOKEN}/sendVideo"
    )

    boundary = (
        "----SuperLiveRecorderBoundary"
        + str(int(time.time() * 1000))
    )

    fields = {
        "chat_id": str(TELEGRAM_CHAT_ID),
        "caption": caption,
        "parse_mode": "HTML",
        "supports_streaming": "true",
        "duration": str(duration),
        "width": str(width),
        "height": str(height),
    }

    body = bytearray()

    for key, value in fields.items():
        body.extend(
            (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; '
                f'name="{key}"\r\n\r\n'
                f"{value}\r\n"
            ).encode("utf-8")
        )

    filename = path.name

    body.extend(
        (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; '
            f'name="video"; filename="{filename}"\r\n'
            f"Content-Type: video/mp4\r\n\r\n"
        ).encode("utf-8")
    )

    try:
        with open(path, "rb") as f:
            body.extend(f.read())

    except Exception as e:
        log(
            f"Unable to read video for Telegram: {e}"
        )
        return False

    body.extend(
        f"\r\n--{boundary}--\r\n".encode(
            "utf-8"
        )
    )

    try:
        req = urllib.request.Request(
            url,
            data=bytes(body),
            method="POST",
            headers={
                "Content-Type": (
                    f"multipart/form-data; "
                    f"boundary={boundary}"
                ),
                "User-Agent":
                    "SuperLiveRecorder/1.0",
            },
        )

        with urllib.request.urlopen(
            req,
            timeout=600,
        ) as response:
            response_body = (
                response.read()
                .decode(
                    "utf-8",
                    errors="replace",
                )
            )

        try:
            result = json.loads(
                response_body
            )

            if result.get("ok"):
                log(
                    f"Telegram upload successful: "
                    f"{path.name}"
                )
                return True

            log(
                "Telegram returned failure: "
                f"{response_body}"
            )

        except Exception:
            log(
                "Telegram response: "
                f"{response_body}"
            )

    except urllib.error.HTTPError as e:
        try:
            error_body = (
                e.read()
                .decode(
                    "utf-8",
                    errors="replace",
                )
            )
        except Exception:
            error_body = str(e)

        log(
            f"Telegram HTTP error {e.code}: "
            f"{error_body}"
        )

    except Exception as e:
        log(
            f"Telegram upload error: "
            f"{e}"
        )

    return False


def send_to_telegram_with_retry(
    path,
    caption="",
):
    for attempt in range(
        1,
        UPLOAD_MAX_RETRIES + 1,
    ):
        log(
            f"Telegram upload attempt "
            f"{attempt}/"
            f"{UPLOAD_MAX_RETRIES}: "
            f"{path}"
        )

        if send_to_telegram(
            path,
            caption,
        ):
            return True

        if attempt < UPLOAD_MAX_RETRIES:
            log(
                f"Waiting "
                f"{UPLOAD_RETRY_DELAY_SECONDS}s "
                f"before retry..."
            )

            time.sleep(
                UPLOAD_RETRY_DELAY_SECONDS
            )

    return False


# ============================================================
# FFMPEG CONVERSION
# ============================================================

def convert_webm_to_mp4(
    webm_path,
    mp4_path,
):
    """
    Convert a COMPLETE MediaRecorder WebM into H.264/AAC MP4.

    The important design rule here is:

        Do not manufacture a CFR timeline.

    The WebM is expected to contain the original MediaRecorder
    timestamps. Video timing is therefore preserved as VFR.

    Audio timestamps are normalized separately because the
    previous recordings showed actual backward audio timestamps.

    This function deliberately does NOT use:

        -fflags +genpts
        fps=30
        -r 30
        -fps_mode cfr
        -vsync cfr
        forced keyframes
    """

    webm_path = Path(webm_path)
    mp4_path = Path(mp4_path)

    if not webm_path.exists():
        raise FileNotFoundError(
            webm_path
        )

    log_section(
        "WEBM SOURCE ANALYSIS"
    )

    webm_info = log_video_info(
        "WEBM BEFORE CONVERSION",
        webm_path,
    )

    if not webm_info:
        raise RuntimeError(
            "Unable to analyze source WebM"
        )

    webm_duration = get_duration(
        webm_info
    )

    webm_video_packets = (
        get_video_packet_count(
            webm_info
        )
    )

    log(
        f"Source WebM duration: "
        f"{webm_duration:.3f}s"
    )

    log(
        f"Source WebM video packets: "
        f"{webm_video_packets}"
    )

    log_section(
        "FFMPEG WEBM -> MP4"
    )

    command = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "warning",
        "-y",

        # ----------------------------------------------------
        # INPUT
        #
        # Preserve the timestamps that are already present in
        # the MediaRecorder WebM.
        #
        # Do not use +genpts.
        #
        # start_at_zero shifts the preserved timeline so that
        # the resulting MP4 starts at zero without rebuilding
        # the frame cadence.
        # ----------------------------------------------------

        "-copyts",
        "-start_at_zero",

        "-i",
        str(webm_path),

        # ----------------------------------------------------
        # STREAM SELECTION
        # ----------------------------------------------------

        "-map",
        "0:v:0",

        "-map",
        "0:a:0?",

        # ----------------------------------------------------
        # VIDEO
        # ----------------------------------------------------

        "-vf",
        "pad=width=ceil(iw/2)*2:"
        "height=ceil(ih/2)*2:"
        "color=black",

        "-fps_mode:v",
        "passthrough",

        "-c:v",
        "libx264",

        "-preset",
        "veryfast",

        "-crf",
        "18",

        "-profile:v",
        "main",

        "-pix_fmt",
        "yuv420p",

        "-threads",
        "0",

        "-bf",
        "0",

        # ----------------------------------------------------
        # AUDIO
        #
        # Repair small backwards/discontinuous audio
        # timestamps without changing video cadence.
        # ----------------------------------------------------

        "-af",
        "aresample=async=1:first_pts=0",

        "-c:a",
        "aac",

        "-b:a",
        "192k",

        "-ar",
        "48000",

        "-ac",
        "2",

        # ----------------------------------------------------
        # VIDEO TIMESTAMP RESOLUTION
        # ----------------------------------------------------

        "-video_track_timescale",
        "90000",

        # ----------------------------------------------------
        # Keep audio/video interleaving deterministic.
        # ----------------------------------------------------

        "-max_interleave_delta",
        "0",

        # ----------------------------------------------------
        # MP4
        # ----------------------------------------------------

        "-movflags",
        "+faststart",

        str(mp4_path),
    ]

    start_time = time.monotonic()

    result = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    elapsed = (
        time.monotonic()
        - start_time
    )

    if result.stdout:
        log(
            "FFmpeg stdout:\n"
            + result.stdout.strip()
        )

    if result.stderr:
        log(
            "FFmpeg output:\n"
            + result.stderr.strip()
        )

    log(
        f"FFmpeg conversion finished in "
        f"{elapsed:.1f}s"
    )

    if result.returncode != 0:
        raise RuntimeError(
            "FFmpeg conversion failed:\n"
            + result.stderr
        )

    if not mp4_path.exists():
        raise RuntimeError(
            "FFmpeg reported success but MP4 "
            "file was not created"
        )

    if mp4_path.stat().st_size <= 10 * 1024:
        raise RuntimeError(
            "Generated MP4 is unexpectedly small"
        )

    log_section(
        "MP4 OUTPUT ANALYSIS"
    )

    mp4_info = log_video_info(
        "MP4 AFTER CONVERSION",
        mp4_path,
    )

    if not mp4_info:
        raise RuntimeError(
            "Unable to analyze generated MP4"
        )

    mp4_duration = get_duration(
        mp4_info
    )

    mp4_video_packets = (
        get_video_packet_count(
            mp4_info
        )
    )

    log(
        f"Output MP4 duration: "
        f"{mp4_duration:.3f}s"
    )

    log(
        f"Output MP4 video packets: "
        f"{mp4_video_packets}"
    )

    # --------------------------------------------------------
    # DURATION SANITY CHECK
    # --------------------------------------------------------

    if (
        webm_duration > 1
        and mp4_duration > 1
    ):
        difference = abs(
            mp4_duration
            - webm_duration
        )

        log(
            f"WebM/MP4 duration difference: "
            f"{difference:.3f}s"
        )

        allowed_difference = max(
            5.0,
            webm_duration * 0.02,
        )

        if difference > allowed_difference:
            raise RuntimeError(
                "MP4 duration differs too much "
                "from the original WebM: "
                f"WebM={webm_duration:.3f}s, "
                f"MP4={mp4_duration:.3f}s, "
                f"difference={difference:.3f}s"
            )

    # --------------------------------------------------------
    # PACKET COUNT SANITY CHECK
    #
    # Re-encoding can change packet count slightly, but it
    # should not catastrophically drop the number of frames.
    #
    # If the output contains substantially fewer frames than
    # the source, something is wrong with the source timeline.
    # --------------------------------------------------------

    if (
        webm_video_packets > 100
        and mp4_video_packets > 0
    ):
        ratio = (
            mp4_video_packets
            / webm_video_packets
        )

        log(
            f"WebM/MP4 video packet ratio: "
            f"{ratio:.4f}"
        )

        if ratio < 0.90:
            raise RuntimeError(
                "MP4 contains substantially fewer "
                "video packets than the source WebM: "
                f"WebM={webm_video_packets}, "
                f"MP4={mp4_video_packets}, "
                f"ratio={ratio:.4f}"
            )

    # --------------------------------------------------------
    # VERIFY GENERATED MP4
    # --------------------------------------------------------

    if not verify_video_file(
        mp4_path
    ):
        raise RuntimeError(
            "Generated MP4 failed verification"
        )

    return True


# ============================================================
# SPLIT MP4
# ============================================================

def split_mp4_if_needed(mp4_path):
    mp4_path = Path(mp4_path)

    size_mb = (
        mp4_path.stat().st_size
        / (1024 * 1024)
    )

    log(
        f"MP4 size: "
        f"{size_mb:.2f} MB"
    )

    if size_mb <= TELEGRAM_MAX_SIZE_MB:
        return [mp4_path]

    info = get_video_info(
        mp4_path
    )

    duration = get_duration(
        info
    )

    if duration <= 1:
        raise RuntimeError(
            "Cannot split MP4: "
            "invalid duration"
        )

    target_bytes = (
        TELEGRAM_TARGET_SIZE_MB
        * 1024
        * 1024
    )

    current_bytes = (
        mp4_path.stat().st_size
    )

    estimated_parts = max(
        2,
        int(
            current_bytes
            / target_bytes
        ) + 1,
    )

    segment_time = max(
        30,
        duration
        / estimated_parts,
    )

    log(
        f"Splitting MP4 into approximately "
        f"{estimated_parts} parts, "
        f"segment_time="
        f"{segment_time:.1f}s"
    )

    output_pattern = (
        mp4_path.parent
        / f"{mp4_path.stem}_part_%03d.mp4"
    )

    command = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "warning",
        "-y",

        "-i",
        str(mp4_path),

        "-map",
        "0:v:0",

        "-map",
        "0:a:0?",

        "-c",
        "copy",

        "-f",
        "segment",

        "-segment_time",
        str(segment_time),

        "-reset_timestamps",
        "1",

        "-movflags",
        "+faststart",

        str(output_pattern),
    ]

    result = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    if result.stderr:
        log(
            "FFmpeg split output:\n"
            + result.stderr.strip()
        )

    if result.returncode != 0:
        raise RuntimeError(
            "FFmpeg split failed:\n"
            + result.stderr
        )

    parts = sorted(
        mp4_path.parent.glob(
            f"{mp4_path.stem}_part_*.mp4"
        )
    )

    if not parts:
        raise RuntimeError(
            "FFmpeg split produced no parts"
        )

    valid_parts = []

    for part in parts:
        part_size_mb = (
            part.stat().st_size
            / (1024 * 1024)
        )

        log(
            f"Split part: "
            f"{part.name} "
            f"{part_size_mb:.2f} MB"
        )

        if (
            part_size_mb
            > TELEGRAM_MAX_SIZE_MB
        ):
            log(
                f"WARNING: {part.name} "
                f"is still larger than "
                f"Telegram limit"
            )

        if not verify_video_file(
            part
        ):
            raise RuntimeError(
                f"Invalid split part: "
                f"{part}"
            )

        valid_parts.append(part)

    return valid_parts


# ============================================================
# PLAYWRIGHT / WEBRTC HOOK
# ============================================================

WEBRTC_HOOK = r"""
(() => {
    if (window.__superlive_hook_installed) {
        return;
    }

    window.__superlive_hook_installed = true;

    window.__superliveVideoTracks = [];
    window.__superliveAudioTracks = [];
    window.__superliveStreams = [];
    window.__superliveTrackLinks = new Map();

    const OriginalRTCPeerConnection =
        window.RTCPeerConnection;

    if (!OriginalRTCPeerConnection) {
        return;
    }

    function rememberTrack(track, stream) {
        if (!track) {
            return;
        }

        if (track.kind === "video") {
            if (
                !window.__superliveVideoTracks.includes(
                    track
                )
            ) {
                window.__superliveVideoTracks.push(
                    track
                );
            }
        }

        if (track.kind === "audio") {
            if (
                !window.__superliveAudioTracks.includes(
                    track
                )
            ) {
                window.__superliveAudioTracks.push(
                    track
                );
            }
        }

        if (stream) {
            if (
                !window.__superliveStreams.includes(
                    stream
                )
            ) {
                window.__superliveStreams.push(
                    stream
                );
            }

            if (
                !window.__superliveTrackLinks.has(
                    track
                )
            ) {
                window.__superliveTrackLinks.set(
                    track,
                    stream
                );
            }
        }
    }

    class WrappedRTCPeerConnection
        extends OriginalRTCPeerConnection {

        constructor(...args) {
            super(...args);

            this.addEventListener(
                "track",
                (event) => {
                    try {
                        const track =
                            event.track;

                        const streams =
                            event.streams || [];

                        if (streams.length) {
                            for (
                                const stream
                                of streams
                            ) {
                                rememberTrack(
                                    track,
                                    stream
                                );
                            }
                        } else {
                            rememberTrack(
                                track,
                                null
                            );
                        }
                    } catch (e) {
                        console.warn(
                            "superlive track hook error",
                            e
                        );
                    }
                }
            );
        }
    }

    window.RTCPeerConnection =
        WrappedRTCPeerConnection;

    window.__superlivePrepare = () => {
        const videos = Array.from(
            document.querySelectorAll(
                "video"
            )
        );

        let selectedVideoTrack = null;
        let selectedStream = null;

        for (const video of videos) {
            try {
                const stream =
                    video.srcObject;

                if (!stream) {
                    continue;
                }

                const videoTracks =
                    stream.getVideoTracks();

                const liveVideoTrack =
                    videoTracks.find(
                        t =>
                            t.readyState
                            === "live"
                    );

                if (liveVideoTrack) {
                    selectedVideoTrack =
                        liveVideoTrack;

                    selectedStream =
                        stream;

                    break;
                }
            } catch (e) {
                console.warn(
                    "superlive video scan error",
                    e
                );
            }
        }

        if (!selectedVideoTrack) {
            selectedVideoTrack =
                window.__superliveVideoTracks.find(
                    t =>
                        t.readyState
                        === "live"
                );
        }

        if (!selectedVideoTrack) {
            throw new Error(
                "No live video track found"
            );
        }

        let selectedAudioTrack = null;

        if (selectedStream) {
            selectedAudioTrack =
                selectedStream
                    .getAudioTracks()
                    .find(
                        t =>
                            t.readyState
                            === "live"
                    );
        }

        if (!selectedAudioTrack) {
            const linkedStream =
                window.__superliveTrackLinks.get(
                    selectedVideoTrack
                );

            if (linkedStream) {
                selectedAudioTrack =
                    linkedStream
                        .getAudioTracks()
                        .find(
                            t =>
                                t.readyState
                                === "live"
                        );
            }
        }

        if (!selectedAudioTrack) {
            selectedAudioTrack =
                window.__superliveAudioTracks.find(
                    t =>
                        t.readyState
                        === "live"
                );
        }

        const tracks = [
            selectedVideoTrack
        ];

        if (selectedAudioTrack) {
            tracks.push(
                selectedAudioTrack
            );
        }

        window.__preparedStream =
            new MediaStream(tracks);

        window.__preparedVideoTrack =
            selectedVideoTrack;

        window.__preparedAudioTrack =
            selectedAudioTrack;

        return {
            hasVideo:
                !!selectedVideoTrack,

            hasAudio:
                !!selectedAudioTrack,

            videoReadyState:
                selectedVideoTrack.readyState,

            audioReadyState:
                selectedAudioTrack
                    ? selectedAudioTrack.readyState
                    : null
        };
    };

    window.__superliveStartRec = (
        videoBitrate,
        audioBitrate,
        timeslice
    ) => {
        if (!window.__preparedStream) {
            throw new Error(
                "Prepared stream is missing"
            );
        }

        let mimeType = "";

        if (
            MediaRecorder.isTypeSupported(
                "video/webm;codecs=vp9,opus"
            )
        ) {
            mimeType =
                "video/webm;codecs=vp9,opus";

        } else if (
            MediaRecorder.isTypeSupported(
                "video/webm;codecs=vp8,opus"
            )
        ) {
            mimeType =
                "video/webm;codecs=vp8,opus";

        } else if (
            MediaRecorder.isTypeSupported(
                "video/webm"
            )
        ) {
            mimeType =
                "video/webm";

        } else {
            throw new Error(
                "No supported WebM MediaRecorder MIME type"
            );
        }

        const recorderOptions = {
            mimeType,
            videoBitsPerSecond:
                videoBitrate,
            audioBitsPerSecond:
                audioBitrate
        };

        const recorder =
            new MediaRecorder(
                window.__preparedStream,
                recorderOptions
            );

        window.__superliveRecorder =
            recorder;

        window.__superliveChunkCount = 0;

        window.__superliveUploadedChunkCount = 0;

        window.__superliveLastChunkAt =
            performance.now();

        window.__superliveLastChunkSize =
            0;

        window.__superliveUploadQueue =
            [];

        window.__superliveIsUploading =
            false;

        window.__superliveUploadError =
            null;

        /*
         * ----------------------------------------------------
         * FINAL DATA SYNCHRONIZATION
         * ----------------------------------------------------
         */

        window.__superlivePendingDataTasks = 0;

        window.__superliveRecorderStopFired =
            false;

        window.__superliveFinalDataReady =
            false;

        window.__superliveFinalDataResolve =
            null;

        window.__superliveFinalDataPromise =
            new Promise((resolve) => {
                window.__superliveFinalDataResolve =
                    resolve;
            });

        window.__superliveMaybeResolveFinalData =
            () => {
                if (
                    window.__superliveRecorderStopFired
                    &&
                    window.__superlivePendingDataTasks
                        === 0
                    &&
                    !window.__superliveFinalDataReady
                ) {
                    window.__superliveFinalDataReady =
                        true;

                    const resolve =
                        window.__superliveFinalDataResolve;

                    window.__superliveFinalDataResolve =
                        null;

                    if (resolve) {
                        resolve(true);
                    }
                }
            };

        /*
         * ----------------------------------------------------
         * SEQUENTIAL QUEUE PROCESSOR
         * ----------------------------------------------------
         *
         * IMPORTANT:
         *
         * MediaRecorder dataavailable events are ordered.
         * We preserve that order by putting the Blob into the
         * queue synchronously.
         *
         * We intentionally DO NOT call:
         *
         *     await event.data.arrayBuffer()
         *
         * before queue.push().
         *
         * Blob itself is a valid fetch body.
         *
         * This removes the asynchronous completion race that
         * could previously reorder chunks.
         */

        async function processQueue() {
            if (
                window.__superliveIsUploading
            ) {
                return;
            }

            window.__superliveIsUploading =
                true;

            try {
                while (
                    window.__superliveUploadQueue
                        .length
                ) {
                    const item =
                        window.__superliveUploadQueue
                            .shift();

                    try {
                        const response =
                            await fetch(
                                "/__slr_chunk",
                                {
                                    method: "POST",
                                    body: item.blob
                                }
                            );

                        if (!response.ok) {
                            throw new Error(
                                "HTTP "
                                + response.status
                            );
                        }

                        window.__superliveUploadedChunkCount++;

                    } catch (e) {
                        console.error(
                            "superlive chunk upload error",
                            e
                        );

                        window.__superliveUploadError =
                            String(e);

                        /*
                         * Put the exact same chunk back at
                         * the front. Because the processor is
                         * strictly sequential, this preserves
                         * chunk order.
                         */

                        window.__superliveUploadQueue
                            .unshift(item);

                        break;
                    }
                }

            } finally {
                window.__superliveIsUploading =
                    false;

                /*
                 * If an upload failed, retry asynchronously.
                 * Do not create a recursive synchronous loop.
                 */

                if (
                    window.__superliveUploadQueue
                        .length
                ) {
                    setTimeout(
                        () => {
                            processQueue();
                        },
                        250
                    );
                }
            }
        }

        /*
         * ----------------------------------------------------
         * DATAAVAILABLE
         * ----------------------------------------------------
         */

        recorder.ondataavailable =
            (event) => {

                window.__superlivePendingDataTasks++;

                try {
                    if (
                        !event.data
                        ||
                        event.data.size < 1
                    ) {
                        return;
                    }

                    window.__superliveChunkCount++;

                    window.__superliveLastChunkAt =
                        performance.now();

                    window.__superliveLastChunkSize =
                        event.data.size;

                    /*
                     * CRITICAL:
                     *
                     * Push the Blob immediately.
                     *
                     * No await.
                     * No arrayBuffer().
                     *
                     * Therefore event order is preserved.
                     */

                    window.__superliveUploadQueue
                        .push({
                            blob: event.data
                        });

                } catch (e) {
                    console.error(
                        "superlive dataavailable error",
                        e
                    );

                    window.__superliveUploadError =
                        String(e);

                } finally {
                    window.__superlivePendingDataTasks--;

                    window
                        .__superliveMaybeResolveFinalData();

                    processQueue();
                }
            };

        /*
         * ----------------------------------------------------
         * STOP
         * ----------------------------------------------------
         */

        recorder.onstop = () => {
            window.__superliveRecorderStopFired =
                true;

            window
                .__superliveMaybeResolveFinalData();
        };

        recorder.onerror = (event) => {
            console.error(
                "superlive MediaRecorder error",
                event
            );

            window.__superliveUploadError =
                "MediaRecorder error";
        };

        /*
         * ----------------------------------------------------
         * WAIT FOR FINAL DATAAVAILABLE
         * ----------------------------------------------------
         */

        window.__superliveWaitRecorderFinal =
            async (timeoutMs) => {
                if (
                    window.__superliveFinalDataReady
                ) {
                    return true;
                }

                const timeoutPromise =
                    new Promise(
                        (resolve) => {
                            setTimeout(
                                () =>
                                    resolve(false),
                                timeoutMs
                            );
                        }
                    );

                const result =
                    await Promise.race([
                        window
                            .__superliveFinalDataPromise,
                        timeoutPromise
                    ]);

                return result === true;
            };

        /*
         * ----------------------------------------------------
         * STOP RECORDER
         * ----------------------------------------------------
         */

        window.__superliveStopRec =
            () => {
                const activeRecorder =
                    window.__superliveRecorder;

                if (
                    activeRecorder
                    &&
                    activeRecorder.state
                        !== "inactive"
                ) {
                    activeRecorder.stop();
                    return true;
                }

                window
                    .__superliveRecorderStopFired =
                    true;

                window
                    .__superliveMaybeResolveFinalData();

                return false;
            };

        /*
         * ----------------------------------------------------
         * STATUS
         * ----------------------------------------------------
         */

        window.__superliveGetStatus =
            () => {
                const videoTrack =
                    window.__preparedVideoTrack;

                return {
                    queueLength:
                        window
                            .__superliveUploadQueue
                            ?
                            window
                                .__superliveUploadQueue
                                .length
                            :
                            0,

                    isUploading:
                        !!window
                            .__superliveIsUploading,

                    chunkCount:
                        window
                            .__superliveChunkCount
                        || 0,

                    uploadedChunkCount:
                        window
                            .__superliveUploadedChunkCount
                        || 0,

                    uploadError:
                        window
                            .__superliveUploadError
                        || null,

                    pendingDataTasks:
                        window
                            .__superlivePendingDataTasks
                        || 0,

                    idleTimeMs:
                        window
                            .__superliveLastChunkAt
                        ?
                        performance.now()
                        -
                        window
                            .__superliveLastChunkAt
                        :
                        Infinity,

                    lastChunkSize:
                        window
                            .__superliveLastChunkSize
                        || 0,

                    videoReadyState:
                        videoTrack
                            ?
                            videoTrack.readyState
                            :
                            null,

                    videoSettings:
                        videoTrack
                        &&
                        videoTrack.getSettings
                        ?
                        videoTrack.getSettings()
                        :
                        null,

                    recorderState:
                        window
                            .__superliveRecorder
                        ?
                        window
                            .__superliveRecorder
                            .state
                        :
                        null,

                    finalDataReady:
                        !!window
                            .__superliveFinalDataReady
                };
            };

        /*
         * Start recording only after all handlers are installed.
         */

        recorder.start(timeslice);

        return {
            mimeType,
            state: recorder.state
        };
    };

    window.__superliveWaitChunk =
        async (timeoutMs) => {
            const start =
                performance.now();

            while (
                window.__superliveChunkCount < 1
            ) {
                if (
                    performance.now()
                    - start
                    > timeoutMs
                ) {
                    return false;
                }

                await new Promise(
                    resolve =>
                        setTimeout(
                            resolve,
                            100
                        )
                );
            }

            return true;
        };
})();
"""


# ============================================================
# SAFE EVAL
# ============================================================

def safe_eval(value):
    try:
        return json.loads(value)
    except Exception:
        return value


# ============================================================
# RECORDING
# ============================================================

async def run_recording(playwright):
    if not URL:
        raise RuntimeError(
            "RECORD_URL environment variable is missing"
        )

    if not STREAM_ID:
        log(
            "WARNING: STREAM_ID is empty"
        )

    chromium_args = [
        "--disable-background-timer-throttling",
        "--disable-backgrounding-occluded-windows",
        "--disable-renderer-backgrounding",
        "--disable-features=CalculateNativeWinOcclusion",
        "--autoplay-policy=no-user-gesture-required",
        "--no-sandbox",
        "--disable-dev-shm-usage",
        "--disable-gpu",
    ]

    browser = await playwright.chromium.launch(
        headless=False,
        args=chromium_args,
    )

    context = await browser.new_context(
        viewport={
            "width": 1920,
            "height": 1080,
        },
        user_agent=USER_AGENT,
    )

    await context.add_init_script(
        WEBRTC_HOOK
    )

    page = await context.new_page()

    chunk_count = 0
    total_bytes = 0

    recording_started_at = (
        time.monotonic()
    )

    timestamp = time.strftime(
        "%Y%m%d_%H%M%S"
    )

    webm_path = (
        TEMP_DIR
        / f"recording_{timestamp}.webm"
    )

    mp4_path = (
        RECORDING_DIR
        / f"recording_{timestamp}.mp4"
    )

    webm_file = None

    try:
        webm_file = open(
            webm_path,
            "wb",
        )

        async def handle_chunk(
            route,
            request,
        ):
            nonlocal chunk_count
            nonlocal total_bytes

            try:
                body = (
                    request.post_data_buffer
                )

                if body:
                    webm_file.write(body)
                    webm_file.flush()

                    chunk_count += 1
                    total_bytes += len(body)

                await route.fulfill(
                    status=200,
                    body=b"OK",
                )

            except Exception as e:
                log(
                    f"Chunk handler error: {e}"
                )

                try:
                    await route.fulfill(
                        status=500,
                        body=b"ERROR",
                    )
                except Exception:
                    pass

        await page.route(
            "**/__slr_chunk",
            handle_chunk,
        )

        log_section(
            "OPENING PAGE"
        )

        await page.goto(
            URL,
            wait_until="domcontentloaded",
            timeout=PAGE_TIMEOUT_MS,
        )

        log(
            "Page loaded. "
            "Waiting for live video..."
        )

        # ----------------------------------------------------
        # Wait for actual video
        # ----------------------------------------------------

        video_ready = False

        wait_started = (
            time.monotonic()
        )

        while (
            time.monotonic()
            - wait_started
            < VIDEO_WAIT_SECONDS
        ):
            try:
                result = await page.evaluate(
                    """
                    () => {
                        const videos =
                            Array.from(
                                document.querySelectorAll("video")
                            );

                        for (const video of videos) {
                            try {
                                const stream =
                                    video.srcObject;

                                if (!stream) {
                                    continue;
                                }

                                const videoTracks =
                                    stream.getVideoTracks();

                                const audioTracks =
                                    stream.getAudioTracks();

                                const liveVideo =
                                    videoTracks.some(
                                        t =>
                                            t.readyState
                                            === "live"
                                    );

                                const liveAudio =
                                    audioTracks.some(
                                        t =>
                                            t.readyState
                                            === "live"
                                    );

                                if (
                                    video.videoWidth > 0 &&
                                    video.videoHeight > 0 &&
                                    liveVideo
                                ) {
                                    return {
                                        ready: true,
                                        width:
                                            video.videoWidth,
                                        height:
                                            video.videoHeight,
                                        audio:
                                            liveAudio
                                    };
                                }
                            } catch (e) {}
                        }

                        return {
                            ready: false
                        };
                    }
                    """
                )

                if result.get("ready"):
                    video_ready = True

                    log(
                        "Live video detected: "
                        f"{result.get('width')}x"
                        f"{result.get('height')} "
                        "audio="
                        f"{result.get('audio')}"
                    )

                    break

            except Exception as e:
                log(
                    f"Video detection error: {e}"
                )

            await asyncio.sleep(1)

        if not video_ready:
            raise RuntimeError(
                "Live video was not detected"
            )

        # ----------------------------------------------------
        # Prepare stream
        # ----------------------------------------------------

        prepared = await page.evaluate(
            """
            () =>
                window.__superlivePrepare()
            """
        )

        log(
            "Prepared stream: "
            + json.dumps(
                prepared,
                ensure_ascii=False,
            )
        )

        # ----------------------------------------------------
        # Start MediaRecorder
        # ----------------------------------------------------

        start_result = await page.evaluate(
            """
            ([videoBitrate, audioBitrate, timeslice]) =>
                window.__superliveStartRec(
                    videoBitrate,
                    audioBitrate,
                    timeslice
                )
            """,
            [
                VIDEO_BITRATE,
                AUDIO_BITRATE,
                1000,
            ],
        )

        log(
            "MediaRecorder started: "
            + json.dumps(
                start_result,
                ensure_ascii=False,
            )
        )

        # ----------------------------------------------------
        # Wait for first chunk
        # ----------------------------------------------------

        first_chunk = await page.evaluate(
            """
            () =>
                window.__superliveWaitChunk(
                    10000
                )
            """
        )

        if not first_chunk:
            raise RuntimeError(
                "First recording chunk was not received"
            )

        log(
            "First recording chunk received"
        )

        # ----------------------------------------------------
        # Main recording loop
        # ----------------------------------------------------

        last_status_log = (
            time.monotonic()
        )

        while True:
            elapsed = (
                time.monotonic()
                - recording_started_at
            )

            if (
                elapsed
                >= MAX_RECORDING_SECONDS
            ):
                log(
                    "Maximum recording duration reached"
                )
                break

            if (
                STREAM_ID
                and check_stop_requested(
                    STREAM_ID
                )
            ):
                log(
                    "Stop requested"
                )
                break

            try:
                status = await page.evaluate(
                    """
                    () =>
                        window.__superliveGetStatus()
                    """
                )

                idle_ms = float(
                    status.get(
                        "idleTimeMs",
                        0,
                    )
                )

                video_state = status.get(
                    "videoReadyState"
                )

                if (
                    video_state
                    and video_state != "live"
                ):
                    log(
                        "Video track is no longer live: "
                        f"{video_state}"
                    )
                    break

                if (
                    idle_ms
                    > STREAM_IDLE_TIMEOUT * 1000
                ):
                    log(
                        "No recording chunk received "
                        f"for "
                        f"{idle_ms / 1000:.1f}s"
                    )
                    break

                if (
                    time.monotonic()
                    - last_status_log
                    >= 30
                ):
                    settings = status.get(
                        "videoSettings"
                    )

                    log(
                        "Recording status: "
                        f"chunks="
                        f"{status.get('chunkCount')} "
                        f"uploaded="
                        f"{status.get('uploadedChunkCount')} "
                        f"queue="
                        f"{status.get('queueLength')} "
                        f"uploading="
                        f"{status.get('isUploading')} "
                        f"idle="
                        f"{idle_ms / 1000:.1f}s "
                        f"last_chunk="
                        f"{status.get('lastChunkSize')} "
                        f"bytes "
                        f"video="
                        f"{video_state} "
                        f"settings="
                        f"{settings}"
                    )

                    log(
                        f"Local WebM received: "
                        f"{total_bytes / 1024 / 1024:.2f} MB"
                    )

                    last_status_log = (
                        time.monotonic()
                    )

            except Exception as e:
                log(
                    f"Status check error: {e}"
                )

            await asyncio.sleep(
                STOP_CHECK_INTERVAL
            )

        # ----------------------------------------------------
        # Stop MediaRecorder
        # ----------------------------------------------------

        log(
            "Stopping MediaRecorder..."
        )

        try:
            stop_result = await page.evaluate(
                """
                () =>
                    window.__superliveStopRec()
                """
            )

            log(
                "MediaRecorder stop requested: "
                f"{stop_result}"
            )

        except Exception as e:
            log(
                f"MediaRecorder stop error: {e}"
            )

        # ----------------------------------------------------
        # WAIT FOR ACTUAL FINAL DATAAVAILABLE
        # ----------------------------------------------------

        log(
            "Waiting for MediaRecorder final "
            "dataavailable event..."
        )

        final_data_ready = False

        try:
            final_data_ready = (
                await page.evaluate(
                    """
                    () =>
                        window.__superliveWaitRecorderFinal(
                            30000
                        )
                    """
                )
            )

        except Exception as e:
            log(
                "Final MediaRecorder wait error: "
                f"{e}"
            )

        if final_data_ready:
            log(
                "MediaRecorder final dataavailable "
                "processing completed"
            )
        else:
            raise RuntimeError(
                "Timed out waiting for MediaRecorder "
                "final dataavailable event"
            )

        # ----------------------------------------------------
        # DRAIN ALL QUEUED CHUNKS
        # ----------------------------------------------------
        #
        # We now wait for:
        #
        #   queue = 0
        #   uploading = false
        #   uploaded == recorder chunk count
        #
        # We do NOT accept a partially assembled WebM.
        # ----------------------------------------------------

        log(
            "Waiting for ALL recording chunks "
            "to upload..."
        )

        drain_started = (
            time.monotonic()
        )

        last_drain_log = (
            time.monotonic()
        )

        while True:
            drain_elapsed = (
                time.monotonic()
                - drain_started
            )

            if (
                drain_elapsed
                >= FINAL_QUEUE_DRAIN_TIMEOUT_SECONDS
            ):
                try:
                    status = await page.evaluate(
                        """
                        () =>
                            window.__superliveGetStatus()
                        """
                    )
                except Exception:
                    status = {}

                raise RuntimeError(
                    "Recording upload queue did not drain "
                    "within the safety timeout: "
                    + json.dumps(
                        status,
                        ensure_ascii=False,
                    )
                )

            try:
                status = await page.evaluate(
                    """
                    () =>
                        window.__superliveGetStatus()
                    """
                )

                queue_length = int(
                    status.get(
                        "queueLength",
                        0,
                    )
                )

                uploading = bool(
                    status.get(
                        "isUploading",
                        False,
                    )
                )

                recorder_chunks = int(
                    status.get(
                        "chunkCount",
                        0,
                    )
                )

                uploaded_chunks = int(
                    status.get(
                        "uploadedChunkCount",
                        0,
                    )
                )

                pending_tasks = int(
                    status.get(
                        "pendingDataTasks",
                        0,
                    )
                )

                upload_error = status.get(
                    "uploadError"
                )

                if (
                    time.monotonic()
                    - last_drain_log
                    >= 10
                ):
                    log(
                        "Drain status: "
                        f"recorder_chunks="
                        f"{recorder_chunks} "
                        f"uploaded="
                        f"{uploaded_chunks} "
                        f"queue="
                        f"{queue_length} "
                        f"uploading="
                        f"{uploading} "
                        f"pending="
                        f"{pending_tasks}"
                    )

                    if upload_error:
                        log(
                            "Current upload error: "
                            f"{upload_error}"
                        )

                    log(
                        f"Local WebM received: "
                        f"{total_bytes / 1024 / 1024:.2f} MB"
                    )

                    last_drain_log = (
                        time.monotonic()
                    )

                if upload_error:
                    log(
                        "WARNING: upload error is "
                        f"currently recorded: "
                        f"{upload_error}"
                    )

                if (
                    queue_length == 0
                    and not uploading
                    and pending_tasks == 0
                    and uploaded_chunks == recorder_chunks
                    and recorder_chunks > 0
                ):
                    break

            except Exception as e:
                log(
                    f"Queue drain status error: {e}"
                )

            await asyncio.sleep(
                0.25
            )

        # ----------------------------------------------------
        # FINAL STATUS
        # ----------------------------------------------------

        final_status = {}

        try:
            final_status = await page.evaluate(
                """
                () =>
                    window.__superliveGetStatus()
                """
            )

            log(
                "Final recorder status: "
                + json.dumps(
                    final_status,
                    ensure_ascii=False,
                )
            )

        except Exception as e:
            raise RuntimeError(
                "Unable to obtain final recorder status: "
                + str(e)
            )

        recorder_chunk_count = int(
            final_status.get(
                "chunkCount",
                0,
            )
        )

        uploaded_chunk_count = int(
            final_status.get(
                "uploadedChunkCount",
                0,
            )
        )

        if (
            recorder_chunk_count <= 0
            or uploaded_chunk_count
            != recorder_chunk_count
        ):
            raise RuntimeError(
                "Chunk integrity check failed: "
                f"recorder={recorder_chunk_count}, "
                f"uploaded={uploaded_chunk_count}"
            )

        if (
            final_status.get("queueLength", 0) != 0
            or final_status.get("isUploading")
        ):
            raise RuntimeError(
                "Queue is not fully drained"
            )

        log(
            f"Final WebM received: "
            f"{total_bytes / 1024 / 1024:.2f} MB, "
            f"chunks={chunk_count}"
        )

        await page.unroute(
            "**/__slr_chunk",
            handle_chunk,
        )

        # ----------------------------------------------------
        # Close WebM safely
        # ----------------------------------------------------

        webm_file.flush()

        os.fsync(
            webm_file.fileno()
        )

        webm_file.close()
        webm_file = None

        await browser.close()

        # ----------------------------------------------------
        # Verify WebM BEFORE conversion
        # ----------------------------------------------------

        log_section(
            "VERIFY ORIGINAL WEBM"
        )

        if not verify_video_file(
            webm_path,
            allow_zero_duration=True,
        ):
            raise RuntimeError(
                "Original WebM failed verification"
            )

        # ----------------------------------------------------
        # Convert WebM -> MP4
        # ----------------------------------------------------

        convert_webm_to_mp4(
            webm_path,
            mp4_path,
        )

        # ----------------------------------------------------
        # Remove WebM only after successful conversion
        # ----------------------------------------------------

        try:
            webm_path.unlink()

            log(
                f"Removed temporary WebM: "
                f"{webm_path}"
            )

        except Exception as e:
            log(
                f"Could not remove WebM: {e}"
            )

        # ----------------------------------------------------
        # Split if necessary
        # ----------------------------------------------------

        parts = split_mp4_if_needed(
            mp4_path
        )

        # ----------------------------------------------------
        # Upload
        # ----------------------------------------------------

        log_section(
            "UPLOADING VIDEO"
        )

        total_parts = len(parts)

        for index, part in enumerate(
            parts,
            start=1,
        ):
            caption = (
                "🎥 Recording"
            )

            if total_parts > 1:
                caption += (
                    f"\nPart "
                    f"{index}/{total_parts}"
                )

            success = (
                send_to_telegram_with_retry(
                    part,
                    caption,
                )
            )

            if not success:
                raise RuntimeError(
                    f"Failed to upload part "
                    f"{index}/{total_parts}: "
                    f"{part}"
                )

            log(
                f"Uploaded part "
                f"{index}/{total_parts}"
            )

        # ----------------------------------------------------
        # Cleanup split parts
        # ----------------------------------------------------

        for part in parts:
            if part != mp4_path:
                try:
                    part.unlink()

                except Exception as e:
                    log(
                        f"Could not remove split part "
                        f"{part}: {e}"
                    )

        try:
            if mp4_path.exists():
                mp4_path.unlink()

        except Exception as e:
            log(
                f"Could not remove MP4: {e}"
            )

        # ----------------------------------------------------
        # State update
        # ----------------------------------------------------

        if STREAM_ID:
            kv_update_state(
                STREAM_ID,
                "completed",
            )

            kv_delete_state(
                STREAM_ID
            )

        send_telegram_notification(
            "✅ Recording completed successfully."
        )

        log_section(
            "RECORDING COMPLETED"
        )

        return True

    except Exception:
        try:
            if webm_file is not None:
                webm_file.flush()
                webm_file.close()
                webm_file = None

        except Exception:
            pass

        try:
            await browser.close()

        except Exception:
            pass

        raise


# ============================================================
# MAIN
# ============================================================

async def main():
    log_section(
        "SUPERLIVE RECORDER "
        "(ORDERED CHUNKS + STABLE VFR MP4)"
    )

    from playwright.async_api import (
        async_playwright
    )

    async with async_playwright() as playwright:
        return await asyncio.wait_for(
            run_recording(playwright),
            timeout=GLOBAL_WATCHDOG_SECONDS,
        )


if __name__ == "__main__":
    try:
        asyncio.run(main())

    except Exception as e:
        log(
            f"FATAL ERROR: {e}"
        )

        import traceback

        traceback.print_exc()

        try:
            if STREAM_ID:
                kv_update_state(
                    STREAM_ID,
                    "failed",
                    error=str(e),
                )

                kv_delete_state(
                    STREAM_ID
                )

        except Exception:
            pass

        sys.exit(1)
