import json
import struct
import threading
import time
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

import backend.app as backend_app
from backend.app import FileEntry
from backend.embedded_metadata import (
    BoundedRangeReader,
    EmbeddedSubtitleProbeError,
    normalize_declared_language,
    probe_embedded_subtitles,
)


def ebml_size(value: int) -> bytes:
    for length in range(1, 9):
        if value < (1 << (7 * length)) - 1:
            encoded = value | (1 << (7 * length))
            return encoded.to_bytes(length, "big")
    raise ValueError("EBML fixture is too large")


def ebml_element(element_id: int, payload: bytes) -> bytes:
    identifier = element_id.to_bytes((element_id.bit_length() + 7) // 8, "big")
    return identifier + ebml_size(len(payload)) + payload


def matroska_track(track_type: int, legacy: str | None = None, bcp47: str | None = None) -> bytes:
    payload = ebml_element(0x83, bytes((track_type,)))
    if legacy is not None:
        payload += ebml_element(0x22B59C, legacy.encode("ascii"))
    if bcp47 is not None:
        payload += ebml_element(0x22B59D, bcp47.encode("ascii"))
    return ebml_element(0xAE, payload)


def mp4_box(atom_type: bytes, payload: bytes, *, extended: bool = False) -> bytes:
    if extended:
        return struct.pack(">I4sQ", 1, atom_type, len(payload) + 16) + payload
    return struct.pack(">I4s", len(payload) + 8, atom_type) + payload


def packed_language(code: str) -> int:
    return sum((ord(character) - 0x60) << shift for character, shift in zip(code, (10, 5, 0)))


def mdhd(language: int, *, version: int = 0) -> bytes:
    if version == 0:
        payload = b"\0\0\0\0" + struct.pack(">IIIIHH", 0, 0, 1000, 1000, language, 0)
    else:
        payload = b"\1\0\0\0" + struct.pack(">QQIQHH", 0, 0, 1000, 1000, language, 0)
    return mp4_box(b"mdhd", payload)


def mp4_track(handler: bytes, language: int | None = None, elng: str | None = None, *, version: int = 0) -> bytes:
    media = mp4_box(b"hdlr", b"\0" * 8 + handler)
    if language is not None:
        media += mdhd(language, version=version)
    if elng is not None:
        media += mp4_box(b"elng", b"\0\0\0\0" + elng.encode("utf-8") + b"\0")
    return mp4_box(b"trak", mp4_box(b"mdia", media))


class RecordingRanges:
    def __init__(self, data: bytes):
        self.data = data
        self.requests: list[tuple[int, int, float]] = []

    def __call__(self, start: int, end: int, timeout: float) -> bytes:
        self.requests.append((start, end, timeout))
        return self.data[start : end + 1]


class FakeMediaSource:
    def __init__(self, entries: list[FileEntry], files: dict[str, bytes]):
        self.entries = entries
        self.files = files
        self.read_calls = 0

    def list(self, relative: str, refresh: bool = False) -> list[FileEntry]:
        del relative, refresh
        return self.entries

    def read_range(
        self,
        relative: str,
        start: int,
        end: int,
        timeout: float | None = None,
    ) -> bytes:
        del timeout
        self.read_calls += 1
        return self.files[relative][start : end + 1]


class EmbeddedSubtitleParserTests(unittest.TestCase):
    def test_matroska_preserves_tracks_and_prefers_bcp47(self):
        tracks = ebml_element(
            0x1654AE6B,
            matroska_track(2, "eng")
            + matroska_track(17, "eng", "en-US")
            + matroska_track(17, "fra")
            + matroska_track(17, "fra")
            + matroska_track(17),
        )
        video = ebml_element(0x18538067, tracks + ebml_element(0x1F43B675, b"video payload"))
        ranges = RecordingRanges(video)

        result = probe_embedded_subtitles("Movie.mkv", len(video), ranges)

        self.assertEqual(result.status, "available")
        self.assertEqual(result.languages, ("en-US", "fra", "fra", None))
        self.assertTrue(all(end - start + 1 <= 256 * 1024 for start, end, _ in ranges.requests))

    def test_matroska_uses_seek_head_to_find_tracks_after_cluster(self):
        tracks = ebml_element(0x1654AE6B, matroska_track(17, "jpn"))
        cluster = ebml_element(0x1F43B675, b"not metadata")
        seek_id = ebml_element(0x53AB, (0x1654AE6B).to_bytes(4, "big"))
        placeholder = ebml_element(0x4DBB, seek_id + ebml_element(0x53AC, b"\0\0\0\0"))
        seek_head = ebml_element(0x114D9B74, placeholder)
        position = len(seek_head) + len(cluster)
        seek = ebml_element(0x4DBB, seek_id + ebml_element(0x53AC, position.to_bytes(4, "big")))
        seek_head = ebml_element(0x114D9B74, seek)
        video = ebml_element(0x18538067, seek_head + cluster + tracks)

        result = probe_embedded_subtitles("Movie.webm", len(video), RecordingRanges(video))

        self.assertEqual(result.languages, ("jpn",))

    def test_mp4_skips_mdat_and_prefers_elng(self):
        ftyp = mp4_box(b"ftyp", b"isom")
        mdat = mp4_box(b"mdat", b"cue and video payload" * 200)
        moov = mp4_box(
            b"moov",
            mp4_track(b"vide", packed_language("eng"))
            + mp4_track(b"sbtl", packed_language("eng"), "zh_Hans")
            + mp4_track(b"text", packed_language("fra"), version=1)
            + mp4_track(b"clcp", None),
            extended=True,
        )
        video = ftyp + mdat + moov
        ranges = RecordingRanges(video)

        result = probe_embedded_subtitles("Movie.mp4", len(video), ranges)

        self.assertEqual(result.status, "available")
        self.assertEqual(result.languages, ("zh-Hans", "fra", None))
        mdat_payload_start = len(ftyp) + 8
        mdat_payload_end = len(ftyp) + len(mdat) - 1
        self.assertFalse(any(
            start <= mdat_payload_end and end >= mdat_payload_start
            for start, end, _ in ranges.requests
        ))

    def test_mp4_decodes_legacy_quicktime_language(self):
        video = mp4_box(b"moov", mp4_track(b"subt", 0))
        result = probe_embedded_subtitles("Movie.mov", len(video), RecordingRanges(video))
        self.assertEqual(result.languages, ("eng",))

    def test_unknown_unsupported_and_malformed_results_are_explicit(self):
        calls = []
        unsupported = probe_embedded_subtitles("Movie.avi", 100, lambda *args: calls.append(args) or b"")
        malformed_bytes = struct.pack(">I4s", 1_000, b"moov")
        malformed = probe_embedded_subtitles(
            "Movie.mp4",
            len(malformed_bytes),
            RecordingRanges(malformed_bytes),
        )
        self.assertEqual(unsupported.status, "unsupported")
        self.assertEqual(malformed.status, "unavailable")
        self.assertEqual(calls, [])

    def test_normalizes_declared_codes_without_adding_subtags(self):
        self.assertEqual(normalize_declared_language(" zh_hans "), "zh-Hans")
        self.assertEqual(normalize_declared_language("fre-ca"), "fre-CA")
        self.assertIsNone(normalize_declared_language("und"))
        self.assertIsNone(normalize_declared_language("not a language"))

    def test_range_reader_enforces_budget_and_deadline(self):
        calls = []
        reader = BoundedRangeReader(10, lambda *args: calls.append(args) or b"12345", max_bytes=4)
        with self.assertRaisesRegex(EmbeddedSubtitleProbeError, "byte budget"):
            reader.read(0, 5)
        self.assertEqual(calls, [])

        times = iter((0.0, 0.0, 3.0))
        timed = BoundedRangeReader(2, lambda *_: b"12", clock=lambda: next(times))
        with self.assertRaisesRegex(EmbeddedSubtitleProbeError, "timed out"):
            timed.read(0, 2)

    def test_range_reader_splits_and_caches_bounded_windows(self):
        data = b"x" * (300 * 1024)
        ranges = RecordingRanges(data)
        reader = BoundedRangeReader(len(data), ranges)

        self.assertEqual(reader.read(0, len(data)), data)
        first_request_count = len(ranges.requests)
        self.assertEqual(reader.read(0, len(data)), data)

        self.assertEqual(first_request_count, 2)
        self.assertEqual(len(ranges.requests), first_request_count)
        self.assertTrue(all(end - start + 1 <= 256 * 1024 for start, end, _ in ranges.requests))


class EmbeddedSubtitleApiTests(unittest.TestCase):
    def setUp(self):
        backend_app.clear_embedded_metadata_cache()

    def _wait_for_cache(self):
        deadline = time.monotonic() + 1
        while backend_app.EMBEDDED_METADATA_INFLIGHT and time.monotonic() < deadline:
            time.sleep(0.001)

    def test_probe_cache_uses_fingerprint_and_refresh_bypasses_it(self):
        video = mp4_box(b"moov", mp4_track(b"subt", packed_language("eng")))
        first_entry = FileEntry("Movie.mp4", "Movie.mp4", "video", len(video), "one")
        source = FakeMediaSource([first_entry], {"Movie.mp4": video})

        backend_app.request_embedded_metadata(source, first_entry).result(timeout=1)
        self._wait_for_cache()
        first_reads = source.read_calls
        backend_app.request_embedded_metadata(source, first_entry).result(timeout=1)
        self.assertEqual(source.read_calls, first_reads)

        changed = FileEntry("Movie.mp4", "Movie.mp4", "video", len(video), "two")
        backend_app.request_embedded_metadata(source, changed).result(timeout=1)
        self._wait_for_cache()
        self.assertGreater(source.read_calls, first_reads)
        changed_reads = source.read_calls

        backend_app.request_embedded_metadata(source, changed, refresh=True).result(timeout=1)
        self._wait_for_cache()
        self.assertGreater(source.read_calls, changed_reads)

    def test_identical_inflight_probes_are_coalesced(self):
        video = mp4_box(b"moov", mp4_track(b"subt", packed_language("eng")))
        entry = FileEntry("Movie.mp4", "Movie.mp4", "video", len(video), "one")
        started = threading.Event()
        release = threading.Event()

        class BlockingSource(FakeMediaSource):
            def read_range(self, relative, start, end, timeout=None):
                started.set()
                release.wait(timeout=1)
                return super().read_range(relative, start, end, timeout)

        source = BlockingSource([entry], {"Movie.mp4": video})
        first = backend_app.request_embedded_metadata(source, entry)
        self.assertTrue(started.wait(timeout=1))
        second = backend_app.request_embedded_metadata(source, entry)
        self.assertIs(first, second)
        release.set()
        self.assertEqual(first.result(timeout=1).languages, ("eng",))

    def test_stream_reports_each_video_and_isolates_probe_failures(self):
        mp4 = mp4_box(b"moov", mp4_track(b"sbtl", packed_language("fra")))
        entries = [
            FileEntry("Good.mp4", "Good.mp4", "video", len(mp4), "now"),
            FileEntry("Broken.mkv", "Broken.mkv", "video", 4, "now"),
            FileEntry("Legacy.avi", "Legacy.avi", "video", 100, "now"),
            FileEntry("Folder", "Folder", "directory"),
        ]
        source = FakeMediaSource(entries, {
            "Good.mp4": mp4,
            "Broken.mkv": b"nope",
            "Legacy.avi": b"x" * 100,
        })

        with patch("backend.app.require_services", return_value=(None, source, None, None)):
            response = TestClient(backend_app.app, base_url="http://127.0.0.1:3666").get("/api/files/embedded-subtitles")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["content-type"], "application/x-ndjson")
        records = {item["path"]: item for item in map(json.loads, response.text.splitlines())}
        self.assertEqual(records["Good.mp4"]["languages"], ["fra"])
        self.assertEqual(records["Broken.mkv"]["status"], "unavailable")
        self.assertEqual(records["Legacy.avi"]["status"], "unsupported")
        self.assertNotIn("Folder", records)


if __name__ == "__main__":
    unittest.main()
