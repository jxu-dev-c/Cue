import os
import io
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
import wave
from datetime import timedelta
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from backend.app import FileEntry, PipelineError, WebDAV
from backend.audio_sample import extract_audio
from backend.media_range import memory_media
from backend.subtitle_sync import sync_remote
from tests.test_subtitle_sync import subtitle_activity
import numpy as np
import srt
from tests.test_app import config


@unittest.skipUnless(os.getenv("RUN_MEDIA_INTEGRATION") == "1", "set RUN_MEDIA_INTEGRATION=1 for FFmpeg smoke test")
class MediaIntegrationTest(unittest.TestCase):
    def test_real_decoder_and_vad_recover_known_timing_without_losing_cues(self):
        rng = np.random.default_rng(8021)
        cues = []
        start = 12.0
        while start < 175:
            end = start + rng.uniform(.35, 1.7)
            cues.append(srt.Subtitle(len(cues)+1, timedelta(seconds=start),
                                    timedelta(seconds=end), "Dialogue"))
            start = end + rng.uniform(.3, 1.6)
        mask = np.repeat(subtitle_activity(cues, 0, 18000), 80)
        times = np.arange(len(mask))/8000
        pcm = (mask*(np.sin(2*np.pi*180*times)+.5*np.sin(2*np.pi*360*times)
                     +.25*np.sin(2*np.pi*720*times))*12000).astype('<i2')
        wav = io.BytesIO()
        with wave.open(wav, 'wb') as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(8000)
            audio.writeframes(pcm.tobytes())
        with tempfile.TemporaryDirectory() as directory, memory_media(wav.getvalue()) as url:
            source = Path(directory)/'input.srt'
            output = Path(directory)/'output.srt'
            for delay in (0.0, 3.4, -2.6):
                with self.subTest(delay=delay):
                    shifted = [srt.Subtitle(c.index, c.start-timedelta(seconds=delay),
                                           c.end-timedelta(seconds=delay), c.content) for c in cues]
                    source.write_text(srt.compose(shifted))
                    sync_remote(url, source, output)
                    result = list(srt.parse(output.read_text()))
                    self.assertEqual(len(result), len(cues))
                    for actual, expected in zip(result, cues):
                        self.assertEqual(actual.content, expected.content)
                        self.assertLess(abs((actual.start-expected.start).total_seconds()), .3)

    def test_remote_sample_matches_local_without_full_transfer(self):
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            self.skipTest("FFmpeg is required")

        def run(*args):
            return subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", *args],
                                  check=True, capture_output=True, timeout=60).stdout

        with tempfile.TemporaryDirectory(prefix="subtitle-media-test-") as directory:
            root = Path(directory)
            original = root / "tail-index.mp4"
            shifted = root / "shifted.srt"
            shifted.write_text("1\n00:00:02,000 --> 00:00:04,000\nTest dialogue\n\n"
                               "2\n00:09:50,000 --> 00:09:52,000\nLate dialogue\n\n", encoding="utf-8")
            # A ten-minute container with its MP4 index at the end exercises
            # backward seeks. Nontrivial video data makes transfer assertions
            # meaningful; a tiny black video would fit in a single cache block.
            run("-f", "lavfi", "-i", "testsrc2=s=320x180:r=10:d=600",
                "-f", "lavfi", "-i", "sine=frequency=220:sample_rate=16000:duration=600",
                "-c:v", "mpeg4", "-q:v", "3", "-c:a", "aac", "-shortest", str(original))
            faststart = root / "faststart.mp4"
            embedded = root / "embedded.mkv"
            run("-i", str(original), "-c", "copy", "-movflags", "+faststart", str(faststart))
            run("-i", str(original), "-i", str(shifted), "-map", "0", "-map", "1", "-c", "copy", str(embedded))
            high_bitrate = root / "high-bitrate.mkv"
            # Uncompressed frames make a reproducible >32 MiB audio interval,
            # independently of any particular show, codec release, or network.
            run("-f", "lavfi", "-i", "testsrc2=s=384x216:r=25:d=120",
                "-f", "lavfi", "-i", "sine=frequency=220:sample_rate=16000:duration=120",
                "-c:v", "rawvideo", "-allow_raw_vfw", "1", "-c:a", "flac", "-shortest", str(high_bitrate))

            class RangeHandler(BaseHTTPRequestHandler):
                transferred = 0
                full_gets = 0
                ranges = []

                def log_message(self, *_):
                    pass

                def do_GET(self):
                    video = root / self.path.lstrip("/")
                    size = video.stat().st_size
                    header = self.headers.get("Range")
                    if not header:
                        RangeHandler.full_gets += 1
                        self.send_error(400)
                        return
                    start, end = map(int, header[6:].split("-"))
                    RangeHandler.ranges.append((start, end))
                    self.send_response(206)
                    self.send_header("Accept-Ranges", "bytes")
                    self.send_header("Content-Length", str(end - start + 1))
                    self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
                    self.end_headers()
                    with video.open("rb") as media:
                        media.seek(start)
                        data = media.read(end - start + 1)
                    RangeHandler.transferred += len(data)
                    self.wfile.write(data)

            server = ThreadingHTTPServer(("127.0.0.1", 0), RangeHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            cfg = replace(config(), webdav_endpoint=f"http://127.0.0.1:{server.server_port}/", webdav_scan_path="")
            source = WebDAV(cfg)
            try:
                for video in (original, faststart, embedded, high_bitrate):
                    with self.subTest(container=video.name):
                        RangeHandler.transferred = RangeHandler.full_gets = 0
                        RangeHandler.ranges = []
                        # Force the same audio reference locally; the MKV also
                        # contains subtitles which must not trigger a full scan.
                        local_audio = extract_audio(str(video))
                        info = FileEntry(video.name, video.name, "video", video.stat().st_size)
                        with patch.object(source, "file_info", return_value=info):
                            started = time.monotonic()
                            with source.sync_input(video.name) as url:
                                remote_audio = extract_audio(url)
                                elapsed = time.monotonic() - started
                                # Transport integrity is distinct from alignment
                                # correctness; equal outputs from two invocations
                                # of an aligner are not proof of correct timing.
                                self.assertEqual(remote_audio, local_audio)
                        self.assertEqual(RangeHandler.full_gets, 0)
                        self.assertEqual(len(RangeHandler.ranges), len(set(RangeHandler.ranges)))
                        # Includes bounded speculative reads during seeks; the
                        # contractual limit is one quarter of the source.
                        self.assertLessEqual(RangeHandler.transferred, video.stat().st_size // 4)
                        if video == high_bitrate:
                            self.assertGreater(RangeHandler.transferred, 32 * 1024 * 1024)
                        print(f"\n{video.name}: {elapsed:.2f}s, transferred {RangeHandler.transferred:,} / {video.stat().st_size:,} bytes")
                # A subtitle-guided seek must preserve AAC delay and the
                # original timeline, not merely work when sampling from zero.
                info = FileEntry(original.name, original.name, "video", original.stat().st_size)
                with patch.object(source, "file_info", return_value=info):
                    with source.sync_input(original.name) as url:
                        self.assertEqual(extract_audio(url, 85), extract_audio(str(original), 85))
                # Exercise the limit through actual FFmpeg requests, including
                # decoder probing after a stream is cut short.
                RangeHandler.transferred = RangeHandler.full_gets = 0
                limit = 2 * 1024 * 1024
                info = FileEntry(original.name, original.name, "video", original.stat().st_size)
                with patch("backend.media_range.MAX_SYNC_BYTES", limit), patch.object(source, "file_info", return_value=info):
                    with self.assertRaisesRegex(PipelineError, "transfer limit"):
                        with source.sync_input(original.name) as url:
                            extract_audio(url)
                self.assertLessEqual(RangeHandler.transferred, limit)
                self.assertEqual(RangeHandler.full_gets, 0)
            finally:
                source.client.close()
                source.media_client.close()
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
