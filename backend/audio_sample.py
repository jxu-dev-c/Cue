"""Decode a small remote audio reference once, entirely in memory."""

import io
import shutil
import subprocess
import wave
from pathlib import Path

import srt

from backend.media_range import MediaReadError

SAMPLE_SECONDS = 15
SAMPLE_RATE = 8000
EXTRACT_TIMEOUT = 90


def sample_start(subtitle: Path) -> int:
    """Skip a long silent intro when the subtitle supplies a usable first cue."""
    try:
        cues = srt.parse(subtitle.read_text(encoding="utf-8-sig"))
        first = next(cue for cue in cues if cue.content.strip())
        return max(0, min(180, int(first.start.total_seconds()) - 5))
    except (OSError, UnicodeError, ValueError, StopIteration, srt.SRTParseError):
        return 0


def extract_audio(
    url: str, start: int = 0, *, duration: int = SAMPLE_SECONDS, pad_timeline: bool = True, stream: str = "0:a:0",
) -> bytes:
    if start < 0 or not 1 <= duration <= SAMPLE_SECONDS:
        raise MediaReadError("Invalid audio sample interval")
    executable = shutil.which("ffmpeg")
    if not executable:
        raise MediaReadError("FFmpeg is not installed")
    try:
        result = subprocess.run(
            [executable, "-hide_banner", "-loglevel", "error", "-nostdin",
             # Probe only enough to identify the audio stream. No separate
             # ffprobe pass or embedded-subtitle scan of the remote container.
             "-probesize", "262144", "-analyzeduration", "1000000",
             "-rw_timeout", "10000000", "-ss", str(start), "-t", str(duration),
             "-discard:v", "all", "-discard:s", "all",
             "-i", url, "-map", stream, "-vn", "-sn", "-dn",
             "-t", str(duration), "-ac", "1", "-ar", str(SAMPLE_RATE),
             "-af", "aresample=async=1", "-c:a", "pcm_s16le", "-f", "s16le", "pipe:1"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            timeout=EXTRACT_TIMEOUT, check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise MediaReadError(f"Remote audio sampling exceeded {EXTRACT_TIMEOUT} seconds; stopped without downloading the video") from exc
    pcm = result.stdout
    if result.returncode or len(pcm) < SAMPLE_RATE * 2:
        raise MediaReadError("Could not decode a short remote audio sample within the read limits")
    # FFmpeg can exit successfully after a truncated HTTP read. A tiny fragment
    # is not the reference we requested, even if it contains decodable audio.
    if len(pcm) < (duration - 0.1) * SAMPLE_RATE * 2:
        raise MediaReadError(
            f"Remote audio sample was incomplete ({len(pcm) / (SAMPLE_RATE * 2):.1f} "
            f"of {duration} seconds); synchronization stopped"
        )
    if len(pcm) > duration * SAMPLE_RATE * 2:
        raise MediaReadError("Remote audio sample exceeded its size limit")
    # A WAV header plus silent prefix preserves the original video timeline
    # after seeking. No AAC remuxing, video files, or audio files on disk.
    output = io.BytesIO()
    with wave.open(output, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(SAMPLE_RATE)
        prefix = b"\0" * (start * SAMPLE_RATE * 2) if pad_timeline else b""
        audio.writeframes(prefix + pcm)
    return output.getvalue()
