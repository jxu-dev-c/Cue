import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import srt

from backend.media_range import MediaReadError
from backend.subtitle_sync import read_cues, select_audio_stream, sync_remote, estimate_offset, subtitle_activity


def reference_cues(seed=174):
    rng=np.random.default_rng(seed)
    cues=[]
    start=12.0
    while start<280:
        end=start+rng.uniform(.3,2)
        cues.append(srt.Subtitle(len(cues)+1,timedelta(seconds=start),timedelta(seconds=end),"Dialogue"))
        start=end+rng.uniform(.3,1.8)
    return cues


class SubtitleSyncTests(unittest.TestCase):
    def test_known_offsets_are_estimated_without_external_sync_engine(self):
        reference=reference_cues()
        observations=[(start,subtitle_activity(reference,start,800))for start in (30,140,250)]
        for delay in (0,3.4,-2.6):
            with self.subTest(delay=delay):
                cues=[srt.Subtitle(c.index,c.start-timedelta(seconds=delay),
                                  c.end-timedelta(seconds=delay),c.content)for c in reference]
                offset,_,_=estimate_offset(cues,observations)
                self.assertAlmostEqual(offset,delay,places=2)

    def test_ambiguous_match_is_allowed_as_best_effort(self):
        cues=[srt.Subtitle(i,timedelta(seconds=100+i*2),timedelta(seconds=101+i*2),"Repeated")
              for i in range(150)]
        observations=[(start,subtitle_activity(cues,start,800))for start in (130,240,350)]
        offset,reason,score=estimate_offset(cues,observations)
        self.assertTrue(-60<=offset<=60)
        self.assertIn("inaccurate",reason)
        self.assertIsNotNone(score)

    def test_silence_preserves_original_timing(self):
        offset,reason,score=estimate_offset(reference_cues(),[(30,np.zeros(800))])
        self.assertEqual(offset,0)
        self.assertIn("original timing",reason)
        self.assertIsNone(score)

    def test_negative_shift_cannot_discard_opening_cues(self):
        cues=reference_cues()
        observations=[(start,subtitle_activity(cues,start+30,800))for start in (30,140,220)]
        offset,reason,_=estimate_offset(cues,observations)
        self.assertEqual(offset,0)
        self.assertIn("opening cues",reason)

    def test_result_is_labelled_approximate_and_preserves_all_content(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            source=root/"input.srt"
            source.write_text(srt.compose(reference_cues()))
            with patch("backend.subtitle_sync.subprocess.run",return_value=SimpleNamespace(
                    returncode=0,stdout=b'{"streams":[{"codec_type":"audio"}]}')) as process, \
                 patch("backend.subtitle_sync.extract_audio",return_value=b"audio") as extract, \
                 patch("backend.subtitle_sync.speech_activity",return_value=np.zeros(800)):
                result=sync_remote("video",source,root/"out.srt")
            self.assertTrue(result["approximate"])
            self.assertEqual(result["method"],"short-sample")
            self.assertEqual(read_cues(source),read_cues(root/"out.srt"))
            self.assertEqual(process.call_args.args[0][0],"ffprobe")
            self.assertEqual(process.call_count,1)
            for call in extract.call_args_list:
                self.assertEqual(call.kwargs,{"duration":8,"pad_timeline":False,"stream":"0:a:0"})

    def test_decoder_failure_does_not_silently_use_partial_audio(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            source=root/"input.srt";source.write_text(srt.compose(reference_cues()))
            with patch("backend.subtitle_sync.subprocess.run",return_value=SimpleNamespace(
                    returncode=0,stdout=b'{"streams":[{"codec_type":"audio"}]}')), \
                 patch("backend.subtitle_sync.extract_audio",side_effect=MediaReadError("incomplete")):
                with self.assertRaisesRegex(MediaReadError,"incomplete"):
                    sync_remote("video",source,root/"out.srt")
            self.assertFalse((root/"out.srt").exists())

    def test_audio_selection_excludes_commentary(self):
        streams=[{"codec_type":"audio","tags":{"title":"Commentary"},"disposition":{"default":1}},
                 {"codec_type":"audio","tags":{"title":"Dialogue"},"disposition":{"default":1}}]
        self.assertEqual(select_audio_stream(streams),"a:1")

    def test_legacy_subtitle_encoding_is_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/"in.srt"
            path.write_bytes("1\n00:00:01,000 --> 00:00:02,000\nПривет, как дела?\n".encode("cp1251"))
            self.assertEqual(read_cues(path)[0].content,"Привет, как дела?")
