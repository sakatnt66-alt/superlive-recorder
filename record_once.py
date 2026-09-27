import asyncio
import json
import os
import subprocess
import struct
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

VIDEO_BITRATE = 2_000_000
AUDIO_BITRATE = 128_000

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
        "-count_packets",
        "-show_entries",
        "stream=index,codec_type,codec_name,width,height,"
        "r_frame_rate,avg_frame_rate,time_base,start_time,duration,"
        "nb_frames,nb_read_packets",
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


def get_stream_packet_count(stream):
    """Return ffprobe's actual packet count for one stream."""
    if not stream:
        return 0

    for key in ("nb_read_packets", "nb_frames"):
        try:
            value = int(stream.get(key) or 0)
        except Exception:
            value = 0

        if value > 0:
            return value

    return 0


def get_video_packet_count(info):
    if not info:
        return 0

    for stream in info.get("streams", []):
        if stream.get("codec_type") != "video":
            continue

        return get_stream_packet_count(stream)

    return 0


def analyze_webm_video_timing(path):
    """
    Analyze the original WebM video packet timeline without decoding or
    re-encoding it.

    This is intentionally performed before splitting/uploading so we can
    distinguish a bad source timeline from a split/upload problem.

    Strict failures:
      * missing/unparseable video timestamps
      * non-finite timestamps
      * backwards PTS values
      * backwards DTS values

    Duplicate PTS values are reported as a diagnostic but are not treated
    as packet loss: MediaRecorder can emit an isolated duplicate timestamp
    while preserving the encoded packet itself. The packet is retained.

    A low effective FPS or a large positive gap is reported as a diagnostic,
    not treated as packet loss by itself. Variable frame rate can be valid.
    """
    path = Path(path)

    command = [
        "ffprobe",
        "-hide_banner",
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_packets",
        "-show_entries",
        "packet=pts_time,dts_time,duration_time",
        "-of",
        "csv=p=0",
        str(path),
    ]

    result = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=120,
    )

    if result.returncode != 0:
        raise RuntimeError(
            "Unable to inspect original WebM video timestamps:\n"
            + result.stderr.strip()
        )

    import math

    pts_values = []
    dts_values = []
    durations = []
    invalid_rows = 0

    for line in result.stdout.splitlines():
        line = line.strip()

        if not line:
            continue

        fields = [field.strip() for field in line.split(",")]

        if len(fields) < 2:
            invalid_rows += 1
            continue

        def parse_timestamp(value):
            if value in ("", "N/A", "NULL"):
                return None
            try:
                number = float(value)
            except Exception:
                return None
            if not math.isfinite(number):
                return None
            return number

        pts = parse_timestamp(fields[0])
        dts = parse_timestamp(fields[1])
        duration = (
            parse_timestamp(fields[2])
            if len(fields) >= 3
            else None
        )

        if pts is None:
            invalid_rows += 1
        else:
            pts_values.append(pts)

        if dts is not None:
            dts_values.append(dts)

        if duration is not None and duration >= 0:
            durations.append(duration)

    if not pts_values:
        raise RuntimeError(
            "Original WebM contains no usable video PTS timestamps; "
            "refusing to upload because video timing cannot be verified"
        )

    duplicate_pts = 0
    backwards_pts = 0
    duplicate_dts = 0
    backwards_dts = 0

    positive_deltas = []
    large_gaps = []

    previous = None
    for index, current in enumerate(pts_values):
        if previous is not None:
            delta = current - previous

            if delta == 0:
                duplicate_pts += 1
            elif delta < 0:
                backwards_pts += 1
            else:
                positive_deltas.append(delta)

                if delta > 0.200:
                    large_gaps.append(
                        (index, previous, current, delta)
                    )

        previous = current

    previous = None
    for current in dts_values:
        if previous is not None:
            delta = current - previous

            if delta == 0:
                duplicate_dts += 1
            elif delta < 0:
                backwards_dts += 1

        previous = current

    first_pts = pts_values[0]
    last_pts = pts_values[-1]
    timeline_span = max(0.0, last_pts - first_pts)

    effective_fps = 0.0
    if timeline_span > 0 and len(pts_values) > 1:
        effective_fps = (
            (len(pts_values) - 1)
            / timeline_span
        )

    min_delta = min(positive_deltas) if positive_deltas else 0.0
    max_delta = max(positive_deltas) if positive_deltas else 0.0

    average_delta = 0.0
    if positive_deltas:
        average_delta = sum(positive_deltas) / len(positive_deltas)

    log(
        "WebM video timing analysis: "
        f"packets={len(pts_values)} "
        f"first_pts={first_pts:.6f}s "
        f"last_pts={last_pts:.6f}s "
        f"timeline={timeline_span:.3f}s "
        f"effective_fps={effective_fps:.3f}"
    )

    log(
        "WebM video timestamp diagnostics: "
        f"duplicate_pts={duplicate_pts} "
        f"backwards_pts={backwards_pts} "
        f"duplicate_dts={duplicate_dts} "
        f"backwards_dts={backwards_dts} "
        f"invalid_rows={invalid_rows}"
    )

    log(
        "WebM video frame intervals: "
        f"min={min_delta:.6f}s "
        f"avg={average_delta:.6f}s "
        f"max={max_delta:.6f}s "
        f"large_gaps_gt_200ms={len(large_gaps)}"
    )

    if large_gaps:
        preview = large_gaps[:10]
        formatted = "; ".join(
            f"#{index}: {delta:.3f}s"
            for index, _, _, delta in preview
        )
        log(
            "Largest video timing gaps (first 10): "
            + formatted
        )

    if invalid_rows:
        raise RuntimeError(
            "Original WebM contains video packets with invalid/missing "
            f"PTS values: {invalid_rows}; refusing to upload"
        )

    if backwards_pts:
        raise RuntimeError(
            "Original WebM video PTS timeline moves backwards: "
            f"backwards_pts={backwards_pts}; refusing to upload"
        )

    if backwards_dts:
        raise RuntimeError(
            "Original WebM video DTS timeline moves backwards: "
            f"backwards_dts={backwards_dts}; refusing to upload"
        )

    return {
        "packet_count": len(pts_values),
        "first_pts": first_pts,
        "last_pts": last_pts,
        "timeline_span": timeline_span,
        "effective_fps": effective_fps,
        "duplicate_pts": duplicate_pts,
        "backwards_pts": backwards_pts,
        "duplicate_dts": duplicate_dts,
        "backwards_dts": backwards_dts,
        "large_gaps": len(large_gaps),
        "min_delta": min_delta,
        "average_delta": average_delta,
        "max_delta": max_delta,
    }


def get_audio_packet_count(info):
    if not info:
        return 0

    total = 0
    found_audio = False

    for stream in info.get("streams", []):
        if stream.get("codec_type") != "audio":
            continue

        found_audio = True
        count = get_stream_packet_count(stream)

        if count <= 0:
            return 0

        total += count

    return total if found_audio else 0


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
    """
    Upload the recorded WebM directly to Telegram.

    Telegram's Bot API does not guarantee WebM as an inline video
    through sendVideo; non-MPEG4 formats may be delivered as documents.
    This direct-WebM test therefore uses sendDocument deliberately so
    that the original MediaRecorder WebM is preserved byte-for-byte.
    """
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
        log("Telegram credentials are missing")
        return False

    size_mb = path.stat().st_size / (1024 * 1024)

    if size_mb > TELEGRAM_MAX_SIZE_MB:
        log(
            f"Telegram upload refused locally: "
            f"{path.name} is {size_mb:.2f} MB "
            f"> {TELEGRAM_MAX_SIZE_MB:.2f} MB"
        )
        return False

    url = (
        f"https://api.telegram.org/bot"
        f"{TELEGRAM_BOT_TOKEN}/sendDocument"
    )

    boundary = (
        "----SuperLiveRecorderBoundary"
        + str(int(time.time() * 1000))
    )

    fields = {
        "chat_id": str(TELEGRAM_CHAT_ID),
        "caption": caption,
        "parse_mode": "HTML",
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
            f'name="document"; filename="{filename}"\r\n'
            f"Content-Type: video/webm\r\n\r\n"
        ).encode("utf-8")
    )

    try:
        with open(path, "rb") as f:
            body.extend(f.read())
    except Exception as e:
        log(f"Unable to read WebM for Telegram: {e}")
        return False

    body.extend(
        f"\r\n--{boundary}--\r\n".encode("utf-8")
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
                "User-Agent": "SuperLiveRecorder/1.0",
            },
        )

        with urllib.request.urlopen(req, timeout=600) as response:
            response_body = response.read().decode(
                "utf-8",
                errors="replace",
            )

        try:
            result = json.loads(response_body)
            if result.get("ok"):
                log(
                    f"Telegram WebM upload successful: "
                    f"{path.name} ({size_mb:.2f} MB)"
                )
                return True

            log(
                "Telegram returned failure: "
                f"{response_body}"
            )
        except Exception:
            log(f"Telegram response: {response_body}")

    except urllib.error.HTTPError as e:
        try:
            error_body = e.read().decode(
                "utf-8",
                errors="replace",
            )
        except Exception:
            error_body = str(e)
        log(
            f"Telegram HTTP error {e.code}: "
            f"{error_body}"
        )
    except Exception as e:
        log(f"Telegram upload error: {e}")

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
        # Do not use +genpts.
        #
        # We want FFmpeg to consume the timestamps actually
        # present in the MediaRecorder WebM.
        # ----------------------------------------------------

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
        "color=black,"
        "setpts=PTS-STARTPTS",

        "-fps_mode:v",
        "vfr",

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
# AUDIO FILE VERIFICATION
# ============================================================

def verify_audio_file(path):
    path = Path(path)

    if not path.exists() or path.stat().st_size <= 4096:
        log(
            f"Audio verification failed: file missing/too small: {path}"
        )
        return False

    info = get_video_info(path)

    if not info:
        log(
            "Audio verification failed: ffprobe information unavailable"
        )
        return False

    audio_streams = [
        stream
        for stream in info.get("streams", [])
        if stream.get("codec_type") == "audio"
    ]

    if not audio_streams:
        log(
            "Audio verification failed: no audio stream"
        )
        return False

    audio_packets = get_audio_packet_count(info)
    codec = audio_streams[0].get("codec_name")

    log(
        "Verified audio: "
        f"{path.stat().st_size / (1024 * 1024):.2f} MB, "
        f"codec={codec}, "
        f"audio_packets={audio_packets}"
    )

    if not codec or audio_packets <= 0:
        log(
            "Audio verification failed: codec or packet count unavailable"
        )
        return False

    return True


# ============================================================
# LOSSLESS WEBM VIDEO + AUDIO MUX
# ============================================================

def remux_ivf_to_webm(
    ivf_path: Path,
    webm_path: Path,
    expected_video_packets: int,
):
    """Remux captured VP8 IVF frames into WebM without re-encoding."""
    if not ivf_path.exists():
        raise RuntimeError(
            f"Encoded VP8 IVF file does not exist: {ivf_path}"
        )

    if ivf_path.stat().st_size <= 32:
        raise RuntimeError(
            "Encoded VP8 IVF file is unexpectedly small"
        )

    command = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "warning",
        "-y",
        "-i",
        str(ivf_path),
        "-map",
        "0:v:0",
        "-c:v",
        "copy",
        "-f",
        "webm",
        str(webm_path),
    ]

    result = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    if result.stdout:
        log("IVF -> WebM stdout:\n" + result.stdout.strip())

    if result.stderr:
        log("IVF -> WebM output:\n" + result.stderr.strip())

    if result.returncode != 0:
        raise RuntimeError(
            "FFmpeg IVF -> WebM remux failed:\n"
            + result.stderr
        )

    if not webm_path.exists() or webm_path.stat().st_size <= 32:
        raise RuntimeError(
            "FFmpeg reported success but encoded WebM was not created"
        )

    actual_packets = get_video_packet_count(get_video_info(webm_path))

    log(
        "Encoded WebM packet integrity: "
        f"expected={expected_video_packets} "
        f"actual={actual_packets}"
    )

    if actual_packets != expected_video_packets:
        raise RuntimeError(
            "Encoded video packet count changed during IVF -> WebM remux: "
            f"expected={expected_video_packets}, actual={actual_packets}"
        )


def mux_webm_video_audio(
    video_path,
    audio_path,
    output_path,
):
    """
    Combine independently recorded WebM video and Opus audio
    without re-encoding either stream.

    Safety rules:
      * Video packets must be preserved exactly.
      * Audio packets must be preserved exactly.
      * The output must contain both streams.
      * Any packet-count mismatch is a hard failure.

    No CFR conversion, timestamp regeneration, or re-encoding is
    performed here.
    """
    video_path = Path(video_path)
    audio_path = Path(audio_path)
    output_path = Path(output_path)

    if not video_path.exists():
        raise FileNotFoundError(video_path)

    if not audio_path.exists():
        raise FileNotFoundError(audio_path)

    video_info = get_video_info(video_path)
    audio_info = get_video_info(audio_path)

    if not video_info or not audio_info:
        raise RuntimeError(
            "Unable to inspect independent WebM streams before muxing"
        )

    source_video_packets = get_video_packet_count(video_info)
    source_audio_packets = get_audio_packet_count(audio_info)

    if source_video_packets <= 0:
        raise RuntimeError(
            "Independent video WebM contains no countable video packets"
        )

    if source_audio_packets <= 0:
        raise RuntimeError(
            "Independent audio WebM contains no countable audio packets"
        )

    log(
        "Independent WebM packet counts before mux: "
        f"video={source_video_packets} "
        f"audio={source_audio_packets}"
    )

    if output_path.exists():
        output_path.unlink()

    command = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "warning",
        "-y",
        "-copyts",
        "-i",
        str(video_path),
        "-copyts",
        "-i",
        str(audio_path),
        "-map",
        "0:v:0",
        "-map",
        "1:a:0",
        "-c",
        "copy",
        "-avoid_negative_ts",
        "disabled",
        "-f",
        "webm",
        str(output_path),
    ]

    result = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=300,
    )

    if result.stderr:
        log(
            "FFmpeg WebM mux output:\n"
            + result.stderr.strip()
        )

    if result.returncode != 0:
        raise RuntimeError(
            "Lossless WebM video/audio mux failed:\n"
            + result.stderr
        )

    if not output_path.exists() or output_path.stat().st_size <= 10 * 1024:
        raise RuntimeError(
            "Lossless WebM mux produced an invalid/empty output"
        )

    output_info = get_video_info(output_path)
    if not output_info:
        raise RuntimeError(
            "Unable to inspect muxed WebM output"
        )

    output_video_packets = get_video_packet_count(output_info)
    output_audio_packets = get_audio_packet_count(output_info)

    log(
        "Muxed WebM packet counts: "
        f"video={output_video_packets}/{source_video_packets} "
        f"audio={output_audio_packets}/{source_audio_packets}"
    )

    if output_video_packets != source_video_packets:
        raise RuntimeError(
            "Video packet loss/change detected during WebM mux: "
            f"source={source_video_packets}, "
            f"output={output_video_packets}"
        )

    if output_audio_packets != source_audio_packets:
        raise RuntimeError(
            "Audio packet loss/change detected during WebM mux: "
            f"source={source_audio_packets}, "
            f"output={output_audio_packets}"
        )

    streams = output_info.get("streams", [])
    has_video = any(
        stream.get("codec_type") == "video"
        for stream in streams
    )
    has_audio = any(
        stream.get("codec_type") == "audio"
        for stream in streams
    )

    if not has_video or not has_audio:
        raise RuntimeError(
            "Muxed WebM does not contain both video and audio streams"
        )

    return output_path


# ============================================================
# SPLIT WEBM
# ============================================================

def split_webm_if_needed(webm_path):
    """
    Split WebM by remuxing only.

    Safety rules:
      * Every output part must remain below the absolute Telegram limit.
      * Video packet count must match the source exactly.
      * If the source has audio, audio packet count must also match exactly.
      * Unknown source packet counts are a hard failure; 0/0 is never treated
        as successful integrity verification.
      * The source WebM is never modified.
    """
    webm_path = Path(webm_path)

    size_mb = webm_path.stat().st_size / (1024 * 1024)
    log(f"WebM size: {size_mb:.2f} MB")

    if size_mb <= TELEGRAM_TARGET_SIZE_MB:
        return [webm_path]

    info = get_video_info(webm_path)
    duration = get_duration(info)

    source_video_packets = get_video_packet_count(info)
    source_audio_packets = get_audio_packet_count(info)
    has_audio_stream = any(
        stream.get("codec_type") == "audio"
        for stream in (info or {}).get("streams", [])
    )

    log(
        "Source WebM packets: "
        f"video={source_video_packets} "
        f"audio={source_audio_packets}"
    )

    # Do not continue if ffprobe cannot count the source packets.
    # A 0/0 comparison is not an integrity check.
    if source_video_packets <= 0:
        raise RuntimeError(
            "Unable to determine source WebM video packet count; "
            "refusing to split/upload because packet loss cannot be verified"
        )

    if has_audio_stream and source_audio_packets <= 0:
        raise RuntimeError(
            "Unable to determine source WebM audio packet count; "
            "refusing to split/upload because audio packet loss cannot be verified"
        )

    # MediaRecorder WebM can have missing container duration metadata.
    if duration <= 1:
        estimated_duration = (
            webm_path.stat().st_size * 8
            / max(1, VIDEO_BITRATE + AUDIO_BITRATE)
        )
        duration = max(10, estimated_duration)
        log(
            "WebM duration metadata is unavailable; "
            f"using estimated duration={duration:.1f}s for splitting"
        )

    target_bytes = TELEGRAM_TARGET_SIZE_MB * 1024 * 1024
    current_bytes = webm_path.stat().st_size

    estimated_parts = max(
        2,
        int(current_bytes / target_bytes) + 1,
    )

    segment_time = max(
        10,
        duration / estimated_parts,
    )

    output_glob = f"{webm_path.stem}_part_*.webm"

    for attempt in range(1, 7):
        old_parts = sorted(
            webm_path.parent.glob(output_glob)
        )
        for old_part in old_parts:
            try:
                old_part.unlink()
            except Exception:
                pass

        log(
            f"Splitting WebM attempt {attempt}/6: "
            f"segment_time={segment_time:.1f}s"
        )

        output_pattern = (
            webm_path.parent
            / f"{webm_path.stem}_part_%03d.webm"
        )

        command = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "warning",
            "-y",
            "-i",
            str(webm_path),
            "-map",
            "0:v:0",
            "-map",
            "0:a:0?",
            # Regenerate missing presentation timestamps before the
            # segment muxer. MediaRecorder WebM can contain duplicate
            # DTS values; without a monotonic timestamp timeline FFmpeg
            # has to rewrite DTS while splitting, which is exactly what
            # produced the non-monotonic-DTS warnings seen in the logs.
            "-fflags",
            "+genpts",
            "-c",
            "copy",
            "-f",
            "segment",
            "-segment_time",
            str(segment_time),
            "-segment_time_delta",
            "1.0",
            "-reset_timestamps",
            "1",
            "-segment_format",
            "webm",
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
                "FFmpeg WebM split output:\n"
                + result.stderr.strip()
            )

        if result.returncode != 0:
            raise RuntimeError(
                "FFmpeg WebM split failed:\n"
                + result.stderr
            )

        parts = sorted(
            webm_path.parent.glob(output_glob)
        )

        if not parts:
            raise RuntimeError(
                "WebM split produced no parts"
            )

        oversized = False
        valid_parts = []
        total_video_packets = 0
        total_audio_packets = 0

        for part in parts:
            part_size_mb = (
                part.stat().st_size
                / (1024 * 1024)
            )

            log(
                f"WebM split part: {part.name} "
                f"{part_size_mb:.2f} MB"
            )

            if part_size_mb > TELEGRAM_MAX_SIZE_MB:
                oversized = True
                log(
                    f"WARNING: {part.name} exceeds the "
                    f"absolute Telegram safety limit of "
                    f"{TELEGRAM_MAX_SIZE_MB:.2f} MB"
                )
                break

            part_info = get_video_info(part)

            if not verify_video_file(
                part,
                allow_zero_duration=True,
            ):
                raise RuntimeError(
                    f"Invalid WebM split part: {part}"
                )

            part_video_packets = get_video_packet_count(
                part_info
            )
            total_video_packets += part_video_packets

            part_audio_packets = get_audio_packet_count(
                part_info
            )

            if has_audio_stream and part_audio_packets <= 0:
                raise RuntimeError(
                    f"Unable to count audio packets in split part: {part}"
                )

            total_audio_packets += part_audio_packets

            valid_parts.append(part)

        if oversized:
            for part in parts:
                try:
                    part.unlink()
                except Exception:
                    pass

            segment_time *= 0.70
            continue

        # ----------------------------------------------------
        # HARD INTEGRITY CHECK
        # ----------------------------------------------------
        # Exact packet-count equality is required.
        # Never accept an unknown/zero source count.
        # ----------------------------------------------------

        if total_video_packets != source_video_packets:
            log(
                "WebM split integrity failure: "
                f"source video packets={source_video_packets}, "
                f"split video packets={total_video_packets}"
            )

            for part in parts:
                try:
                    part.unlink()
                except Exception:
                    pass

            segment_time *= 0.85
            continue

        if has_audio_stream and total_audio_packets != source_audio_packets:
            log(
                "WebM split integrity failure: "
                f"source audio packets={source_audio_packets}, "
                f"split audio packets={total_audio_packets}"
            )

            for part in parts:
                try:
                    part.unlink()
                except Exception:
                    pass

            segment_time *= 0.85
            continue

        log(
            "WebM split integrity verified: "
            f"video={total_video_packets}/"
            f"{source_video_packets} "
            f"audio={total_audio_packets}/"
            f"{source_audio_packets}"
        )

        return valid_parts

    raise RuntimeError(
        "Unable to split WebM safely below Telegram limit "
        "without losing packets"
    )


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
    window.__superliveTrackPeers = new WeakMap();
    window.__superliveTrackStreams = new WeakMap();
    window.__superliveTrackStreamIds = new WeakMap();
    window.__superlivePeerConnections = [];
    window.__superliveInboundStats = [];
    window.__superliveEncodedVideo = {
        supported: false,
        receivers: new Map(),
        attachedTrackId: null,
        receiverFound: false,
        worker: null,
        workerUrl: null,
        port: null,
        frameQueue: [],
        uploadQueue: [],
        uploading: false,
        flushTimer: null,
        frameCount: 0,
        keyFrameCount: 0,
        uploadedBatchCount: 0,
        uploadedFrameCount: 0,
        pendingDataTasks: 0,
        uploadError: null,
        firstFramePromise: null,
        firstFrameResolve: null,
        firstFrameReject: null,
        started: false,
        stopping: false,
        lastTimestamp: null,
        firstTimestamp: null,
    };


    /* Set from Python after navigation so the selector can use the
     * exact livestream id as a positive identity hint when the page
     * exposes it in DOM/stream metadata. */
    window.__superliveTargetStreamId = null;

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

    const installEncodedVideoTransform = (receiver, track) => {
        const state = window.__superliveEncodedVideo;

        if (!receiver || !track || track.kind !== "video") {
            return false;
        }

        if (state.receivers.has(track.id)) {
            state.receiverFound = true;
            return true;
        }

        if (
            typeof RTCRtpScriptTransform !== "function"
            || !window.RTCRtpReceiver
            || !("transform" in RTCRtpReceiver.prototype)
            || typeof Worker !== "function"
            || typeof Blob !== "function"
            || typeof URL.createObjectURL !== "function"
        ) {
            state.supported = false;
            return false;
        }

        try {
            const workerSource = `
                addEventListener("rtctransform", (event) => {
                    const transformer = event.transformer;
                    const port = transformer.options.port;
                    let active = false;

                    port.onmessage = (messageEvent) => {
                        const message = messageEvent.data || {};
                        if (message.command === "set-active") {
                            active = !!message.active;
                            if (
                                active
                                && typeof transformer.sendKeyFrameRequest === "function"
                            ) {
                                transformer.sendKeyFrameRequest().catch(() => {});
                            }
                        }
                    };

                    const transform = new TransformStream({
                        async transform(encodedFrame, controller) {
                            try {
                                if (!active) {
                                    controller.enqueue(encodedFrame);
                                    return;
                                }

                                const source = new Uint8Array(encodedFrame.data);
                                const copy = new Uint8Array(source.byteLength);
                                copy.set(source);

                                port.postMessage({
                                    timestamp: Number(encodedFrame.timestamp || 0),
                                    duration: Number(encodedFrame.duration || 0),
                                    type: encodedFrame.type || "delta",
                                    data: copy.buffer,
                                }, [copy.buffer]);
                            } catch (error) {
                                port.postMessage({
                                    error: String(error),
                                });
                            }

                            controller.enqueue(encodedFrame);
                        },
                    });

                    transformer.readable
                        .pipeThrough(transform)
                        .pipeTo(transformer.writable)
                        .catch((error) => {
                            try {
                                port.postMessage({
                                    error: String(error),
                                });
                            } catch (ignored) {}
                        });
                });
            `;

            const blob = new Blob(
                [workerSource],
                {type: "application/javascript"}
            );
            const workerUrl = URL.createObjectURL(blob);
            const worker = new Worker(workerUrl);
            const channel = new MessageChannel();

            state.supported = true;
            state.receiverFound = true;

            const entry = {
                trackId: track.id,
                worker,
                workerUrl,
                port: channel.port1,
                active: false,
            };

            state.receivers.set(track.id, entry);

            channel.port1.onmessage = (event) => {
                const message = event.data || {};

                if (message.error) {
                    state.uploadError =
                        state.uploadError ||
                        String(message.error);
                    return;
                }

                if (!message.data) {
                    return;
                }

                const timestamp = Number(message.timestamp);
                const data = message.data;

                if (!entry.active) {
                    return;
                }

                if (!Number.isFinite(timestamp)) {
                    state.uploadError =
                        "Encoded video frame has invalid timestamp";
                    return;
                }

                if (message.type === "key") {
                    state.seenKeyFrame = true;
                    state.keyFrameCount++;
                } else if (!state.seenKeyFrame) {
                    // Never start an IVF/WebM file with a delta frame.
                    return;
                }

                state.frameCount++;

                if (state.firstTimestamp === null) {
                    state.firstTimestamp = timestamp;
                    if (state.firstFrameResolve) {
                        state.firstFrameResolve({
                            ok: true,
                            timestamp,
                            frameCount: state.frameCount,
                        });
                        state.firstFrameResolve = null;
                    }
                }

                if (
                    state.lastTimestamp !== null
                    && timestamp < state.lastTimestamp
                ) {
                    state.uploadError =
                        "Encoded video timestamp moved backwards";
                }

                state.lastTimestamp = timestamp;
                state.frameQueue.push({timestamp, data});

                if (!state.flushTimer) {
                    state.flushTimer = setTimeout(
                        () => {
                            state.flushTimer = null;
                            flushEncodedVideoQueue();
                        },
                        250
                    );
                }
            };

            channel.port1.start();

            receiver.transform = new RTCRtpScriptTransform(
                worker,
                {port: channel.port2},
                [channel.port2]
            );

            return true;
        } catch (error) {
            state.supported = false;
            state.receiverFound = false;
            state.uploadError =
                "Encoded video transform setup failed: "
                + String(error);
            return false;
        }
    };

    const flushEncodedVideoQueue = async () => {
        const state = window.__superliveEncodedVideo;

        if (state.uploading || !state.frameQueue.length) {
            return;
        }

        state.uploading = true;
        state.pendingDataTasks++;

        try {
            while (state.frameQueue.length) {
                const frames = state.frameQueue.splice(
                    0,
                    state.frameQueue.length
                );

                let payloadSize = 8;
                for (const frame of frames) {
                    payloadSize += 8 + 1 + 4 + frame.data.byteLength;
                }

                const payload = new ArrayBuffer(payloadSize);
                const view = new DataView(payload);
                const magic = [83, 76, 86, 70, 1, 0, 0, 0];
                for (let i = 0; i < magic.length; i++) {
                    view.setUint8(i, magic[i]);
                }

                let offset = 8;
                for (const frame of frames) {
                    view.setBigUint64(
                        offset,
                        BigInt(Math.max(0, Math.round(frame.timestamp))),
                        true
                    );
                    offset += 8;
                    view.setUint8(
                        offset,
                        frame.type === "key" ? 1 : 0
                    );
                    offset += 1;
                    view.setUint32(
                        offset,
                        frame.data.byteLength,
                        true
                    );
                    offset += 4;
                    new Uint8Array(payload, offset, frame.data.byteLength)
                        .set(new Uint8Array(frame.data));
                    offset += frame.data.byteLength;
                }

                let uploaded = false;
                let lastError = null;

                for (let attempt = 0; attempt < 3; attempt++) {
                    try {
                        const response = await fetch(
                            "/__slr_encoded_video_chunk",
                            {
                                method: "POST",
                                body: payload,
                                headers: {
                                    "Content-Type":
                                        "application/octet-stream",
                                },
                            }
                        );

                        if (!response.ok) {
                            throw new Error(
                                `HTTP ${response.status}`
                            );
                        }

                        uploaded = true;
                        break;
                    } catch (error) {
                        lastError = error;
                        await new Promise(
                            resolve => setTimeout(resolve, 300 * (attempt + 1))
                        );
                    }
                }

                if (!uploaded) {
                    state.uploadError =
                        "Encoded video batch upload failed: "
                        + String(lastError);
                    return;
                }

                state.uploadedBatchCount++;
                state.uploadedFrameCount += frames.length;
            }
        } finally {
            state.uploading = false;
            state.pendingDataTasks--;

            if (
                state.frameQueue.length
                && !state.flushTimer
            ) {
                state.flushTimer = setTimeout(
                    () => {
                        state.flushTimer = null;
                        flushEncodedVideoQueue();
                    },
                    0
                );
            }
        }
    };

    window.__superliveEnableEncodedVideo = (track) => {
        const state = window.__superliveEncodedVideo;

        if (!track || track.kind !== "video") {
            return {
                supported: false,
                reason: "invalid_video_track",
            };
        }

        const peer = window.__superliveTrackPeers.get(track);
        if (!peer) {
            return {
                supported: false,
                reason: "selected_track_peer_not_found",
            };
        }

        let receiver = null;
        try {
            receiver = peer.getReceivers().find(
                item => item && item.track === track
            ) || null;
        } catch (error) {
            return {
                supported: false,
                reason: "receiver_lookup_failed",
                error: String(error),
            };
        }

        if (!receiver) {
            return {
                supported: false,
                reason: "selected_track_receiver_not_found",
            };
        }

        let receiverCodecs = [];
        try {
            receiverCodecs = (receiver.getParameters().codecs || [])
                .map(codec => String(codec.mimeType || "").toLowerCase())
                .filter(Boolean);
        } catch (error) {
            return {
                supported: false,
                reason: "receiver_codec_lookup_failed",
                error: String(error),
            };
        }

        if (!receiverCodecs.includes("video/vp8")) {
            return {
                supported: false,
                reason: "selected_webRTC_codec_is_not_vp8",
                codecs: receiverCodecs,
            };
        }

        let ok = state.receivers.has(track.id);
        if (!ok) {
            ok = installEncodedVideoTransform(
                receiver,
                track
            );
        }

        if (!ok) {
            return {
                supported: false,
                reason:
                    state.uploadError
                    || "encoded_transform_unavailable",
            };
        }

        const entry = state.receivers.get(track.id);
        if (!entry) {
            return {
                supported: false,
                reason: "encoded_receiver_entry_missing",
            };
        }

        state.attachedTrackId = track.id;
        state.started = true;
        state.stopping = false;
        state.firstTimestamp = null;
        state.lastTimestamp = null;
        state.seenKeyFrame = false;
        state.frameCount = 0;
        state.keyFrameCount = 0;
        state.uploadedBatchCount = 0;
        state.uploadedFrameCount = 0;
        state.pendingDataTasks = 0;
        state.uploadError = null;
        state.frameQueue.length = 0;

        state.firstFramePromise = new Promise((resolve, reject) => {
            state.firstFrameResolve = resolve;
            state.firstFrameReject = reject;
        });

        entry.active = true;
        try {
            entry.port.postMessage({
                command: "set-active",
                active: true,
            });
        } catch (e) {
            entry.active = false;
            state.uploadError = String(e);
            return {
                supported: false,
                reason: "encoded_receiver_activation_failed",
            };
        }

        return {
            supported: true,
            receiverFound: true,
            trackId: track.id,
        };
    };

    window.__superliveWaitEncodedVideoFrame = async (timeoutMs) => {
        const state = window.__superliveEncodedVideo;

        if (state.frameCount > 0) {
            return {
                ok: true,
                frameCount: state.frameCount,
                timestamp: state.firstTimestamp,
            };
        }

        if (!state.firstFramePromise) {
            return {
                ok: false,
                reason: "encoded_capture_not_started",
            };
        }

        const timeoutPromise = new Promise(resolve => {
            setTimeout(
                () => resolve({
                    ok: false,
                    reason: "timeout_waiting_for_encoded_video_frame",
                }),
                timeoutMs
            );
        });

        return await Promise.race([
            state.firstFramePromise,
            timeoutPromise,
        ]);
    };

    window.__superliveStopEncodedVideo = async () => {
        const state = window.__superliveEncodedVideo;
        state.stopping = true;

        if (state.attachedTrackId) {
            const entry = state.receivers.get(state.attachedTrackId);
            if (entry) {
                entry.active = false;
                try {
                    entry.port.postMessage({
                        command: "set-active",
                        active: false,
                    });
                } catch (e) {}
            }
        }

        if (state.flushTimer) {
            clearTimeout(state.flushTimer);
            state.flushTimer = null;
        }

        await flushEncodedVideoQueue();

        const deadline = performance.now() + 30000;
        while (
            state.uploading
            || state.pendingDataTasks > 0
            || state.frameQueue.length > 0
        ) {
            if (performance.now() >= deadline) {
                state.uploadError =
                    state.uploadError
                    || "Timed out draining encoded video upload queue";
                break;
            }
            await new Promise(resolve => setTimeout(resolve, 25));
        }

        return {
            frameCount: state.frameCount,
            keyFrameCount: state.keyFrameCount,
            uploadedFrameCount: state.uploadedFrameCount,
            uploadedBatchCount: state.uploadedBatchCount,
            queueLength: state.frameQueue.length,
            uploading: state.uploading,
            pendingDataTasks: state.pendingDataTasks,
            uploadError: state.uploadError,
            firstTimestamp: state.firstTimestamp,
            lastTimestamp: state.lastTimestamp,
            seenKeyFrame: state.seenKeyFrame,
        };
    };

    window.__superliveGetEncodedVideoStatus = () => {
        const state = window.__superliveEncodedVideo;
        return {
            supported: state.supported,
            started: state.started,
            stopping: state.stopping,
            attachedTrackId: state.attachedTrackId || null,
            receiverTrackIds: Array.from(state.receivers.keys()),
            receiverFound: state.receiverFound,
            frameCount: state.frameCount,
            keyFrameCount: state.keyFrameCount,
            uploadedFrameCount: state.uploadedFrameCount,
            uploadedBatchCount: state.uploadedBatchCount,
            queueLength: state.frameQueue.length,
            uploading: state.uploading,
            pendingDataTasks: state.pendingDataTasks,
            uploadError: state.uploadError,
            firstTimestamp: state.firstTimestamp,
            lastTimestamp: state.lastTimestamp,
        };
    };

    class WrappedRTCPeerConnection
        extends OriginalRTCPeerConnection {

        constructor(...args) {
            super(...args);

            window.__superlivePeerConnections.push(this);

            this.addEventListener(
                "track",
                (event) => {
                    try {
                        const track =
                            event.track;

                        window.__superliveTrackPeers.set(
                            track,
                            this
                        );

                        if (track.kind === "video") {
                            try {
                                installEncodedVideoTransform(
                                    event.receiver,
                                    track
                                );
                            } catch (error) {
                                console.warn(
                                    "superlive encoded video transform setup error",
                                    error
                                );
                            }
                        }

                        const streams =
                            event.streams || [];

                        if (streams.length) {
                            window.__superliveTrackStreams.set(
                                track,
                                streams.slice()
                            );
                            window.__superliveTrackStreamIds.set(
                                track,
                                streams.map(
                                    stream => stream && stream.id
                                ).filter(Boolean)
                            );

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
                            window.__superliveTrackStreams.set(
                                track,
                                []
                            );
                            window.__superliveTrackStreamIds.set(
                                track,
                                []
                            );
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

    window.__superliveSelectTargetVideo = async () => {
        const videos = Array.from(
            document.querySelectorAll("video")
        );

        const viewportWidth =
            window.innerWidth || document.documentElement.clientWidth || 0;
        const viewportHeight =
            window.innerHeight || document.documentElement.clientHeight || 0;

        const targetId =
            window.__superliveTargetStreamId
                ? String(window.__superliveTargetStreamId)
                : null;

        const getInboundStats = async () => {
            const results = [];

            for (const pc of window.__superlivePeerConnections) {
                try {
                    const stats = await pc.getStats();
                    stats.forEach(report => {
                        if (report.type !== "inbound-rtp") return;
                        const kind = report.kind || report.mediaType || null;
                        if (kind !== "video" && kind !== "audio") return;

                        results.push({
                            kind,
                            trackIdentifier: report.trackIdentifier || null,
                            framesReceived: report.framesReceived ?? null,
                            framesDecoded: report.framesDecoded ?? null,
                            framesDropped: report.framesDropped ?? null,
                            framesPerSecond: report.framesPerSecond ?? null,
                            packetsReceived: report.packetsReceived ?? null,
                            packetsLost: report.packetsLost ?? null,
                            bytesReceived: report.bytesReceived ?? null,
                        });
                    });
                } catch (e) {}
            }

            return results;
        };

        const inboundStats = await getInboundStats();
        window.__superliveInboundStats = inboundStats;

        const findTrackStats = trackId =>
            inboundStats.find(
                item =>
                    item.kind === "video"
                    && item.trackIdentifier === trackId
            ) || null;

        const domContainsTargetId = (element) => {
            if (!targetId || !element) return false;

            let node = element;
            let depth = 0;

            while (node && depth < 8) {
                try {
                    const values = [
                        node.id,
                        node.className,
                        node.getAttribute && node.getAttribute("data-stream-id"),
                        node.getAttribute && node.getAttribute("data-id"),
                        node.getAttribute && node.getAttribute("data-livestream-id"),
                        node.getAttribute && node.getAttribute("data-channel-id"),
                        node.getAttribute && node.getAttribute("data-video-id"),
                        node.getAttribute && node.getAttribute("href"),
                    ];

                    if (
                        values.some(
                            value =>
                                value != null
                                && String(value).includes(targetId)
                        )
                    ) {
                        return true;
                    }

                    /* A player container may keep the livestream id on
                     * a descendant link/button rather than on the
                     * container itself. Check a small subtree so a
                     * recommendation/player card can be tied to the
                     * exact requested livestream without using global
                     * page state. */
                    if (node.querySelector) {
                        const descendants = node.querySelectorAll(
                            '[href], [data-stream-id], [data-livestream-id], [data-video-id]'
                        );

                        for (const descendant of descendants) {
                            const descendantValues = [
                                descendant.getAttribute && descendant.getAttribute("href"),
                                descendant.getAttribute && descendant.getAttribute("data-stream-id"),
                                descendant.getAttribute && descendant.getAttribute("data-livestream-id"),
                                descendant.getAttribute && descendant.getAttribute("data-video-id"),
                            ];

                            if (
                                descendantValues.some(
                                    value =>
                                        value != null
                                        && String(value).includes(targetId)
                                )
                            ) {
                                return true;
                            }
                        }
                    }
                } catch (e) {}

                node = node.parentElement;
                depth++;
            }

            return false;
        };

        const candidates = [];

        for (let index = 0; index < videos.length; index++) {
            const video = videos[index];

            try {
                const stream = video.srcObject;
                if (!stream) continue;

                const videoTrack = stream
                    .getVideoTracks()
                    .find(t => t.readyState === "live");

                if (!videoTrack) continue;
                if (video.videoWidth <= 0 || video.videoHeight <= 0) continue;
                if (video.readyState < 2) continue;

                const style = getComputedStyle(video);
                if (
                    style.display === "none"
                    || style.visibility === "hidden"
                    || style.opacity === "0"
                ) {
                    continue;
                }

                const rect = video.getBoundingClientRect();
                const left = Math.max(0, rect.left);
                const top = Math.max(0, rect.top);
                const right = Math.min(viewportWidth, rect.right);
                const bottom = Math.min(viewportHeight, rect.bottom);
                const visibleWidth = Math.max(0, right - left);
                const visibleHeight = Math.max(0, bottom - top);
                const visibleArea = visibleWidth * visibleHeight;

                if (visibleArea <= 0) continue;

                const stats = findTrackStats(videoTrack.id);
                const streamIds =
                    window.__superliveTrackStreamIds.get(videoTrack) || [];
                const peer =
                    window.__superliveTrackPeers.get(videoTrack) || null;

                const sameTargetDom = domContainsTargetId(video);
                const activePackets =
                    stats && Number(stats.packetsReceived || 0) > 0;
                const activeDecoded =
                    stats && Number(stats.framesDecoded || 0) > 0;
                const fps =
                    stats && Number.isFinite(Number(stats.framesPerSecond))
                        ? Number(stats.framesPerSecond)
                        : 0;

                /*
                 * Selection priority:
                 *   1. An explicit DOM reference to the requested stream id.
                 *   2. A real inbound WebRTC receiver for this exact track.
                 *   3. Visibility/size of the actual player.
                 *
                 * FPS is deliberately NOT used as identity.  It is only a
                 * diagnostic field, because another broadcaster can also be
                 * 30 FPS.
                 */
                let identityScore = 0;
                if (sameTargetDom) identityScore += 1000000;
                if (activePackets) identityScore += 10000;
                if (activeDecoded) identityScore += 1000;

                const score =
                    identityScore
                    + Math.min(visibleArea, 1000000) / 100
                    + Math.min(fps, 120);

                candidates.push({
                    index,
                    video,
                    stream,
                    videoTrack,
                    rect,
                    visibleArea,
                    streamIds,
                    peer,
                    sameTargetDom,
                    stats,
                    score,
                });
            } catch (e) {
                console.warn("superlive target selection error", e);
            }
        }

        if (!candidates.length) {
            throw new Error("No visible live video target found");
        }

        candidates.sort((a, b) => b.score - a.score);
        const best = candidates[0];

        window.__superliveSelectedVideo = best.video;
        window.__superliveSelectedStream = best.stream;
        window.__superliveSelectedVideoTrack = best.videoTrack;

        return {
            selected: {
                index: best.index,
                width: best.video.videoWidth,
                height: best.video.videoHeight,
                visibleArea: best.visibleArea,
                rect: {
                    x: best.rect.x,
                    y: best.rect.y,
                    width: best.rect.width,
                    height: best.rect.height,
                },
                trackId: best.videoTrack.id,
                streamId: best.stream.id || null,
                streamIds: best.streamIds,
                sameTargetDom: best.sameTargetDom,
                inboundStats: best.stats,
            },
            candidates: candidates.map(item => ({
                index: item.index,
                width: item.video.videoWidth,
                height: item.video.videoHeight,
                visibleArea: item.visibleArea,
                rect: {
                    x: item.rect.x,
                    y: item.rect.y,
                    width: item.rect.width,
                    height: item.rect.height,
                },
                trackId: item.videoTrack.id,
                streamId: item.stream.id || null,
                streamIds: item.streamIds,
                sameTargetDom: item.sameTargetDom,
                score: item.score,
                inboundStats: item.stats,
            })),
        };
    };

    window.__superliveGetWebRTCStats = async () => {
        const results = [];

        for (const pc of window.__superlivePeerConnections) {
            try {
                const stats = await pc.getStats();

                stats.forEach(report => {
                    if (report.type !== "inbound-rtp") {
                        return;
                    }

                    const kind = report.kind || report.mediaType || null;
                    if (kind !== "video" && kind !== "audio") {
                        return;
                    }

                    results.push({
                        kind,
                        trackIdentifier: report.trackIdentifier || null,
                        ssrc: report.ssrc || null,
                        framesReceived: report.framesReceived ?? null,
                        framesDecoded: report.framesDecoded ?? null,
                        framesDropped: report.framesDropped ?? null,
                        framesPerSecond: report.framesPerSecond ?? null,
                        packetsReceived: report.packetsReceived ?? null,
                        packetsLost: report.packetsLost ?? null,
                        jitter: report.jitter ?? null,
                        bytesReceived: report.bytesReceived ?? null,
                        totalSamplesReceived: report.totalSamplesReceived ?? null,
                        totalSamplesDuration: report.totalSamplesDuration ?? null,
                    });
                });
            } catch (e) {
                // A peer connection can disappear while the page is switching
                // receivers. Ignore that connection for this diagnostic.
            }
        }

        const selectedTrack =
            window.__superliveSelectedVideoTrack || null;
        const selectedAudioTrack =
            window.__preparedAudioTrack
            || null;

        const matching = selectedTrack
            ? results.filter(
                item =>
                    item.kind === "video"
                    && item.trackIdentifier === selectedTrack.id
            )
            : [];

        const matchingAudio = selectedAudioTrack
            ? results.filter(
                item =>
                    item.kind === "audio"
                    && item.trackIdentifier === selectedAudioTrack.id
            )
            : [];

        return {
            selectedTrackId: selectedTrack
                ? selectedTrack.id
                : null,
            selectedAudioTrackId: selectedAudioTrack
                ? selectedAudioTrack.id
                : null,
            matching,
            matchingAudio,
            inboundVideo: results.filter(
                item => item.kind === "video"
            ),
            inboundAudio: results.filter(
                item => item.kind === "audio"
            ),
        };
    };

    window.__superliveEnsureSelectedVideoPlaying = async () => {
        const video = window.__superliveSelectedVideo;

        if (!video) {
            return {
                ok: false,
                reason: "no_selected_video",
            };
        }

        const start = performance.now();
        let playResult = "not_needed";

        try {
            if (
                video.paused
                || video.readyState < 2
                || video.videoWidth === 0
                || video.videoHeight === 0
            ) {
                try {
                    await video.play();
                    playResult = "played";
                } catch (e) {
                    playResult = "play_rejected";
                }
            }
        } catch (e) {
            playResult = "play_error";
        }

        while (performance.now() - start < 10000) {
            if (
                !video.paused
                && video.readyState >= 2
                && video.videoWidth > 0
                && video.videoHeight > 0
            ) {
                return {
                    ok: true,
                    playResult,
                    readyState: video.readyState,
                    paused: video.paused,
                    currentTime: video.currentTime,
                    width: video.videoWidth,
                    height: video.videoHeight,
                };
            }

            await new Promise(resolve => setTimeout(resolve, 250));
        }

        return {
            ok: false,
            playResult,
            readyState: video.readyState,
            paused: video.paused,
            currentTime: video.currentTime,
            width: video.videoWidth,
            height: video.videoHeight,
        };
    };

    window.__superlivePrepare = async () => {
        let selectedVideo =
            window.__superliveSelectedVideo;
        let selectedStream =
            window.__superliveSelectedStream;
        let selectedVideoTrack =
            window.__superliveSelectedVideoTrack;

        try {
            if (
                !selectedVideoTrack ||
                selectedVideoTrack.readyState !== "live" ||
                !selectedVideo ||
                !selectedStream
            ) {
                await window.__superliveSelectTargetVideo();
                selectedVideo = window.__superliveSelectedVideo;
                selectedStream = window.__superliveSelectedStream;
                selectedVideoTrack = window.__superliveSelectedVideoTrack;
            }
        } catch (e) {
            selectedVideo = null;
            selectedStream = null;
            selectedVideoTrack = null;
        }

        if (!selectedVideoTrack) {
            selectedVideoTrack = window.__superliveVideoTracks.find(
                t => t.readyState === "live"
            );
        }

        if (!selectedVideoTrack) {
            throw new Error("No live video track found");
        }

        /*
         * AUDIO MUST STAY ASSOCIATED WITH THE SELECTED VIDEO.
         *
         * The previous implementation fell back to
         * window.__superliveAudioTracks when the selected video
         * stream did not expose audio immediately.  That array is
         * global to the page and can contain audio receivers from
         * other live players/broadcasters.  The logs confirmed that
         * this can produce a perfectly valid audio recording that
         * belongs to a different broadcast.
         *
         * Never use a page-global audio fallback.  Only accept audio
         * tracks belonging to the SAME MediaStream that carries the
         * selected video track (or a stream explicitly linked to that
         * selected video track).  If the audio track is not attached
         * yet, wait briefly for the page to attach it.
         */
        const audioCandidates = [];

        const addAudioCandidate = (track) => {
            if (!track || audioCandidates.includes(track)) {
                return;
            }
            if (track.readyState === "live") {
                audioCandidates.push(track);
            }
        };

        const getPeerAudioCandidates = () => {
            const candidates = [];
            const peer =
                window.__superliveTrackPeers.get(
                    selectedVideoTrack
                );

            if (!peer) {
                return candidates;
            }

            try {
                for (const receiver of peer.getReceivers()) {
                    const track = receiver && receiver.track;

                    if (
                        !track
                        || track.kind !== "audio"
                        || track.readyState !== "live"
                    ) {
                        continue;
                    }

                    candidates.push(track);
                }
            } catch (e) {
                console.warn(
                    "superlive peer audio receiver lookup error",
                    e
                );
            }

            return candidates;
        };

        const collectAssociatedAudio = () => {
            audioCandidates.length = 0;

            if (selectedStream) {
                for (const track of selectedStream.getAudioTracks()) {
                    addAudioCandidate(track);
                }
            }

            const linkedStream =
                window.__superliveTrackLinks.get(selectedVideoTrack);

            if (linkedStream) {
                for (const track of linkedStream.getAudioTracks()) {
                    addAudioCandidate(track);
                }
            }

            /*
             * Some players attach audio to the same MediaStream object
             * a little after the video track arrives.  Search every
             * remembered stream, but ONLY when that stream contains
             * the exact selected video track.
             */
            /*
             * Some players expose video and audio on different
             * MediaStream objects even though both tracks belong to
             * the SAME RTCPeerConnection.  Inspect only receivers on
             * the PeerConnection that delivered the selected video.
             * Never use page-global audio here.
             */
            for (const track of getPeerAudioCandidates()) {
                addAudioCandidate(track);
            }

            for (const stream of window.__superliveStreams) {
                try {
                    const hasSelectedVideo =
                        stream.getVideoTracks().includes(
                            selectedVideoTrack
                        );

                    if (!hasSelectedVideo) {
                        continue;
                    }

                    for (const track of stream.getAudioTracks()) {
                        addAudioCandidate(track);
                    }
                } catch (e) {
                    // Ignore stale/detached MediaStream objects.
                }
            }
        };

        collectAssociatedAudio();

        const audioWaitDeadline =
            performance.now() + 10000;

        while (
            audioCandidates.length === 0
            && performance.now() < audioWaitDeadline
        ) {
            await new Promise(
                resolve => setTimeout(resolve, 500)
            );

            collectAssociatedAudio();
        }

        let selectedAudioTrack =
            audioCandidates.find(t => !t.muted)
            || null;

        if (!selectedAudioTrack) {
            selectedAudioTrack =
                getPeerAudioCandidates().find(
                    t => !t.muted
                ) || null;
        }

        if (!selectedAudioTrack) {
            throw new Error(
                "No audio track associated with the selected video stream; "
                + "refusing to use unrelated page audio"
            );
        }

        let captureStream = null;
        let captureVideoTrack = null;

        if (selectedVideo) {
            try {
                const captureFn =
                    selectedVideo.captureStream ||
                    selectedVideo.mozCaptureStream;

                if (typeof captureFn === "function") {
                    captureStream = captureFn.call(
                        selectedVideo,
                        30
                    );

                    captureVideoTrack = captureStream
                        .getVideoTracks()
                        .find(t => t.readyState === "live");
                }
            } catch (e) {
                console.warn(
                    "superlive captureStream error",
                    e
                );
            }
        }

        /*
         * captureStream() can return a LIVE track that is MUTED
         * when the rendered <video> is not actually producing
         * frames.  MediaRecorder can then stay in "recording"
         * forever without producing a single dataavailable event.
         *
         * The original WebRTC track can also be temporarily muted
         * while the receiver is still starting.  Do not start the
         * recorder in that state: wait for the SAME selected track
         * to become unmuted, then prepare the recorder from it.
         *
         * This does not change stream selection.  It only waits for
         * the already-selected video source to actually carry frames.
         */
        if (
            !captureVideoTrack
            || captureVideoTrack.readyState !== "live"
            || captureVideoTrack.muted
        ) {
            if (captureVideoTrack && captureVideoTrack.muted) {
                console.warn(
                    "superlive captureStream track is muted; falling back to source WebRTC track"
                );
            }

            captureVideoTrack = selectedVideoTrack;
        }

        if (
            captureVideoTrack.readyState !== "live"
            || captureVideoTrack.muted
        ) {
            /*
             * WebRTC tracks can be temporarily muted during receiver
             * startup.  Refresh the SAME selected <video> periodically
             * instead of aborting after the first 30 seconds.
             *
             * The selector itself is unchanged: it still chooses the
             * visible target video.  We are only refreshing its current
             * WebRTC track in case the page replaced the receiver track
             * while the player was starting.
             */
            const waitDeadline =
                performance.now() + 60000;

            while (
                performance.now() < waitDeadline
                &&
                (
                    selectedVideoTrack.readyState
                        !== "live"
                    || selectedVideoTrack.muted
                )
            ) {
                await new Promise(
                    resolve =>
                        setTimeout(resolve, 1000)
                );

                try {
                    await window.__superliveSelectTargetVideo();
                    selectedVideo =
                        window.__superliveSelectedVideo;
                    selectedStream =
                        window.__superliveSelectedStream;
                    selectedVideoTrack =
                        window.__superliveSelectedVideoTrack;

                    if (selectedVideoTrack) {
                        const refreshedStreamAudio = selectedStream
                            ? selectedStream
                                .getAudioTracks()
                                .find(
                                    t =>
                                        t.readyState === "live"
                                        && !t.muted
                                )
                            : null;

                        const refreshedPeerAudio =
                            getPeerAudioCandidates().find(
                                t => !t.muted
                            ) || null;

                        const refreshedAudio =
                            refreshedStreamAudio
                            || refreshedPeerAudio;

                        if (refreshedAudio) {
                            selectedAudioTrack =
                                refreshedAudio;
                        }
                    }
                } catch (e) {
                    // Keep waiting for the already-selected target.
                }
            }

            if (
                !selectedVideoTrack
                || selectedVideoTrack.readyState
                    !== "live"
                || selectedVideoTrack.muted
            ) {
                throw new Error(
                    "Selected WebRTC video track remained muted after 60s; no video frames are available"
                );
            }

            captureVideoTrack =
                selectedVideoTrack;
        }

        const tracks = [captureVideoTrack];

        if (selectedAudioTrack) {
            tracks.push(selectedAudioTrack);
        }

        window.__preparedStream = new MediaStream(tracks);
        window.__preparedVideoTrack = captureVideoTrack;
        window.__preparedSourceVideoTrack = selectedVideoTrack;
        window.__preparedAudioTrack = selectedAudioTrack;
        window.__preparedAudioAssociation = {
            selectedVideoTrackId: selectedVideoTrack.id,
            selectedStreamId: selectedStream
                ? selectedStream.id
                : null,
            selectedAudioTrackId: selectedAudioTrack
                ? selectedAudioTrack.id
                : null,
            selectedAudioTrackMuted: selectedAudioTrack
                ? !!selectedAudioTrack.muted
                : null,
            selectedStreamAudioTrackIds: selectedStream
                ? selectedStream.getAudioTracks().map(
                    track => track.id
                )
                : [],
        };

        /* Transactional MediaRecorder candidates. A recorder is only
         * accepted after it emits a real dataavailable chunk. */
        window.__superliveRecordingCandidates = [];
        window.__superliveVideoTransformCleanups = [];

        /*
         * Some Chromium builds encode a direct WebRTC MediaStreamTrack
         * noticeably below the receiver's decoded frame rate. The source
         * track itself can still be healthy at ~30 FPS.
         *
         * Use the browser's MediaStreamTrackProcessor/Generator pair as a
         * very small transport boundary: every VideoFrame is pulled from
         * the already-selected WebRTC track and written to a fresh local
         * video track before MediaRecorder sees it. No canvas, captureStream
         * rendering, frame-rate synthesis, or re-encoding is introduced at
         * this stage. MediaRecorder remains the only video encoder.
         *
         * This is an optional candidate. If the browser does not support
         * these APIs, the existing direct-WebRTC candidate remains intact.
         */
        const createProcessedVideoTrack = (sourceTrack) => {
            if (!sourceTrack || sourceTrack.readyState !== "live") {
                return null;
            }

            if (
                typeof MediaStreamTrackProcessor !== "function"
                || typeof MediaStreamTrackGenerator !== "function"
            ) {
                return null;
            }

            let processor;
            let generator;
            let reader;
            let writer;

            try {
                processor = new MediaStreamTrackProcessor({
                    track: sourceTrack
                });

                generator = new MediaStreamTrackGenerator({
                    kind: "video"
                });

                reader = processor.readable.getReader();
                writer = generator.writable.getWriter();

                if ("contentHint" in generator) {
                    generator.contentHint = "motion";
                }

                const state = {
                    active: true,
                    frameCount: 0,
                    error: null,
                };

                const pump = (async () => {
                    try {
                        while (state.active) {
                            const result = await reader.read();

                            if (result.done) {
                                break;
                            }

                            const frame = result.value;

                            try {
                                state.frameCount++;
                                await writer.write(frame);
                            } finally {
                                try {
                                    frame.close();
                                } catch (e) {
                                    // VideoFrame.close() is best-effort cleanup.
                                }
                            }
                        }
                    } catch (e) {
                        if (state.active) {
                            state.error = String(e);
                            console.warn(
                                "superlive processed video track error",
                                e
                            );
                        }
                    } finally {
                        state.active = false;

                        try {
                            writer.releaseLock();
                        } catch (e) {
                            // Best-effort cleanup.
                        }

                        try {
                            reader.releaseLock();
                        } catch (e) {
                            // Best-effort cleanup.
                        }

                        try {
                            generator.stop();
                        } catch (e) {
                            // Best-effort cleanup.
                        }
                    }
                })();

                const cleanup = () => {
                    if (!state.active) {
                        return;
                    }

                    state.active = false;

                    try {
                        reader.cancel();
                    } catch (e) {
                        // Best-effort cleanup.
                    }

                    try {
                        writer.abort();
                    } catch (e) {
                        // Best-effort cleanup.
                    }

                    try {
                        generator.stop();
                    } catch (e) {
                        // Best-effort cleanup.
                    }
                };

                window.__superliveVideoTransformCleanups.push(cleanup);

                return {
                    track: generator,
                    state,
                    pump,
                    cleanup,
                };
            } catch (e) {
                try {
                    if (reader) reader.cancel();
                } catch (ignored) {}

                try {
                    if (writer) writer.abort();
                } catch (ignored) {}

                try {
                    if (generator) generator.stop();
                } catch (ignored) {}

                console.warn(
                    "superlive processed video track unavailable",
                    e
                );

                return null;
            }
        };

        const addRecordingCandidate = (
            name,
            videoTrack,
            audioTrack,
            cloneTracks = false
        ) => {
            if (!videoTrack || videoTrack.readyState !== "live") return;

            let recordingVideoTrack = videoTrack;
            let recordingAudioTrack =
                audioTrack && audioTrack.readyState === "live"
                    ? audioTrack
                    : null;

            if (cloneTracks) {
                try {
                    recordingVideoTrack = videoTrack.clone();
                    if (recordingAudioTrack) {
                        recordingAudioTrack =
                            recordingAudioTrack.clone();
                    }
                } catch (e) {
                    console.warn(
                        "superlive track clone candidate failed",
                        e
                    );
                    return;
                }
            }

            const candidateTracks = [recordingVideoTrack];
            if (recordingAudioTrack) {
                candidateTracks.push(recordingAudioTrack);
            }

            try {
                if (recordingVideoTrack && "contentHint" in recordingVideoTrack) {
                    recordingVideoTrack.contentHint = "motion";
                }
            } catch (e) {
                // contentHint is an optional optimization; ignore unsupported browsers.
            }

            window.__superliveRecordingCandidates.push({
                name,
                stream: new MediaStream(candidateTracks),
                videoTrack: recordingVideoTrack,
                sourceVideoTrack: videoTrack,
                audioTrack: recordingAudioTrack,
                sourceAudioTrack: audioTrack || null,
            });
        };

        const processedVideo =
            createProcessedVideoTrack(selectedVideoTrack);

        if (processedVideo && processedVideo.track) {
            addRecordingCandidate(
                "webrtc-processed-video-only",
                processedVideo.track,
                null
            );
        }

        /*
         * FINAL RECORDING STRATEGY:
         *
         * Do not put audio and video into the same MediaRecorder.
         * The live tests proved that Chromium can enter
         * `recording` forever when this particular WebRTC audio
         * track is present, while video-only recording works.
         *
         * Video is therefore recorded independently from the
         * selected WebRTC/capture video source. Audio is recorded
         * independently and muxed losslessly after capture.
         *
          * Prefer the selected WebRTC source track directly.
         * Recent tests showed the isolated clone can reduce the effective
         * recorded frame rate, while the direct WebRTC track performs better.
         * Keep the clone immediately available as the first fallback if the
         * direct source fails to produce a real recording chunk.
         */

        addRecordingCandidate(
            "webrtc-video-only",
            selectedVideoTrack,
            null
        );

        addRecordingCandidate(
            "webrtc-video-only-clone",
            selectedVideoTrack,
            null,
            true
        );

        if (
            captureVideoTrack
            && captureVideoTrack.readyState === "live"
            && !captureVideoTrack.muted
        ) {
            addRecordingCandidate(
                "capture-video-only",
                captureVideoTrack,
                null
            );
        }

        /* Keep AV candidates available for diagnostics/fallback on
         * browsers where combined recording actually works. They
         * are intentionally placed last so they can never delay the
         * normal video+audio dual-recorder path. */
        if (
            captureVideoTrack
            && captureVideoTrack.readyState === "live"
            && !captureVideoTrack.muted
        ) {
            addRecordingCandidate(
                "capture-av",
                captureVideoTrack,
                selectedAudioTrack
            );
        }

        addRecordingCandidate(
            "webrtc-av",
            selectedVideoTrack,
            selectedAudioTrack
        );

        addRecordingCandidate(
            "webrtc-av-clone",
            selectedVideoTrack,
            selectedAudioTrack,
            true
        );

        /*
         * Rendered-frame diagnostic only. This does NOT alter the
         * selected stream or recording path. It measures the actual
         * frames presented by the selected <video> element so we can
         * compare them with the frames that MediaRecorder writes into
         * the WebM file.
         */
        window.__superliveRenderDiag = {
            supported: false,
            running: false,
            callbackCount: 0,
            firstTimestamp: null,
            lastTimestamp: null,
            lastMediaTime: null,
            largeGaps: 0,
            maxGap: 0,
            totalGap: 0,
            lastGap: 0,
            callbackId: null,
        };

        window.__superliveStartRenderDiagnostics = () => {
            const video = window.__superliveSelectedVideo;
            const diag = window.__superliveRenderDiag;

            if (!video || typeof video.requestVideoFrameCallback !== "function") {
                diag.supported = false;
                return {
                    supported: false,
                    reason: "requestVideoFrameCallback_unavailable",
                };
            }

            if (diag.running) {
                return {
                    supported: true,
                    running: true,
                };
            }

            diag.supported = true;
            diag.running = true;
            diag.callbackCount = 0;
            diag.firstTimestamp = null;
            diag.lastTimestamp = null;
            diag.lastMediaTime = null;
            diag.largeGaps = 0;
            diag.maxGap = 0;
            diag.totalGap = 0;
            diag.lastGap = 0;

            const onFrame = (now, metadata) => {
                if (!diag.running) {
                    return;
                }

                const timestamp = Number.isFinite(metadata && metadata.expectedDisplayTime)
                    ? metadata.expectedDisplayTime
                    : now;

                if (diag.firstTimestamp === null) {
                    diag.firstTimestamp = timestamp;
                }

                if (diag.lastTimestamp !== null) {
                    const gap = Math.max(0, (timestamp - diag.lastTimestamp) / 1000);
                    diag.lastGap = gap;
                    diag.totalGap += gap;

                    if (gap > diag.maxGap) {
                        diag.maxGap = gap;
                    }

                    if (gap > 0.2) {
                        diag.largeGaps += 1;
                    }
                }

                diag.lastTimestamp = timestamp;
                diag.lastMediaTime = Number.isFinite(metadata && metadata.mediaTime)
                    ? metadata.mediaTime
                    : null;
                diag.callbackCount += 1;

                try {
                    diag.callbackId = video.requestVideoFrameCallback(onFrame);
                } catch (e) {
                    diag.running = false;
                    console.warn(
                        "superlive render diagnostic callback error",
                        e
                    );
                }
            };

            try {
                diag.callbackId = video.requestVideoFrameCallback(onFrame);
            } catch (e) {
                diag.running = false;
                console.warn(
                    "superlive render diagnostic start error",
                    e
                );
                return {
                    supported: true,
                    running: false,
                    error: String(e),
                };
            }

            return {
                supported: true,
                running: true,
            };
        };

        window.__superliveStopRenderDiagnostics = () => {
            const diag = window.__superliveRenderDiag;
            diag.running = false;
            return true;
        };

        window.__superliveGetRenderDiagnostics = () => {
            const diag = window.__superliveRenderDiag;
            const duration =
                diag.firstTimestamp !== null && diag.lastTimestamp !== null
                    ? Math.max(0, (diag.lastTimestamp - diag.firstTimestamp) / 1000)
                    : 0;

            const fps =
                duration > 0 && diag.callbackCount > 1
                    ? (diag.callbackCount - 1) / duration
                    : 0;

            return {
                supported: !!diag.supported,
                running: !!diag.running,
                callbackCount: diag.callbackCount,
                durationSeconds: duration,
                effectiveFps: fps,
                largeGaps: diag.largeGaps,
                maxGapSeconds: diag.maxGap,
                lastGapSeconds: diag.lastGap,
                lastMediaTime: diag.lastMediaTime,
            };
        };

        return {
            hasVideo: !!captureVideoTrack,
            hasAudio: !!selectedAudioTrack,
            videoReadyState: captureVideoTrack.readyState,
            audioReadyState: selectedAudioTrack
                ? selectedAudioTrack.readyState
                : null,
            sourceVideoTrackId: selectedVideoTrack.id,
            recordedVideoTrackId: captureVideoTrack.id,
            sourceVideoTrackMuted: selectedVideoTrack.muted,
            recordedVideoTrackMuted: captureVideoTrack.muted,
            sourceAudioTrackId: selectedAudioTrack
                ? selectedAudioTrack.id
                : null,
            sourceAudioTrackMuted: selectedAudioTrack
                ? selectedAudioTrack.muted
                : null,
            videoWidth: (() => {
                try {
                    return Number(selectedVideoTrack.getSettings().width || 0);
                } catch (e) {
                    return 0;
                }
            })(),
            videoHeight: (() => {
                try {
                    return Number(selectedVideoTrack.getSettings().height || 0);
                } catch (e) {
                    return 0;
                }
            })(),
        };
    };

    window.__superliveStartRec = (
        videoBitrate,
        audioBitrate,
        timeslice,
        candidateIndex = 0,
        attemptId = "default"
    ) => {
        const candidate =
            window.__superliveRecordingCandidates
            && window.__superliveRecordingCandidates[candidateIndex]
            ? window.__superliveRecordingCandidates[candidateIndex]
            : null;

        const recordingStream = candidate
            ? candidate.stream
            : window.__preparedStream;

        if (!recordingStream) {
            throw new Error(
                "Prepared recording stream is missing"
            );
        }

        window.__superliveActiveAttemptId = attemptId;
        window.__superliveActiveCandidateName =
            candidate ? candidate.name : "prepared";

        let mimeType = "";

        if (
            MediaRecorder.isTypeSupported(
                "video/webm;codecs=vp8,opus"
            )
        ) {
            mimeType =
                "video/webm;codecs=vp8,opus";

        } else if (
            MediaRecorder.isTypeSupported(
                "video/webm;codecs=vp9,opus"
            )
        ) {
            mimeType =
                "video/webm;codecs=vp9,opus";

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

        /* Keep the encoder focused on motion rather than quality-at-all-costs.
         * On the live 720x1280 source, higher bitrates previously increased
         * encoder pressure without restoring the missing frames. */
        try {
            if (recordingStream.getVideoTracks) {
                const videoTrack = recordingStream.getVideoTracks()[0];
                if (videoTrack && "contentHint" in videoTrack) {
                    videoTrack.contentHint = "motion";
                }
            }
        } catch (e) {
            // Optional optimization only.
        }

        const recorder =
            new MediaRecorder(
                recordingStream,
                recorderOptions
            );

        window.__superliveRecorder =
            recorder;
        window.__superliveRecorderAttemptId = attemptId;

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

                if (attemptId !== window.__superliveActiveAttemptId) {
                    return;
                }

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

                    if (
                        window.__superliveVideoTransformCleanups
                    ) {
                        for (const cleanup of
                            window.__superliveVideoTransformCleanups) {
                            try {
                                cleanup();
                            } catch (e) {
                                console.warn(
                                    "superlive processed video cleanup error",
                                    e
                                );
                            }
                        }
                    }

                    return true;
                }

                if (
                    window.__superliveVideoTransformCleanups
                ) {
                    for (const cleanup of
                        window.__superliveVideoTransformCleanups) {
                    try {
                        cleanup();
                    } catch (e) {
                        console.warn(
                            "superlive processed video cleanup error",
                            e
                        );
                    }
                }
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

                    renderDiagnostics:
                        window.__superliveGetRenderDiagnostics
                        ?
                        window.__superliveGetRenderDiagnostics()
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
            state: recorder.state,
            candidateIndex,
            candidateName: candidate ? candidate.name : "prepared",
            attemptId,
            videoTrackId: candidate && candidate.videoTrack
                ? candidate.videoTrack.id
                : null,
            hasAudio: !!(candidate && candidate.audioTrack),
        };
    };

    /* ========================================================
     * INDEPENDENT AUDIO RECORDER
     * ========================================================
     *
     * Audio is intentionally recorded separately from video.
     * Some Chromium/WebRTC combinations can leave an AV
     * MediaRecorder in "recording" without ever emitting data,
     * while the same video track records correctly by itself.
     *
     * The audio WebM is therefore captured independently and later
     * muxed into the video WebM with stream copy. Packet counts are
     * checked before anything is uploaded.
     */

    window.__superliveAudioRecorder = null;
    window.__superliveAudioAttemptId = null;
    window.__superliveAudioChunkCount = 0;
    window.__superliveAudioUploadedChunkCount = 0;
    window.__superliveAudioUploadQueue = [];
    window.__superliveAudioIsUploading = false;
    window.__superliveAudioUploadError = null;
    window.__superliveAudioLastChunkAt = 0;
    window.__superliveAudioStopFired = false;
    window.__superliveAudioPendingDataTasks = 0;
    window.__superliveAudioFinalDataReady = false;
    window.__superliveAudioFinalDataResolve = null;
    window.__superliveAudioFinalDataPromise = null;

    window.__superliveStartAudioRec = (
        audioBitrate,
        timeslice,
        attemptId = "audio-default"
    ) => {
        const sourceAudioTrack =
            window.__preparedAudioTrack;

        if (
            !sourceAudioTrack
            || sourceAudioTrack.readyState !== "live"
        ) {
            throw new Error(
                "Selected audio track is not live"
            );
        }

        /*
         * MediaStreamTrack.muted means the source is temporarily not
         * providing media; it is NOT equivalent to readyState=ended.
         * A receiver can begin muted and become unmuted shortly after
         * the recorder starts.  Starting the recorder in that state is
         * therefore valid.  The Python side still requires actual
         * audio chunks before the file can be uploaded.
         */
        const sourceAudioTrackMuted = !!sourceAudioTrack.muted;

        let audioTrack = sourceAudioTrack;

        try {
            audioTrack = sourceAudioTrack.clone();
        } catch (e) {
            console.warn(
                "superlive audio clone failed; using source track",
                e
            );
        }

        const audioStream =
            new MediaStream([audioTrack]);

        let mimeType = "";

        if (
            MediaRecorder.isTypeSupported(
                "audio/webm;codecs=opus"
            )
        ) {
            mimeType =
                "audio/webm;codecs=opus";
        } else if (
            MediaRecorder.isTypeSupported(
                "audio/webm"
            )
        ) {
            mimeType = "audio/webm";
        } else {
            throw new Error(
                "No supported audio WebM MediaRecorder MIME type"
            );
        }

        const recorder =
            new MediaRecorder(
                audioStream,
                {
                    mimeType,
                    audioBitsPerSecond: audioBitrate
                }
            );

        window.__superliveAudioRecorder = recorder;
        window.__superliveAudioAttemptId = attemptId;
        window.__superliveAudioChunkCount = 0;
        window.__superliveAudioUploadedChunkCount = 0;
        window.__superliveAudioUploadQueue = [];
        window.__superliveAudioIsUploading = false;
        window.__superliveAudioUploadError = null;
        window.__superliveAudioLastChunkAt = performance.now();
        window.__superliveAudioStopFired = false;
        window.__superliveAudioPendingDataTasks = 0;
        window.__superliveAudioFinalDataReady = false;
        window.__superliveAudioFinalDataResolve = null;
        window.__superliveAudioFinalDataPromise =
            new Promise((resolve) => {
                window.__superliveAudioFinalDataResolve = resolve;
            });

        async function processAudioQueue() {
            if (window.__superliveAudioIsUploading) {
                return;
            }

            window.__superliveAudioIsUploading = true;

            try {
                while (
                    window.__superliveAudioUploadQueue.length
                ) {
                    const item =
                        window.__superliveAudioUploadQueue.shift();

                    try {
                        let success = false;
                        let lastError = null;

                        for (let attempt = 1; attempt <= 3; attempt++) {
                            try {
                                const response = await fetch(
                                    "/__slr_audio_chunk",
                                    {
                                        method: "POST",
                                        body: item.blob
                                    }
                                );

                                if (!response.ok) {
                                    throw new Error(
                                        "HTTP " + response.status
                                    );
                                }

                                success = true;
                                break;
                            } catch (e) {
                                lastError = e;
                                await new Promise(
                                    resolve => setTimeout(resolve, 250 * attempt)
                                );
                            }
                        }

                        if (!success) {
                            window.__superliveAudioUploadError =
                                String(lastError || "audio upload failed");
                            window.__superliveAudioUploadQueue.unshift(item);
                            break;
                        }

                        window.__superliveAudioUploadedChunkCount++;
                    } catch (e) {
                        window.__superliveAudioUploadError = String(e);
                        window.__superliveAudioUploadQueue.unshift(item);
                        break;
                    }
                }
            } finally {
                window.__superliveAudioIsUploading = false;

                if (window.__superliveAudioUploadQueue.length) {
                    setTimeout(
                        () => processAudioQueue(),
                        250
                    );
                }

                window.__superliveMaybeResolveAudioFinal();
            }
        }

        window.__superliveMaybeResolveAudioFinal = () => {
            /*
             * Final recorder completion is deliberately independent of the
             * Telegram upload queue.  Once MediaRecorder has stopped and
             * every dataavailable handler has finished enqueueing its blob,
             * all recorder data is locally accounted for.  The upload queue
             * is drained separately by the Python-side final queue check.
             *
             * Previously this also waited for the upload queue to become
             * empty, so a large backlog at the exact moment the video track
             * ended could make __superliveWaitAudioFinal() time out even
             * though the audio recorder had already stopped correctly.
             */
            if (
                window.__superliveAudioStopFired
                && window.__superliveAudioPendingDataTasks === 0
                && !window.__superliveAudioFinalDataReady
            ) {
                window.__superliveAudioFinalDataReady = true;
                const resolve =
                    window.__superliveAudioFinalDataResolve;
                window.__superliveAudioFinalDataResolve = null;
                if (resolve) {
                    resolve(true);
                }
            }
        };

        recorder.ondataavailable = (event) => {
            if (
                attemptId
                !== window.__superliveAudioAttemptId
            ) {
                return;
            }

            window.__superliveAudioPendingDataTasks++;

            try {
                if (
                    event.data
                    && event.data.size > 0
                ) {
                    window.__superliveAudioChunkCount++;
                    window.__superliveAudioLastChunkAt =
                        performance.now();
                    window.__superliveAudioUploadQueue.push({
                        blob: event.data
                    });
                }
            } catch (e) {
                window.__superliveAudioUploadError = String(e);
            } finally {
                window.__superliveAudioPendingDataTasks--;
                processAudioQueue();
                window.__superliveMaybeResolveAudioFinal();
            }
        };

        recorder.onerror = (event) => {
            window.__superliveAudioUploadError =
                "Audio MediaRecorder error";
            console.error(
                "superlive audio MediaRecorder error",
                event
            );
        };

        recorder.onstop = () => {
            window.__superliveAudioStopFired = true;
            window.__superliveMaybeResolveAudioFinal();
        };

        recorder.start(timeslice);

        return {
            mimeType,
            state: recorder.state,
            trackId: audioTrack.id,
            sourceTrackId: sourceAudioTrack.id,
            sourceTrackMuted: sourceAudioTrackMuted,
            recordedTrackMuted: !!audioTrack.muted,
            readyState: audioTrack.readyState
        };
    };

    window.__superliveWaitAudioChunk = async (timeoutMs) => {
        const start = performance.now();

        while (
            window.__superliveAudioChunkCount < 1
        ) {
            const recorder =
                window.__superliveAudioRecorder;

            if (
                recorder
                && recorder.state !== "recording"
            ) {
                return {
                    ok: false,
                    reason: "recorder_not_recording",
                    chunkCount: window.__superliveAudioChunkCount,
                    recorderState: recorder.state,
                    error: window.__superliveAudioUploadError
                };
            }

            if (performance.now() - start > timeoutMs) {
                return {
                    ok: false,
                    reason: "timeout",
                    chunkCount: window.__superliveAudioChunkCount,
                    recorderState: recorder
                        ? recorder.state
                        : null,
                    error: window.__superliveAudioUploadError,
                    trackState:
                        window.__preparedAudioTrack
                            ? window.__preparedAudioTrack.readyState
                            : null,
                    trackMuted:
                        window.__preparedAudioTrack
                            ? window.__preparedAudioTrack.muted
                            : null
                };
            }

            await new Promise(
                resolve => setTimeout(resolve, 100)
            );
        }

        return {
            ok: true,
            chunkCount: window.__superliveAudioChunkCount
        };
    };

    window.__superliveStopAudioRec = () => {
        const recorder = window.__superliveAudioRecorder;

        if (
            recorder
            && recorder.state !== "inactive"
        ) {
            recorder.stop();
            return true;
        }

        window.__superliveAudioStopFired = true;
        window.__superliveMaybeResolveAudioFinal();
        return false;
    };

    window.__superliveWaitAudioFinal = async (timeoutMs) => {
        if (window.__superliveAudioFinalDataReady) {
            return true;
        }

        const timeoutPromise = new Promise(
            resolve => setTimeout(() => resolve(false), timeoutMs)
        );

        const result = await Promise.race([
            window.__superliveAudioFinalDataPromise,
            timeoutPromise
        ]);

        return result === true;
    };

    window.__superliveGetAudioStatus = () => ({
        chunkCount: window.__superliveAudioChunkCount,
        uploadedChunkCount:
            window.__superliveAudioUploadedChunkCount,
        queueLength:
            window.__superliveAudioUploadQueue.length,
        isUploading:
            window.__superliveAudioIsUploading,
        uploadError:
            window.__superliveAudioUploadError,
        pendingDataTasks:
            window.__superliveAudioPendingDataTasks,
        recorderState:
            window.__superliveAudioRecorder
                ? window.__superliveAudioRecorder.state
                : null,
        finalDataReady:
            window.__superliveAudioFinalDataReady
    });

    window.__superliveWaitChunk =
        async (timeoutMs) => {
            const start =
                performance.now();

            let nextRequestAt =
                start + 1500;

            while (
                window.__superliveChunkCount < 1
            ) {
                const now =
                    performance.now();

                const recorder =
                    window.__superliveRecorder;

                if (
                    recorder
                    && recorder.state !== "recording"
                ) {
                    return {
                        ok: false,
                        reason: "recorder_not_recording",
                        chunkCount:
                            window.__superliveChunkCount,
                        recorderState:
                            recorder.state,
                        uploadError:
                            window.__superliveUploadError,
                        renderDiagnostics:
                            window.__superliveGetRenderDiagnostics
                            ? window.__superliveGetRenderDiagnostics()
                            : null,
                        webrtcDiagnostics:
                            window.__superliveGetWebRTCStats
                            ? await window.__superliveGetWebRTCStats()
                            : null
                    };
                }

                if (now >= nextRequestAt) {
                    try {
                        if (
                            recorder
                            && recorder.state === "recording"
                            && typeof recorder.requestData ===
                                "function"
                        ) {
                            recorder.requestData();
                        }
                    } catch (e) {
                        console.warn(
                            "superlive requestData error",
                            e
                        );
                    }

                    nextRequestAt =
                        now + 1500;
                }

                if (
                    now - start
                    > timeoutMs
                ) {
                    const video =
                        window.__superliveSelectedVideo;

                    const videoTrack =
                        window.__preparedVideoTrack;

                    return {
                        ok: false,
                        reason: "timeout",
                        chunkCount:
                            window.__superliveChunkCount,
                        recorderState: recorder
                            ? recorder.state
                            : null,
                        recorderMimeType: recorder
                            ? recorder.mimeType
                            : null,
                        videoReadyState: video
                            ? video.readyState
                            : null,
                        videoPaused: video
                            ? video.paused
                            : null,
                        videoCurrentTime: video
                            ? video.currentTime
                            : null,
                        videoWidth: video
                            ? video.videoWidth
                            : null,
                        videoHeight: video
                            ? video.videoHeight
                            : null,
                        videoTrackState: videoTrack
                            ? videoTrack.readyState
                            : null,
                        videoTrackMuted: videoTrack
                            ? videoTrack.muted
                            : null,
                        uploadError:
                            window.__superliveUploadError,
                        activeCandidateName:
                            window.__superliveActiveCandidateName || null,
                        renderDiagnostics:
                            window.__superliveGetRenderDiagnostics
                            ? window.__superliveGetRenderDiagnostics()
                            : null,
                        webrtcDiagnostics:
                            window.__superliveGetWebRTCStats
                            ? await window.__superliveGetWebRTCStats()
                            : null
                    };
                }

                await new Promise(
                    resolve =>
                        setTimeout(
                            resolve,
                            100
                        )
                );
            }

            return {
                ok: true,
                chunkCount:
                    window.__superliveChunkCount
            };
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

    audio_webm_path = (
        TEMP_DIR
        / f"recording_{timestamp}_audio.webm"
    )

    muxed_webm_path = (
        TEMP_DIR
        / f"recording_{timestamp}_av.webm"
    )

    mp4_path = (
        RECORDING_DIR
        / f"recording_{timestamp}.mp4"
    )

    webm_file = None
    audio_webm_file = None

    audio_chunk_count = 0
    audio_total_bytes = 0

    encoded_video_chunk_count = 0
    encoded_video_frame_count = 0
    encoded_video_keyframe_count = 0
    encoded_video_total_bytes = 0
    encoded_video_first_timestamp = None
    encoded_video_last_timestamp = None
    encoded_video_ivf_path = (
        TEMP_DIR
        / f"recording_{timestamp}_encoded.ivf"
    )
    encoded_video_file = None
    encoded_video_mode = False

    try:
        # Video output is opened lazily: encoded WebRTC capture writes IVF
        # first, while the MediaRecorder fallback writes WebM chunks directly.
        webm_file = None

        audio_webm_file = open(
            audio_webm_path,
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
                    if webm_file is None:
                        raise RuntimeError(
                            "MediaRecorder chunk received before WebM file was opened"
                        )
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

        async def handle_audio_chunk(
            route,
            request,
        ):
            nonlocal audio_chunk_count
            nonlocal audio_total_bytes

            try:
                body = request.post_data_buffer

                if body:
                    audio_webm_file.write(body)
                    audio_webm_file.flush()
                    audio_chunk_count += 1
                    audio_total_bytes += len(body)

                await route.fulfill(
                    status=200,
                    body=b"OK",
                )

            except Exception as e:
                log(
                    f"Audio chunk handler error: {e}"
                )

                try:
                    await route.fulfill(
                        status=500,
                        body=b"ERROR",
                    )
                except Exception:
                    pass

        async def handle_encoded_video_chunk(
            route,
            request,
        ):
            nonlocal encoded_video_chunk_count
            nonlocal encoded_video_frame_count
            nonlocal encoded_video_keyframe_count
            nonlocal encoded_video_total_bytes
            nonlocal encoded_video_first_timestamp
            nonlocal encoded_video_last_timestamp

            try:
                body = request.post_data_buffer or b""

                if not body:
                    raise RuntimeError(
                        "Empty encoded video batch"
                    )

                if len(body) < 8 or body[:4] != b"SLVF":
                    raise RuntimeError(
                        "Invalid encoded video batch header"
                    )

                if body[4] != 1:
                    raise RuntimeError(
                        f"Unsupported encoded video batch version: {body[4]}"
                    )

                offset = 8
                batch_frames = 0

                while offset < len(body):
                    if offset + 13 > len(body):
                        raise RuntimeError(
                            "Truncated encoded video frame header"
                        )

                    timestamp_us = struct.unpack_from(
                        "<Q",
                        body,
                        offset,
                    )[0]
                    offset += 8

                    frame_type = body[offset]
                    offset += 1

                    frame_size = struct.unpack_from(
                        "<I",
                        body,
                        offset,
                    )[0]
                    offset += 4

                    if frame_size <= 0:
                        raise RuntimeError(
                            "Encoded video frame has invalid size"
                        )

                    end = offset + frame_size
                    if end > len(body):
                        raise RuntimeError(
                            "Truncated encoded video frame payload"
                        )

                    frame_data = body[offset:end]
                    offset = end

                    if encoded_video_first_timestamp is None:
                        encoded_video_first_timestamp = timestamp_us

                    if (
                        encoded_video_last_timestamp is not None
                        and timestamp_us < encoded_video_last_timestamp
                    ):
                        raise RuntimeError(
                            "Encoded video timestamp moved backwards"
                        )

                    encoded_video_last_timestamp = timestamp_us

                    encoded_video_file.write(
                        struct.pack(
                            "<IQ",
                            frame_size,
                            max(
                                0,
                                timestamp_us
                                - encoded_video_first_timestamp,
                            ),
                        )
                    )
                    encoded_video_file.write(frame_data)

                    encoded_video_frame_count += 1
                    batch_frames += 1
                    encoded_video_total_bytes += frame_size

                    if frame_type == 1:
                        encoded_video_keyframe_count += 1

                encoded_video_file.flush()
                encoded_video_chunk_count += 1

                await route.fulfill(
                    status=200,
                    body=b"OK",
                )

                log(
                    "Encoded video batch received: "
                    f"batch={encoded_video_chunk_count} "
                    f"frames={batch_frames} "
                    f"total_frames={encoded_video_frame_count}"
                )

            except Exception as e:
                log(
                    f"Encoded video batch handler error: {e}"
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

        await page.route(
            "**/__slr_audio_chunk",
            handle_audio_chunk,
        )

        await page.route(
            "**/__slr_encoded_video_chunk",
            handle_encoded_video_chunk,
        )

        log_section(
            "OPENING PAGE"
        )

        await page.goto(
            URL,
            wait_until="domcontentloaded",
            timeout=PAGE_TIMEOUT_MS,
        )

        await page.evaluate(
            """
            (streamId) => {
                window.__superliveTargetStreamId =
                    streamId || null;
            }
            """,
            STREAM_ID,
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
                    async () => {
                        try {
                            if (!window.__superliveSelectTargetVideo) {
                                return {ready: false};
                            }

                            const selection =
                                await window.__superliveSelectTargetVideo();

                            return {
                                ready: true,
                                selection,
                            };
                        } catch (e) {
                            return {
                                ready: false,
                                error: String(e),
                            };
                        }
                    }
                    """
                )

                if result.get("ready"):
                    video_ready = True

                    selection = result.get("selection") or {}
                    selected = selection.get("selected") or {}

                    log(
                        "Live video detected: "
                        f"{selected.get('width')}x"
                        f"{selected.get('height')} "
                        f"track={selected.get('trackId')} "
                        f"stream={selected.get('streamId')}"
                    )

                    log(
                        "Video target candidates: "
                        + json.dumps(
                            selection.get("candidates", []),
                            ensure_ascii=False,
                        )
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

        # The encoded WebRTC path writes IVF frames locally.  The IVF header
        # is finalized only after the first real encoded frame stream is
        # accepted, because the selected track dimensions are known then.
        encoded_video_width = int(
            prepared.get("videoWidth")
            or 0
        )
        encoded_video_height = int(
            prepared.get("videoHeight")
            or 0
        )

        # ----------------------------------------------------
        # Ensure selected video is actually playing
        # ----------------------------------------------------

        play_result = await page.evaluate(
            """
            () =>
                window.__superliveEnsureSelectedVideoPlaying
                    ? window.__superliveEnsureSelectedVideoPlaying()
                    : null
            """
        )

        log(
            "Selected video playback check: "
            + json.dumps(
                play_result,
                ensure_ascii=False,
            )
        )

        # ----------------------------------------------------
        # Transactional MediaRecorder startup
        # ----------------------------------------------------
        candidates = await page.evaluate(
            """
            () => (window.__superliveRecordingCandidates || []).map(
                (candidate, index) => ({
                    index,
                    name: candidate.name,
                    hasAudio: !!candidate.audioTrack,
                    videoTrackId: candidate.videoTrack
                        ? candidate.videoTrack.id
                        : null,
                    sourceVideoTrackId: candidate.sourceVideoTrack
                        ? candidate.sourceVideoTrack.id
                        : null,
                    videoTrackState: candidate.videoTrack
                        ? candidate.videoTrack.readyState
                        : null,
                    videoTrackMuted: candidate.videoTrack
                        ? candidate.videoTrack.muted
                        : null,
                })
            )
            """
        )

        log(
            "Recorder candidates: "
            + json.dumps(candidates, ensure_ascii=False)
        )

        candidates = [
            candidate
            for candidate in candidates
            if not candidate.get("hasAudio")
        ]

        log(
            "Video-only recorder candidates selected: "
            + json.dumps(candidates, ensure_ascii=False)
        )

        if not candidates:
            raise RuntimeError(
                "No usable video-only recording candidates were created"
            )

        first_chunk = None
        accepted_candidate = None
        video_capture_mode = "mediarecorder"
        encoded_video_started = False

        # ----------------------------------------------------
        # Preferred video path: capture the already-encoded VP8
        # frames directly from the selected WebRTC receiver.
        # ----------------------------------------------------
        encoded_video_width = encoded_video_width or 720
        encoded_video_height = encoded_video_height or 1280

        if encoded_video_width <= 0 or encoded_video_height <= 0:
            raise RuntimeError(
                "Selected video dimensions are unavailable for IVF header"
            )

        encoded_video_file = open(
            encoded_video_ivf_path,
            "wb",
        )
        encoded_video_file.write(
            struct.pack(
                "<4sHH4sHHIIII",
                b"DKIF",
                0,
                32,
                b"VP80",
                encoded_video_width,
                encoded_video_height,
                1_000_000,
                1,
                0,
                0,
                0,
                0,
            )
        )
        encoded_video_file.flush()

        encoded_capability = await page.evaluate(
            """
            (trackId) => {
                const track = (window.__superliveVideoTracks || [])
                    .find(item => item && item.id === trackId);
                return window.__superliveEnableEncodedVideo
                    ? window.__superliveEnableEncodedVideo(track)
                    : {supported: false, reason: "encoded_api_missing"};
            }
            """,
            prepared.get("sourceVideoTrackId"),
        )

        log(
            "Encoded WebRTC video capability: "
            + json.dumps(
                encoded_capability,
                ensure_ascii=False,
            )
        )

        if encoded_capability.get("supported"):
            try:
                encoded_wait = await page.evaluate(
                    """
                    () => window.__superliveWaitEncodedVideoFrame
                        ? window.__superliveWaitEncodedVideoFrame(5000)
                        : {ok: false, reason: "encoded_wait_missing"}
                    """
                )

                log(
                    "First encoded WebRTC video frame: "
                    + json.dumps(
                        encoded_wait,
                        ensure_ascii=False,
                    )
                )

                if encoded_wait.get("ok"):
                    encoded_video_mode = True
                    encoded_video_started = True
                    video_capture_mode = "webrtc-encoded-vp8"
                    log(
                        "Using direct encoded WebRTC VP8 capture; "
                        "MediaRecorder video encoder is bypassed"
                    )
                else:
                    raise RuntimeError(
                        "Encoded WebRTC transform was installed but did not "
                        "produce a first frame: "
                        + json.dumps(encoded_wait, ensure_ascii=False)
                    )

            except Exception as e:
                log(
                    "Encoded WebRTC video path rejected; "
                    f"falling back to MediaRecorder: {e}"
                )
                encoded_video_mode = False
                encoded_video_started = False
                video_capture_mode = "mediarecorder"

                try:
                    if encoded_video_file is not None:
                        encoded_video_file.close()
                except Exception:
                    pass
                encoded_video_file = None

                try:
                    encoded_video_ivf_path.unlink()
                except FileNotFoundError:
                    pass

        if not encoded_video_started:
            try:
                if encoded_video_file is not None:
                    encoded_video_file.close()
            except Exception:
                pass
            encoded_video_file = None
            try:
                encoded_video_ivf_path.unlink()
            except FileNotFoundError:
                pass

            webm_file = open(
                webm_path,
                "wb",
            )

            # ----------------------------------------------------
            # Fallback: existing transactional MediaRecorder path.
            # ----------------------------------------------------
            for candidate in candidates:
                chunk_count = 0
                total_bytes = 0
                attempt_id = f"{candidate['index']}-{time.monotonic_ns()}"

                start_result = await page.evaluate(
                    """
                    ([videoBitrate, audioBitrate, timeslice, candidateIndex, attemptId]) =>
                        window.__superliveStartRec(
                            videoBitrate,
                            audioBitrate,
                            timeslice,
                            candidateIndex,
                            attemptId
                        )
                    """,
                    [
                        VIDEO_BITRATE,
                        AUDIO_BITRATE,
                        1000,
                        candidate["index"],
                        attempt_id,
                    ],
                )

                log(
                    "MediaRecorder startup attempt: "
                    + json.dumps(start_result, ensure_ascii=False)
                )

                attempt_result = await page.evaluate(
                    """
                    () => window.__superliveWaitChunk(4000)
                    """
                )

                if (
                    attempt_result
                    and attempt_result.get("ok")
                    and attempt_result.get("chunkCount", 0) >= 1
                ):
                    first_chunk = attempt_result
                    accepted_candidate = candidate
                    log(
                        "MediaRecorder candidate accepted: "
                        + json.dumps(
                            {"candidate": candidate, "result": attempt_result},
                            ensure_ascii=False,
                        )
                    )
                    break

                log(
                    "MediaRecorder candidate rejected: "
                    + json.dumps(
                        {"candidate": candidate, "result": attempt_result},
                        ensure_ascii=False,
                    )
                )

                try:
                    await page.evaluate(
                        """
                        () => window.__superliveStopRec
                            ? window.__superliveStopRec()
                            : false
                        """
                    )
                except Exception as e:
                    log(
                        f"Failed to stop rejected recorder candidate: {e}"
                    )

                if webm_file is not None:
                    webm_file.seek(0)
                    webm_file.truncate(0)
                    webm_file.flush()

                chunk_count = 0
                total_bytes = 0
                await asyncio.sleep(1)

            if accepted_candidate is None:
                raise RuntimeError(
                    "All MediaRecorder candidates failed to produce a first chunk; "
                    + json.dumps(
                        {
                            "candidates": candidates,
                            "lastDiagnostics": first_chunk,
                        },
                        ensure_ascii=False,
                    )
                )

            log(
                "First recording chunk received: "
                + json.dumps(first_chunk, ensure_ascii=False)
            )
        else:
            log(
                "Encoded video capture accepted: "
                + json.dumps(
                    {
                        "mode": video_capture_mode,
                        "width": encoded_video_width,
                        "height": encoded_video_height,
                    },
                    ensure_ascii=False,
                )
            )

        # ----------------------------------------------------
        # Start independent audio recorder
        # ----------------------------------------------------

        audio_expected = bool(
            prepared.get("hasAudio")
        )

        audio_first_chunk = None

        if audio_expected:
            audio_attempt_id = (
                f"audio-{time.monotonic_ns()}"
            )

            try:
                audio_start_result = await page.evaluate(
                    """
                    ([audioBitrate, timeslice, attemptId]) =>
                        window.__superliveStartAudioRec(
                            audioBitrate,
                            timeslice,
                            attemptId
                        )
                    """,
                    [
                        AUDIO_BITRATE,
                        1000,
                        audio_attempt_id,
                    ],
                )

                log(
                    "Independent audio recorder started: "
                    + json.dumps(
                        audio_start_result,
                        ensure_ascii=False,
                    )
                )

                audio_first_chunk = await page.evaluate(
                    """
                    () =>
                        window.__superliveWaitAudioChunk(15000)
                    """
                )

                log(
                    "Independent audio first chunk: "
                    + json.dumps(
                        audio_first_chunk,
                        ensure_ascii=False,
                    )
                )

                if not audio_first_chunk.get("ok"):
                    audio_diag = None
                    try:
                        audio_diag = await page.evaluate(
                            """
                            async () => ({
                                audioStatus: window.__superliveGetAudioStatus
                                    ? window.__superliveGetAudioStatus()
                                    : null,
                                webrtcStats: window.__superliveGetWebRTCStats
                                    ? await window.__superliveGetWebRTCStats()
                                    : null
                            })
                            """
                        )
                    except Exception as diag_error:
                        audio_diag = {
                            "diagnosticError": str(diag_error)
                        }

                    log(
                        "Independent audio diagnostics after first-chunk failure: "
                        + json.dumps(
                            audio_diag,
                            ensure_ascii=False,
                        )
                    )

                    raise RuntimeError(
                        "Independent audio recorder failed to produce data"
                    )

            except Exception:
                try:
                    await page.evaluate(
                        """
                        () =>
                            window.__superliveStopAudioRec
                                ? window.__superliveStopAudioRec()
                                : false
                        """
                    )
                except Exception:
                    pass
                raise

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

                encoded_status = {}
                if encoded_video_mode:
                    encoded_status = await page.evaluate(
                        """
                        () => window.__superliveGetEncodedVideoStatus
                            ? window.__superliveGetEncodedVideoStatus()
                            : {}
                        """
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

                if not encoded_video_mode and (
                    idle_ms
                    > STREAM_IDLE_TIMEOUT * 1000
                ):
                    log(
                        "No recording chunk received "
                        f"for "
                        f"{idle_ms / 1000:.1f}s"
                    )
                    break

                if encoded_video_mode:
                    if encoded_status.get("uploadError"):
                        raise RuntimeError(
                            "Encoded video capture upload error: "
                            + str(encoded_status.get("uploadError"))
                        )
                    if (
                        encoded_status.get("frameCount", 0) > 0
                        and encoded_status.get("uploadedFrameCount", 0)
                        < encoded_status.get("frameCount", 0)
                        and encoded_status.get("pendingDataTasks", 0) == 0
                        and not encoded_status.get("uploading")
                    ):
                        # The browser only reaches this state when a batch was
                        # dropped before the upload queue could recover.
                        raise RuntimeError(
                            "Encoded video upload counters stopped advancing"
                        )

                if (
                    time.monotonic()
                    - last_status_log
                    >= 30
                ):
                    settings = status.get(
                        "videoSettings"
                    )

                    if encoded_video_mode:
                        log(
                            "Encoded video status: "
                            + json.dumps(
                                encoded_status,
                                ensure_ascii=False,
                            )
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

                    if encoded_video_mode:
                        log(
                            "Encoded VP8 received locally: "
                            f"{encoded_video_total_bytes / 1024 / 1024:.2f} MB "
                            f"frames={encoded_video_frame_count}"
                        )
                    else:
                        log(
                            f"Local WebM received: "
                            f"{total_bytes / 1024 / 1024:.2f} MB"
                        )

                    render_diag = status.get(
                        "renderDiagnostics"
                    )

                    log(
                        "Rendered-frame status: "
                        + json.dumps(
                            render_diag,
                            ensure_ascii=False,
                        )
                    )

                    try:
                        webrtc_diag = await page.evaluate(
                            """
                            () =>
                                window.__superliveGetWebRTCStats
                                    ? window.__superliveGetWebRTCStats()
                                    : null
                            """
                        )
                        log(
                            "WebRTC inbound video stats: "
                            + json.dumps(
                                webrtc_diag,
                                ensure_ascii=False,
                            )
                        )
                    except Exception as e:
                        log(
                            f"WebRTC stats diagnostic error: {e}"
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

        final_render_diag = await page.evaluate(
            """
            () => {
                if (window.__superliveStopRenderDiagnostics) {
                    window.__superliveStopRenderDiagnostics();
                }
                return window.__superliveGetRenderDiagnostics
                    ? window.__superliveGetRenderDiagnostics()
                    : null;
            }
            """
        )

        log(
            "Final rendered-frame diagnostics: "
            + json.dumps(
                final_render_diag,
                ensure_ascii=False,
            )
        )

        log(
            "Stopping video capture..."
        )

        try:
            if encoded_video_mode:
                stop_result = await page.evaluate(
                    """
                    () => window.__superliveStopEncodedVideo
                        ? window.__superliveStopEncodedVideo()
                        : null
                    """
                )
                log(
                    "Encoded WebRTC video capture stop result: "
                    + json.dumps(
                        stop_result,
                        ensure_ascii=False,
                    )
                )
            else:
                stop_result = await page.evaluate(
                    """
                    () => window.__superliveStopRec()
                    """
                )

                log(
                    "MediaRecorder stop requested: "
                    f"{stop_result}"
                )

            if audio_expected:
                audio_stop_result = await page.evaluate(
                    """
                    () =>
                        window.__superliveStopAudioRec()
                    """
                )

                log(
                    "Independent audio recorder stop requested: "
                    f"{audio_stop_result}"
                )

        except Exception as e:
            log(
                f"MediaRecorder stop error: {e}"
            )

        # ----------------------------------------------------
        # WAIT FOR ACTUAL FINAL DATAAVAILABLE
        # ----------------------------------------------------

        if encoded_video_mode:
            final_data_ready = True
            log(
                "Encoded WebRTC video final batch processing completed"
            )
        else:
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

        if audio_expected:
            audio_final_ready = await page.evaluate(
                """
                () =>
                    window.__superliveWaitAudioFinal(30000)
                """
            )

            if not audio_final_ready:
                raise RuntimeError(
                    "Timed out waiting for independent audio final dataavailable event"
                )

            log(
                "Independent audio final dataavailable processing completed"
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
                        () => window.__superliveGetStatus()
                        """
                    )

                    if encoded_video_mode:
                        encoded_status = await page.evaluate(
                            """
                            () => window.__superliveGetEncodedVideoStatus
                                ? window.__superliveGetEncodedVideoStatus()
                                : {}
                            """
                        )
                        queue_length = int(encoded_status.get("queueLength", 0))
                        uploading = bool(encoded_status.get("uploading", False))
                        recorder_chunks = int(encoded_status.get("frameCount", 0))
                        uploaded_chunks = int(encoded_status.get("uploadedFrameCount", 0))
                        pending_tasks = int(encoded_status.get("pendingDataTasks", 0))
                        upload_error = encoded_status.get("uploadError")
                    else:
                        queue_length = int(status.get("queueLength", 0))
                        uploading = bool(status.get("isUploading", False))
                        recorder_chunks = int(status.get("chunkCount", 0))
                        uploaded_chunks = int(status.get("uploadedChunkCount", 0))
                        pending_tasks = int(status.get("pendingDataTasks", 0))
                        upload_error = status.get("uploadError")

                    audio_status = {}
                    if audio_expected:
                        try:
                            audio_status = await page.evaluate(
                                """
                                () =>
                                    window.__superliveGetAudioStatus()
                                """
                            )
                        except Exception as e:
                            raise RuntimeError(
                                "Unable to read independent audio recorder status: "
                                + str(e)
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
    
                        if audio_expected:
                            log(
                                "Independent audio drain status: "
                                + json.dumps(
                                    audio_status,
                                    ensure_ascii=False,
                                )
                            )
    
                        log(
                            f"Local WebM received: "
                            f"{total_bytes / 1024 / 1024:.2f} MB"
                        )
    
                        log(
                            f"Local audio WebM received: "
                            f"{audio_total_bytes / 1024 / 1024:.2f} MB"
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
    
                    audio_drained = True
                    if audio_expected:
                        audio_drained = (
                            int(audio_status.get("queueLength", 0)) == 0
                            and not bool(audio_status.get("isUploading"))
                            and int(audio_status.get("pendingDataTasks", 0)) == 0
                            and int(audio_status.get("uploadedChunkCount", 0))
                                == int(audio_status.get("chunkCount", 0))
                            and int(audio_status.get("chunkCount", 0)) > 0
                            and not audio_status.get("uploadError")
                        )
    
                    if (
                        queue_length == 0
                        and not uploading
                        and pending_tasks == 0
                        and uploaded_chunks == recorder_chunks
                        and recorder_chunks > 0
                        and not upload_error
                        and audio_drained
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
            if encoded_video_mode:
                final_status = await page.evaluate(
                    """
                    () => window.__superliveGetEncodedVideoStatus
                        ? window.__superliveGetEncodedVideoStatus()
                        : null
                    """
                ) or {}
            else:
                final_status = await page.evaluate(
                    """
                    () => window.__superliveGetStatus()
                    """
                )

            log(
                "Final video recorder status: "
                + json.dumps(
                    final_status,
                    ensure_ascii=False,
                )
            )

        except Exception as e:
            raise RuntimeError(
                "Unable to obtain final video recorder status: "
                + str(e)
            )

        if encoded_video_mode:
            recorder_chunk_count = int(final_status.get("frameCount", 0))
            uploaded_chunk_count = int(final_status.get("uploadedFrameCount", 0))
        else:
            recorder_chunk_count = int(final_status.get("chunkCount", 0))
            uploaded_chunk_count = int(final_status.get("uploadedChunkCount", 0))

        if (
            recorder_chunk_count <= 0
            or uploaded_chunk_count != recorder_chunk_count
        ):
            raise RuntimeError(
                "Video capture integrity check failed: "
                f"captured={recorder_chunk_count}, "
                f"uploaded={uploaded_chunk_count}"
            )

        if encoded_video_mode:
            if (
                final_status.get("queueLength", 0) != 0
                or final_status.get("uploading")
                or final_status.get("uploadError")
                or final_status.get("pendingDataTasks", 0) != 0
            ):
                raise RuntimeError(
                    "Encoded video queue is not fully drained or contains an upload error"
                )
        else:
            if (
                final_status.get("queueLength", 0) != 0
                or final_status.get("isUploading")
                or final_status.get("uploadError")
            ):
                raise RuntimeError(
                    "Queue is not fully drained or contains an upload error"
                )

        if audio_expected:
            final_audio_status = await page.evaluate(
                """
                () =>
                    window.__superliveGetAudioStatus()
                """
            )

            log(
                "Final independent audio recorder status: "
                + json.dumps(
                    final_audio_status,
                    ensure_ascii=False,
                )
            )

            if (
                int(final_audio_status.get("chunkCount", 0)) <= 0
                or int(final_audio_status.get("uploadedChunkCount", 0))
                    != int(final_audio_status.get("chunkCount", 0))
                or final_audio_status.get("queueLength", 0) != 0
                or final_audio_status.get("isUploading")
                or final_audio_status.get("uploadError")
            ):
                raise RuntimeError(
                    "Independent audio chunk integrity check failed: "
                    + json.dumps(
                        final_audio_status,
                        ensure_ascii=False,
                    )
                )

        if encoded_video_mode:
            log(
                "Final encoded VP8 received: "
                f"{encoded_video_total_bytes / 1024 / 1024:.2f} MB, "
                f"frames={encoded_video_frame_count}, "
                f"batches={encoded_video_chunk_count}"
            )
        else:
            log(
                f"Final WebM received: "
                f"{total_bytes / 1024 / 1024:.2f} MB, "
                f"chunks={chunk_count}"
            )

        await page.unroute(
            "**/__slr_chunk",
            handle_chunk,
        )

        await page.unroute(
            "**/__slr_audio_chunk",
            handle_audio_chunk,
        )

        await page.unroute(
            "**/__slr_encoded_video_chunk",
            handle_encoded_video_chunk,
        )

        # ----------------------------------------------------
        # Close independent WebM files safely
        # ----------------------------------------------------

        if encoded_video_mode:
            if encoded_video_file is None:
                raise RuntimeError(
                    "Encoded video file handle is missing"
                )

            encoded_video_file.flush()
            os.fsync(encoded_video_file.fileno())

            # Patch IVF frame count in the fixed 32-byte header.
            encoded_video_file.seek(24)
            encoded_video_file.write(
                struct.pack(
                    "<I",
                    encoded_video_frame_count,
                )
            )
            encoded_video_file.flush()
            os.fsync(encoded_video_file.fileno())
            encoded_video_file.close()
            encoded_video_file = None

            log(
                "Final encoded VP8 IVF: "
                f"{encoded_video_ivf_path.stat().st_size / 1024 / 1024:.2f} MB, "
                f"frames={encoded_video_frame_count}, "
                f"keyframes={encoded_video_keyframe_count}"
            )

            remux_ivf_to_webm(
                encoded_video_ivf_path,
                webm_path,
                encoded_video_frame_count,
            )

            try:
                encoded_video_ivf_path.unlink()
            except FileNotFoundError:
                pass
        else:
            webm_file.flush()
            os.fsync(webm_file.fileno())
            webm_file.close()
            webm_file = None

        if audio_webm_file is not None:
            audio_webm_file.flush()
            os.fsync(audio_webm_file.fileno())
            audio_webm_file.close()
            audio_webm_file = None

        await browser.close()

        # ----------------------------------------------------
        # Verify independent streams before muxing
        # ----------------------------------------------------

        log_section(
            "VERIFY INDEPENDENT WEBM STREAMS"
        )

        if not verify_video_file(
            webm_path,
            allow_zero_duration=True,
        ):
            raise RuntimeError(
                "Independent video WebM failed verification"
            )

        if audio_expected:
            if not verify_audio_file(
                audio_webm_path
            ):
                raise RuntimeError(
                    "Independent audio WebM failed verification"
                )

            mux_webm_video_audio(
                webm_path,
                audio_webm_path,
                muxed_webm_path,
            )

            # The muxed file becomes the ONLY file eligible for
            # timing analysis, splitting, and Telegram upload.
            webm_path.unlink(missing_ok=True)
            audio_webm_path.unlink(missing_ok=True)
            webm_path = muxed_webm_path

            log(
                "Lossless WebM AV mux completed; both streams are present "
                "and packet counts were preserved."
            )

        # ----------------------------------------------------
        # STRICT VIDEO TIMING ANALYSIS
        # ----------------------------------------------------
        #
        # Read the original WebM packet timestamps before any
        # split or Telegram upload. This does not decode or
        # re-encode the video and therefore cannot alter it.
        #
        # A low effective FPS or a positive timing gap is only
        # diagnostic. Duplicate/backwards timestamps are a hard
        # failure because they make the source timeline unsafe.
        # ----------------------------------------------------

        timing_info = analyze_webm_video_timing(
            webm_path
        )

        log(
            "Original WebM timing verified: "
            f"effective_fps="
            f"{timing_info['effective_fps']:.3f}, "
            f"timeline="
            f"{timing_info['timeline_span']:.3f}s, "
            f"large_gaps="
            f"{timing_info['large_gaps']}"
        )

        # ----------------------------------------------------
        # DIRECT WEBM TEST
        # ----------------------------------------------------
        #
        # IMPORTANT:
        # No WebM -> MP4 conversion is performed in this mode.
        # The MediaRecorder WebM is preserved as-is so we can
        # determine whether the MP4 conversion was causing frame
        # loss/timestamp problems.
        # ----------------------------------------------------

        log_section(
            "DIRECT WEBM MODE (NO MP4 CONVERSION)"
        )

        parts = split_webm_if_needed(
            webm_path
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
                "🎥 Recording (WebM)"
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
        # Cleanup WebM parts
        # ----------------------------------------------------

        for part in parts:
            try:
                if part.exists():
                    part.unlink()

            except Exception as e:
                log(
                    f"Could not remove WebM part "
                    f"{part}: {e}"
                )

        if webm_path.exists():
            try:
                webm_path.unlink()
            except Exception as e:
                log(
                    f"Could not remove original WebM: {e}"
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
            if audio_webm_file is not None:
                audio_webm_file.flush()
                audio_webm_file.close()
                audio_webm_file = None

            if encoded_video_file is not None:
                encoded_video_file.flush()
                encoded_video_file.close()
                encoded_video_file = None

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
        "(DIRECT WEBM + ORDERED CHUNKS)"
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
