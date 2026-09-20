"""Read indexed Matroska text subtitles without scanning video payloads.

EBML parsing is provided by Enzyme. Cue/Block timestamps follow the Matroska
specification: https://www.matroska.org/technical/cues.html
"""

from __future__ import annotations

import io
import logging
import re
import struct
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import timedelta
from typing import Callable

import httpx
import pysubs2
import srt
from enzyme.parsers import ebml
from enzyme.exceptions import ParserError

from backend.media_range import RangeCache, MediaReadError

LOGGER = logging.getLogger("uvicorn.error")
SPECS = ebml.get_matroska_specs()
SPECS[0x22B59D] = (ebml.STRING, "LanguageIETF", 3)
SPECS[0x56AA] = (ebml.UINTEGER, "CodecDelay", 3)
MAX_ELEMENT = 8 * 1024 * 1024
MAX_TEXT = 10 * 1024 * 1024
MAX_CUES = 20000
LANGUAGES = {"eng": "en", "zho": "zh", "chi": "zh", "zh-hans": "zh-cn", "zh-hant": "zh-tw", "spa": "es", "fra": "fr",
             "fre": "fr", "deu": "de", "ger": "de", "jpn": "ja", "kor": "ko",
             "ita": "it", "rus": "ru", "por": "pt"}


class IndexedSubtitleUnavailable(ValueError):
    """No complete supported indexed text track; use the normal sync engine."""


@dataclass(frozen=True)
class EmbeddedSubtitle:
    language: str
    name: str
    data: bytes


class SparseReader:
    def __init__(self, size: int, opener: Callable[[dict[str, str]], httpx.Response]):
        self.size = size
        self.position = 0
        self.cache = RangeCache(size, opener, max_bytes=min(64 * 1024 * 1024, size // 4), block_bytes=16384)
        # Sparse subtitle extraction makes many tiny seeks. Allow latency for
        # those requests without increasing its strict byte or memory budget.
        self.cache.deadline = time.monotonic() + 300

    def tell(self) -> int:
        return self.position

    def seek(self, offset: int, whence: int = 0) -> int:
        position = offset if whence == 0 else self.position + offset if whence == 1 else self.size + offset
        if not 0 <= position <= self.size:
            raise IndexedSubtitleUnavailable("Invalid Matroska index position")
        self.position = position
        return position

    def read(self, length: int = -1) -> bytes:
        if not 0 <= length <= MAX_ELEMENT:
            raise IndexedSubtitleUnavailable("Matroska element exceeds the metadata limit")
        end = min(self.position + length, self.size)
        parts = []
        block_size = self.cache.block_bytes
        while self.position < end:
            block = self.cache.block(self.position // block_size)
            offset = self.position % block_size
            count = min(end - self.position, len(block) - offset)
            parts.append(block[offset:offset + count])
            self.position += count
        return b"".join(parts)

    def prefetch(self, positions: list[int]) -> None:
        block_size = self.cache.block_bytes
        indices = sorted({p // block_size for p in positions if 0 <= p < self.size} - self.cache.blocks.keys())
        required = sum(min(block_size, self.size - i * block_size) for i in indices)
        if self.cache.fetched + required > self.cache.max_bytes:
            raise MediaReadError("Indexed subtitle extraction reached its transfer limit")
        with ThreadPoolExecutor(max_workers=8, thread_name_prefix="subtitle-range") as pool:
            for index, data in zip(indices, pool.map(self.cache._fetch, indices)):
                self.cache._remember(index, data)


def header(stream) -> tuple[int, int, int]:
    ident = ebml.read_element_id(stream)
    size = ebml.read_element_size(stream)
    if ident is None or size is None:
        raise IndexedSubtitleUnavailable("Invalid Matroska element")
    return ident, stream.tell(), size


def element(stream, position: int, expected: int):
    stream.seek(position)
    ident, data_position, size = header(stream)
    if ident != expected or size > MAX_ELEMENT:
        raise IndexedSubtitleUnavailable("Invalid or oversized Matroska index element")
    stream.seek(position)
    data = stream.read(data_position + size - position)
    return ebml.parse_element(io.BytesIO(data), SPECS, load_children=True)


def extract_indexed(stream, languages: tuple[str, ...]) -> EmbeddedSubtitle | None:
    """Return only a complete indexed, enabled, non-forced text subtitle track."""
    try:
        stream.seek(0)
        ident, position, size = header(stream)
        if ident != 0x1A45DFA3:
            return None
        stream.seek(position + size)
        ident, segment, _ = header(stream)
        if ident != 0x18538067:
            return None
        seek_head = element(stream, segment, 0x114D9B74)
        positions = {ebml.read_element_id(x["SeekID"].data): segment + x.get("SeekPosition")
                     for x in seek_head if x.name == "Seek"}
        if not all(key in positions for key in (0x1654AE6B, 0x1549A966, 0x1C53BB6B)):
            return None
        tracks = element(stream, positions[0x1654AE6B], 0x1654AE6B)
        eligible = []
        for track in tracks:
            if track.name != "TrackEntry" or track.get("TrackType") != 17:
                continue
            language = track.get("LanguageIETF") or track.get("Language", "")
            language = LANGUAGES.get(language.lower(), language.lower())
            if language not in languages and language.split("-")[0] in languages:
                language = language.split("-")[0]
            name = track.get("Name", "")
            if (language not in languages or not track.get("FlagEnabled", 1) or track.get("FlagForced", 0)
                    or track.get("CodecID") not in {"S_TEXT/UTF8", "S_TEXT/ASS", "S_TEXT/SSA"}
                    or track.get("TrackTimecodeScale", 1.0) != 1.0
                    or track.get("TrackOffset", 0) != 0 or track.get("CodecDelay", 0) != 0
                    or "ContentEncodings" in track
                    or (not re.search(r"\bfull\b", name, re.I)
                        and re.search(r"\b(forced|signs|songs|commentary)\b", name, re.I))):
                continue
            eligible.append((languages.index(language), -track.get("FlagDefault", 1), language, track))
        if not eligible:
            return None
        _, _, language, track = min(eligible, key=lambda t: t[:2])
        number = track.get("TrackNumber")
        info = element(stream, positions[0x1549A966], 0x1549A966)
        scale = info.get("TimecodeScale", 1000000) / 1000000
        if not 0 < scale <= 1000:
            return None
        index = element(stream, positions[0x1C53BB6B], 0x1C53BB6B)
        entries = []
        for cue in index:
            if cue.name != "CuePoint":
                continue
            for point in cue:
                if point.name == "CueTrackPositions" and point.get("CueTrack") == number:
                    if "CueRelativePosition" not in point:
                        return None
                    entries.append((cue.get("CueTime"), segment + point.get("CueClusterPosition"),
                                    point.get("CueRelativePosition"), point.get("CueDuration")))
        if not entries or len(entries) > MAX_CUES:
            return None
        # Cues are a seek index and can be sparse. Independently verify coverage
        # against the muxer's per-track statistics before calling it complete.
        if 0x1254C367 not in positions:
            return None
        tags = element(stream, positions[0x1254C367], 0x1254C367)
        stats = {}
        for tag in tags:
            if tag.name == "Tag" and "Targets" in tag and tag["Targets"].get("TagTrackUID") == track.get("TrackUID"):
                stats.update({child.get("TagName"): child.get("TagString")
                              for child in tag if child.name == "SimpleTag"})
        if int(stats.get("NUMBER_OF_FRAMES", 0)) != len(entries):
            return None
        expected_bytes = int(stats.get("NUMBER_OF_BYTES", 0))
        if not 0 < expected_bytes <= MAX_TEXT:
            return None
        if len({(cluster, relative) for _, cluster, relative, _ in entries}) != len(entries):
            return None
        events = []
        clusters = {}
        total_text = 0
        codec = track.get("CodecID")
        for batch_start in range(0, len(entries), 16):
            batch = entries[batch_start:batch_start + 16]
            if hasattr(stream, "prefetch"):
                stream.prefetch([p for _, cluster, relative, _ in batch
                                 for p in (cluster, cluster + relative, cluster + relative + 12)])
            for cue_time, cluster, relative, duration in batch:
                if cluster not in clusters:
                    stream.seek(cluster)
                    kind, cluster_data, cluster_size = header(stream)
                    if kind != 0x1F43B675:
                        raise IndexedSubtitleUnavailable("Cue does not point to a cluster")
                    for _ in range(8):
                        kind, child_data, child_size = header(stream)
                        if kind == 0xE7:
                            clusters[cluster] = (cluster_data, cluster_data + cluster_size,
                                                 ebml.read_element_uinteger(stream, child_size))
                            break
                        stream.seek(child_data + child_size)
                    else:
                        raise IndexedSubtitleUnavailable("Cluster has no timestamp")
                cluster_data, cluster_end, cluster_time = clusters[cluster]
                if not cluster_data <= cluster_data + relative < cluster_end:
                    raise IndexedSubtitleUnavailable("Subtitle cue is outside its cluster")
                group = element(stream, cluster_data + relative, 0xA0)
                block = group["Block"].data
                block_track = ebml.read_element_size(block)
                relative_time, flags = struct.unpack(">hB", block.read(3))
                if block_track != number or flags & 6 or cluster_time + relative_time != cue_time:
                    raise IndexedSubtitleUnavailable("Subtitle index and block timestamps disagree")
                block_duration = group.get("BlockDuration")
                if block_duration is None or (duration is not None and block_duration != duration):
                    raise IndexedSubtitleUnavailable("Subtitle duration is missing or inconsistent")
                duration = block_duration
                payload = block.read()
                text = payload.decode("utf-8")
                total_text += len(payload)
                if total_text > MAX_TEXT:
                    raise IndexedSubtitleUnavailable("Embedded subtitle is too large")
                style = "Default"
                if codec in {"S_TEXT/ASS", "S_TEXT/SSA"}:
                    fields = text.split(",", 8)
                    if len(fields) != 9:
                        raise IndexedSubtitleUnavailable("Invalid embedded ASS dialogue")
                    style, text = fields[2], fields[8]
                if codec == "S_TEXT/UTF8":
                    if duration > 0 and text.strip():
                        events.append(srt.Subtitle(len(events)+1, timedelta(milliseconds=cue_time*scale),
                                                  timedelta(milliseconds=(cue_time+duration)*scale), text))
                else:
                    event = pysubs2.SSAEvent(start=round(cue_time * scale), end=round((cue_time + duration) * scale), text=text, style=style)
                    if duration > 0 and not event.is_drawing and event.plaintext.strip():
                        events.append(event)
        if total_text != expected_bytes:
            return None
        if codec == "S_TEXT/UTF8":
            data = srt.compose(sorted(events, key=lambda e: (e.start, e.end))).encode("utf-8")
        else:
            subs = pysubs2.SSAFile()
            subs.events = sorted(events, key=lambda e: (e.start, e.end))
            data = subs.to_string("srt").encode("utf-8")
        if not data or len(data) > MAX_TEXT:
            return None
        return EmbeddedSubtitle(language, track.get("Name", "Embedded subtitle"), data)
    except (ParserError, ValueError, KeyError, TypeError, struct.error, RecursionError) as exc:
        LOGGER.info("Indexed subtitle unavailable: %s", exc)
        return None
