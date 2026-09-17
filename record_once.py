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

VIDEO_BITRATE = 3_000_000
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

FINAL_QUEUE_DRAIN_TIMEOUT_SECONDS = 15 * 60

KEEP_SOURCE_WEBM = os.environ.get(
    "KEEP_SOURCE_WEBM", "1"
).lower() in (
    "1", "true", "yes", "on"
)

# Diagnostic A/B mode:
#   record      = normal recording pipeline (MediaRecorder enabled)
#   source_only = same browser/page/stream diagnostics, but NO MediaRecorder
#
# For the decisive A/B test, run this same file twice:
#   1) AB_TEST_MODE=source_only
#   2) AB_TEST_MODE=record
#
# In source_only mode the script does not create WebM/MP4 and does not upload.
AB_TEST_MODE = "source_only"

DIAGNOSTIC_SECONDS = 300

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

def get_video_packet_count(info):
    if not info:
        return 0

    for stream in info.get("streams", []):
        if stream.get("codec_type") != "video":
            continue

        try:
            return int(
                stream.get("nb_read_packets")
                or stream.get("nb_frames")
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
                f"{stream.get('nb_frames')} "
                f"nb_read_packets="
                f"{stream.get('nb_read_packets')}"
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

def analyze_video_timestamps(
    label,
    path,
    max_logged_anomalies=10,
    ):
    """
    Inspect actual video packet PTS/DTS values.

    This is diagnostic only.

    It does NOT modify the file and does NOT change the
    conversion pipeline.

    We specifically look for:

        - backward PTS
        - duplicate PTS
        - backward DTS
        - duplicate DTS
        - unusually large PTS gaps

    This allows us to determine whether the timing problem
    already exists in the MediaRecorder WebM or is introduced
    during WebM -> MP4 conversion.
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
        "packet=pts,pts_time,dts,dts_time,duration_time,flags",
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
            timeout=180,
        )

        if result.returncode != 0:
            log(
                f"{label} timestamp analysis failed: "
                + result.stderr.strip()
            )
            return None

        data = json.loads(result.stdout)

        packets = data.get("packets", [])

        if not packets:
            log(
                f"{label} timestamp analysis: "
                "no video packets found"
            )
            return None

        previous_pts = None
        previous_dts = None

        backward_pts = 0
        duplicate_pts = 0
        backward_dts = 0
        duplicate_dts = 0

        largest_pts_gap = 0.0
        largest_dts_gap = 0.0

        largest_pts_gap_index = None
        largest_pts_gap_previous = None
        largest_pts_gap_current = None

        largest_dts_gap_index = None
        largest_dts_gap_previous = None
        largest_dts_gap_current = None

        pts_gap_count_100ms = 0
        pts_gap_count_250ms = 0
        pts_gap_count_500ms = 0
        pts_gap_count_1s = 0
        pts_intervals = []

        first_pts = None
        last_pts = None

        first_dts = None
        last_dts = None

        pts_anomalies = []
        dts_anomalies = []

        for index, packet in enumerate(packets):
            pts_time_raw = packet.get("pts_time")
            dts_time_raw = packet.get("dts_time")

            try:
                pts_time = (
                    float(pts_time_raw)
                    if pts_time_raw is not None
                    else None
                )
            except Exception:
                pts_time = None

            try:
                dts_time = (
                    float(dts_time_raw)
                    if dts_time_raw is not None
                    else None
                )
            except Exception:
                dts_time = None

            if pts_time is not None:
                if first_pts is None:
                    first_pts = pts_time

                if previous_pts is not None:
                    delta = (
                        pts_time
                        - previous_pts
                    )

                    if delta < 0:
                        backward_pts += 1

                        if (
                            len(pts_anomalies)
                            < max_logged_anomalies
                        ):
                            pts_anomalies.append(
                                (
                                    index,
                                    previous_pts,
                                    pts_time,
                                    delta,
                                )
                            )

                    elif delta == 0:
                        duplicate_pts += 1

                        if (
                            len(pts_anomalies)
                            < max_logged_anomalies
                        ):
                            pts_anomalies.append(
                                (
                                    index,
                                    previous_pts,
                                    pts_time,
                                    delta,
                                )
                            )

                    if delta > 0:
                        pts_intervals.append(delta)
                    if delta >= 0.100:
                        pts_gap_count_100ms += 1
                    if delta >= 0.250:
                        pts_gap_count_250ms += 1
                    if delta >= 0.500:
                        pts_gap_count_500ms += 1
                    if delta >= 1.000:
                        pts_gap_count_1s += 1

                    if delta > largest_pts_gap:
                        largest_pts_gap = delta
                        largest_pts_gap_index = index
                        largest_pts_gap_previous = previous_pts
                        largest_pts_gap_current = pts_time

                previous_pts = pts_time
                last_pts = pts_time

            if dts_time is not None:
                if first_dts is None:
                    first_dts = dts_time

                if previous_dts is not None:
                    delta = (
                        dts_time
                        - previous_dts
                    )

                    if delta < 0:
                        backward_dts += 1

                        if (
                            len(dts_anomalies)
                            < max_logged_anomalies
                        ):
                            dts_anomalies.append(
                                (
                                    index,
                                    previous_dts,
                                    dts_time,
                                    delta,
                                )
                            )

                    elif delta == 0:
                        duplicate_dts += 1

                        if (
                            len(dts_anomalies)
                            < max_logged_anomalies
                        ):
                            dts_anomalies.append(
                                (
                                    index,
                                    previous_dts,
                                    dts_time,
                                    delta,
                                )
                            )

                    if delta > largest_dts_gap:
                        largest_dts_gap = delta
                        largest_dts_gap_index = index
                        largest_dts_gap_previous = previous_dts
                        largest_dts_gap_current = dts_time

                previous_dts = dts_time
                last_dts = dts_time

        log(
            f"{label} timestamp summary: "
            f"packets={len(packets)} "
            f"first_pts={first_pts} "
            f"last_pts={last_pts} "
            f"pts_span="
            f"{(last_pts - first_pts) if first_pts is not None and last_pts is not None else None} "
            f"backward_pts={backward_pts} "
            f"duplicate_pts={duplicate_pts} "
            f"largest_pts_gap={largest_pts_gap:.6f}s "
            f"first_dts={first_dts} "
            f"last_dts={last_dts} "
            f"backward_dts={backward_dts} "
            f"duplicate_dts={duplicate_dts} "
            f"largest_dts_gap={largest_dts_gap:.6f}s"
        )

        if pts_intervals:
            avg_interval = sum(pts_intervals) / len(pts_intervals)
            estimated_fps = (1.0 / avg_interval) if avg_interval > 0 else 0.0
            log(
                f"{label} cadence diagnostics: "
                f"avg_pts_interval={avg_interval:.6f}s "
                f"estimated_avg_fps={estimated_fps:.3f} "
                f"gaps>=100ms={pts_gap_count_100ms} "
                f"gaps>=250ms={pts_gap_count_250ms} "
                f"gaps>=500ms={pts_gap_count_500ms} "
                f"gaps>=1s={pts_gap_count_1s}"
            )

        if largest_pts_gap_index is not None:
            log(
                f"{label} largest PTS gap location: "
                f"packet={largest_pts_gap_index} "
                f"previous_pts={largest_pts_gap_previous:.6f} "
                f"current_pts={largest_pts_gap_current:.6f} "
                f"gap={largest_pts_gap:.6f}s"
            )

        if largest_dts_gap_index is not None:
            log(
                f"{label} largest DTS gap location: "
                f"packet={largest_dts_gap_index} "
                f"previous_dts={largest_dts_gap_previous:.6f} "
                f"current_dts={largest_dts_gap_current:.6f} "
                f"gap={largest_dts_gap:.6f}s"
            )

        if pts_anomalies:
            log(
                f"{label} PTS anomalies:"
            )

            for (
                index,
                previous,
                current,
                delta,
            ) in pts_anomalies:
                log(
                    f"  packet={index} "
                    f"previous_pts={previous:.6f} "
                    f"current_pts={current:.6f} "
                    f"delta={delta:.6f}"
                )

        if dts_anomalies:
            log(
                f"{label} DTS anomalies:"
            )

            for (
                index,
                previous,
                current,
                delta,
            ) in dts_anomalies:
                log(
                    f"  packet={index} "
                    f"previous_dts={previous:.6f} "
                    f"current_dts={current:.6f} "
                    f"delta={delta:.6f}"
                )

        return {
            "packets": len(packets),
            "first_pts": first_pts,
            "last_pts": last_pts,
            "pts_span": (
                last_pts - first_pts
                if first_pts is not None
                and last_pts is not None
                else None
            ),
            "backward_pts": backward_pts,
            "duplicate_pts": duplicate_pts,
            "largest_pts_gap": largest_pts_gap,
            "largest_pts_gap_index": largest_pts_gap_index,
            "largest_pts_gap_previous": largest_pts_gap_previous,
            "largest_pts_gap_current": largest_pts_gap_current,
            "pts_gap_count_100ms": pts_gap_count_100ms,
            "pts_gap_count_250ms": pts_gap_count_250ms,
            "pts_gap_count_500ms": pts_gap_count_500ms,
            "pts_gap_count_1s": pts_gap_count_1s,
            "avg_pts_interval": (
                sum(pts_intervals) / len(pts_intervals)
                if pts_intervals else None
            ),
            "estimated_avg_fps": (
                1.0 / (sum(pts_intervals) / len(pts_intervals))
                if pts_intervals and sum(pts_intervals) > 0
                else None
            ),
            "first_dts": first_dts,
            "last_dts": last_dts,
            "backward_dts": backward_dts,
            "duplicate_dts": duplicate_dts,
            "largest_dts_gap": largest_dts_gap,
        }

    except Exception as e:
        log(
            f"{label} timestamp analysis exception: "
            f"{e}"
        )
        return None

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

    The source WebM has unreliable container frame-rate metadata
    (ffprobe reports 1000 fps even though the live track is about 30 fps).
    Convert it to a stable 30 fps CFR timeline so playback does not inherit
    large VFR timestamp gaps. The 30 fps value is based on the live track
    reported by MediaStream, not the bogus WebM r_frame_rate metadata.

    Audio timestamps are normalized separately because the
    previous recordings showed actual backward audio timestamps.

    This function deliberately does NOT use the WebM-reported 1000 fps
    as the output frame rate. It uses a fixed 30 fps CFR output instead.
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
        "WEBM VIDEO TIMESTAMP ANALYSIS"
    )

    webm_timestamp_info = (
        analyze_video_timestamps(
            "WEBM BEFORE CONVERSION",
            webm_path,
        )
    )

    # --------------------------------------------------------
    # THREE-STAGE FRAME-RATE COMPARISON
    # --------------------------------------------------------
    # Source/rendered values come from live browser diagnostics.
    # The WebM value is measured from the actual recorded packet
    # timestamps, so it is the best available measurement of the
    # encoded/recorded frame cadence.
    # --------------------------------------------------------
    # Use the frozen values captured immediately after the final recorder
    # status. This prevents a later diagnostic status read from resetting
    # the window counters and producing misleading 0.000 FPS values.
    source_overall_fps = final_source_fps
    rendered_overall_fps = final_rendered_fps

    recorded_webm_fps = None
    if webm_timestamp_info:
        try:
            recorded_webm_fps = float(
                webm_timestamp_info.get(
                    "estimated_avg_fps"
                )
                or 0
            )
        except Exception:
            recorded_webm_fps = 0.0

    log_section(
        "THREE-STAGE FRAME RATE COMPARISON"
    )

    log(
        f"Source/WebRTC delivered FPS (overall): "
        f"{source_overall_fps:.3f}"
    )

    log(
        f"Rendered <video> FPS (overall): "
        f"{rendered_overall_fps:.3f}"
    )

    if recorded_webm_fps is not None:
        log(
            f"Recorded WebM packet FPS (timestamp-based): "
            f"{recorded_webm_fps:.3f}"
        )

        log(
            "Frame-rate gaps: "
            f"source->rendered="
            f"{(source_overall_fps - rendered_overall_fps):.3f} FPS, "
            f"rendered->recorded="
            f"{(rendered_overall_fps - recorded_webm_fps):.3f} FPS, "
            f"source->recorded="
            f"{(source_overall_fps - recorded_webm_fps):.3f} FPS"
        )
    else:
        log(
            "Recorded WebM packet FPS could not be calculated."
        )

    log(
        "Interpretation: source≈rendered means WebRTC/Chromium "
        "delivery is stable; rendered≫recorded points toward "
        "MediaRecorder/encoding loss; source≫rendered points "
        "toward Chromium rendering/decoding loss."
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
        "color=black,"
        "fps=30",

        "-fps_mode:v",
        "cfr",

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

        "-shortest",

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

    log_section(
        "MP4 VIDEO TIMESTAMP ANALYSIS"
    )

    mp4_timestamp_info = (
        analyze_video_timestamps(
            "MP4 AFTER CONVERSION",
            mp4_path,
        )
    )

    # --------------------------------------------------------
    # SOURCE / OUTPUT TIMESTAMP COMPARISON
    # --------------------------------------------------------

    if (
        webm_timestamp_info
        and mp4_timestamp_info
    ):
        source_span = (
            webm_timestamp_info.get(
                "pts_span"
            )
        )

        output_span = (
            mp4_timestamp_info.get(
                "pts_span"
            )
        )

        if (
            source_span is not None
            and output_span is not None
        ):
            log(
                f"Video PTS span comparison: "
                f"WebM={source_span:.6f}s "
                f"MP4={output_span:.6f}s "
                f"difference="
                f"{abs(output_span - source_span):.6f}s"
            )

        log(
            "Timestamp anomaly comparison: "
            f"WebM backward_pts="
            f"{webm_timestamp_info.get('backward_pts')} "
            f"duplicate_pts="
            f"{webm_timestamp_info.get('duplicate_pts')} "
            f"| MP4 backward_pts="
            f"{mp4_timestamp_info.get('backward_pts')} "
            f"duplicate_pts="
            f"{mp4_timestamp_info.get('duplicate_pts')}"
        )

        log(
            "DTS anomaly comparison: "
            f"WebM backward_dts="
            f"{webm_timestamp_info.get('backward_dts')} "
            f"duplicate_dts="
            f"{webm_timestamp_info.get('duplicate_dts')} "
            f"| MP4 backward_dts="
            f"{mp4_timestamp_info.get('backward_dts')} "
            f"duplicate_dts="
            f"{mp4_timestamp_info.get('duplicate_dts')}"
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

// ------------------------------------------------------------
// FRAME-CADENCE DIAGNOSTICS
// ------------------------------------------------------------
// These counters deliberately measure three different stages:
//
//   1) sourceVideoFrameCount  = frames delivered by the selected
//      WebRTC MediaStreamTrack (using a cloned track)
//   2) renderedVideoFrameCount = frames actually presented by the
//      page's <video> element (requestVideoFrameCallback)
//   3) recorded WebM FPS       = measured later from real WebM
//      packet timestamps after MediaRecorder stops
//
// This lets us distinguish source/WebRTC delivery, Chromium
// rendering, and MediaRecorder/encoding losses without changing
// the recording stream itself.
// ------------------------------------------------------------
window.__superliveSourceVideoFrameCount = 0;
window.__superliveSourceVideoFrameCountAtLastStatus = 0;
window.__superliveSourceVideoFrameLastStatusAt = performance.now();
window.__superliveSourceVideoFrameCallbackActive = false;
window.__superliveSourceVideoFrameReader = null;
window.__superliveSourceVideoFrameClone = null;

window.__superliveRenderedVideoFrameCount = 0;
window.__superliveRenderedVideoFrameCountAtLastStatus = 0;
window.__superliveRenderedVideoFrameLastStatusAt = performance.now();
window.__superliveRenderedVideoFrameCallbackActive = false;
window.__superliveDiagnosticsStartedAt = performance.now();

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

    // Initialize recording-state fields even when MediaRecorder is not used.
    window.__superliveRecorder = null;
    window.__superliveChunkCount = 0;
    window.__superliveUploadedChunkCount = 0;
    window.__superliveLastChunkAt = performance.now();
    window.__superliveLastChunkSize = 0;
    window.__superliveUploadQueue = [];
    window.__superliveIsUploading = false;
    window.__superliveUploadError = null;
    window.__superlivePendingDataTasks = 0;
    window.__superliveRecorderStopFired = false;
    window.__superliveFinalDataReady = false;

    window.__superliveVideoElement = null;

    for (const video of videos) {
        try {
            const stream = video.srcObject;
            if (stream &&
                stream.getVideoTracks().includes(selectedVideoTrack)) {
                window.__superliveVideoElement = video;
                break;
            }
        } catch (e) {
            console.warn(
                "superlive video element selection error",
                e
            );
        }
    }

    // --------------------------------------------------------
    // SOURCE FRAME COUNTER
    // --------------------------------------------------------
    // Count actual frames delivered by the selected WebRTC track.
    // A clone is used so this diagnostic reader never consumes the
    // frames from the track used by MediaRecorder.
    window.__superliveSourceVideoFrameCount = 0;
    window.__superliveSourceVideoFrameCountAtLastStatus = 0;
    window.__superliveSourceVideoFrameLastStatusAt = performance.now();
    window.__superliveSourceVideoFrameCallbackActive = false;
    window.__superliveSourceVideoFrameReader = null;
    window.__superliveSourceVideoFrameClone = null;

    if (
        typeof window.MediaStreamTrackProcessor === "function" &&
        selectedVideoTrack &&
        typeof selectedVideoTrack.clone === "function"
    ) {
        try {
            const diagnosticClone = selectedVideoTrack.clone();
            const processor = new MediaStreamTrackProcessor({
                track: diagnosticClone
            });
            const reader = processor.readable.getReader();

            window.__superliveSourceVideoFrameClone = diagnosticClone;
            window.__superliveSourceVideoFrameReader = reader;
            window.__superliveSourceVideoFrameCallbackActive = true;

            (async () => {
                try {
                    while (true) {
                        const result = await reader.read();

                        if (result.done) {
                            break;
                        }

                        window.__superliveSourceVideoFrameCount += 1;

                        try {
                            result.value.close();
                        } catch (e) {}
                    }
                } catch (e) {
                    console.warn(
                        "superlive source frame counter stopped",
                        e
                    );
                } finally {
                    window.__superliveSourceVideoFrameCallbackActive = false;
                }
            })();
        } catch (e) {
            console.warn(
                "superlive source frame counter unavailable",
                e
            );
            window.__superliveSourceVideoFrameCallbackActive = false;
        }
    } else {
        console.warn(
            "superlive source frame counter unavailable: " +
            "MediaStreamTrackProcessor is not supported"
        );
    }

    // --------------------------------------------------------
    // RENDERED FRAME COUNTER
    // --------------------------------------------------------
    window.__superliveRenderedVideoFrameCount = 0;
    window.__superliveRenderedVideoFrameCountAtLastStatus = 0;
    window.__superliveRenderedVideoFrameLastStatusAt = performance.now();
    window.__superliveRenderedVideoFrameCallbackActive = false;
    window.__superliveDiagnosticsStartedAt = performance.now();

    // --------------------------------------------------------
    // EVENT-LOOP / MAIN-THREAD DIAGNOSTICS
    // --------------------------------------------------------
    // This does not attempt to measure CPU percentage. Instead it measures
    // how much the page's main-thread timer is delayed, which can expose
    // browser-side contention without changing the media pipeline.
    window.__superliveEventLoopSamples = 0;
    window.__superliveEventLoopDelayTotalMs = 0;
    window.__superliveEventLoopMaxDelayMs = 0;
    window.__superliveLongTaskCount = 0;
    window.__superliveLongTaskTotalMs = 0;
    window.__superliveEventLoopTimer = null;

    try {
        const longTaskObserver =
            new PerformanceObserver((list) => {
                for (const entry of list.getEntries()) {
                    window.__superliveLongTaskCount += 1;
                    window.__superliveLongTaskTotalMs += entry.duration || 0;
                }
            });
        longTaskObserver.observe({type: "longtask", buffered: true});
        window.__superliveLongTaskObserver = longTaskObserver;
    } catch (e) {
        window.__superliveLongTaskObserver = null;
    }

    const eventLoopTickExpected = performance.now() + 1000;
    let nextExpected = eventLoopTickExpected;
    window.__superliveEventLoopTimer = setInterval(() => {
        const now = performance.now();
        const delay = Math.max(0, now - nextExpected);
        window.__superliveEventLoopSamples += 1;
        window.__superliveEventLoopDelayTotalMs += delay;
        if (delay > window.__superliveEventLoopMaxDelayMs) {
            window.__superliveEventLoopMaxDelayMs = delay;
        }
        nextExpected += 1000;
        if (now - nextExpected > 5000) {
            nextExpected = now + 1000;
        }
    }, 1000);

    const videoElement = window.__superliveVideoElement;

    if (videoElement &&
        typeof videoElement.requestVideoFrameCallback === "function") {
        window.__superliveRenderedVideoFrameCallbackActive = true;

        const onVideoFrame = () => {
            window.__superliveRenderedVideoFrameCount += 1;

            if (window.__superliveVideoElement === videoElement &&
                videoElement.readyState > 0) {
                try {
                    videoElement.requestVideoFrameCallback(onVideoFrame);
                } catch (e) {
                    window.__superliveRenderedVideoFrameCallbackActive = false;
                }
            }
        };

        try {
            videoElement.requestVideoFrameCallback(onVideoFrame);
        } catch (e) {
            window.__superliveRenderedVideoFrameCallbackActive = false;
        }
    }

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

window.__superliveGetDiagnostics = () => {
    const now = performance.now();
    const start = window.__superliveDiagnosticsStartedAt || now;
    const elapsed = Math.max(0, (now - start) / 1000);

    const sourceCount = window.__superliveSourceVideoFrameCount || 0;
    const renderedCount = window.__superliveRenderedVideoFrameCount || 0;

    const sourcePreviousTime =
        window.__superliveSourceVideoFrameLastStatusAt || now;
    const renderedPreviousTime =
        window.__superliveRenderedVideoFrameLastStatusAt || now;

    const sourcePreviousCount =
        window.__superliveSourceVideoFrameCountAtLastStatus || 0;
    const renderedPreviousCount =
        window.__superliveRenderedVideoFrameCountAtLastStatus || 0;

    const sourceElapsed = Math.max(0, (now - sourcePreviousTime) / 1000);
    const renderedElapsed = Math.max(0, (now - renderedPreviousTime) / 1000);

    const sourceFps = sourceElapsed > 0
        ? (sourceCount - sourcePreviousCount) / sourceElapsed
        : 0;
    const renderedFps = renderedElapsed > 0
        ? (renderedCount - renderedPreviousCount) / renderedElapsed
        : 0;

    window.__superliveSourceVideoFrameLastStatusAt = now;
    window.__superliveSourceVideoFrameCountAtLastStatus = sourceCount;
    window.__superliveRenderedVideoFrameLastStatusAt = now;
    window.__superliveRenderedVideoFrameCountAtLastStatus = renderedCount;

    let playbackQuality = null;
    const video = window.__superliveVideoElement;
    if (video) {
        try {
            const q = video.getVideoPlaybackQuality
                ? video.getVideoPlaybackQuality()
                : null;
            playbackQuality = {
                totalVideoFrames: q && q.totalVideoFrames != null
                    ? q.totalVideoFrames : null,
                droppedVideoFrames: q && q.droppedVideoFrames != null
                    ? q.droppedVideoFrames : null,
                corruptedVideoFrames: q && q.corruptedVideoFrames != null
                    ? q.corruptedVideoFrames : null
            };
        } catch (e) {}
    }

    const eventSamples = window.__superliveEventLoopSamples || 0;
    const eventTotal = window.__superliveEventLoopDelayTotalMs || 0;

    return {
        elapsedSeconds: elapsed,
        sourceVideoFrameCount: sourceCount,
        renderedVideoFrameCount: renderedCount,
        sourceFpsSinceStatus: sourceFps,
        renderedFpsSinceStatus: renderedFps,
        sourceFpsOverall: elapsed > 0 ? sourceCount / elapsed : 0,
        renderedFpsOverall: elapsed > 0 ? renderedCount / elapsed : 0,
        sourceCallbackActive: !!window.__superliveSourceVideoFrameCallbackActive,
        renderedCallbackActive: !!window.__superliveRenderedVideoFrameCallbackActive,
        videoReadyState: video ? video.readyState : null,
        videoPaused: video ? video.paused : null,
        videoEnded: video ? video.ended : null,
        videoWidth: video ? video.videoWidth : 0,
        videoHeight: video ? video.videoHeight : 0,
        videoCurrentTime: video ? video.currentTime : null,
        playbackQuality: playbackQuality,
        eventLoopSamples: eventSamples,
        eventLoopAvgDelayMs: eventSamples > 0 ? eventTotal / eventSamples : 0,
        eventLoopMaxDelayMs: window.__superliveEventLoopMaxDelayMs || 0,
        longTaskCount: window.__superliveLongTaskCount || 0,
        longTaskTotalMs: window.__superliveLongTaskTotalMs || 0
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

                    window.__superliveUploadQueue
                        .unshift(item);

                    break;
                }
            }

        } finally {
            window.__superliveIsUploading =
                false;

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

                renderedVideoFrameCount:
                    window
                        .__superliveRenderedVideoFrameCount
                    || 0,

                renderedVideoFrameCallbackActive:
                    !!window
                        .__superliveRenderedVideoFrameCallbackActive,

                renderedVideoFpsSinceStatus:
                    (() => {
                        const now = performance.now();
                        const previousTime =
                            window
                                .__superliveRenderedVideoFrameLastStatusAt
                            || now;
                        const elapsed =
                            (now - previousTime) / 1000;
                        const currentCount =
                            window
                                .__superliveRenderedVideoFrameCount
                            || 0;
                        const previousCount =
                            window
                                .__superliveRenderedVideoFrameCountAtLastStatus
                            || 0;

                        window
                            .__superliveRenderedVideoFrameLastStatusAt = now;
                        window
                            .__superliveRenderedVideoFrameCountAtLastStatus =
                            currentCount;

                        return elapsed > 0
                            ? (currentCount - previousCount) / elapsed
                            : 0;
                    })(),

                sourceVideoFrameCount:
                    window
                        .__superliveSourceVideoFrameCount
                    || 0,

                sourceVideoFrameCallbackActive:
                    !!window
                        .__superliveSourceVideoFrameCallbackActive,

                sourceVideoFpsSinceStatus:
                    (() => {
                        const now = performance.now();
                        const previousTime =
                            window
                                .__superliveSourceVideoFrameLastStatusAt
                            || now;
                        const elapsed =
                            (now - previousTime) / 1000;
                        const currentCount =
                            window
                                .__superliveSourceVideoFrameCount
                            || 0;
                        const previousCount =
                            window
                                .__superliveSourceVideoFrameCountAtLastStatus
                            || 0;

                        window
                            .__superliveSourceVideoFrameLastStatusAt = now;
                        window
                            .__superliveSourceVideoFrameCountAtLastStatus =
                            currentCount;

                        return elapsed > 0
                            ? (currentCount - previousCount) / elapsed
                            : 0;
                    })(),

                diagnosticsElapsedSeconds:
                    Math.max(
                        0,
                        (
                            performance.now()
                            - (
                                window.__superliveDiagnosticsStartedAt
                                || performance.now()
                            )
                        ) / 1000
                    ),

                renderedVideoFpsOverall:
                    (() => {
                        const elapsed =
                            (
                                performance.now()
                                - (
                                    window.__superliveDiagnosticsStartedAt
                                    || performance.now()
                                )
                            ) / 1000;
                        const count =
                            window.__superliveRenderedVideoFrameCount
                            || 0;
                        return elapsed > 0
                            ? count / elapsed
                            : 0;
                    })(),

                sourceVideoFpsOverall:
                    (() => {
                        const elapsed =
                            (
                                performance.now()
                                - (
                                    window.__superliveDiagnosticsStartedAt
                                    || performance.now()
                                )
                            ) / 1000;
                        const count =
                            window.__superliveSourceVideoFrameCount
                            || 0;
                        return elapsed > 0
                            ? count / elapsed
                            : 0;
                    })(),

                eventLoopSamples:
                    window.__superliveEventLoopSamples || 0,

                eventLoopAvgDelayMs:
                    (() => {
                        const samples =
                            window.__superliveEventLoopSamples || 0;
                        const total =
                            window.__superliveEventLoopDelayTotalMs || 0;
                        return samples > 0
                            ? total / samples
                            : 0;
                    })(),

                eventLoopMaxDelayMs:
                    window.__superliveEventLoopMaxDelayMs || 0,

                longTaskCount:
                    window.__superliveLongTaskCount || 0,

                longTaskTotalMs:
                    window.__superliveLongTaskTotalMs || 0,

                playbackQuality:
                    (() => {
                        const video =
                            window.__superliveVideoElement;
                        if (!video) {
                            return null;
                        }
                        try {
                            const q =
                                video.getVideoPlaybackQuality
                                ? video.getVideoPlaybackQuality()
                                : null;
                            return {
                                totalVideoFrames:
                                    q && q.totalVideoFrames != null
                                    ? q.totalVideoFrames
                                    : null,
                                droppedVideoFrames:
                                    q && q.droppedVideoFrames != null
                                    ? q.droppedVideoFrames
                                    : null,
                                corruptedVideoFrames:
                                    q && q.corruptedVideoFrames != null
                                    ? q.corruptedVideoFrames
                                    : null
                            };
                        } catch (e) {
                            return null;
                        }
                    })(),

                finalDataReady:
                    !!window
                        .__superliveFinalDataReady
            };
        };

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
    if AB_TEST_MODE not in ("record", "source_only"):
        raise RuntimeError(
            "AB_TEST_MODE must be either 'record' or 'source_only'"
        )

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

        if AB_TEST_MODE == "source_only":
            log_section(
                "A/B TEST A - SOURCE + RENDER ONLY (NO MEDIARECORDER)"
            )
            log(
                f"MediaRecorder is DISABLED. Diagnostics will run for "
                f"{DIAGNOSTIC_SECONDS}s or until the video ends."
            )
            log(
                "IMPORTANT: no WebM, MP4, FFmpeg, Telegram upload, "
                "or recording chunk route is used in this mode."
            )

            diagnostic_started = time.monotonic()
            last_diag_log = diagnostic_started

            while True:
                elapsed = time.monotonic() - diagnostic_started

                if elapsed >= DIAGNOSTIC_SECONDS:
                    log(
                        "A/B source-only diagnostic duration reached."
                    )
                    break

                try:
                    diag = await page.evaluate(
                        """
                        () => window.__superliveGetDiagnostics()
                        """
                    )

                    video_state = diag.get("videoReadyState")

                    if video_state == 4 or diag.get("videoEnded"):
                        log(
                            "Video playback ended during source-only test."
                        )
                        break

                    if (
                        time.monotonic() - last_diag_log >= 10
                    ):
                        quality = diag.get("playbackQuality") or {}
                        log(
                            "A/B SOURCE-ONLY status: "
                            f"elapsed={diag.get('elapsedSeconds', 0):.1f}s "
                            f"source_fps={diag.get('sourceFpsSinceStatus', 0):.2f} "
                            f"rendered_fps={diag.get('renderedFpsSinceStatus', 0):.2f} "
                            f"source_overall={diag.get('sourceFpsOverall', 0):.2f} "
                            f"rendered_overall={diag.get('renderedFpsOverall', 0):.2f} "
                            f"source_frames={diag.get('sourceVideoFrameCount', 0)} "
                            f"rendered_frames={diag.get('renderedVideoFrameCount', 0)} "
                            f"dropped={quality.get('droppedVideoFrames')} "
                            f"eventloop_avg_delay_ms={diag.get('eventLoopAvgDelayMs', 0):.2f} "
                            f"eventloop_max_delay_ms={diag.get('eventLoopMaxDelayMs', 0):.2f} "
                            f"longtasks={diag.get('longTaskCount', 0)}"
                        )
                        last_diag_log = time.monotonic()

                except Exception as e:
                    log(
                        f"A/B source-only diagnostic error: {e}"
                    )

                await asyncio.sleep(1)

            final_diag = await page.evaluate(
                """
                () => window.__superliveGetDiagnostics()
                """
            )

            log_section(
                "A/B TEST A - FINAL SOURCE/RENDER DIAGNOSTICS"
            )
            log(
                "FINAL A/B A: "
                + json.dumps(
                    final_diag,
                    ensure_ascii=False,
                )
            )
            log(
                "A/B conclusion helper: compare this run against the "
                "AB_TEST_MODE=record run. If source/render remain near "
                "30 FPS here but recording mode shows a much lower WebM "
                "cadence, MediaRecorder/encoding is implicated. If the "
                "same source/render FPS dips occur here, the issue exists "
                "before MediaRecorder."
            )

            try:
                await page.unroute(
                    "**/__slr_chunk",
                    handle_chunk,
                )
            except Exception:
                pass

            try:
                if webm_file is not None:
                    webm_file.close()
                    webm_file = None
            except Exception:
                pass

            try:
                if webm_path.exists():
                    webm_path.unlink()
            except Exception:
                pass

            try:
                await browser.close()
            except Exception:
                pass

            return True

        log_section(
            "A/B TEST B - SOURCE + RENDER + MEDIARECORDER"
        )
        log(
            "MediaRecorder is ENABLED. This is the recording-side run "
            "for comparison with AB_TEST_MODE=source_only."
        )

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

                    rendered_fps = status.get(
                        "renderedVideoFpsSinceStatus"
                    ) or 0.0

                    rendered_count = status.get(
                        "renderedVideoFrameCount"
                    ) or 0

                    source_fps = status.get(
                        "sourceVideoFpsSinceStatus"
                    ) or 0.0

                    source_count = status.get(
                        "sourceVideoFrameCount"
                    ) or 0

                    rendered_overall = status.get(
                        "renderedVideoFpsOverall"
                    ) or 0.0

                    source_overall = status.get(
                        "sourceVideoFpsOverall"
                    ) or 0.0

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
                        f"{settings} "
                        f"source_fps={source_fps:.2f} "
                        f"source_frames={source_count} "
                        f"rendered_fps={rendered_fps:.2f} "
                        f"rendered_frames={rendered_count} "
                        f"source_overall_fps={source_overall:.2f} "
                        f"rendered_overall_fps={rendered_overall:.2f}"
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

        # Freeze the final browser diagnostics now. Do not call the status
        # endpoint again before the A/B comparison, because the windowed
        # counters are intentionally updated by status reads.
        final_source_fps = float(
            final_status.get("sourceVideoFpsOverall", 0) or 0
        )
        final_rendered_fps = float(
            final_status.get("renderedVideoFpsOverall", 0) or 0
        )

        log(
            "FINAL A/B B diagnostics snapshot: "
            f"source_overall={final_source_fps:.3f} FPS "
            f"rendered_overall={final_rendered_fps:.3f} FPS "
            f"source_frames={final_status.get('sourceVideoFrameCount', 0)} "
            f"rendered_frames={final_status.get('renderedVideoFrameCount', 0)} "
            f"eventloop_avg_delay_ms={float(final_status.get('eventLoopAvgDelayMs', 0) or 0):.2f} "
            f"eventloop_max_delay_ms={float(final_status.get('eventLoopMaxDelayMs', 0) or 0):.2f} "
            f"longtasks={final_status.get('longTaskCount', 0)} "
            f"playback_quality={json.dumps(final_status.get('playbackQuality'), ensure_ascii=False)}"
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

        webm_file.flush()

        os.fsync(
            webm_file.fileno()
        )

        webm_file.close()
        webm_file = None

        await browser.close()

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

        convert_webm_to_mp4(
            webm_path,
            mp4_path,
        )

        if KEEP_SOURCE_WEBM:
            log(
                f"KEEP_SOURCE_WEBM is enabled; source WebM retained: "
                f"{webm_path}"
            )
        else:
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

        parts = split_mp4_if_needed(
            mp4_path
        )

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
    "SUPERLIVE RECORDER / A-B DIAGNOSTIC "
    f"(MODE={AB_TEST_MODE})"
    )

    if AB_TEST_MODE == "source_only":
        log("A/B TEST A selected: NO MediaRecorder / NO encoding / NO upload")
    else:
        log("A/B TEST B selected: normal MediaRecorder recording pipeline")

    log(
        f"AB_TEST_MODE={AB_TEST_MODE}; "
        f"DIAGNOSTIC_SECONDS={DIAGNOSTIC_SECONDS}"
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
