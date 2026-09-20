"""Short-lived, bounded HTTP access to original remote media for FFmpeg."""

from __future__ import annotations

import logging
import re
import secrets
import threading
import time
from collections import OrderedDict
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Iterator

import httpx

BLOCK_BYTES = 1024 * 1024
# A short sample from a high-bitrate remux still includes interleaved video
# and container headers. Keep the network budget separate from the RAM cache.
MAX_SYNC_BYTES = 256 * 1024 * 1024
MAX_CACHE_BYTES = 16 * 1024 * 1024
REMOTE_SECONDS = 120
LOGGER = logging.getLogger("uvicorn.error")


class MediaReadError(RuntimeError):
    pass


class RangeCache:
    def __init__(self, size: int, open_range: Callable[[dict[str, str]], httpx.Response], *, max_bytes: int | None = None, prefetch: bool = False, block_bytes: int | None = None):
        self.size = size
        self.block_bytes = BLOCK_BYTES if block_bytes is None else block_bytes
        self.blocks: OrderedDict[int, bytes] = OrderedDict()
        self.cached_bytes = 0
        self.max_bytes = MAX_SYNC_BYTES if max_bytes is None else max_bytes
        self.open_range = open_range
        self.fetched = 0
        self.reserved = 0
        self.pending: dict[int, Future[bytes]] = {}
        self.executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="media-range") if prefetch else None
        self.error: MediaReadError | None = None
        self.lock = threading.Lock()
        self.stopped = threading.Event()
        self.deadline = time.monotonic() + REMOTE_SECONDS

    def block(self, index: int) -> bytes:
        if not self.executor:
            return self._block(index)
        with self.lock:
            if self.stopped.is_set():
                raise MediaReadError("Remote synchronization ended")
            if self.error:
                raise self.error
            if time.monotonic() > self.deadline:
                self.error = MediaReadError("Remote synchronization timed out")
                raise self.error
            if index in self.blocks:
                self.blocks.move_to_end(index)
                return self.blocks[index]
            # Reserve the entire requested range before scheduling any I/O.
            # Optional read-ahead never triggers retries or exceeds the budget.
            for candidate in range(index, min(index + 4, (self.size + self.block_bytes - 1) // self.block_bytes)):
                if candidate in self.blocks or candidate in self.pending:
                    continue
                expected = min(self.block_bytes, self.size - candidate * self.block_bytes)
                if self.reserved + expected > self.max_bytes or len(self.pending) >= 4:
                    break
                self.reserved += expected
                self.pending[candidate] = self.executor.submit(self._fetch, candidate)
            future = self.pending.get(index)
        if future is None:
            # A seek may leave speculative requests in flight. Retire them
            # before scheduling the demanded range, without re-fetching bytes.
            with self.lock:
                waiting = list(self.pending.items())
            if waiting:
                for candidate, pending in waiting:
                    try:
                        data = pending.result()
                    except Exception:
                        data = None
                    with self.lock:
                        self.pending.pop(candidate, None)
                        if data is not None:
                            self._remember(candidate, data)
                return self.block(index)
            self.error = MediaReadError(
                f"Remote synchronization reached its {self.max_bytes / (1024 * 1024):g} MiB "
                "transfer limit; use a local video or an exact-match subtitle"
            )
            raise self.error
        try:
            data = future.result()
        except Exception as exc:
            self.error = exc if isinstance(exc, MediaReadError) else MediaReadError("Could not read the remote audio sample")
            if self.error is exc:
                raise
            raise self.error from exc
        with self.lock:
            self.pending.pop(index, None)
            self._remember(index, data)
        return data

    def _remember(self, index: int, data: bytes) -> None:
        if index in self.blocks:
            return
        # Reserve space for up to four completed/in-flight prefetch blocks.
        cache_limit = max(self.block_bytes, MAX_CACHE_BYTES - (4 * self.block_bytes if self.executor else 0))
        while self.blocks and self.cached_bytes + len(data) > cache_limit:
            _, discarded = self.blocks.popitem(last=False)
            self.cached_bytes -= len(discarded)
        self.blocks[index] = data
        self.cached_bytes += len(data)

    def _fetch(self, index: int) -> bytes:
        # Reuse the bounded response validation, but keep network I/O outside
        # the parent cache lock so four independent ranges can overlap.
        reader = RangeCache(self.size, self.open_range, max_bytes=self.block_bytes, block_bytes=self.block_bytes)
        reader.deadline = self.deadline
        try:
            return reader._block(index)
        finally:
            with self.lock:
                self.fetched += reader.fetched

    def _block(self, index: int) -> bytes:
        # Serialize cache misses so concurrent probes share bytes and a budget.
        with self.lock:
            if self.stopped.is_set():
                raise MediaReadError("Remote synchronization ended")
            if self.error:
                raise self.error
            try:
                if time.monotonic() > self.deadline:
                    raise MediaReadError("Remote synchronization timed out")
                if index in self.blocks:
                    self.blocks.move_to_end(index)
                    return self.blocks[index]
                start = index * self.block_bytes
                end = min(start + self.block_bytes, self.size) - 1
                expected = end - start + 1
                if self.fetched + expected > self.max_bytes:
                    raise MediaReadError(
                        f"Remote synchronization reached its {self.max_bytes / (1024 * 1024):g} MiB "
                        "transfer limit; use a local video or an exact-match subtitle"
                    )
                response = self.open_range({
                    "Range": f"bytes={start}-{end}",
                    "Accept-Encoding": "identity",
                })
                try:
                    if response.status_code != 206:
                        if response.status_code == 200:
                            raise MediaReadError("WebDAV server does not support required byte ranges; full download refused")
                        raise MediaReadError(f"Remote media range request failed (HTTP {response.status_code})")
                    if response.headers.get("content-range") != f"bytes {start}-{end}/{self.size}":
                        raise MediaReadError("WebDAV returned an invalid media byte range")
                    if response.headers.get("content-encoding", "identity").lower() != "identity":
                        raise MediaReadError("WebDAV returned encoded media bytes")
                    length = response.headers.get("content-length")
                    if length is not None and int(length) != expected:
                        raise MediaReadError("WebDAV returned an invalid media range length")
                    data = bytearray()
                    for chunk in response.iter_bytes(chunk_size=64 * 1024):
                        self.fetched += len(chunk)
                        if len(data) + len(chunk) > expected:
                            raise MediaReadError("WebDAV returned an oversized media byte range")
                        if time.monotonic() > self.deadline:
                            raise MediaReadError("Remote synchronization timed out")
                        data.extend(chunk)
                    if len(data) != expected:
                        raise MediaReadError("WebDAV returned an incomplete media byte range")
                    block = bytes(data)
                    self._remember(index, block)
                    return block
                finally:
                    response.close()
            except Exception as exc:
                self.error = exc if isinstance(exc, MediaReadError) else MediaReadError("Could not read or cache the remote video")
                if self.error is exc:
                    raise
                raise self.error from exc


@contextmanager
def remote_media(size: int, suffix: str, open_range: Callable[[dict[str, str]], httpx.Response]) -> Iterator[str]:
    # Never permit synchronization to fetch the entire source, even for short
    # videos. Container probing and rereads share this same transfer budget.
    cache = RangeCache(size, open_range, max_bytes=min(MAX_SYNC_BYTES, size // 4), prefetch=True)
    started = time.monotonic()
    try:
        with serve_media(cache, suffix) as url:
            yield url
    finally:
        LOGGER.info("remote_sync bytes=%d cache_bytes=%d elapsed=%.2fs", cache.fetched,
                    cache.cached_bytes, time.monotonic() - started)


@contextmanager
def memory_media(data: bytes, suffix: str = ".wav") -> Iterator[str]:
    """Expose in-memory media to a decoder without writing media to disk."""
    def read(headers: dict[str, str]) -> httpx.Response:
        start, end = map(int, headers["Range"][6:].split("-"))
        return httpx.Response(206, headers={"Content-Range": f"bytes {start}-{end}/{len(data)}"},
                              content=data[start:end + 1])
    cache = RangeCache(len(data), read)
    with serve_media(cache, suffix) as url:
        yield url


@contextmanager
def serve_media(cache: RangeCache, suffix: str) -> Iterator[str]:
    """Serve a token-protected loopback URL without the app's HTTP server."""
    size = cache.size
    route = "/" + secrets.token_hex(24) + suffix

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def setup(self):
            super().setup()
            self.connection.settimeout(5)

        def do_HEAD(self):
            self.handle_media(False)

        def do_GET(self):
            self.handle_media(True)

        def handle_media(self, body: bool):
            try:
                self.serve(body)
            except OSError:
                self.close_connection = True

        def serve(self, body: bool):
            if self.path != route:
                self.send_error(404)
                return
            start, end = 0, size - 1
            header = self.headers.get("Range")
            if header:
                match = re.fullmatch(r"bytes=(\d{0,20})-(\d{0,20})", header)
                if not match or not any(match.groups()):
                    self.send_error(416)
                    return
                first, last = match.groups()
                if first:
                    start = int(first)
                    end = min(int(last), end) if last else end
                else:
                    start = max(0, size - int(last))
                if start > end or start >= size:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.end_headers()
                    return
            try:
                # Validate/fetch the first block before sending a success status.
                data = cache.block(start // cache.block_bytes) if body else b""
            except MediaReadError:
                self.send_error(502)
                return
            self.send_response(206 if header else 200)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(end - start + 1))
            if header:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.end_headers()
            if not body:
                return
            try:
                while start <= end and not cache.stopped.is_set():
                    length = min(len(data) - start % cache.block_bytes, end - start + 1)
                    self.wfile.write(data[start % cache.block_bytes : start % cache.block_bytes + length])
                    start += length
                    if start <= end:
                        data = cache.block(start // cache.block_bytes)
            except (OSError, MediaReadError):
                # FFmpeg closes requests when seeking or finishing its sample.
                # Cache errors are retained and raised to the job, even if
                # a decoder treats a truncated response as usable audio.
                self.close_connection = True

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = False
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}{route}"
    finally:
        cache.stopped.set()
        if cache.executor:
            cache.executor.shutdown(wait=True, cancel_futures=True)
        server.shutdown()
        server.server_close()
        thread.join()
        if cache.error:
            raise cache.error
