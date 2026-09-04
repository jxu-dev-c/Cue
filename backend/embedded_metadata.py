from __future__ import annotations

import re
import struct
import time
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Callable, Iterator


RANGE_WINDOW_BYTES = 256 * 1024
MAX_PROBE_BYTES = 8 * 1024 * 1024
PROBE_TIMEOUT_SECONDS = 2.0
MAX_RANGE_REQUESTS = 256
MAX_PARSED_ELEMENTS = 10_000

MATROSKA_EXTENSIONS = {".mkv", ".webm"}
MP4_EXTENSIONS = {".m4v", ".mov", ".mp4"}
SUBTITLE_HANDLER_TYPES = {b"clcp", b"sbtl", b"subt", b"text"}

EBML_ID_SEGMENT = 0x18538067
EBML_ID_SEEK_HEAD = 0x114D9B74
EBML_ID_SEEK = 0x4DBB
EBML_ID_SEEK_ID = 0x53AB
EBML_ID_SEEK_POSITION = 0x53AC
EBML_ID_TRACKS = 0x1654AE6B
EBML_ID_TRACK_ENTRY = 0xAE
EBML_ID_TRACK_TYPE = 0x83
EBML_ID_TRACK_LANGUAGE = 0x22B59C
EBML_ID_TRACK_LANGUAGE_BCP47 = 0x22B59D
EBML_ID_CLUSTER = 0x1F43B675
MATROSKA_SUBTITLE_TRACK_TYPE = 17

_LANGUAGE_TAG = re.compile(r"^[A-Za-z]{1,8}(?:[-_][A-Za-z0-9]{1,8})*$")

# QuickTime's legacy numeric language table. Empty entries are reserved or
# cannot be represented without inventing information that is not in the file.
_QUICKTIME_LANGUAGES: tuple[str | None, ...] = (
    "eng", "fra", "ger", "ita", "dut", "sve", "spa", "dan", "por", "nor",
    "heb", "jpn", "ara", "fin", "gre", "ice", "mlt", "tur", "hr", "chi",
    "urd", "hin", "tha", "kor", "lit", "pol", "hun", "est", "lav", "smi",
    "fo", "per", "rus", "chi", None, "iri", "alb", "ron", "ces", "slk",
    "slv", "yid", "sr", "mac", "bul", "ukr", "bel", "uzb", "kaz", "aze",
    "aze", "arm", "geo", "mol", "kir", "tgk", "tuk", "mon", None, "pus",
    "kur", "kas", "snd", "tib", "nep", "san", "mar", "ben", "asm", "guj",
    "pa", "ori", "mal", "kan", "tam", "tel", "sin", "bur", "khm", "lao",
    "vie", "ind", "tgl", "may", "may", "amh", None, "orm", "som", "swa",
    "kin", "run", "nya", "mlg", "epo",
    *([None] * 33),
    "wel", "baq", "cat", "lat", "que", "grn", "aym", "tat", "uig", "dzo", "jav",
)


class EmbeddedSubtitleProbeError(RuntimeError):
    pass


@dataclass(frozen=True)
class EmbeddedSubtitleMetadata:
    status: str
    languages: tuple[str | None, ...] = ()


@dataclass(frozen=True)
class _EbmlHeader:
    element_id: int
    data_offset: int
    data_size: int | None

    @property
    def end(self) -> int | None:
        return None if self.data_size is None else self.data_offset + self.data_size


@dataclass
class _ParseGuard:
    elements: int = 0

    def step(self) -> None:
        self.elements += 1
        if self.elements > MAX_PARSED_ELEMENTS:
            raise EmbeddedSubtitleProbeError("Container metadata has too many elements")


class BoundedRangeReader:
    def __init__(
        self,
        size: int,
        read_range: Callable[[int, int, float], bytes],
        *,
        max_bytes: int = MAX_PROBE_BYTES,
        timeout: float = PROBE_TIMEOUT_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ):
        if size <= 0:
            raise EmbeddedSubtitleProbeError("Video size is unavailable")
        self.size = size
        self._read_range = read_range
        self._max_bytes = max_bytes
        self._clock = clock
        self._deadline = clock() + timeout
        self._cache: dict[tuple[int, int], bytes] = {}
        self.bytes_read = 0
        self.requests = 0

    def read(self, offset: int, length: int) -> bytes:
        if length == 0:
            return b""
        if offset < 0 or length < 0 or offset + length > self.size:
            raise EmbeddedSubtitleProbeError("Container metadata points outside the video")
        chunks: list[bytes] = []
        cursor = offset
        remaining_length = length
        while remaining_length:
            chunk_length = min(remaining_length, RANGE_WINDOW_BYTES)
            chunks.append(self._read_chunk(cursor, chunk_length))
            cursor += chunk_length
            remaining_length -= chunk_length
        return b"".join(chunks)

    def _read_chunk(self, offset: int, length: int) -> bytes:
        cache_key = (offset, length)
        if cache_key in self._cache:
            return self._cache[cache_key]
        if self.bytes_read + length > self._max_bytes:
            raise EmbeddedSubtitleProbeError("Container metadata exceeds the probe byte budget")
        if self.requests >= MAX_RANGE_REQUESTS:
            raise EmbeddedSubtitleProbeError("Container metadata requires too many range requests")
        remaining = self._deadline - self._clock()
        if remaining <= 0:
            raise EmbeddedSubtitleProbeError("Container metadata probe timed out")

        self.requests += 1
        self.bytes_read += length
        try:
            data = self._read_range(offset, offset + length - 1, remaining)
        except EmbeddedSubtitleProbeError:
            raise
        except Exception as exc:
            raise EmbeddedSubtitleProbeError("Could not read container metadata") from exc
        if len(data) != length:
            raise EmbeddedSubtitleProbeError("Container range read was incomplete")
        if self._clock() > self._deadline:
            raise EmbeddedSubtitleProbeError("Container metadata probe timed out")
        self._cache[cache_key] = data
        return data


def normalize_declared_language(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip().replace("_", "-")
    if not value or value.casefold() == "und" or not _LANGUAGE_TAG.fullmatch(value):
        return None
    parts = value.split("-")
    normalized = [parts[0].lower()]
    for part in parts[1:]:
        if len(part) == 4 and part.isalpha():
            normalized.append(part.title())
        elif (len(part) == 2 and part.isalpha()) or (len(part) == 3 and part.isdigit()):
            normalized.append(part.upper())
        else:
            normalized.append(part.lower())
    return "-".join(normalized)


def _read_ebml_vint(reader: BoundedRangeReader, offset: int, *, identifier: bool) -> tuple[int | None, int]:
    first = reader.read(offset, 1)[0]
    if first == 0:
        raise EmbeddedSubtitleProbeError("Invalid EBML variable-length integer")
    mask = 0x80
    length = 1
    while not first & mask:
        mask >>= 1
        length += 1
    maximum = 4 if identifier else 8
    if length > maximum:
        raise EmbeddedSubtitleProbeError("Invalid EBML variable-length integer")
    raw = bytes((first,)) + reader.read(offset + 1, length - 1)
    if identifier:
        return int.from_bytes(raw, "big"), length
    value = first & (mask - 1)
    for byte in raw[1:]:
        value = (value << 8) | byte
    if value == (1 << (7 * length)) - 1:
        return None, length
    return value, length


def _read_ebml_header(reader: BoundedRangeReader, offset: int, guard: _ParseGuard) -> _EbmlHeader:
    guard.step()
    element_id, id_length = _read_ebml_vint(reader, offset, identifier=True)
    size, size_length = _read_ebml_vint(reader, offset + id_length, identifier=False)
    assert element_id is not None
    data_offset = offset + id_length + size_length
    if size is not None and data_offset + size > reader.size:
        raise EmbeddedSubtitleProbeError("EBML element extends past the video")
    return _EbmlHeader(element_id, data_offset, size)


def _memory_ebml_vint(data: bytes, offset: int, *, identifier: bool) -> tuple[int | None, int]:
    if offset >= len(data) or data[offset] == 0:
        raise EmbeddedSubtitleProbeError("Invalid EBML metadata")
    first = data[offset]
    mask = 0x80
    length = 1
    while not first & mask:
        mask >>= 1
        length += 1
    maximum = 4 if identifier else 8
    if length > maximum or offset + length > len(data):
        raise EmbeddedSubtitleProbeError("Truncated EBML metadata")
    raw = data[offset : offset + length]
    if identifier:
        return int.from_bytes(raw, "big"), length
    value = first & (mask - 1)
    for byte in raw[1:]:
        value = (value << 8) | byte
    if value == (1 << (7 * length)) - 1:
        return None, length
    return value, length


def _iter_memory_ebml(data: bytes, guard: _ParseGuard) -> Iterator[tuple[int, bytes]]:
    offset = 0
    while offset < len(data):
        guard.step()
        element_id, id_length = _memory_ebml_vint(data, offset, identifier=True)
        size, size_length = _memory_ebml_vint(data, offset + id_length, identifier=False)
        if size is None:
            raise EmbeddedSubtitleProbeError("Unexpected unknown-size EBML metadata element")
        start = offset + id_length + size_length
        end = start + size
        if end > len(data):
            raise EmbeddedSubtitleProbeError("Truncated EBML metadata element")
        assert element_id is not None
        yield element_id, data[start:end]
        offset = end


def _unsigned(data: bytes) -> int:
    if not data or len(data) > 8:
        raise EmbeddedSubtitleProbeError("Invalid unsigned metadata value")
    return int.from_bytes(data, "big")


def _seek_tracks_position(data: bytes, guard: _ParseGuard) -> int | None:
    for element_id, payload in _iter_memory_ebml(data, guard):
        if element_id != EBML_ID_SEEK:
            continue
        seek_id: bytes | None = None
        position: int | None = None
        for child_id, child in _iter_memory_ebml(payload, guard):
            if child_id == EBML_ID_SEEK_ID:
                seek_id = child
            elif child_id == EBML_ID_SEEK_POSITION:
                position = _unsigned(child)
        if seek_id == EBML_ID_TRACKS.to_bytes(4, "big") and position is not None:
            return position
    return None


def _parse_matroska_tracks(data: bytes, guard: _ParseGuard) -> tuple[str | None, ...]:
    languages: list[str | None] = []
    for element_id, payload in _iter_memory_ebml(data, guard):
        if element_id != EBML_ID_TRACK_ENTRY:
            continue
        track_type: int | None = None
        legacy: str | None = None
        bcp47: str | None = None
        bcp47_present = False
        for child_id, child in _iter_memory_ebml(payload, guard):
            if child_id == EBML_ID_TRACK_TYPE:
                track_type = _unsigned(child)
            elif child_id == EBML_ID_TRACK_LANGUAGE:
                try:
                    legacy = normalize_declared_language(child.decode("ascii"))
                except UnicodeDecodeError:
                    legacy = None
            elif child_id == EBML_ID_TRACK_LANGUAGE_BCP47:
                bcp47_present = True
                try:
                    bcp47 = normalize_declared_language(child.decode("ascii"))
                except UnicodeDecodeError:
                    bcp47 = None
        if track_type == MATROSKA_SUBTITLE_TRACK_TYPE:
            languages.append(bcp47 if bcp47_present else legacy)
    return tuple(languages)


def _parse_matroska(reader: BoundedRangeReader) -> tuple[str | None, ...]:
    guard = _ParseGuard()
    offset = 0
    segment: _EbmlHeader | None = None
    while offset < reader.size:
        header = _read_ebml_header(reader, offset, guard)
        if header.element_id == EBML_ID_SEGMENT:
            segment = header
            break
        if header.end is None:
            raise EmbeddedSubtitleProbeError("Unknown-size element before Matroska segment")
        offset = header.end
    if segment is None:
        raise EmbeddedSubtitleProbeError("Matroska segment is missing")

    segment_end = segment.end or reader.size
    cursor = segment.data_offset
    seek_tracks: int | None = None
    while cursor < segment_end:
        header = _read_ebml_header(reader, cursor, guard)
        if header.element_id == EBML_ID_TRACKS:
            if header.data_size is None:
                raise EmbeddedSubtitleProbeError("Matroska Tracks has unknown size")
            return _parse_matroska_tracks(reader.read(header.data_offset, header.data_size), guard)
        if header.element_id == EBML_ID_SEEK_HEAD and header.data_size is not None:
            seek_tracks = _seek_tracks_position(reader.read(header.data_offset, header.data_size), guard)
            if seek_tracks is not None:
                target = _read_ebml_header(reader, segment.data_offset + seek_tracks, guard)
                if target.element_id == EBML_ID_TRACKS and target.data_size is not None:
                    return _parse_matroska_tracks(reader.read(target.data_offset, target.data_size), guard)
        if header.element_id == EBML_ID_CLUSTER:
            break
        if header.end is None:
            raise EmbeddedSubtitleProbeError("Unknown-size Matroska metadata element")
        cursor = header.end
    raise EmbeddedSubtitleProbeError("Matroska Tracks metadata is unavailable")


def _box_header(data: bytes, offset: int, end: int) -> tuple[bytes, int, int]:
    if end - offset < 8:
        raise EmbeddedSubtitleProbeError("Truncated MP4 atom header")
    size32, atom_type = struct.unpack_from(">I4s", data, offset)
    header_size = 8
    if size32 == 1:
        if end - offset < 16:
            raise EmbeddedSubtitleProbeError("Truncated extended MP4 atom header")
        size = struct.unpack_from(">Q", data, offset + 8)[0]
        header_size = 16
    elif size32 == 0:
        size = end - offset
    else:
        size = size32
    if size < header_size or offset + size > end:
        raise EmbeddedSubtitleProbeError("Invalid MP4 atom size")
    return atom_type, offset + header_size, offset + size


def _iter_boxes(data: bytes, guard: _ParseGuard) -> Iterator[tuple[bytes, bytes]]:
    offset = 0
    while offset < len(data):
        if len(data) - offset < 8 and not any(data[offset:]):
            break
        guard.step()
        atom_type, payload_start, atom_end = _box_header(data, offset, len(data))
        yield atom_type, data[payload_start:atom_end]
        offset = atom_end


def _decode_mdhd_language(value: int) -> str | None:
    if value == 0x7FFF:
        return None
    if value < 0x400:
        return _QUICKTIME_LANGUAGES[value] if value < len(_QUICKTIME_LANGUAGES) else None
    characters = tuple(((value >> shift) & 0x1F) + 0x60 for shift in (10, 5, 0))
    if any(character < ord("a") or character > ord("z") for character in characters):
        return None
    return normalize_declared_language("".join(chr(character) for character in characters))


def _parse_mdhd(payload: bytes) -> str | None:
    if len(payload) < 4:
        raise EmbeddedSubtitleProbeError("Truncated MP4 media header")
    version = payload[0]
    language_offset = 20 if version == 0 else 32 if version == 1 else -1
    if language_offset < 0 or len(payload) < language_offset + 2:
        raise EmbeddedSubtitleProbeError("Unsupported or truncated MP4 media header")
    return _decode_mdhd_language(struct.unpack_from(">H", payload, language_offset)[0])


def _parse_elng(payload: bytes) -> str | None:
    if len(payload) < 4:
        raise EmbeddedSubtitleProbeError("Truncated MP4 extended language atom")
    encoded = payload[4:].split(b"\0", 1)[0]
    try:
        return normalize_declared_language(encoded.decode("utf-8"))
    except UnicodeDecodeError:
        return None


def _parse_mdia(payload: bytes, guard: _ParseGuard) -> tuple[bool, str | None]:
    handler: bytes | None = None
    legacy: str | None = None
    extended: str | None = None
    extended_present = False
    for atom_type, child in _iter_boxes(payload, guard):
        if atom_type == b"hdlr":
            if len(child) < 12:
                raise EmbeddedSubtitleProbeError("Truncated MP4 handler atom")
            handler = child[8:12]
        elif atom_type == b"mdhd":
            legacy = _parse_mdhd(child)
        elif atom_type == b"elng":
            extended_present = True
            extended = _parse_elng(child)
    return handler in SUBTITLE_HANDLER_TYPES, extended if extended_present else legacy


def _parse_mp4_moov(payload: bytes) -> tuple[str | None, ...]:
    guard = _ParseGuard()
    languages: list[str | None] = []
    for atom_type, track_payload in _iter_boxes(payload, guard):
        if atom_type != b"trak":
            continue
        for child_type, child in _iter_boxes(track_payload, guard):
            if child_type != b"mdia":
                continue
            subtitle, language = _parse_mdia(child, guard)
            if subtitle:
                languages.append(language)
            break
    return tuple(languages)


def _read_mp4_header(reader: BoundedRangeReader, offset: int) -> tuple[bytes, int, int]:
    initial = reader.read(offset, 8)
    size32, atom_type = struct.unpack(">I4s", initial)
    header_size = 8
    if size32 == 1:
        size = struct.unpack(">Q", reader.read(offset + 8, 8))[0]
        header_size = 16
    elif size32 == 0:
        size = reader.size - offset
    else:
        size = size32
    if size < header_size or offset + size > reader.size:
        raise EmbeddedSubtitleProbeError("Invalid MP4 atom size")
    return atom_type, offset + header_size, offset + size


def _parse_mp4(reader: BoundedRangeReader) -> tuple[str | None, ...]:
    offset = 0
    elements = 0
    while offset < reader.size:
        elements += 1
        if elements > MAX_PARSED_ELEMENTS:
            raise EmbeddedSubtitleProbeError("MP4 has too many top-level atoms")
        atom_type, payload_start, atom_end = _read_mp4_header(reader, offset)
        if atom_type == b"moov":
            return _parse_mp4_moov(reader.read(payload_start, atom_end - payload_start))
        offset = atom_end
    raise EmbeddedSubtitleProbeError("MP4 movie metadata is missing")


def probe_embedded_subtitles(
    path: str,
    size: int | None,
    read_range: Callable[[int, int, float], bytes],
) -> EmbeddedSubtitleMetadata:
    suffix = PurePosixPath(path).suffix.casefold()
    if suffix not in MATROSKA_EXTENSIONS | MP4_EXTENSIONS:
        return EmbeddedSubtitleMetadata("unsupported")
    if not size:
        return EmbeddedSubtitleMetadata("unavailable")
    try:
        reader = BoundedRangeReader(size, read_range)
        languages = _parse_matroska(reader) if suffix in MATROSKA_EXTENSIONS else _parse_mp4(reader)
        return EmbeddedSubtitleMetadata("available", languages)
    except (EmbeddedSubtitleProbeError, OSError, ValueError, struct.error):
        return EmbeddedSubtitleMetadata("unavailable")
