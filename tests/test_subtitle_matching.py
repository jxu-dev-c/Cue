"""Subtitle identity rules across independent movies, series, and API shapes."""

import unittest

import httpx

from backend.app import OpenSubtitles, PipelineError, guessit
from tests.test_app import config


def subtitle(title, *, episode=None, season=1, year=None, filename="subtitle.srt", release="", **attrs):
    details = {"feature_type": "Movie", "title": title, "year": year}
    if episode is not None:
        details.update(feature_type="Episode", parent_title=title, season_number=season, episode_number=episode)
    return {"attributes": {
        "language": "en", "release": release, "feature_details": details,
        "files": [{"file_id": 1, "file_name": filename}], **attrs,
    }}


class SubtitleMatchingTests(unittest.TestCase):
    def test_forced_only_results_are_not_full_dialogue_candidates(self):
        item = subtitle("Movie", year=2021, foreign_parts_only=True)
        self.assertIsNone(OpenSubtitles._candidate(item))

    def test_title_evidence_and_identity_matrix(self):
        cases = [
            # Movie title aliases from either release field, with year checks.
            ("Spirited.Away.2001.mkv", subtitle("Sen to Chihiro no Kamikakushi", year=2001,
             filename="Spirited.Away.2001.srt"), True),
            ("Spirited.Away.2001.mkv", subtitle("Sen to Chihiro no Kamikakushi", year="2001",
             release="Spirited.Away.2001.BluRay"), True),
            ("Spirited.Away.2001.mkv", subtitle("Sen to Chihiro no Kamikakushi", year=2002,
             filename="Spirited.Away.2001.srt"), False),
            # Canonical identity still works without a recognizable release.
            ("Alien.1979.mkv", subtitle("Alien", year=1979), True),
            ("Alien.1979.mkv", subtitle("Alien", year=None), False),
            # Explicit release metadata can fill missing catalogue fields.
            ("Alien.1979.mkv", subtitle("Alien", filename="Alien.1979.srt"), True),
            ("Money.Heist.S02E03.mkv", subtitle("La Casa de Papel", episode=3, season=2,
             filename="Money.Heist.S02E03.srt"), True),
            ("Money.Heist.S02E03.mkv", subtitle("La Casa de Papel", episode="3", season="2",
             release="Money.Heist.S02E03"), True),
            ("Money.Heist.S02E03.mkv", subtitle("La Casa de Papel", episode=3, season=2), False),
            ("Money.Heist.S02E03.mkv", subtitle("La Casa de Papel", episode=4, season=2,
             filename="Money.Heist.S02E03.srt"), False),
            # Even a canonical title cannot override a conflicting release.
            ("Dark.S02E03.mkv", subtitle("Dark", episode=3, season=2,
             filename="Dark.S02E04.srt"), False),
            ("Dark.S02E03.mkv", subtitle("Dark", episode=3, season=2,
             filename="Dark.S02E03E04.srt"), False),
            # Remakes, sequels, substrings and movie/episode collisions.
            ("Dune.2021.mkv", subtitle("Dune", year=1984), False),
            ("Alien.1979.mkv", subtitle("Alien 3", year=1979), False),
            ("It.2017.mkv", subtitle("Little Women", year=2017), False),
            ("Dark.S02E03.mkv", subtitle("Dark", year=2019), False),
            ("Dark.2019.mkv", subtitle("Dark", episode=3, year=2019), False),
            # Do not invent a season for absolute anime numbering.
            ("Example.Show.29.mkv", subtitle("Example Show", episode=29, season=2), False),
        ]
        for video, item, expected in cases:
            with self.subTest(video=video, item=item):
                self.assertEqual(OpenSubtitles._metadata_match(item, guessit(video)), expected)

    def test_explicit_release_can_fill_missing_feature_details(self):
        item = subtitle("", feature_details=None, filename="Dark.S02E03.srt")
        self.assertTrue(OpenSubtitles._metadata_match(item, guessit("Dark.S02E03.mkv")))
        item["attributes"]["files"][0]["file_name"] = "Dark - 03.srt"
        self.assertFalse(OpenSubtitles._metadata_match(item, guessit("Dark.S02E03.mkv")))

    def test_second_file_cannot_validate_the_file_being_downloaded(self):
        item = subtitle("La Casa de Papel", episode=3, season=2)
        item["attributes"]["files"].append({"file_id": 2, "file_name": "Money.Heist.S02E03.srt"})
        self.assertFalse(OpenSubtitles._metadata_match(item, guessit("Money.Heist.S02E03.mkv")))

    def test_catalogue_aliases_use_the_exact_movie_or_parent_series_id(self):
        for kind in ("movie", "episode"):
            with self.subTest(kind=kind):
                if kind == "movie":
                    video = "Spirited.Away.2001.mkv"
                    item = subtitle("Sen to Chihiro no Kamikakushi", year=2001)
                    alias = "Spirited Away"
                    key = "feature_id"
                else:
                    video = "Money.Heist.S02E03.mkv"
                    item = subtitle("La Casa de Papel", episode=3, season=2)
                    alias = "Money Heist"
                    key = "parent_feature_id"
                item["attributes"]["feature_details"][key] = 42
                requests = []

                def handler(request):
                    requests.append(request)
                    if request.url.path.endswith("features"):
                        self.assertEqual(request.url.params["feature_id"], "42")
                        return httpx.Response(200, json={"data": [{"attributes": {
                            "feature_id": "42", "title_aka": [alias],
                        }}]})
                    results = [] if "moviehash" in request.url.params else [item, item]
                    return httpx.Response(200, json={"data": results})

                client = httpx.Client(base_url="https://api.opensubtitles.com/api/v1/",
                                      transport=httpx.MockTransport(handler))
                selected = OpenSubtitles(config(), client).find(video, "1234567890123456", ("en",))
                self.assertEqual(selected.file_id, 1)
                self.assertFalse(selected.moviehash_match)
                self.assertEqual(sum(r.url.path.endswith("features") for r in requests), 1)

    def test_alias_response_cannot_substitute_an_unrelated_feature_id(self):
        client = httpx.Client(base_url="https://api.opensubtitles.com/api/v1/",
            transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"data": [{
                "attributes": {"feature_id": 99, "title_aka": ["Dark"]},
            }]})))
        self.assertEqual(OpenSubtitles(config(), client)._feature_titles(42), ())

    def test_catalogue_alias_keeps_requested_language_ahead_of_english(self):
        english = subtitle("Money Heist", episode=3, season=2)
        spanish = subtitle("La Casa de Papel", episode=3, season=2, language="es")
        spanish["attributes"]["feature_details"]["parent_feature_id"] = 42
        spanish["attributes"]["files"][0]["file_id"] = 2
        service = OpenSubtitles(config())
        service._search = lambda params: [] if "moviehash" in params else [english, spanish]
        service._feature_titles = lambda _: ("Money Heist",)
        self.assertEqual(service.find("Money.Heist.S02E03.mkv", "1234567890123456", ("es", "en")).file_id, 2)

    def test_catalogue_alias_cannot_override_wrong_episode(self):
        item = subtitle("La Casa de Papel", episode=4, season=2)
        item["attributes"]["feature_details"]["parent_feature_id"] = 42
        service = OpenSubtitles(config())
        service._search = lambda params: [] if "moviehash" in params else [item]
        service._feature_titles = lambda _: self.fail("Wrong episodes must be rejected before alias lookup")
        with self.assertLogs("uvicorn.error", level="WARNING"):
            with self.assertRaises(PipelineError):
                service.find("Money.Heist.S02E03.mkv", "1234567890123456", ("en",))

    def test_hash_match_keeps_priority_and_does_not_require_title_lookup(self):
        item = subtitle("Unrecognized title", moviehash_match=True)
        service = OpenSubtitles(config())
        service._search = lambda _: [item]
        service._feature_titles = lambda _: self.fail("Hash matches must not require title lookup")
        self.assertTrue(service.find("Dark.S01E01.mkv", "1234567890123456", ("en",)).moviehash_match)

    def test_failure_reports_returned_and_matched_counts(self):
        for results, expected in [([], "returned 0"), ([subtitle("Unrelated", year=2021)], "returned 1")]:
            with self.subTest(expected=expected):
                service = OpenSubtitles(config())
                service._search = lambda _: results
                with self.assertLogs("uvicorn.error", level="WARNING"):
                    with self.assertRaisesRegex(PipelineError, expected):
                        service.find("Dune.2021.mkv", "1234567890123456", ("en",))


if __name__ == "__main__":
    unittest.main()
