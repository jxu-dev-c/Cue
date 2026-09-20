import io
import subprocess
import tempfile
import unittest
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from backend.audio_sample import extract_audio, sample_start, SAMPLE_RATE, SAMPLE_SECONDS, EXTRACT_TIMEOUT
from backend.media_range import MediaReadError


class AudioSampleTests(unittest.TestCase):
    def test_audio_stays_in_ram_and_seek_keeps_original_timeline(self):
        pcm = b"\x10\x01" * SAMPLE_RATE * SAMPLE_SECONDS
        with patch("backend.audio_sample.shutil.which", return_value="ffmpeg"), \
             patch("backend.audio_sample.subprocess.run", return_value=SimpleNamespace(returncode=0, stdout=pcm)), \
             patch("tempfile.TemporaryDirectory", side_effect=AssertionError("No media directories")), \
             patch.object(Path, "write_bytes", side_effect=AssertionError("No media writes")):
            data = extract_audio("http://127.0.0.1/video", 85)
        with wave.open(io.BytesIO(data), "rb") as audio:
            self.assertEqual(audio.getframerate(), SAMPLE_RATE)
            self.assertEqual(audio.getnchannels(), 1)
            self.assertEqual(audio.readframes(85 * SAMPLE_RATE), b"\0" * (85 * SAMPLE_RATE * 2))
            self.assertEqual(audio.readframes(SAMPLE_RATE * SAMPLE_SECONDS), pcm)

    def test_sampling_timeout_does_not_retry_or_download_more(self):
        with patch("backend.audio_sample.shutil.which", return_value="ffmpeg"), \
             patch("backend.audio_sample.subprocess.run", side_effect=subprocess.TimeoutExpired("ffmpeg", EXTRACT_TIMEOUT)) as run:
            with self.assertRaisesRegex(MediaReadError, f"{EXTRACT_TIMEOUT} seconds"):
                extract_audio("http://127.0.0.1/video")
            self.assertEqual(run.call_count, 1)

    def test_successful_decoder_exit_with_truncated_audio_is_rejected(self):
        result = SimpleNamespace(returncode=0, stdout=b"x" * (SAMPLE_RATE * 2 * 3))
        with patch("backend.audio_sample.shutil.which", return_value="ffmpeg"), \
             patch("backend.audio_sample.subprocess.run", return_value=result):
            with self.assertRaisesRegex(MediaReadError, "incomplete.*3.0 of 15 seconds"):
                extract_audio("http://127.0.0.1/video")

    def test_late_sample_does_not_fabricate_silent_audio(self):
        pcm = b"\x10\x01" * SAMPLE_RATE * 8
        with patch("backend.audio_sample.shutil.which", return_value="ffmpeg"), \
             patch("backend.audio_sample.subprocess.run", return_value=SimpleNamespace(returncode=0, stdout=pcm)):
            data = extract_audio("http://127.0.0.1/video", 3600, duration=8, pad_timeline=False)
        with wave.open(io.BytesIO(data), "rb") as audio:
            self.assertEqual(audio.getnframes(), SAMPLE_RATE*8)
            self.assertEqual(audio.readframes(audio.getnframes()), pcm)

    def test_intro_seek_uses_first_cue_and_is_bounded(self):
        with tempfile.TemporaryDirectory() as directory:
            subtitle = Path(directory) / "in.srt"
            for timestamp, expected in (("00:00:02", 0), ("00:01:30", 85), ("00:10:00", 180)):
                subtitle.write_text(f"1\n{timestamp},000 --> 00:11:00,000\nDialogue\n\n")
                self.assertEqual(sample_start(subtitle), expected)
            subtitle.write_text("invalid or non-SRT subtitle")
            self.assertEqual(sample_start(subtitle), 0)

    def test_failed_or_oversized_decode_is_not_used_as_reference(self):
        for result in (SimpleNamespace(returncode=1, stdout=b"x" * 16000),
                       SimpleNamespace(returncode=0, stdout=b""),
                       SimpleNamespace(returncode=0, stdout=b"x" * (SAMPLE_RATE * SAMPLE_SECONDS * 2 + 2))):
            with self.subTest(result_code=result.returncode), \
                 patch("backend.audio_sample.shutil.which", return_value="ffmpeg"), \
                 patch("backend.audio_sample.subprocess.run", return_value=result):
                with self.assertRaises(MediaReadError):
                    extract_audio("http://127.0.0.1/video")
