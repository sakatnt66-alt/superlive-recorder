import asyncio
import base64
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from playwright.async_api import async_playwright


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

MAX_CANDIDATES = int(
    os.environ.get("MAX_CANDIDATES", "8")
)


# ============================================================
# WEBRTC + MEDIARECORDER HOOK
# ============================================================

WEBRTC_HOOK = r"""
(() => {
    const state = {
        pcs: [],
        streams: [],
        videoElements: [],
        audioElements: [],
        videoTracks: [],
        audioTracks: [],
        recorder: null,
        recorderQueue: [],
        recorderError: null,
        recorderStartedAt: 0,
        dataEvents: 0,
        totalBytes: 0,
        preparedStream: null,
        preparedVideoTrack: null,
        preparedAudioTrack: null
    };

    window.__superliveState = state;

    function uniquePush(arr, item) {
        if (!item) return;

        if (!arr.includes(item)) {
            arr.push(item);
        }
    }

    function rememberTrack(track) {
        if (!track) return;

        if (track.kind === "video") {
            uniquePush(state.videoTracks, track);
        } else if (track.kind === "audio") {
            uniquePush(state.audioTracks, track);
        }
    }

    function rememberStream(stream) {
        if (!stream) return;

        uniquePush(state.streams, stream);

        try {
            for (const track of stream.getTracks()) {
                rememberTrack(track);
            }
        } catch (_) {}
    }

    function rememberVideo(video) {
        if (!video) return;

        uniquePush(state.videoElements, video);

        try {
            if (video.srcObject instanceof MediaStream) {
                rememberStream(video.srcObject);
            }
        } catch (_) {}
    }

    function rememberAudio(audio) {
        if (!audio) return;

        uniquePush(state.audioElements, audio);

        try {
            if (audio.srcObject instanceof MediaStream) {
                rememberStream(audio.srcObject);
            }
        } catch (_) {}
    }


    // --------------------------------------------------------
    // RTCPeerConnection interception
    // --------------------------------------------------------

    const OriginalRTCPeerConnection = window.RTCPeerConnection;

    if (OriginalRTCPeerConnection) {
        class WrappedRTCPeerConnection extends OriginalRTCPeerConnection {
            constructor(...args) {
                super(...args);

                try {
                    uniquePush(state.pcs, this);
                } catch (_) {}

                this.addEventListener("track", (event) => {
                    try {
                        rememberTrack(event.track);

                        if (event.streams) {
                            for (const stream of event.streams) {
                                rememberStream(stream);
                            }
                        }
                    } catch (_) {}
                });

                this.addEventListener("connectionstatechange", () => {
                    try {
                        window.__superliveLastConnectionState =
                            this.connectionState;
                    } catch (_) {}
                });
            }
        }

        window.RTCPeerConnection = WrappedRTCPeerConnection;
    }


    // --------------------------------------------------------
    // DOM media monitoring
    // --------------------------------------------------------

    function scanMediaElements() {
        try {
            const videos = Array.from(
                document.querySelectorAll("video")
            );

            for (const video of videos) {
                rememberVideo(video);
            }
        } catch (_) {}

        try {
            const audios = Array.from(
                document.querySelectorAll("audio")
            );

            for (const audio of audios) {
                rememberAudio(audio);
            }
        } catch (_) {}
    }


    // --------------------------------------------------------
    // Candidate scoring
    // --------------------------------------------------------

    function trackInfo(track) {
        if (!track) {
            return null;
        }

        return {
            id: track.id || null,
            kind: track.kind || null,
            readyState: track.readyState || null,
            muted: !!track.muted,
            enabled: !!track.enabled,
        };
    }

    function scoreTrack(track) {
        if (!track) {
            return -100000;
        }

        let score = 0;

        if (track.readyState === "live") {
            score += 1000;
        }

        if (!track.muted) {
            score += 500;
        }

        if (track.enabled) {
            score += 100;
        }

        return score;
    }

    function bestTrack(tracks, kind) {
        const candidates = [];

        for (const track of tracks) {
            if (!track) continue;
            if (track.kind !== kind) continue;
            if (track.readyState !== "live") continue;

            candidates.push(track);
        }

        candidates.sort((a, b) => {
            return scoreTrack(b) - scoreTrack(a);
        });

        return candidates.length ? candidates[0] : null;
    }


    function streamCandidateInfo(stream, index) {
        let videoTracks = [];
        let audioTracks = [];

        try {
            videoTracks = stream.getVideoTracks();
        } catch (_) {}

        try {
            audioTracks = stream.getAudioTracks();
        } catch (_) {}

        const video = bestTrack(videoTracks, "video");
        const audio = bestTrack(audioTracks, "audio");

        let score = 0;

        if (video) {
            score += scoreTrack(video);
        }

        if (audio) {
            score += scoreTrack(audio);
        }

        if (video && audio) {
            score += 2000;
        }

        if (video && !video.muted) {
            score += 3000;
        }

        if (audio && !audio.muted) {
            score += 3000;
        }

        return {
            index,
            id: stream.id || null,
            score,
            hasVideo: !!video,
            hasAudio: !!audio,
            video: trackInfo(video),
            audio: trackInfo(audio),
        };
    }


    // --------------------------------------------------------
    // Public diagnostics
    // --------------------------------------------------------

    window.__superliveGetCandidates = () => {
        scanMediaElements();

        const candidates = [];

        for (let i = 0; i < state.streams.length; i++) {
            try {
                candidates.push(
                    streamCandidateInfo(state.streams[i], i)
                );
            } catch (_) {}
        }

        // Global fallback candidate information
        const globalVideo = bestTrack(
            state.videoTracks,
            "video"
        );

        const globalAudio = bestTrack(
            state.audioTracks,
            "audio"
        );

        candidates.sort((a, b) => b.score - a.score);

        return {
            streams: candidates,
            globalVideo: trackInfo(globalVideo),
            globalAudio: trackInfo(globalAudio),
        };
    };


    window.__superliveGetState = () => {
        scanMediaElements();

        const videos = state.videoElements.map((video) => {
            let hasSrcObject = false;

            try {
                hasSrcObject =
                    video.srcObject instanceof MediaStream;
            } catch (_) {}

            return {
                readyState: video.readyState,
                paused: video.paused,
                ended: video.ended,
                currentTime: video.currentTime,
                width: video.videoWidth,
                height: video.videoHeight,
                muted: video.muted,
                volume: video.volume,
                hasSrcObject,
                captureStreamAvailable:
                    typeof video.captureStream === "function",
            };
        });

        const tracks = [];

        for (const track of state.videoTracks) {
            tracks.push({
                kind: "video",
                id: track.id,
                readyState: track.readyState,
                muted: track.muted,
                enabled: track.enabled,
            });
        }

        for (const track of state.audioTracks) {
            tracks.push({
                kind: "audio",
                id: track.id,
                readyState: track.readyState,
                muted: track.muted,
                enabled: track.enabled,
            });
        }

        return {
            videos,
            tracks,
            streamCount: state.streams.length,
            peerConnectionCount: state.pcs.length,
            recorder: {
                exists: !!state.recorder,
                state: state.recorder
                    ? state.recorder.state
                    : null,
                dataEvents: state.dataEvents,
                queueLength: state.recorderQueue.length,
                totalBytes: state.totalBytes,
                error: state.recorderError,
            },
        };
    };


    // --------------------------------------------------------
    // Force media playback where possible
    // --------------------------------------------------------

    window.__superliveForcePlay = async () => {
        scanMediaElements();

        let played = 0;

        for (const video of state.videoElements) {
            try {
                video.muted = false;
            } catch (_) {}

            try {
                await video.play();
                played++;
            } catch (_) {}
        }

        for (const audio of state.audioElements) {
            try {
                audio.muted = false;
            } catch (_) {}

            try {
                await audio.play();
                played++;
            } catch (_) {}
        }

        return {
            played
        };
    };


    // --------------------------------------------------------
    // Prepare a specific MediaStream
    // --------------------------------------------------------

    window.__superlivePrepareRecordingStream = (streamIndex) => {
        scanMediaElements();

        state.preparedStream = null;
        state.preparedVideoTrack = null;
        state.preparedAudioTrack = null;

        // First choice: requested stream
        if (
            Number.isInteger(streamIndex) &&
            streamIndex >= 0 &&
            streamIndex < state.streams.length
        ) {
            const stream = state.streams[streamIndex];

            let videoTracks = [];
            let audioTracks = [];

            try {
                videoTracks = stream.getVideoTracks();
            } catch (_) {}

            try {
                audioTracks = stream.getAudioTracks();
            } catch (_) {}

            const video = bestTrack(videoTracks, "video");
            const audio = bestTrack(audioTracks, "audio");

            if (video) {
                state.preparedStream = stream;
                state.preparedVideoTrack = video;
                state.preparedAudioTrack = audio || null;

                return {
                    ok: true,
                    source: "stream",
                    streamId: stream.id || null,
                    hasVideo: !!video,
                    hasAudio: !!audio,
                    videoTrackId: video.id,
                    videoMuted: !!video.muted,
                    audioTrackId: audio ? audio.id : null,
                    audioMuted: audio ? !!audio.muted : null,
                };
            }
        }


        // Second choice: global best video + global best audio
        const globalVideo = bestTrack(
            state.videoTracks,
            "video"
        );

        const globalAudio = bestTrack(
            state.audioTracks,
            "audio"
        );

        if (globalVideo) {
            const stream = new MediaStream();

            try {
                stream.addTrack(globalVideo);
            } catch (_) {}

            if (globalAudio) {
                try {
                    stream.addTrack(globalAudio);
                } catch (_) {}
            }

            state.preparedStream = stream;
            state.preparedVideoTrack = globalVideo;
            state.preparedAudioTrack = globalAudio || null;

            return {
                ok: true,
                source: "global",
                streamId: stream.id,
                hasVideo: true,
                hasAudio: !!globalAudio,
                videoTrackId: globalVideo.id,
                videoMuted: !!globalVideo.muted,
                audioTrackId: globalAudio
                    ? globalAudio.id
                    : null,
                audioMuted: globalAudio
                    ? !!globalAudio.muted
                    : null,
            };
        }


        // Third choice: capture from a video element
        for (const video of state.videoElements) {
            try {
                if (
                    typeof video.captureStream === "function"
                ) {
                    const captured =
                        video.captureStream();

                    const v = bestTrack(
                        captured.getVideoTracks(),
                        "video"
                    );

                    const a = bestTrack(
                        captured.getAudioTracks(),
                        "audio"
                    );

                    if (v) {
                        state.preparedStream = captured;
                        state.preparedVideoTrack = v;
                        state.preparedAudioTrack = a || null;

                        return {
                            ok: true,
                            source: "captureStream",
                            streamId: captured.id,
                            hasVideo: true,
                            hasAudio: !!a,
                            videoTrackId: v.id,
                            videoMuted: !!v.muted,
                            audioTrackId: a
                                ? a.id
                                : null,
                            audioMuted: a
                                ? !!a.muted
                                : null,
                        };
                    }
                }
            } catch (_) {}
        }

        return {
            ok: false,
            reason: "No live video track available"
        };
    };


    // --------------------------------------------------------
    // Start MediaRecorder
    // --------------------------------------------------------

    window.__superliveStartRecorder = () => {
        state.recorderQueue = [];
        state.recorderError = null;
        state.dataEvents = 0;
        state.totalBytes = 0;

        if (!state.preparedStream) {
            return {
                ok: false,
                reason: "No prepared stream"
            };
        }

        const stream =
            state.preparedStream;

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

        if (!mimeType) {
            return {
                ok: false,
                reason: "No supported MediaRecorder MIME type"
            };
        }

        let recorder;

        try {
            recorder = new MediaRecorder(
                stream,
                {
                    mimeType,
                    videoBitsPerSecond: 4000000,
                    audioBitsPerSecond: 128000
                }
            );
        } catch (error) {
            return {
                ok: false,
                reason: String(error)
            };
        }

        recorder.ondataavailable = async (event) => {
            try {
                if (!event.data) {
                    return;
                }

                if (event.data.size <= 0) {
                    return;
                }

                state.dataEvents++;
                state.totalBytes += event.data.size;

                state.recorderQueue.push(
                    event.data
                );
            } catch (error) {
                state.recorderError =
                    String(error);
            }
        };

        recorder.onerror = (event) => {
            try {
                state.recorderError =
                    String(event.error || event);
            } catch (_) {
                state.recorderError =
                    "MediaRecorder error";
            }
        };

        recorder.onstop = () => {
            state.recorderStoppedAt =
                Date.now();
        };

        try {
            recorder.start(1000);
        } catch (error) {
            return {
                ok: false,
                reason: String(error)
            };
        }

        state.recorder = recorder;
        state.recorderStartedAt = Date.now();

        return {
            ok: true,
            mimeType,
            videoTrackId:
                state.preparedVideoTrack
                    ? state.preparedVideoTrack.id
                    : null,
            videoMuted:
                state.preparedVideoTrack
                    ? !!state.preparedVideoTrack.muted
                    : null,
            audioTrackId:
                state.preparedAudioTrack
                    ? state.preparedAudioTrack.id
                    : null,
            audioMuted:
                state.preparedAudioTrack
                    ? !!state.preparedAudioTrack.muted
                    : null,
        };
    };


    // --------------------------------------------------------
    // Recorder status
    // --------------------------------------------------------

    window.__superliveRecorderStatus = () => {
        return {
            exists: !!state.recorder,
            state: state.recorder
                ? state.recorder.state
                : null,
            queueLength:
                state.recorderQueue.length,
            dataEvents:
                state.dataEvents,
            totalBytes:
                state.totalBytes,
            error:
                state.recorderError,
            elapsedSeconds:
                state.recorderStartedAt
                    ? (
                        (Date.now() -
                            state.recorderStartedAt) /
                        1000
                    )
                    : 0,
        };
    };


    // --------------------------------------------------------
    // Wait until first chunk exists
    // --------------------------------------------------------

    window.__superliveWaitForFirstChunk = async (
        timeoutMs
    ) => {
        const started = Date.now();

        while (
            Date.now() - started <
            timeoutMs
        ) {
            if (
                state.recorderError
            ) {
                return {
                    ok: false,
                    reason:
                        state.recorderError
                };
            }

            if (
                state.recorderQueue.length >
                0
            ) {
                return {
                    ok: true,
                    queueLength:
                        state.recorderQueue.length,
                    dataEvents:
                        state.dataEvents,
                    totalBytes:
                        state.totalBytes
                };
            }

            await new Promise(
                resolve =>
                    setTimeout(
                        resolve,
                        250
                    )
            );
        }

        return {
            ok: false,
            reason:
                "No MediaRecorder data received within timeout",
            queueLength:
                state.recorderQueue.length,
            dataEvents:
                state.dataEvents,
            totalBytes:
                state.totalBytes
        };
    };


    // --------------------------------------------------------
    // Take one queued chunk
    // --------------------------------------------------------

    window.__superliveTakeRecorderChunk =
        async () => {
            if (
                state.recorderQueue.length ===
                0
            ) {
                return null;
            }

            const blob =
                state.recorderQueue.shift();

            const buffer =
                await blob.arrayBuffer();

            const bytes =
                new Uint8Array(buffer);

            let binary = "";

            const chunkSize = 0x8000;

            for (
                let i = 0;
                i < bytes.length;
                i += chunkSize
            ) {
                binary += String.fromCharCode(
                    ...bytes.subarray(
                        i,
                        Math.min(
                            i + chunkSize,
                            bytes.length
                        )
                    )
                );
            }

            return {
                base64:
                    btoa(binary),
                size:
                    bytes.length,
                type:
                    blob.type || ""
            };
        };


    // --------------------------------------------------------
    // Stop recorder
    // --------------------------------------------------------

    window.__superliveStopRecorder =
        async () => {
            if (!state.recorder) {
                return {
                    ok: true,
                    stopped: false
                };
            }

            const recorder =
                state.recorder;

            if (
                recorder.state !== "inactive"
            ) {
                try {
                    recorder.stop();
                } catch (_) {}
            }

            // Give the final dataavailable event
            // a chance to arrive.
            await new Promise(
                resolve =>
                    setTimeout(
                        resolve,
                        1500
                    )
            );

            return {
                ok: true,
                stopped: true,
                state:
                    recorder.state,
                queueLength:
                    state.recorderQueue.length,
                dataEvents:
                    state.dataEvents,
                totalBytes:
                    state.totalBytes,
                error:
                    state.recorderError
            };
        };


    // --------------------------------------------------------
    // Background media scanning
    // --------------------------------------------------------

    setInterval(() => {
        try {
            scanMediaElements();

            for (const pc of state.pcs) {
                try {
                    const receivers =
                        pc.getReceivers();

                    for (
                        const receiver
                        of receivers
                    ) {
                        if (
                            receiver.track
                        ) {
                            rememberTrack(
                                receiver.track
                            );
                        }
                    }
                } catch (_) {}
            }
        } catch (_) {}
    }, 1000);
})();
"""


# ============================================================
# HELPERS
# ============================================================

def log(message=""):
    print(message, flush=True)


def ensure_dirs():
    RECORDINGS_DIR.mkdir(
        parents=True,
        exist_ok=True
    )

    TEMP_DIR.mkdir(
        parents=True,
        exist_ok=True
    )


def check_command(command):
    return shutil.which(command) is not None


def validate_mp4(path):
    if not path.exists():
        return False

    if path.stat().st_size < 1024:
        return False

    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=format_name,duration",
                "-of",
                "json",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )

        if result.returncode != 0:
            return False

        data = json.loads(
            result.stdout or "{}"
        )

        fmt = data.get(
            "format",
            {}
        )

        duration = float(
            fmt.get(
                "duration",
                0
            ) or 0
        )

        format_name = str(
            fmt.get(
                "format_name",
                ""
            )
        )

        return (
            duration > 0
            and "mp4" in format_name
        )

    except Exception:
        return False


async def save_diagnostics(page):
    log("[DIAGNOSTIC] Writing diagnostic files...")

    try:
        await page.screenshot(
            path=str(
                TEMP_DIR /
                "_diagnostic.png"
            ),
            full_page=True,
        )
    except Exception as exc:
        log(
            f"[DIAGNOSTIC] Screenshot failed: {exc}"
        )

    try:
        html = await page.content()

        (
            TEMP_DIR /
            "_diagnostic.html"
        ).write_text(
            html,
            encoding="utf-8"
        )

    except Exception as exc:
        log(
            f"[DIAGNOSTIC] HTML failed: {exc}"
        )

    try:
        state = await page.evaluate(
            "() => window.__superliveGetState ? window.__superliveGetState() : null"
        )

        (
            TEMP_DIR /
            "_diagnostic.json"
        ).write_text(
            json.dumps(
                state,
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

    except Exception as exc:
        log(
            f"[DIAGNOSTIC] JSON failed: {exc}"
        )


async def wait_for_media(page):
    log(
        f"[2/6] Waiting for live WebRTC media "
        f"(up to {VIDEO_WAIT_SECONDS}s)..."
    )

    started = time.monotonic()

    while (
        time.monotonic() - started
        < VIDEO_WAIT_SECONDS
    ):
        try:
            state = await page.evaluate(
                "() => window.__superliveGetState ? window.__superliveGetState() : null"
            )

            if state:
                tracks = state.get(
                    "tracks",
                    []
                )

                live_video = [
                    t for t in tracks
                    if (
                        t.get("kind") == "video"
                        and t.get("readyState") == "live"
                    )
                ]

                live_audio = [
                    t for t in tracks
                    if (
                        t.get("kind") == "audio"
                        and t.get("readyState") == "live"
                    )
                ]

                if live_video:
                    log(
                        "[MEDIA] Candidate media detected."
                    )

                    log(
                        "[MEDIA] "
                        f"live video tracks={len(live_video)}, "
                        f"live audio tracks={len(live_audio)}"
                    )

                    return True

        except Exception as exc:
            log(
                f"[MEDIA] Check error: {exc}"
            )

        await asyncio.sleep(2)

    return False


async def get_candidates(page):
    try:
        result = await page.evaluate(
            "() => window.__superliveGetCandidates ? window.__superliveGetCandidates() : null"
        )

        return result or {}

    except Exception as exc:
        log(
            f"[CANDIDATES] Failed: {exc}"
        )
        return {}


def print_candidates(candidates):
    streams = candidates.get(
        "streams",
        []
    )

    log(
        f"[CANDIDATES] Found {len(streams)} stream candidates."
    )

    for item in streams[:MAX_CANDIDATES]:
        video = item.get("video")
        audio = item.get("audio")

        log(
            "[CANDIDATE] "
            f"index={item.get('index')} "
            f"score={item.get('score')} "
            f"stream={item.get('id')} "
            f"video={video.get('id') if video else None} "
            f"video_muted={video.get('muted') if video else None} "
            f"audio={audio.get('id') if audio else None} "
            f"audio_muted={audio.get('muted') if audio else None}"
        )


async def prepare_and_start_candidate(
    page,
    stream_index,
):
    try:
        prepared = await page.evaluate(
            """
            (index) => {
                return window.__superlivePrepareRecordingStream
                    ? window.__superlivePrepareRecordingStream(index)
                    : {ok:false, reason:"Hook missing"};
            }
            """,
            stream_index,
        )

        log(
            f"[STREAM] {prepared}"
        )

        if not prepared.get("ok"):
            return False

        # Critical safety check:
        # Do not knowingly select a muted video when
        # an unmuted candidate is available.
        if prepared.get(
            "videoMuted"
        ):
            log(
                "[STREAM] WARNING: selected video track is muted."
            )

        started = await page.evaluate(
            "() => window.__superliveStartRecorder ? window.__superliveStartRecorder() : {ok:false, reason:'Hook missing'}"
        )

        log(
            f"[RECORDER] {started}"
        )

        if not started.get("ok"):
            return False

        return True

    except Exception as exc:
        log(
            f"[RECORDER] Candidate start failed: {exc}"
        )
        return False


async def stop_browser_recorder(page):
    try:
        result = await page.evaluate(
            "() => window.__superliveStopRecorder ? window.__superliveStopRecorder() : {ok:true}"
        )

        log(
            f"[RECORDER] Stop result: {result}"
        )

    except Exception as exc:
        log(
            f"[RECORDER] Stop error: {exc}"
        )


async def take_chunk(page):
    try:
        result = await page.evaluate(
            """
            () => window.__superliveTakeRecorderChunk
                ? window.__superliveTakeRecorderChunk()
                : null
            """
        )

        return result

    except Exception as exc:
        log(
            f"[CHUNK] Error: {exc}"
        )
        return None


def start_ffmpeg():
    output_pattern = str(
        TEMP_DIR /
        "segment_%05d.mp4"
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
        str(
            VIDEO_BITRATE * 2
        ),

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
        "expr:gte(t,n_forced*60)",

        "-f",
        "segment",

        "-segment_time",
        str(SEGMENT_SECONDS),

        "-reset_timestamps",
        "1",

        "-segment_format",
        "mp4",

        output_pattern,
    ]

    log(
        "[FFMPEG] Starting:"
    )

    log(
        " ".join(command)
    )

    process = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )

    return process


async def read_ffmpeg_stderr(
    process,
    holder,
):
    try:
        data = await asyncio.to_thread(
            process.stderr.read
        )

        holder.append(
            data.decode(
                "utf-8",
                errors="replace"
            )
        )

    except Exception as exc:
        holder.append(
            f"stderr read error: {exc}"
        )


async def write_chunk(
    process,
    data,
):
    if not process.stdin:
        raise RuntimeError(
            "FFmpeg stdin is not available."
        )

    process.stdin.write(data)

    await asyncio.to_thread(
        process.stdin.flush
    )


async def close_ffmpeg(
    process,
):
    if process.stdin:
        try:
            process.stdin.close()
        except Exception:
            pass

    return await asyncio.to_thread(
        process.wait
    )


def collect_recordings():
    return sorted(
        RECORDINGS_DIR.glob(
            "*.mp4"
        )
    )


def clean_temp():
    if not TEMP_DIR.exists():
        return

    for item in TEMP_DIR.iterdir():
        try:
            if item.is_file():
                item.unlink()
            elif item.is_dir():
                shutil.rmtree(item)
        except Exception:
            pass


def rename_segments():
    segments = sorted(
        TEMP_DIR.glob(
            "segment_*.mp4"
        )
    )

    renamed = []

    for index, source in enumerate(
        segments
    ):
        destination = (
            RECORDINGS_DIR /
            f"recording_{int(time.time())}_{index:03d}.mp4"
        )

        try:
            source.replace(
                destination
            )

            renamed.append(
                destination
            )

        except Exception as exc:
            log(
                f"[OUTPUT] Rename failed: {exc}"
            )

    return renamed


# ============================================================
# MAIN
# ============================================================

async def main():
    ensure_dirs()
    clean_temp()

    log("=" * 60)
    log("SUPERLIVE RECORDER")
    log("=" * 60)

    log(
        f"URL: {URL}"
    )

    log(
        f"Output: {RECORDINGS_DIR}"
    )

    log(
        f"Temp: {TEMP_DIR}"
    )

    log(
        f"Segment: {SEGMENT_SECONDS}s"
    )

    log(
        f"Duration limit: {RECORD_DURATION_SECONDS}s"
    )

    log(
        f"Video bitrate: {VIDEO_BITRATE}"
    )

    log(
        f"Audio bitrate: {AUDIO_BITRATE}"
    )

    log("=" * 60)


    # --------------------------------------------------------
    # Environment checks
    # --------------------------------------------------------

    if not check_command("ffmpeg"):
        raise RuntimeError(
            "FFmpeg is not installed."
        )

    if not check_command("ffprobe"):
        raise RuntimeError(
            "ffprobe is not installed."
        )


    async with async_playwright() as playwright:

        log(
            "[1/6] Launching full Chromium..."
        )

        browser = await playwright.chromium.launch(
            channel="chromium",
            headless=True,

            # Playwright normally adds --mute-audio.
            # Remove it so WebRTC audio is not forcibly muted.
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
                "Mozilla/5.0 "
                "(X11; Linux x86_64) "
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


        # ----------------------------------------------------
        # IMPORTANT:
        # Install WebRTC hook BEFORE page navigation.
        # ----------------------------------------------------

        await context.add_init_script(
            WEBRTC_HOOK
        )


        page = await context.new_page()


        # ----------------------------------------------------
        # Diagnostics
        # ----------------------------------------------------

        def on_console(message):
            try:
                text = message.text

                if (
                    "error" in message.type
                    or "warning" in message.type
                ):
                    log(
                        f"[CONSOLE:{message.type}] {text}"
                    )

            except Exception:
                pass


        page.on(
            "console",
            on_console
        )


        def on_page_error(error):
            log(
                f"[PAGE ERROR] {error}"
            )


        page.on(
            "pageerror",
            on_page_error
        )


        # ----------------------------------------------------
        # Navigation
        # ----------------------------------------------------

        log(
            f"[NAVIGATION] Opening {URL}"
        )

        try:
            await page.goto(
                URL,
                wait_until="domcontentloaded",
                timeout=PAGE_TIMEOUT_MS,
            )

        except Exception as exc:
            log(
                f"[NAVIGATION] goto warning: {exc}"
            )


        log(
            f"[NAVIGATION] Current URL: {page.url}"
        )


        # Give page scripts / WebRTC a little time.
        await asyncio.sleep(5)


        # ----------------------------------------------------
        # Media detection
        # ----------------------------------------------------

        media_found = await wait_for_media(
            page
        )

        if not media_found:
            log(
                "[FATAL] No live WebRTC video tracks detected."
            )

            await save_diagnostics(
                page
            )

            await browser.close()

            return 1


        # Print complete media state.
        try:
            state = await page.evaluate(
                "() => window.__superliveGetState()"
            )

            log(
                "[MEDIA STATE]"
            )

            log(
                json.dumps(
                    state,
                    ensure_ascii=False,
                    indent=2,
                )
            )

        except Exception as exc:
            log(
                f"[MEDIA STATE] Failed: {exc}"
            )


        # ----------------------------------------------------
        # Force playback
        # ----------------------------------------------------

        try:
            played = await page.evaluate(
                "() => window.__superliveForcePlay()"
            )

            log(
                f"[MEDIA] Force-play result: {played}"
            )

        except Exception as exc:
            log(
                f"[MEDIA] Force-play failed: {exc}"
            )


        # ----------------------------------------------------
        # Candidate selection
        # ----------------------------------------------------

        log(
            "[3/6] Finding best WebRTC recording candidates..."
        )

        candidates = await get_candidates(
            page
        )

        print_candidates(
            candidates
        )

        streams = candidates.get(
            "streams",
            []
        )

        # Prefer candidates with both video and audio.
        usable_streams = [
            item for item in streams
            if item.get("hasVideo")
        ]

        usable_streams = sorted(
            usable_streams,
            key=lambda item:
                item.get(
                    "score",
                    0
                ),
            reverse=True,
        )

        usable_streams = usable_streams[
            :MAX_CANDIDATES
        ]


        if not usable_streams:
            log(
                "[FATAL] No usable WebRTC stream candidates."
            )

            await save_diagnostics(
                page
            )

            await browser.close()

            return 1


        # ----------------------------------------------------
        # Try candidates until MediaRecorder actually produces
        # data.
        # ----------------------------------------------------

        selected = None

        for position, candidate in enumerate(
            usable_streams,
            start=1,
        ):
            index = candidate.get(
                "index"
            )

            log(
                ""
            )

            log(
                f"[CANDIDATE TEST] "
                f"{position}/{len(usable_streams)} "
                f"stream index={index}"
            )

            log(
                f"[CANDIDATE TEST] "
                f"score={candidate.get('score')}"
            )

            video = candidate.get(
                "video"
            )

            audio = candidate.get(
                "audio"
            )

            log(
                "[CANDIDATE TEST] "
                f"video={video.get('id') if video else None} "
                f"muted={video.get('muted') if video else None}"
            )

            log(
                "[CANDIDATE TEST] "
                f"audio={audio.get('id') if audio else None} "
                f"muted={audio.get('muted') if audio else None}"
            )


            # Prepare + start.
            started = await prepare_and_start_candidate(
                page,
                index,
            )

            if not started:
                log(
                    "[CANDIDATE TEST] Could not start recorder."
                )

                await stop_browser_recorder(
                    page
                )

                continue


            log(
                "[CANDIDATE TEST] "
                f"Waiting up to "
                f"{FIRST_CHUNK_TIMEOUT_SECONDS}s "
                f"for actual media data..."
            )


            try:
                first_chunk = await page.evaluate(
                    """
                    (timeoutMs) => {
                        return window.__superliveWaitForFirstChunk
                            ? window.__superliveWaitForFirstChunk(timeoutMs)
                            : {ok:false, reason:"Hook missing"};
                    }
                    """,
                    FIRST_CHUNK_TIMEOUT_SECONDS * 1000,
                )

            except Exception as exc:
                first_chunk = {
                    "ok": False,
                    "reason": str(exc),
                }


            log(
                f"[CANDIDATE TEST] First chunk result: {first_chunk}"
            )


            if first_chunk.get("ok"):
                selected = candidate

                log(
                    "[CANDIDATE TEST] SUCCESS!"
                )

                break


            log(
                "[CANDIDATE TEST] "
                "No actual MediaRecorder data."
            )

            await stop_browser_recorder(
                page
            )

            # Small pause before next candidate.
            await asyncio.sleep(1)


        if selected is None:
            log(
                "[FATAL] None of the WebRTC candidates "
                "produced MediaRecorder data."
            )

            await save_diagnostics(
                page
            )

            await browser.close()

            return 1


        log(
            ""
        )

        log(
            "[SELECTED STREAM]"
        )

        log(
            json.dumps(
                selected,
                ensure_ascii=False,
                indent=2,
            )
        )


        # ----------------------------------------------------
        # FFmpeg
        # ----------------------------------------------------

        log(
            "[4/6] Starting FFmpeg..."
        )

        ffmpeg_process = start_ffmpeg()

        stderr_holder = []

        stderr_task = asyncio.create_task(
            read_ffmpeg_stderr(
                ffmpeg_process,
                stderr_holder,
            )
        )


        # ----------------------------------------------------
        # IMPORTANT:
        # The first chunk already exists.
        # Take it before starting the main loop.
        # ----------------------------------------------------

        first_chunk = await take_chunk(
            page
        )

        if not first_chunk:
            log(
                "[FATAL] First chunk disappeared."
            )

            await stop_browser_recorder(
                page
            )

            try:
                ffmpeg_process.kill()
            except Exception:
                pass

            await browser.close()

            return 1


        try:
            first_data = base64.b64decode(
                first_chunk["base64"]
            )

            await write_chunk(
                ffmpeg_process,
                first_data,
            )

            log(
                f"[FFMPEG] First chunk written: "
                f"{len(first_data)} bytes"
            )

        except Exception as exc:
            log(
                f"[FATAL] Failed to write first chunk: {exc}"
            )

            await stop_browser_recorder(
                page
            )

            try:
                ffmpeg_process.kill()
            except Exception:
                pass

            await browser.close()

            return 1


        # ----------------------------------------------------
        # Recording loop
        # ----------------------------------------------------

        log(
            "[5/6] Recording and feeding FFmpeg..."
        )

        recording_started = time.monotonic()

        last_report = 0

        total_bytes = len(
            first_data
        )

        chunk_count = 1


        while True:
            elapsed = (
                time.monotonic()
                - recording_started
            )

            if (
                elapsed >=
                RECORD_DURATION_SECONDS
            ):
                break


            chunk = await take_chunk(
                page
            )

            if chunk:
                try:
                    data = base64.b64decode(
                        chunk["base64"]
                    )

                    if data:
                        await write_chunk(
                            ffmpeg_process,
                            data,
                        )

                        total_bytes += len(
                            data
                        )

                        chunk_count += 1

                except Exception as exc:
                    log(
                        f"[CHUNK] Write error: {exc}"
                    )

                    break


            now = int(
                elapsed
            )

            if (
                now >= last_report + 10
            ):
                last_report = now

                try:
                    status = await page.evaluate(
                        "() => window.__superliveRecorderStatus()"
                    )

                    log(
                        f"[RECORDING] "
                        f"{now}/"
                        f"{RECORD_DURATION_SECONDS}s "
                        f"queue="
                        f"{status.get('queueLength')} "
                        f"events="
                        f"{status.get('dataEvents')} "
                        f"bytes="
                        f"{status.get('totalBytes')}"
                    )

                except Exception:
                    log(
                        f"[RECORDING] "
                        f"{now}/"
                        f"{RECORD_DURATION_SECONDS}s "
                        f"chunks={chunk_count} "
                        f"bytes={total_bytes}"
                    )


            await asyncio.sleep(
                0.15
            )


        # ----------------------------------------------------
        # Stop browser recorder
        # ----------------------------------------------------

        log(
            "[RECORDER] Stopping browser recorder..."
        )

        await stop_browser_recorder(
            page
        )


        # ----------------------------------------------------
        # Drain remaining chunks
        # ----------------------------------------------------

        log(
            "[RECORDER] Draining final chunks..."
        )

        drain_deadline = (
            time.monotonic()
            + 5
        )

        while (
            time.monotonic()
            < drain_deadline
        ):
            chunk = await take_chunk(
                page
            )

            if not chunk:
                await asyncio.sleep(
                    0.2
                )
                continue

            try:
                data = base64.b64decode(
                    chunk["base64"]
                )

                if data:
                    await write_chunk(
                        ffmpeg_process,
                        data,
                    )

                    total_bytes += len(
                        data
                    )

                    chunk_count += 1

            except Exception as exc:
                log(
                    f"[DRAIN] Error: {exc}"
                )

                break


        # ----------------------------------------------------
        # Close FFmpeg input
        # ----------------------------------------------------

        log(
            "[FFMPEG] Input stream closed. "
            "Waiting for FFmpeg..."
        )

        return_code = await close_ffmpeg(
            ffmpeg_process
        )

        await stderr_task


        if stderr_holder:
            stderr_text = "\n".join(
                stderr_holder
            ).strip()

            if stderr_text:
                log(
                    "[FFMPEG STDERR]"
                )

                log(
                    stderr_text
                )


        log(
            f"[FFMPEG] Exit code: {return_code}"
        )

        log(
            f"[FFMPEG] Total browser bytes: {total_bytes}"
        )

        log(
            f"[FFMPEG] Total chunks: {chunk_count}"
        )


        # ----------------------------------------------------
        # Validate
        # ----------------------------------------------------

        if return_code != 0:
            log(
                "[FATAL] FFmpeg did not finish successfully."
            )

            await save_diagnostics(
                page
            )

            await browser.close()

            return 1


        log(
            "[6/6] Validating MP4 segments..."
        )

        segments = sorted(
            TEMP_DIR.glob(
                "segment_*.mp4"
            )
        )


        valid_segments = []

        for segment in segments:
            size_mb = (
                segment.stat().st_size
                / (
                    1024 * 1024
                )
            )

            valid = validate_mp4(
                segment
            )

            log(
                f"[MP4] "
                f"{segment.name} "
                f"{size_mb:.2f} MB "
                f"valid={valid}"
            )

            if valid:
                valid_segments.append(
                    segment
                )


        if not valid_segments:
            log(
                "[FATAL] No valid MP4 recordings were produced."
            )

            await save_diagnostics(
                page
            )

            await browser.close()

            return 1


        # ----------------------------------------------------
        # Move valid segments to recordings/
        # ----------------------------------------------------

        final_files = []

        timestamp = int(
            time.time()
        )

        for index, segment in enumerate(
            valid_segments
        ):
            destination = (
                RECORDINGS_DIR /
                f"recording_{timestamp}_{index:03d}.mp4"
            )

            try:
                segment.replace(
                    destination
                )

                final_files.append(
                    destination
                )

            except Exception as exc:
                log(
                    f"[OUTPUT] Failed to move "
                    f"{segment}: {exc}"
                )


        # ----------------------------------------------------
        # Final report
        # ----------------------------------------------------

        log("")
        log("=" * 60)
        log("RECORDING COMPLETE")
        log("=" * 60)

        for file in final_files:
            size_mb = (
                file.stat().st_size
                / (
                    1024 * 1024
                )
            )

            log(
                f"{file.name} "
                f"({size_mb:.2f} MB)"
            )

        log(
            f"Segments produced: "
            f"{len(final_files)}"
        )

        log("=" * 60)


        await browser.close()

        return 0


# ============================================================
# ENTRY POINT
# ============================================================

if __name__ == "__main__":
    try:
        exit_code = asyncio.run(
            main()
        )

        sys.exit(
            exit_code
        )

    except KeyboardInterrupt:
        print(
            "\nInterrupted.",
            flush=True
        )

        sys.exit(130)

    except Exception as exc:
        print(
            f"[FATAL] {exc}",
            flush=True
        )

        sys.exit(1)
