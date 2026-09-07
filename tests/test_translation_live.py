"""Opt-in alignment checks against the configured AI endpoint; consumes API tokens.

RUN_TRANSLATION_LIVE=1 uv run python -m unittest tests.test_translation_live

These checks detect specific phrases moving between cues. They are not a
general translation-quality score. Inspect the printed translations as well.
"""

import json
import os
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

import srt

from backend.app import Config, translate_srt


# Sentence fragments from the reported failure. Required/forbidden concepts
# detect the observed reordering without requiring an exact Chinese wording.
CASES = [
    ("I want to know\nwhat you would have gone on to say", ["说|讲"], ["兴趣"]),
    ("if they hadn't had lost interest.", ["兴趣"], ["说|讲"]),
    ("We don't wanna needlessly antagonize\nthe current regime", ["政权|政府"], ["批评|讨好|示好"]),
    ("by cozying up to one of its major critics.", ["批评"], ["激怒|无谓"]),
    ("Also has the added distinction\nof being the only other human being,", ["唯一|仅有", "另"], ["迪基|波波夫|见过|已故"]),
    ("apart from the late Dickie Bough,", ["迪基|狄基", "除"], ["波波夫|见过"]),
    ("who claims to have seen Alexander Popov.", ["波波夫", "见"], ["迪基|狄基"]),
    ('You know, "cicada" was a... a term\nfor a sleeper', ["蝉", "潜伏|沉睡"], ["柏林|墙"]),
    ("on the dark side of the Wall.", ["墙"], ["蝉|潜伏|沉睡"]),
    ("We're checking the room for hidden microphones.", ["窃听|麦克风|话筒"], []),
    ("You can come on the bug sweep.\nBut no guns.", ["窃听|监听", "枪"], ["虫"]),
    ("You keep asking about the suspect.", ["嫌疑"], []),
    ("A copper?", ["警察|警员|条子"], ["铜"]),
]


# Fresh examples from travel, everyday dialogue, and software operations.
# Kept separate from the episode-specific regression cases.
FRESH_CASES = [
    ("The train leaves at seven.", ["七|7"], []),
    ("We need platform four.", ["四|4", "站台|月台"], []),
    ("I left the tickets", ["票"], ["厨房|餐桌"]),
    ("on the kitchen table.", ["厨房|餐桌"], []),
    ("Don't restart the server", ["重启|重新启动", "服务器"], ["备份"]),
    ("until the backup finishes.", ["备份", "完成|结束"], ["重启|重新启动"]),
    ("The report was approved by Maria.", ["玛丽亚|玛莉亚", "报告"], ["丹尼尔"]),
    ("Not by Daniel.", ["丹尼尔", "不"], ["玛丽亚|玛莉亚"]),
    ("- Are you coming?\n- In five minutes.", ["五|5", "分钟"], []),
    ("[door closes]", ["门", "关"], []),
    ("I can lend you the blue coat,", ["蓝", "外套|大衣"], ["红"]),
    ("but the red one belongs to my sister.", ["红", "姐姐|妹妹|姐妹"], ["蓝"]),
    ("Please don't delete", ["删除|删掉"], ["Archive|归档|存档"]),
    ("the folder named Archive.", ["Archive|归档|存档", "文件夹|目录"], ["删除|删掉"]),
    ("I'm not angry.", ["生气|愤怒"], ["疲惫|累"]),
    ("I'm just tired.", ["疲惫|累"], ["生气|愤怒"]),
]


@unittest.skipUnless(os.environ.get("RUN_TRANSLATION_LIVE") == "1", "live translation evaluation is opt-in")
class LiveTranslationAlignmentTests(unittest.TestCase):
    def test_reported_alignment_cases(self):
        self.check_cases(CASES)

    def test_fresh_alignment_cases(self):
        self.check_cases(FRESH_CASES)

    def check_cases(self, cases):
        config = Config.load()
        originals = [
            srt.Subtitle(index=i + 1, start=timedelta(seconds=3 * i),
                         end=timedelta(seconds=3 * i + 2), content=case[0])
            for i, case in enumerate(cases)
        ]
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.srt"
            output = Path(directory) / "translated.srt"
            source.write_text(srt.compose(originals, reindex=False), encoding="utf-8")
            for run in range(3):
                usage = translate_srt(source, output, config, target_language="zh-cn", subtitle_mode="target")
                translated = list(srt.parse(output.read_text(encoding="utf-8")))
                print(json.dumps({"run": run + 1, "model": config.openai_model_id, "usage": usage,
                                  "translations": [cue.content for cue in translated]}, ensure_ascii=False), flush=True)
                self.assertEqual(len(translated), len(originals))
                for before, after, (_, required, forbidden) in zip(originals, translated, cases):
                    with self.subTest(run=run + 1, cue=before.index):
                        self.assertEqual((before.index, before.start, before.end), (after.index, after.start, after.end))
                        for pattern in required:
                            self.assertRegex(after.content, pattern)
                        for pattern in forbidden:
                            self.assertNotRegex(after.content, pattern)


if __name__ == "__main__":
    unittest.main()
