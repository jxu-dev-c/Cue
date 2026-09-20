"""Best-effort short-sample alignment, used only without embedded/exact subtitles."""

import io
import json
import logging
import subprocess
import wave
from datetime import timedelta
from pathlib import Path

import chardet
import numpy as np
import pysubs2
import srt
import webrtcvad

from backend.audio_sample import SAMPLE_RATE, extract_audio
from backend.media_range import MediaReadError

LOGGER = logging.getLogger("uvicorn.error")
ACTIVITY_RATE = 100
WINDOW_SECONDS = 8
MAX_OFFSET_SECONDS = 60


def read_cues(path: Path) -> list[srt.Subtitle]:
    raw = path.read_bytes()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        try:
            text = raw.decode(chardet.detect(raw).get("encoding") or "utf-8")
        except (UnicodeError, LookupError) as exc:
            raise MediaReadError("Subtitle text encoding could not be read") from exc
    if path.suffix.lower() in {".ass", ".ssa"}:
        text = pysubs2.SSAFile.from_string(text).to_string("srt")
    cues = list(srt.parse(text))
    if not cues or any(c.start.total_seconds() < 0 or c.end <= c.start for c in cues):
        raise MediaReadError("Subtitle contains no valid timing reference")
    return cues


def select_audio_stream(streams: list[dict]) -> str:
    audio = [s for s in streams if s.get("codec_type") == "audio"]
    eligible = [s for s in audio if not any(word in str(s.get("tags", {}).get("title", "")).lower()
                                          for word in ("commentary", "description"))]
    if not eligible:
        raise MediaReadError("No dialogue audio track was found")
    selected = max(eligible, key=lambda s: s.get("disposition", {}).get("default", 0))
    return f"a:{audio.index(selected)}"


def subtitle_activity(cues, start, frames):
    activity = np.zeros(frames)
    for cue in cues:
        left = max(0, round((cue.start.total_seconds()-start)*ACTIVITY_RATE))
        right = min(frames, round((cue.end.total_seconds()-start)*ACTIVITY_RATE))
        if left < right:
            activity[left:right] = 1
    return activity


def sample_windows(cues):
    end = max(c.end.total_seconds() for c in cues)
    windows = []
    for lo, hi in ((.1,.3),(.4,.6),(.7,.9)):
        starts = sorted({max(0,int(c.start.total_seconds())-2) for c in cues
                         if lo*end <= c.start.total_seconds()-2 <= hi*end})
        if len(starts) > 100:
            starts = [starts[i] for i in np.linspace(0,len(starts)-1,100,dtype=int)]
        if not starts:
            starts = [max(0, int(min((lo+hi)*end/2, end-WINDOW_SECONDS)))]
        ranked = []
        for start in starts:
            activity = subtitle_activity(cues,start,WINDOW_SECONDS*ACTIVITY_RATE)
            density = activity.mean()
            ranked.append((np.count_nonzero(np.diff(activity))*4*density*(1-density),start))
        windows.append(max(ranked)[1])
    return sorted(set(windows))


def speech_activity(wav):
    with wave.open(io.BytesIO(wav)) as audio:
        if (audio.getnchannels(),audio.getsampwidth(),audio.getframerate()) != (1,2,SAMPLE_RATE):
            raise MediaReadError("Invalid synchronization audio format")
        pcm = audio.readframes(audio.getnframes())
    detector = webrtcvad.Vad(3)
    size = SAMPLE_RATE*2//ACTIVITY_RATE
    return np.array([float(detector.is_speech(pcm[i:i+size],SAMPLE_RATE))
                     for i in range(0,len(pcm)-size+1,size)])


def estimate_offset(cues, observations):
    offsets = np.arange(-MAX_OFFSET_SECONDS*ACTIVITY_RATE,MAX_OFFSET_SECONDS*ACTIVITY_RATE+1,2)
    scores = []
    for start,speech in observations:
        centered = speech-speech.mean()
        norm = np.linalg.norm(centered)
        if not norm:
            continue
        margin = MAX_OFFSET_SECONDS*ACTIVITY_RATE
        activity = subtitle_activity(cues,start-MAX_OFFSET_SECONDS,len(speech)+2*margin)
        window_scores = []
        for offset in offsets:
            segment = activity[margin-offset:margin-offset+len(speech)]
            denominator = norm*np.sqrt(segment.sum()*(1-segment.mean()))
            window_scores.append(float(np.dot(centered,segment)/denominator) if denominator else -1.0)
        scores.append(window_scores)
    if not scores:
        return 0.0, "No speech variation; original timing retained", None
    joint = np.mean(scores,axis=0)
    best = int(np.argmax(joint))
    offset = float(offsets[best]/ACTIVITY_RATE)
    reason = "Short-sample estimate; timing may be inaccurate"
    if joint[best] <= 0:
        offset,reason = 0.0,"No positive match; original timing retained"
    elif min(c.start.total_seconds() for c in cues)+offset < 0:
        offset,reason = 0.0,"Estimated shift would lose opening cues; original timing retained"
    elif abs(offset) <= .25:
        offset = 0.0
    return offset,reason,float(joint[best])


def sync_remote(url: str, input_path: Path, output_path: Path) -> dict:
    cues = read_cues(input_path)
    try:
        probe = subprocess.run(
            ["ffprobe","-v","error","-probesize","262144","-analyzeduration","1000000",
             "-show_entries","stream=codec_type:stream_tags=title:stream_disposition=default",
             "-of","json",url],capture_output=True,timeout=30,check=False)
        if probe.returncode:
            raise MediaReadError("Could not inspect synchronization audio tracks")
        track = select_audio_stream(json.loads(probe.stdout).get("streams",[]))
    except (ValueError,subprocess.TimeoutExpired) as exc:
        raise MediaReadError("Could not inspect synchronization audio tracks") from exc
    observations = [(start,speech_activity(extract_audio(
        url,start,duration=WINDOW_SECONDS,pad_timeline=False,stream=f"0:{track}")))
        for start in sample_windows(cues)]
    offset,reason,score = estimate_offset(cues,observations)
    delta = timedelta(seconds=offset)
    shifted = [srt.Subtitle(c.index,c.start+delta,c.end+delta,c.content,c.proprietary) for c in cues]
    output_path.write_text(srt.compose(shifted,reindex=False),encoding="utf-8")
    LOGGER.warning("subtitle_alignment method=short-sample approximate=true offset=%s score=%s reason=%s",
                   offset,score,reason)
    return {"method":"short-sample","approximate":True,"offsetSeconds":offset,"warning":reason}
