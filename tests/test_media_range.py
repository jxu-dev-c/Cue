import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import httpx

from backend.app import FileEntry, PipelineError, WebDAV
from backend.media_range import MediaReadError, RangeCache, remote_media, serve_media
from tests.test_app import config


class MediaRangeTests(unittest.TestCase):
    def setUp(self):
        self.data = bytes(range(256)) * 16
        self.requests = []

    def open_range(self, headers):
        self.requests.append(headers)
        self.assertEqual(headers["Accept-Encoding"], "identity")
        start, end = map(int, headers["Range"][6:].split("-"))
        return httpx.Response(206, headers={"Content-Range": f"bytes {start}-{end}/{len(self.data)}"},
                              content=self.data[start:end + 1])

    def test_http_seeks_head_and_reopens_reuse_cache_and_cleanup(self):
        with patch("backend.media_range.BLOCK_BYTES", 1024), httpx.Client(trust_env=False) as client:
            with serve_media(RangeCache(len(self.data), self.open_range), ".mp4") as url:
                response = client.head(url)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(int(response.headers["content-length"]), len(self.data))
                self.assertEqual(self.requests, [])
                self.assertEqual(client.get(url + "wrong").status_code, 404)
                for header, expected in (("bytes=2040-2060", self.data[2040:2061]),
                                         ("bytes=2048-2050", self.data[2048:2051]),
                                         ("bytes=-10", self.data[-10:]),
                                         ("bytes=4090-", self.data[4090:])):
                    response = client.get(url, headers={"Range": header})
                    self.assertEqual(response.status_code, 206)
                    self.assertEqual(response.content, expected)
                self.assertEqual(len(self.requests), 3)
                for header in ("bytes=4096-", "bytes=12-3", "bytes=-0", "bytes=", "bytes=0-1,4-5"):
                    self.assertEqual(client.get(url, headers={"Range": header}).status_code, 416)
            with self.assertRaises(httpx.ConnectError):
                client.get(url)

    def test_budget_is_shared_across_requests_and_failures_reach_job(self):
        with patch("backend.media_range.BLOCK_BYTES", 1024), patch("backend.media_range.MAX_SYNC_BYTES", 2048):
            with httpx.Client(trust_env=False) as client, self.assertRaisesRegex(MediaReadError, "transfer limit"):
                with serve_media(RangeCache(len(self.data), self.open_range), ".mkv") as url:
                    for start in (0, 0, 1024):
                        self.assertEqual(client.get(url, headers={"Range": f"bytes={start}-{start}"}).status_code, 206)
                    self.assertEqual(client.get(url, headers={"Range": "bytes=2048-2048"}).status_code, 502)
                    self.assertEqual(len(self.requests), 2)

    def test_simultaneous_probes_fetch_each_block_once(self):
        with tempfile.TemporaryDirectory() as directory, patch("backend.media_range.BLOCK_BYTES", 1024):
            cache = RangeCache(len(self.data), self.open_range)
            with ThreadPoolExecutor(max_workers=4) as executor:
                results = list(executor.map(cache.block, [0] * 8))
            self.assertEqual(results, [self.data[:1024]] * 8)
            self.assertEqual(cache.fetched, 1024)
            self.assertEqual(len(self.requests), 1)

    def test_invalid_responses_are_closed_without_retry_or_full_download(self):
        class TrackedStream(httpx.SyncByteStream):
            read = False
            closed = False

            def __iter__(self):
                self.read = True
                yield b"x" * 1024

            def close(self):
                self.closed = True

        for status, headers, message in (
            (200, {}, "full download refused"),
            (429, {}, "HTTP 429"),
            (503, {}, "HTTP 503"),
            (206, {"Content-Range": "bytes 1-1024/4096"}, "invalid media byte range"),
            (206, {"Content-Range": "bytes 0-1023/4096", "Content-Encoding": "gzip"}, "encoded media"),
            (206, {"Content-Range": "bytes 0-1023/4096", "Content-Length": "1025"}, "range length"),
        ):
            with self.subTest(status=status, headers=headers), tempfile.TemporaryDirectory() as directory:
                stream = TrackedStream()
                with patch("backend.media_range.BLOCK_BYTES", 1024):
                    cache = RangeCache(4096, lambda _: httpx.Response(status, headers=headers, stream=stream))
                    with self.assertRaisesRegex(MediaReadError, message):
                        cache.block(0)
                self.assertTrue(stream.closed)
                self.assertFalse(stream.read)

    def test_truncated_and_oversized_bodies_are_not_cached(self):
        for length in (1023, 1025):
            with self.subTest(length=length), tempfile.TemporaryDirectory() as directory:
                with patch("backend.media_range.BLOCK_BYTES", 1024):
                    cache = RangeCache(4096, lambda _: httpx.Response(
                        206, headers={"Content-Range": "bytes 0-1023/4096"},
                        stream=httpx.ByteStream(b"x" * length)))
                    with self.assertRaisesRegex(MediaReadError, "incomplete|oversized"):
                        cache.block(0)
                    self.assertEqual(cache.blocks, {})

    def test_webdav_surfaces_range_failure_instead_of_generic_sync_failure(self):
        upstream = httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, content=b"ignored")))
        source = WebDAV(config(), upstream, upstream)
        try:
            with patch("backend.media_range.BLOCK_BYTES", 1024), patch.object(source, "file_info", return_value=FileEntry("v.mp4", "v.mp4", "video", 4096)):
                with httpx.Client(trust_env=False) as client, self.assertRaisesRegex(PipelineError, "full download refused"):
                    with source.sync_input("v.mp4") as url:
                        self.assertEqual(client.get(url, headers={"Range": "bytes=0-1"}).status_code, 502)
                        raise PipelineError("Subtitle synchronization failed")
        finally:
            upstream.close()

    def test_media_cache_never_writes_files_even_on_consumer_failure(self):
        with patch("tempfile.TemporaryDirectory", side_effect=AssertionError("No media temp directory")), \
             patch.object(Path, "write_bytes", side_effect=AssertionError("No media disk writes")), \
             httpx.Client(trust_env=False) as client:
            with self.assertRaisesRegex(RuntimeError, "consumer failed"):
                with serve_media(RangeCache(len(self.data), self.open_range), ".mp4") as url:
                    self.assertEqual(client.get(url, headers={"Range": "bytes=0-1"}).status_code, 206)
                    raise RuntimeError("consumer failed")

    def test_evicted_blocks_count_against_transfer_budget(self):
        with patch("backend.media_range.BLOCK_BYTES", 1024), patch("backend.media_range.MAX_CACHE_BYTES", 1024):
            cache = RangeCache(len(self.data), self.open_range, max_bytes=3072)
            for index in (0, 1, 0):
                cache.block(index)
                self.assertLessEqual(cache.cached_bytes, 1024)
            self.assertEqual(cache.fetched, 3072)
            with self.assertRaisesRegex(MediaReadError, "transfer limit"):
                cache.block(1)
            self.assertEqual(len(self.requests), 3)

    def test_small_video_cannot_be_downloaded_in_full(self):
        with patch("backend.media_range.BLOCK_BYTES", 1024), httpx.Client(trust_env=False) as client:
            with self.assertRaisesRegex(MediaReadError, "transfer limit"):
                with remote_media(len(self.data), ".mp4", self.open_range) as url:
                    self.assertEqual(client.get(url, headers={"Range": "bytes=0-0"}).status_code, 206)
                    self.assertEqual(client.get(url, headers={"Range": "bytes=1024-1024"}).status_code, 502)
            self.assertEqual(len(self.requests), 1)

    def test_signed_media_url_is_reused_without_forwarding_credentials(self):
        initial_requests = []
        media_requests = []
        size = 16 * 1024 * 1024

        def initial(request):
            initial_requests.append(request)
            return httpx.Response(302, headers={"Location": "https://cdn.example.test/video?token=private"})

        def media(request):
            media_requests.append(request)
            self.assertNotIn("authorization", request.headers)
            start, end = map(int, request.headers["Range"][6:].split("-"))
            return httpx.Response(206, headers={"Content-Range": f"bytes {start}-{end}/{size}"},
                                  content=b"x" * (end - start + 1))

        with httpx.Client(auth=("user", "pass"), transport=httpx.MockTransport(initial)) as initial_client, \
             httpx.Client(transport=httpx.MockTransport(media)) as media_client, \
             httpx.Client(trust_env=False) as local_client:
            source = WebDAV(config(), initial_client, media_client)
            with patch.object(source, "file_info", return_value=FileEntry("v.mkv", "v.mkv", "video", size)):
                with source.sync_input("v.mkv") as url:
                    for offset in (0, 1048576, 0):
                        response = local_client.get(url, headers={"Range": f"bytes={offset}-{offset}"})
                        self.assertEqual(response.content, b"x")
            self.assertEqual(len(initial_requests), 1)
            self.assertEqual(len(media_requests), 4)  # One demanded block plus three read-ahead blocks.

    def test_parallel_reads_overlap_and_reserve_budget_before_fetching(self):
        import threading

        barrier = threading.Barrier(4)
        ranges = []

        def fetch(headers):
            ranges.append(headers["Range"])
            barrier.wait(timeout=2)
            return self.open_range(headers)

        with patch("backend.media_range.BLOCK_BYTES", 1024):
            cache = RangeCache(len(self.data), fetch, max_bytes=4096, prefetch=True)
            try:
                self.assertEqual(cache.block(0), self.data[:1024])
                for index in (1, 2, 3):
                    self.assertEqual(cache.block(index), self.data[index * 1024:(index + 1) * 1024])
                self.assertEqual(cache.fetched, 4096)
                self.assertEqual(len(ranges), 4)
            finally:
                cache.executor.shutdown(wait=True)
