import json
import os
import shutil
import struct
import tempfile
import unittest
from contextlib import contextmanager
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import srt

from backend.app import (
    Config,
    FileEntry,
    JOBS,
    JOBS_LOCK,
    Job,
    JobItem,
    JobRequest,
    LocalStorage,
    OpenSubtitles,
    PipelineError,
    RenameRequest,
    SettingsRequest,
    SubtitleCandidate,
    TARGET_LANGUAGES,
    WebDAV,
    apply_subtitle_font_size,
    apply_unix_permissions,
    calculate_moviehash,
    choose_output_path,
    detect_sidecar_language,
    detect_sidecar_language_from_name,
    normalize_relative,
    numbered_output_path,
    process_video,
    create_job,
    get_settings,
    local_folders,
    files,
    open_credentials_file,
    open_local_folder,
    require_services,
    rename_files,
    run_job,
    sync_subtitle,
    subtitle_font_size,
    batch_cues,
    translate_srt,
    update_job,
    update_settings,
    app,
)
from fastapi import HTTPException
from fastapi.testclient import TestClient


def config() -> Config:
    return Config(
        webdav_username="user",
        webdav_password="pass",
        webdav_endpoint="https://example.test/dav/",
        webdav_scan_path="Media Library",
        opensubtitles_api_key="key",
        opensubtitles_consumer_name="tests",
        openai_base_url="https://ai.example.test/v1/",
        openai_api_key="ai-key",
        openai_model_id="gpt-5.6-luna",
        openai_reasoning_effort="low",
        openai_rename_reasoning_effort="low",
        target_language="zh-cn",
        default_subtitle_mode="bilingual",
    )


SRT = b"1\n00:00:01,000 --> 00:00:03,000\nHello there.\n\n"


class CoreTests(unittest.TestCase):
    def test_unix_permissions_warn_and_skip_on_windows(self):
        path = Path("config.json")
        with (
            patch("backend.app.os.name", "nt"),
            patch("backend.app.os.chmod") as chmod,
            self.assertLogs("uvicorn.error", level="WARNING") as logs,
        ):
            apply_unix_permissions(path, 0o600)

        chmod.assert_not_called()
        self.assertIn("Skipping Unix permissions 600", logs.output[0])

    def test_local_folder_browser_lists_only_visible_directories(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            (root / "Media & TV").mkdir()
            (root / "empty").mkdir()
            (root / ".hidden").mkdir()
            (root / "video.mp4").touch()
            result = local_folders(str(root))
            self.assertEqual(result["path"], str(root))
            self.assertEqual(result["parent"], str(root.parent))
            self.assertEqual([folder["name"] for folder in result["folders"]], ["empty", "Media & TV"])
            self.assertEqual(local_folders(str(root / "empty"))["folders"], [])
            for invalid in ("relative", str(root / "missing"), str(root / "video.mp4")):
                with self.assertRaises(HTTPException) as caught:
                    local_folders(invalid)
                self.assertEqual(caught.exception.status_code, 400)
            with patch("backend.app.Path.home", return_value=root):
                self.assertEqual(local_folders("")["path"], str(root))

    def test_setup_is_required_only_for_empty_configuration(self):
        with tempfile.TemporaryDirectory() as directory, (
            patch("backend.app.CONFIG_PATH", Path(directory) / "config.json")
        ), patch("backend.app.ENV_PATH", Path(directory) / ".env"):
            self.assertTrue(get_settings()["setup_required"])
            (Path(directory) / "config.json").write_text("{}")
            self.assertTrue(get_settings()["setup_required"])
            (Path(directory) / "config.json").write_text(json.dumps({"local_scan_path": directory}))
            self.assertFalse(get_settings()["setup_required"])
            (Path(directory) / "config.json").write_text(json.dumps({"webdav_endpoint": "https://example.test"}))
            self.assertFalse(get_settings()["setup_required"])
            (Path(directory) / "config.json").write_text("{}")
            (Path(directory) / ".env").write_text("OPENAI_API_KEY=legacy-key\n")
            self.assertFalse(get_settings()["setup_required"])

    def test_settings_save_secrets_to_config_and_remove_legacy_env(self):
        values = {
            "webdav_username": "user",
            "webdav_endpoint": "https://example.test/dav",
            "webdav_scan_path": "Media",
            "opensubtitles_consumer_name": "tests",
            "opensubtitles_username": "subtitle-user",
            "openai_base_url": "https://ai.example.test/v1",
            "openai_model_id": "model",
            "openai_reasoning_effort": "low",
            "openai_rename_reasoning_effort": "medium",
            "target_language": "zh-cn",
            "default_subtitle_mode": "bilingual",
        }
        secrets = {
            "webdav_password": "webdav-secret",
            "opensubtitles_api_key": "subtitle-key",
            "opensubtitles_password": "subtitle-secret",
            "openai_api_key": "ai-secret",
        }
        with (
            tempfile.TemporaryDirectory() as directory,
            patch("backend.app.CONFIG_DIR", Path(directory)),
            patch("backend.app.CONFIG_PATH", Path(directory) / "config.json"),
            patch("backend.app.ENV_PATH", Path(directory) / ".env"),
        ):
            (Path(directory) / ".env").write_text("OPENAI_API_KEY=legacy-key\nUNRELATED=value\n")
            defaults = get_settings()["values"]
            self.assertEqual(defaults["target_language"], "zh-cn")
            self.assertEqual(defaults["default_subtitle_mode"], "bilingual")
            self.assertEqual(defaults["openai_rename_reasoning_effort"], "low")
            self.assertTrue(get_settings()["secrets"]["openai_api_key"])
            result = update_settings(SettingsRequest(values=values, secrets=secrets))
            self.assertEqual(result, {"saved": True, "message": "Settings saved and applied."})
            active_config, active_source, active_destination, active_opensubtitles = require_services()
            self.assertEqual(active_config.webdav_scan_path, "Media")
            self.assertIsInstance(active_source, WebDAV)
            self.assertIs(active_destination, active_source)
            self.assertIsInstance(active_opensubtitles, OpenSubtitles)
            response = get_settings()
            self.assertFalse(response["setup_required"])
            self.assertEqual(response["values"], {
                "source_type": "webdav",
                "subtitle_destination": "source",
                "local_scan_path": "",
                "local_output_path": "",
            } | values)
            self.assertEqual(len(response["options"]["target_languages"]), 20)
            self.assertEqual(response["options"]["subtitle_modes"][-1], {"value": "bilingual", "label": "English & target language"})
            self.assertTrue(all(response["secrets"].values()))
            config_text = (Path(directory) / "config.json").read_text()
            saved_config = json.loads(config_text)
            self.assertEqual(saved_config["webdav_password"], "webdav-secret")
            self.assertEqual(saved_config["opensubtitles_api_key"], "subtitle-key")
            self.assertEqual(saved_config["opensubtitles_password"], "subtitle-secret")
            self.assertEqual(saved_config["openai_api_key"], "ai-secret")
            self.assertNotIn("openai_api_key", response["values"])
            if os.name != "nt":
                self.assertEqual((Path(directory) / "config.json").stat().st_mode & 0o777, 0o600)
            env_text = (Path(directory) / ".env").read_text()
            self.assertNotIn("OPENAI_API_KEY", env_text)
            self.assertIn("UNRELATED=value", env_text)
            if os.name != "nt":
                self.assertEqual((Path(directory) / ".env").stat().st_mode & 0o777, 0o600)

            update_settings(SettingsRequest(values=values, clear_secrets=["opensubtitles_password"]))
            self.assertNotIn("opensubtitles_password", json.loads((Path(directory) / "config.json").read_text()))
            with self.assertRaises(HTTPException) as raised:
                update_settings(SettingsRequest(values=values | {"unknown": "value"}))
            self.assertEqual(raised.exception.status_code, 400)
            with self.assertRaises(HTTPException) as raised:
                update_settings(SettingsRequest(values=values | {"target_language": "klingon"}))
            self.assertEqual(raised.exception.status_code, 400)
            with self.assertRaises(HTTPException) as raised:
                update_settings(SettingsRequest(values=values | {"default_subtitle_mode": "invalid"}))
            self.assertEqual(raised.exception.status_code, 400)
            with self.assertRaises(HTTPException) as raised:
                update_settings(SettingsRequest(values=values | {"openai_rename_reasoning_effort": "invalid"}))
            self.assertEqual(raised.exception.status_code, 400)

    def test_legacy_settings_use_existing_reasoning_effort_for_rename_jobs(self):
        with tempfile.TemporaryDirectory() as directory, patch(
            "backend.app.CONFIG_PATH", Path(directory) / "config.json"
        ) as config_path:
            config_path.write_text(json.dumps({"openai_reasoning_effort": "high"}), encoding="utf-8")
            values = get_settings()["values"]

        self.assertEqual(values["openai_reasoning_effort"], "high")
        self.assertEqual(values["openai_rename_reasoning_effort"], "high")

    def test_paths_cannot_escape_scan_root(self):
        self.assertEqual(normalize_relative("Shows/Series/Episode.mkv"), "Shows/Series/Episode.mkv")
        for value in ("../secret", "Shows/../secret", "/etc/passwd", "C:\\secret", "%2e%2e/secret"):
            with self.subTest(value=value), self.assertRaises(PipelineError):
                normalize_relative(value)

    def test_service_urls_reject_embedded_credentials_and_malformed_authorities(self):
        values = config().__dict__
        for setting in ("webdav_endpoint", "openai_base_url"):
            for value in (
                "https://user:password@example.test/",
                "https://example.test:invalid/",
                "https://example.test:99999/",
                "https://[invalid/",
                "https://example.test/?api_key=secret",
                "https://example.test/#secret",
                "https://example.test/\npath",
                "file:///etc/passwd",
            ):
                with self.subTest(setting=setting, value=value), self.assertRaises(PipelineError):
                    Config.from_settings(values | {setting: value}, values)
        saved = Config.from_settings(values | {"openai_base_url": "http://127.0.0.1:8000/v1"}, values)
        self.assertEqual(saved.openai_base_url, "http://127.0.0.1:8000/v1/")
        with tempfile.TemporaryDirectory() as directory, self.assertRaises(PipelineError):
            Config.from_settings(values | {
                "source_type": "local",
                "local_scan_path": directory,
                "webdav_endpoint": "https://user:password@example.test/",
            }, values)

    def test_local_configuration_does_not_require_webdav(self):
        with tempfile.TemporaryDirectory() as directory:
            values = {
                "source_type": "local",
                "subtitle_destination": "source",
                "local_scan_path": directory,
                "local_output_path": "",
                "webdav_username": "",
                "webdav_endpoint": "",
                "webdav_scan_path": "",
                "opensubtitles_consumer_name": "tests",
                "opensubtitles_username": "",
                "openai_base_url": "https://ai.example.test/v1",
                "openai_model_id": "model",
                "openai_reasoning_effort": "low",
                "openai_rename_reasoning_effort": "medium",
                "target_language": "zh-cn",
                "default_subtitle_mode": "target",
            }
            local = Config.from_settings(values, {"opensubtitles_api_key": "key", "openai_api_key": "ai"})
            self.assertEqual(local.source_type, "local")
            self.assertEqual(local.local_scan_path, str(Path(directory).resolve()))
            webdav_to_local = Config.from_settings(
                values | {
                    "source_type": "webdav",
                    "subtitle_destination": "local",
                    "local_scan_path": "",
                    "local_output_path": directory,
                    "webdav_username": "user",
                    "webdav_endpoint": "dav.example.test",
                    "webdav_scan_path": "Media",
                },
                {
                    "webdav_password": "password",
                    "opensubtitles_api_key": "key",
                    "openai_api_key": "ai",
                },
            )
            self.assertEqual(webdav_to_local.subtitle_destination, "local")
            self.assertEqual(webdav_to_local.local_output_path, str(Path(directory).resolve()))
            with self.assertRaisesRegex(PipelineError, "beside the source"):
                Config.from_settings(values | {"subtitle_destination": "local"}, {
                    "opensubtitles_api_key": "key",
                    "openai_api_key": "ai",
                })

    def test_moviehash_matches_unsigned_little_endian_sum(self):
        first = struct.pack("<8192Q", *range(8192))
        last = struct.pack("<8192Q", *range(8192, 16384))
        size = len(first) + len(last)
        expected = (size + sum(range(16384))) & 0xFFFFFFFFFFFFFFFF
        self.assertEqual(calculate_moviehash(size, first, last), f"{expected:016x}")

    def test_output_name_always_includes_language_without_overwrite(self):
        self.assertEqual(choose_output_path("Movies/Movie.mkv", "es", lambda _: False), "Movies/Movie.es.srt")
        with self.assertRaises(PipelineError):
            choose_output_path("Movies/Movie.mkv", "es", lambda _: True)
        existing = {"Movie.zh-Hans.srt", "Movie (1).zh-Hans.srt"}
        self.assertEqual(
            numbered_output_path("Movie.zh-Hans.srt", existing.__contains__),
            "Movie (2).zh-Hans.srt",
        )

    def test_subtitle_font_size_scales_with_release_resolution(self):
        self.assertEqual(subtitle_font_size("Movie.480p.mkv"), 22)
        self.assertEqual(subtitle_font_size("Movie.1920x1080.mkv"), 49)
        self.assertEqual(subtitle_font_size("Movie.2160p.mkv"), 97)
        self.assertEqual(subtitle_font_size("Movie.mkv"), 49)

    def test_resolution_aware_font_size_is_applied_to_every_cue(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "Movie.es.srt"
            path.write_bytes(SRT)
            apply_subtitle_font_size(path, "Movie.720p.mkv")
            subtitles = list(srt.parse(path.read_text(encoding="utf-8")))

        self.assertEqual(subtitles[0].content, '<font size="32">Hello there.</font>')

    def test_candidate_preference_is_chinese_then_english(self):
        def item(language: str, file_id: int):
            return {
                "attributes": {
                    "language": language,
                    "nb_cd": 1,
                    "release": language,
                    "files": [{"file_id": file_id}],
                }
            }

        selected = OpenSubtitles._prefer([item("en", 1), item("es", 2)], ("es", "en"))
        self.assertEqual(selected.file_id, 2)
        self.assertEqual(len(TARGET_LANGUAGES), 20)

    def test_opensubtitles_login_precedes_download(self):
        requests = []

        def handler(request: httpx.Request):
            requests.append(request)
            if request.url.path.endswith("/login"):
                return httpx.Response(200, json={"token": "jwt"})
            if request.url.path.endswith("/download"):
                self.assertEqual(request.headers.get("authorization"), "Bearer jwt")
                return httpx.Response(200, json={"link": "https://files.example.test/sub.srt", "remaining": 4})
            self.assertNotIn("authorization", request.headers)
            self.assertNotIn("api-key", request.headers)
            self.assertNotIn("cookie", request.headers)
            return httpx.Response(200, content=SRT)

        authenticated = Config(**{
            **config().__dict__,
            "opensubtitles_username": "member",
            "opensubtitles_password": "secret",
        })
        client = httpx.Client(
            base_url="https://api.opensubtitles.com/api/v1/",
            headers={"Api-Key": "consumer-secret"},
            cookies={"session": "api-cookie"},
            transport=httpx.MockTransport(handler),
        )
        download_client = httpx.Client(transport=httpx.MockTransport(handler))
        data, quota = OpenSubtitles(authenticated, client, download_client).download(
            SubtitleCandidate(1, "en", "release", True)
        )
        self.assertEqual(data, SRT)
        self.assertEqual(quota["remaining"], 4)
        self.assertTrue(requests[0].url.path.endswith("/login"))

    def test_opensubtitles_does_not_follow_authenticated_api_redirects(self):
        requests = []

        def handler(request):
            requests.append(request)
            return httpx.Response(307, headers={"Location": "https://other.example.test/login"})

        client = httpx.Client(
            base_url="https://api.opensubtitles.com/api/v1/",
            follow_redirects=True,
            transport=httpx.MockTransport(handler),
        )
        authenticated = replace(config(), opensubtitles_username="member", opensubtitles_password="secret")
        with self.assertRaisesRegex(PipelineError, "login failed"):
            OpenSubtitles(authenticated, client)._login()
        self.assertEqual(len(requests), 1)

    def test_opensubtitles_search_follows_same_origin_redirects(self):
        requests = []

        def handler(request: httpx.Request):
            requests.append(request)
            if len(requests) == 1:
                return httpx.Response(301, headers={"Location": "/api/v1/subtitles/?query=movie"})
            return httpx.Response(200, json={"data": []})

        client = httpx.Client(
            base_url="https://api.opensubtitles.com/api/v1/",
            headers={"Api-Key": "consumer-secret"},
            transport=httpx.MockTransport(handler),
        )
        self.assertEqual(OpenSubtitles(config(), client)._search({"query": "movie"}), [])
        self.assertEqual(len(requests), 2)
        self.assertEqual(str(requests[1].url), "https://api.opensubtitles.com/api/v1/subtitles/?query=movie")
        self.assertEqual(requests[1].headers.get("api-key"), "consumer-secret")

    def test_opensubtitles_search_explains_blocked_redirect(self):
        requests = []

        def handler(request: httpx.Request):
            requests.append(request)
            return httpx.Response(
                301,
                headers={"Location": "https://user:password@other.example.test/login?token=secret"},
            )

        client = httpx.Client(
            base_url="https://api.opensubtitles.com/api/v1/",
            transport=httpx.MockTransport(handler),
        )
        with self.assertRaisesRegex(
            PipelineError,
            r"OpenSubtitles search failed \(301 Moved Permanently\): redirected to "
            r"https://other\.example\.test/login; that destination was not safe to follow",
        ) as caught:
            OpenSubtitles(config(), client)._search({"query": "movie"})
        self.assertNotIn("secret", str(caught.exception))
        self.assertNotIn("password", str(caught.exception))
        self.assertEqual(len(requests), 1)

    def test_opensubtitles_download_validates_links_and_redirects(self):
        requests = []

        def handler(request):
            requests.append(request)
            if request.url.path == "/start":
                return httpx.Response(302, headers={"Location": "/final"})
            if request.url.path == "/unsafe":
                return httpx.Response(302, headers={"Location": "http://files.example.test/final"})
            return httpx.Response(200, content=SRT)

        client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)
        service = OpenSubtitles(config(), client, client)
        self.assertEqual(service._download_file("https://files.example.test/start"), SRT)
        self.assertEqual(len(requests), 2)
        for link in ("http://files.example.test/file", "file:///etc/passwd", "https://user:pass@files.example.test/file", "https://files.example.test:bad/file"):
            with self.subTest(link=link), self.assertRaisesRegex(PipelineError, "unsafe download link"):
                service._download_file(link)
        self.assertEqual(len(requests), 2)
        with self.assertRaisesRegex(PipelineError, "unsafe download link"):
            service._download_file("https://files.example.test/unsafe")
        self.assertEqual(len(requests), 3)

    def test_opensubtitles_loads_remaining_quota(self):
        def handler(request: httpx.Request):
            if request.url.path.endswith("/login"):
                return httpx.Response(200, json={"token": "jwt"})
            self.assertEqual(request.headers.get("authorization"), "Bearer jwt")
            return httpx.Response(200, json={"data": {"remaining_downloads": 20}})

        authenticated = Config(**{
            **config().__dict__,
            "opensubtitles_username": "member",
            "opensubtitles_password": "secret",
        })
        client = httpx.Client(
            base_url="https://api.opensubtitles.com/api/v1/",
            transport=httpx.MockTransport(handler),
        )
        self.assertEqual(OpenSubtitles(authenticated, client).remaining_downloads(), 20)

    def test_opensubtitles_retries_server_and_rate_limit_responses(self):
        responses = iter([
            httpx.Response(503),
            httpx.Response(429, headers={"Retry-After": "7"}),
            httpx.Response(200, json={"data": []}),
        ])
        client = httpx.Client(
            base_url="https://api.opensubtitles.com/api/v1/",
            transport=httpx.MockTransport(lambda _: next(responses)),
        )
        with patch("backend.app.random.uniform", return_value=0.1), patch("backend.app.time.sleep") as sleep:
            self.assertEqual(OpenSubtitles(config(), client)._search({"query": "movie"}), [])
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [1.1, 7.0])

    def test_opensubtitles_reports_api_error_detail(self):
        client = httpx.Client(
            base_url="https://api.opensubtitles.com/api/v1/",
            transport=httpx.MockTransport(
                lambda _: httpx.Response(400, json={"errors": ["Not enough parameters"]})
            ),
        )
        with self.assertRaisesRegex(
            PipelineError, r"OpenSubtitles search failed \(400\): Not enough parameters"
        ):
            OpenSubtitles(config(), client)._search({"query": ""})

    def test_opensubtitles_normalizes_bracketed_anime_release(self):
        requests = []

        def handler(request: httpx.Request):
            requests.append(request)
            return httpx.Response(200, json={"data": []})

        client = httpx.Client(
            base_url="https://api.opensubtitles.com/api/v1/",
            transport=httpx.MockTransport(handler),
        )
        with self.assertRaisesRegex(PipelineError, "No reliable requested subtitle"):
            OpenSubtitles(config(), client).find(
                "[Airota][Sousou no Frieren][29][1080p HEVC-10bit AAC ASS].mkv",
                "9e433af2ca9cd6a5",
                ("zh-cn", "en"),
            )

        self.assertEqual(requests[1].url.params["query"], "sousou no frieren")
        self.assertEqual(requests[1].url.params["episode_number"], "29")

    def test_opensubtitles_ignores_missing_feature_details(self):
        self.assertFalse(
            OpenSubtitles._metadata_match(
                {"attributes": {"feature_details": None}},
                {"title": "Slow Horses", "type": "episode", "season": 2, "episode": 2},
            )
        )

    def test_opensubtitles_episode_search_omits_series_year(self):
        requests = []

        def handler(request: httpx.Request):
            requests.append(request)
            return httpx.Response(200, json={"data": []})

        client = httpx.Client(
            base_url="https://api.opensubtitles.com/api/v1/",
            transport=httpx.MockTransport(handler),
        )
        with self.assertRaisesRegex(PipelineError, "No reliable requested subtitle"):
            OpenSubtitles(config(), client).find(
                "Slow Horses (2022) - S02E02 - From Upshott With Love.mkv",
                "9e433af2ca9cd6a5",
                ("en",),
            )

        self.assertNotIn("year", requests[1].url.params)

    def test_sync_uses_16khz_speech_analysis(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.srt"
            output = Path(directory) / "output.srt"
            source.write_bytes(SRT)

            def run(args, **_):
                output.write_bytes(SRT)
                return SimpleNamespace(returncode=0)

            with patch("backend.app.shutil.which", return_value="ffsubsync"), patch(
                "backend.app.subprocess.run", side_effect=run
            ) as process:
                sync_subtitle("/media/Movie.mkv", source, output, config())

        arguments = process.call_args.args[0]
        self.assertEqual(arguments[1], "/media/Movie.mkv")
        self.assertEqual(arguments[arguments.index("--frame-rate") + 1], "16000")

    def test_sidecar_language_uses_content_and_language_suffix(self):
        video = "Movie.2026.mkv"
        self.assertEqual(detect_sidecar_language_from_name(video, "Movie.2026.en.srt"), "en")
        self.assertEqual(detect_sidecar_language_from_name(video, "Movie.2026.zh-Hant.srt"), "zh-tw")
        self.assertIsNone(detect_sidecar_language_from_name(video, "Movie.2026.srt"))
        self.assertEqual(detect_sidecar_language(video, "Movie.2026.srt", "你好，世界".encode()), "zh-cn")
        self.assertEqual(detect_sidecar_language(video, "Movie.2026.en.srt", b"Short"), "en")
        self.assertEqual(detect_sidecar_language(video, "Movie.2026.ja.srt", "日本語です".encode()), "ja")
        self.assertEqual(detect_sidecar_language(video, "Movie.2026.zh-Hant.srt", "繁體中文".encode()), "zh-tw")
        self.assertEqual(
            detect_sidecar_language_from_name(video, "Movie.2026.zh-Hans.en.srt"),
            "zh-cn+en",
        )


class WebDAVTests(unittest.TestCase):
    def test_redirects_reject_encoded_traversal_before_sending_credentials(self):
        for target in (
            "/dav/Media%20Library/%2e%2e/private",
            "/dav/Media%20Library/%252e%252e/private",
            "/dav/Media%20Library/folder%5c..%5c..%5cprivate",
            "https://other.example.test/dav/Media%20Library/",
        ):
            requests = []

            def handler(request):
                requests.append(request)
                return httpx.Response(307, headers={"Location": target})

            client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)
            with self.subTest(target=target), self.assertRaises(PipelineError):
                WebDAV(config(), client).list("")
            self.assertEqual(len(requests), 1)

    def test_listing_ignores_entries_outside_scan_root(self):
        xml = b"""<?xml version="1.0"?>
        <d:multistatus xmlns:d="DAV:">
          <d:response><d:href>/dav/Media%20Library/</d:href><d:propstat><d:prop><d:resourcetype><d:collection/></d:resourcetype></d:prop></d:propstat></d:response>
          <d:response><d:href>/dav/Media%20Library/Shows/</d:href><d:propstat><d:prop><d:resourcetype><d:collection/></d:resourcetype></d:prop></d:propstat></d:response>
          <d:response><d:href>/dav/Media%20Library/Movie.mkv</d:href><d:propstat><d:prop><d:resourcetype/><d:getcontentlength>123</d:getcontentlength></d:prop></d:propstat></d:response>
          <d:response><d:href>/dav/Media%20Library/Movie.en.srt</d:href><d:propstat><d:prop><d:resourcetype/><d:getcontentlength>42</d:getcontentlength></d:prop></d:propstat></d:response>
          <d:response><d:href>/dav/private.mkv</d:href><d:propstat><d:prop><d:resourcetype/><d:getcontentlength>999</d:getcontentlength></d:prop></d:propstat></d:response>
        </d:multistatus>"""

        def handler(request: httpx.Request):
            self.assertEqual(request.method, "PROPFIND")
            return httpx.Response(207, content=xml)

        client = httpx.Client(transport=httpx.MockTransport(handler))
        entries = WebDAV(config(), client).list("")
        self.assertEqual([(entry.type, entry.path) for entry in entries], [("directory", "Shows"), ("video", "Movie.mkv")])
        self.assertEqual([(subtitle.name, subtitle.language) for subtitle in entries[1].subtitles], [("Movie.en.srt", "en")])

    def test_directory_listing_cache_refresh_and_rename_invalidation(self):
        xml = b"""<?xml version="1.0"?>
        <d:multistatus xmlns:d="DAV:">
          <d:response><d:href>/dav/Media%20Library/</d:href><d:propstat><d:prop><d:resourcetype><d:collection/></d:resourcetype></d:prop></d:propstat></d:response>
          <d:response><d:href>/dav/Media%20Library/Movie.mkv</d:href><d:propstat><d:prop><d:resourcetype/><d:getcontentlength>123</d:getcontentlength></d:prop></d:propstat></d:response>
        </d:multistatus>"""
        requests = []

        def handler(request: httpx.Request):
            requests.append(request.method)
            return httpx.Response(207, content=xml) if request.method == "PROPFIND" else httpx.Response(200)

        webdav = WebDAV(config(), httpx.Client(transport=httpx.MockTransport(handler)))
        self.assertEqual(webdav.list("")[0].path, "Movie.mkv")
        self.assertEqual(webdav.list("")[0].path, "Movie.mkv")
        self.assertEqual(requests.count("PROPFIND"), 1)

        webdav.list("", refresh=True)
        self.assertEqual(requests.count("PROPFIND"), 2)

        webdav.move("Movie.mkv", "Renamed.mkv")
        webdav.list("")
        self.assertEqual(requests.count("PROPFIND"), 3)

    def test_range_request_requires_206(self):
        client = httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, content=b"ignored")))
        with self.assertRaisesRegex(PipelineError, "does not support"):
            WebDAV(config(), client).read_range("Movie.mkv", 0, 9)

    def test_media_redirect_does_not_forward_webdav_credentials(self):
        initial = httpx.Client(
            auth=("user", "pass"),
            transport=httpx.MockTransport(
                lambda _: httpx.Response(302, headers={"Location": "https://cdn.example.test/file"})
            ),
        )

        def cdn(request: httpx.Request):
            self.assertNotIn("authorization", request.headers)
            self.assertEqual(request.headers["range"], "bytes=0-9")
            return httpx.Response(206, content=b"0123456789")

        media = httpx.Client(transport=httpx.MockTransport(cdn))
        self.assertEqual(WebDAV(config(), initial, media).read_range("Movie.mkv", 0, 9), b"0123456789")

    def test_move_disables_overwrite(self):
        requests = []

        def handler(request: httpx.Request):
            requests.append(request)
            return httpx.Response(200)

        WebDAV(config(), httpx.Client(transport=httpx.MockTransport(handler))).move(
            "old name.mkv", "Game.of.Thrones.S01E01.mkv"
        )

        self.assertEqual(requests[0].method, "MOVE")
        self.assertEqual(requests[0].headers["overwrite"], "F")
        self.assertEqual(
            requests[0].headers["destination"],
            "https://example.test/dav/Media%20Library/Game.of.Thrones.S01E01.mkv",
        )


class ResponseLimitTests(unittest.TestCase):
    def test_oversized_streams_are_stopped_and_closed(self):
        class OversizedStream(httpx.SyncByteStream):
            closed = False

            def __iter__(self):
                yield b"1234"
                yield b"56789"
                raise AssertionError("The oversized response must not be drained")

            def close(self):
                self.closed = True

        for operation in ("range", "directory", "sidecar", "download"):
            for headers in ({}, {"Content-Length": "1"}):
                with self.subTest(operation=operation, headers=headers):
                    stream = OversizedStream()
                    status = {"range": 206, "directory": 207}.get(operation, 200)
                    client = httpx.Client(transport=httpx.MockTransport(
                        lambda _: httpx.Response(status, headers=headers, stream=stream)
                    ))
                    with (
                        patch("backend.app.MAX_DIRECTORY_BYTES", 8),
                        patch("backend.app.MAX_SUBTITLE_BYTES", 8),
                        self.assertRaisesRegex(PipelineError, "oversized|too large|unexpectedly large"),
                    ):
                        if operation == "range":
                            WebDAV(config(), client).read_range("Movie.mkv", 0, 7)
                        elif operation == "directory":
                            WebDAV(config(), client).list("")
                        elif operation == "sidecar":
                            WebDAV(config(), client).read_small("Movie.srt", limit=8)
                        else:
                            OpenSubtitles(config(), client, client)._download_file("https://files.example.test/sub.srt")
                    self.assertTrue(stream.closed)
                    client.close()


class LocalStorageTests(unittest.TestCase):
    def test_rename_preserves_existing_destination_and_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "Movie.mkv").write_bytes(b"video")
            (root / "Renamed.mkv").write_bytes(b"existing")
            with self.assertRaisesRegex(PipelineError, "already exists"):
                LocalStorage(directory).move("Movie.mkv", "Renamed.mkv")
            self.assertEqual((root / "Movie.mkv").read_bytes(), b"video")
            self.assertEqual((root / "Renamed.mkv").read_bytes(), b"existing")

    def test_rename_reports_unlink_failure_without_deleting_video(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "Movie.mkv").write_bytes(b"video")
            with patch.object(Path, "unlink", side_effect=PermissionError), self.assertRaisesRegex(PipelineError, "could not remove the original"):
                LocalStorage(directory).move("Movie.mkv", "Renamed.mkv")
            self.assertEqual((root / "Movie.mkv").read_bytes(), b"video")
            self.assertEqual((root / "Renamed.mkv").read_bytes(), b"video")

    def test_local_subtitle_reads_enforce_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "Movie.srt").write_bytes(b"123456789")
            storage = LocalStorage(directory)
            with self.assertRaisesRegex(PipelineError, "unexpectedly large"):
                storage.read_small("Movie.srt", limit=8)
            self.assertEqual(storage.read_small("Movie.srt", limit=9), b"123456789")

    def test_browses_hashes_writes_and_renames_inside_root(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shows = root / "Shows"
            shows.mkdir()
            video = shows / "Movie.mkv"
            video.write_bytes(bytes(range(256)) * 512)
            sidecar = shows / "Movie.en.srt"
            sidecar.write_bytes(SRT)
            (shows / "notes.txt").write_text("ignore", encoding="utf-8")
            outside = root.parent / f"{root.name}-outside.mkv"
            outside.write_bytes(b"outside")
            try:
                (shows / "escape.mkv").symlink_to(outside)
                storage = LocalStorage(str(root))
                self.assertEqual([(entry.type, entry.path) for entry in storage.list("")], [("directory", "Shows")])
                self.assertEqual([entry.path for entry in storage.list("Shows")], ["Shows/Movie.mkv"])
                self.assertEqual(
                    [(subtitle.name, subtitle.language) for subtitle in storage.list("Shows")[0].subtitles],
                    [("Movie.en.srt", "en")],
                )
                self.assertEqual(storage.file_info("Shows/Movie.mkv").size, 131_072)
                self.assertEqual([entry.path for entry in storage.sidecars("Shows/Movie.mkv")], ["Shows/Movie.en.srt"])
                self.assertEqual(storage.read_small("Shows/Movie.en.srt"), SRT)
                self.assertEqual(len(storage.moviehash("Shows/Movie.mkv", 131_072)), 16)
                with storage.sync_input("Shows/Movie.mkv") as media_input:
                    self.assertEqual(media_input, str(video.resolve()))

                storage.put("Shows/Movie.zh-Hans.srt", SRT)
                with self.assertRaisesRegex(PipelineError, "nothing was overwritten"):
                    storage.put("Shows/Movie.zh-Hans.srt", b"replacement")
                storage.move("Shows/Movie.mkv", "Shows/Renamed.mkv")
                self.assertFalse(video.exists())
                self.assertTrue((shows / "Renamed.mkv").exists())
                with self.assertRaises(PipelineError):
                    storage.file_info("Shows/escape.mkv")
            finally:
                outside.unlink(missing_ok=True)


class FakeWebDAV:
    def __init__(self):
        self.uploads = {}
        self.sidecar_entries = []
        self.sidecar_data = {}
        self.hash_calls = 0

    def file_info(self, relative):
        return FileEntry(name=Path(relative).name, path=relative, type="video", size=131_072)

    def exists(self, _):
        return False

    def moviehash(self, *_):
        self.hash_calls += 1
        return "1234567890abcdef"

    def sidecars(self, _):
        return self.sidecar_entries

    def read_small(self, relative):
        return self.sidecar_data[relative]

    def put(self, relative, data):
        self.uploads[relative] = data

    @contextmanager
    def sync_input(self, relative):
        yield f"https://media.example.test/{relative}"


class FakeOpenSubtitles:
    def __init__(self, language):
        self.language = language
        self.languages = None

    def find(self, *args):
        self.languages = args[-1]
        return SubtitleCandidate(file_id=1, language=self.language, release="Matched.Release", moviehash_match=True)

    def download(self, _):
        return SRT, {"remaining": 4, "resetTimeUtc": "tomorrow"}


def copy_sync(_, source, destination, __):
    shutil.copyfile(source, destination)


class PipelineTests(unittest.TestCase):
    def test_existing_chinese_sidecar_completes_without_network_or_upload(self):
        webdav = FakeWebDAV()
        entry = FileEntry("Movie.zh-Hans.srt", "Movie.zh-Hans.srt", "file", 20)
        webdav.sidecar_entries = [entry]
        webdav.sidecar_data[entry.path] = "1\n00:00:01,000 --> 00:00:02,000\n你好\n\n".encode()

        class ForbiddenOpenSubtitles:
            def find(self, *_):
                raise AssertionError("Existing Chinese must skip OpenSubtitles")

        result = process_video(
            "Movie.mkv",
            config(),
            webdav,
            ForbiddenOpenSubtitles(),
            lambda *_: None,
            syncer=copy_sync,
            subtitle_mode="target",
        )
        self.assertTrue(result["existing"])
        self.assertEqual(result["outputPath"], entry.path)
        self.assertEqual(webdav.hash_calls, 0)
        self.assertEqual(webdav.uploads, {})

    def test_bilingual_ignores_existing_target_sidecar_and_uses_english(self):
        webdav = FakeWebDAV()
        entry = FileEntry("Movie.zh-Hans.srt", "Movie.zh-Hans.srt", "file", 20)
        webdav.sidecar_entries = [entry]
        webdav.sidecar_data[entry.path] = "1\n00:00:01,000 --> 00:00:02,000\n你好\n\n".encode()

        def translator(source, destination, _, **options):
            self.assertEqual(options["subtitle_mode"], "bilingual")
            destination.write_text(source.read_text() + "\nTranslated", encoding="utf-8")
            return {"promptTokens": 1, "completionTokens": 1, "totalTokens": 2}

        result = process_video(
            "Movie.mkv",
            config(),
            webdav,
            FakeOpenSubtitles("en"),
            lambda *_: None,
            syncer=copy_sync,
            translator=translator,
        )
        self.assertNotIn("existing", result)
        self.assertEqual(result["outputPath"], "Movie.zh-Hans.en.srt")
        self.assertIn(b"Translated", webdav.uploads["Movie.zh-Hans.en.srt"])

    def test_existing_english_sidecar_is_translated_to_language_output(self):
        webdav = FakeWebDAV()
        entry = FileEntry("Movie.en.srt", "Movie.en.srt", "file", len(SRT))
        webdav.sidecar_entries = [entry]
        webdav.sidecar_data[entry.path] = SRT

        class ForbiddenOpenSubtitles:
            def find(self, *_):
                raise AssertionError("Existing English must skip OpenSubtitles")

        def translator(source, destination, _, **__):
            destination.write_text(source.read_text() + "\nTranslated", encoding="utf-8")
            return {"promptTokens": 1, "completionTokens": 1, "totalTokens": 2}

        result = process_video(
            "Movie.mkv",
            config(),
            webdav,
            ForbiddenOpenSubtitles(),
            lambda *_: None,
            syncer=copy_sync,
            translator=translator,
        )
        self.assertEqual(result["outputPath"], "Movie.zh-Hans.en.srt")
        self.assertEqual(webdav.hash_calls, 0)
        self.assertIn(b"Translated", webdav.uploads["Movie.zh-Hans.en.srt"])

    def test_chinese_flow_skips_ai_and_uploads_srt(self):
        webdav = FakeWebDAV()
        opensubtitles = FakeOpenSubtitles("zh-cn")

        def forbidden_ai(*_):
            raise AssertionError("Chinese subtitles must not call AI")

        result = process_video(
            "Movie.mkv",
            config(),
            webdav,
            opensubtitles,
            lambda *_: None,
            syncer=copy_sync,
            translator=forbidden_ai,
            subtitle_mode="target",
        )
        self.assertEqual(result["outputPath"], "Movie.zh-Hans.srt")
        self.assertIn(b'<font size="49">Hello there.</font>', webdav.uploads["Movie.zh-Hans.srt"])
        self.assertEqual(opensubtitles.languages, ("zh-cn", "en"))

    def test_exact_moviehash_skips_sync_but_metadata_match_does_not(self):
        webdav = FakeWebDAV()

        def forbidden_sync(*_):
            raise AssertionError("Exact movie hash must skip synchronization")

        process_video(
            "Exact.mkv",
            config(),
            webdav,
            FakeOpenSubtitles("zh-cn"),
            lambda *_: None,
            syncer=forbidden_sync,
            subtitle_mode="target",
        )

        class MetadataMatch(FakeOpenSubtitles):
            def find(self, *_):
                return SubtitleCandidate(1, "zh-cn", "Metadata.Match", False)

        sync_calls = []

        def record_sync(*args):
            sync_calls.append(args)
            copy_sync(*args)

        process_video(
            "Fallback.mkv",
            config(),
            webdav,
            MetadataMatch("zh-cn"),
            lambda *_: None,
            syncer=record_sync,
            subtitle_mode="target",
        )
        self.assertEqual(len(sync_calls), 1)

        class InvalidExact(FakeOpenSubtitles):
            def download(self, _):
                return b"not an srt", {"remaining": 3, "resetTimeUtc": "tomorrow"}

        process_video(
            "Invalid.mkv",
            config(),
            webdav,
            InvalidExact("zh-cn"),
            lambda *_: None,
            syncer=record_sync,
            subtitle_mode="target",
        )
        self.assertEqual(len(sync_calls), 2)

    def test_english_flow_translates_before_upload(self):
        webdav = FakeWebDAV()

        def translator(source, destination, _, **__):
            destination.write_text(source.read_text() + "\nTranslated", encoding="utf-8")
            return {"promptTokens": 10, "completionTokens": 5, "totalTokens": 15}

        opensubtitles = FakeOpenSubtitles("en")
        result = process_video(
            "Movie.mkv",
            config(),
            webdav,
            opensubtitles,
            lambda *_: None,
            syncer=copy_sync,
            translator=translator,
        )
        self.assertIn(b"Translated", webdav.uploads["Movie.zh-Hans.en.srt"])
        self.assertEqual(result["aiUsage"]["totalTokens"], 15)
        self.assertEqual(opensubtitles.languages, ("en",))

    def test_webdav_target_sidecar_is_copied_to_numbered_local_output(self):
        webdav = FakeWebDAV()
        entry = FileEntry("Movie.srt", "Shows/Movie.srt", "file", 20)
        data = "1\n00:00:01,000 --> 00:00:02,000\n你好，世界。这是一段中文字幕。\n\n".encode()
        webdav.sidecar_entries = [entry]
        webdav.sidecar_data[entry.path] = data

        class ForbiddenOpenSubtitles:
            def find(self, *_):
                raise AssertionError("Existing target subtitle must be reused")

        with tempfile.TemporaryDirectory() as directory:
            destination = LocalStorage(directory)
            first = process_video(
                "Shows/Movie.mkv",
                config(),
                webdav,
                ForbiddenOpenSubtitles(),
                lambda *_: None,
                subtitle_mode="target",
                destination=destination,
                flat_output=True,
            )
            second = process_video(
                "Shows/Movie.mkv",
                config(),
                webdav,
                ForbiddenOpenSubtitles(),
                lambda *_: None,
                subtitle_mode="target",
                destination=destination,
                flat_output=True,
            )
            self.assertEqual(first["outputPath"], "Movie.zh-Hans.srt")
            self.assertEqual(second["outputPath"], "Movie (1).zh-Hans.srt")
            self.assertTrue(first["reusedSourceSidecar"])
            self.assertEqual((Path(directory) / second["outputPath"]).read_bytes(), data)
            self.assertEqual(webdav.uploads, {})

    def test_local_source_writes_beside_video(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            folder = root / "Shows"
            folder.mkdir()
            (folder / "Movie.mkv").write_bytes(bytes(range(256)) * 512)
            storage = LocalStorage(directory)
            result = process_video(
                "Shows/Movie.mkv",
                config(),
                storage,
                FakeOpenSubtitles("zh-cn"),
                lambda *_: None,
                subtitle_mode="target",
            )
            self.assertEqual(result["outputPath"], "Shows/Movie.zh-Hans.srt")
            self.assertIn(b'<font size="49">Hello there.</font>', (folder / "Movie.zh-Hans.srt").read_bytes())

    def test_no_subtitle_stops_before_download_or_upload(self):
        webdav = FakeWebDAV()

        class NoResults(FakeOpenSubtitles):
            def find(self, *_):
                raise PipelineError("No reliable subtitle")

            def download(self, _):
                raise AssertionError("No candidate means no download")

        with self.assertRaisesRegex(PipelineError, "No reliable"):
            process_video(
                "Movie.mkv",
                config(),
                webdav,
                NoResults("en"),
                lambda *_: None,
                syncer=copy_sync,
            )
        self.assertEqual(webdav.uploads, {})


class BatchJobTests(unittest.TestCase):
    def setUp(self):
        with JOBS_LOCK:
            JOBS.clear()

    def tearDown(self):
        with JOBS_LOCK:
            JOBS.clear()

    def test_backend_logs_rejected_requests_with_correlation_id(self):
        with (
            patch("backend.app.require_services", return_value=(config(), object(), object(), object())),
            TestClient(app, base_url="http://127.0.0.1:3666") as client,
            self.assertLogs("uvicorn.error", level="INFO") as captured,
        ):
            rejected = client.post("/api/jobs", json={"paths": []})
            invalid = client.put(
                "/api/settings",
                json={"values": [], "secrets": {"openai_api_key": "must-not-be-logged"}},
            )

        logs = "\n".join(captured.output)
        self.assertEqual(rejected.status_code, 400)
        self.assertTrue(rejected.headers["X-Request-ID"])
        self.assertEqual(invalid.status_code, 422)
        self.assertTrue(invalid.headers["X-Request-ID"])
        self.assertIn("request_rejected", logs)
        self.assertIn("detail='Select at least one video'", logs)
        self.assertIn("request_validation_failed", logs)
        self.assertIn('"field": "body.values"', logs)
        self.assertNotIn("must-not-be-logged", logs)

    def test_validation_errors_do_not_echo_submitted_credentials(self):
        with TestClient(app, base_url="http://127.0.0.1:3666") as client:
            response = client.put("/api/settings", json={"values": {}, "secrets": {"openai_api_key": ["private-value"]}})
        self.assertEqual(response.status_code, 422)
        self.assertNotIn("private-value", response.text)
        self.assertEqual(response.json()["detail"][0]["loc"], ["body", "secrets", "openai_api_key"])

    def test_local_api_rejects_untrusted_hosts_and_origins_before_side_effects(self):
        with (
            patch("backend.app.require_services", return_value=(config(), object(), object(), object())),
            patch("backend.app.EXECUTOR.submit") as submit,
            TestClient(app, base_url="http://127.0.0.1:3666") as client,
        ):
            for headers in (
                {"Origin": "https://untrusted.example"},
                {"Origin": "null"},
                {"Sec-Fetch-Site": "cross-site"},
            ):
                with self.subTest(headers=headers):
                    self.assertEqual(client.post("/api/jobs", json={"paths": ["Movie.mkv"]}, headers=headers).status_code, 403)
            rebound = client.post("/api/jobs", json={"paths": ["Movie.mkv"]}, headers={"Host": "untrusted.example", "Origin": "http://untrusted.example"})
            self.assertEqual(rebound.status_code, 400)
            self.assertFalse(JOBS)
            submit.assert_not_called()

            for index, origin in enumerate((None, "http://127.0.0.1:3666", "http://127.0.0.1:3000", "http://localhost:3000")):
                headers = {"Origin": origin} if origin else {}
                self.assertEqual(client.post("/api/jobs", json={"paths": [f"Movie{index}.mkv"]}, headers=headers).status_code, 202)
            preflight = client.options("/api/settings", headers={"Origin": "http://localhost:3000", "Access-Control-Request-Method": "PUT", "Access-Control-Request-Headers": "Content-Type"})
            self.assertEqual(preflight.status_code, 200)
            self.assertEqual(preflight.headers["access-control-allow-origin"], "http://localhost:3000")

    def test_create_job_validates_paths_keeps_order_and_queues_more(self):
        job_config = Config(**(config().__dict__ | {"target_language": "es"}))
        with patch("backend.app.require_services", return_value=(job_config, object(), object(), object())), patch(
            "backend.app.EXECUTOR.submit"
        ) as submit:
            for paths in ([], ["Movie.mkv", "./Movie.mkv"], ["../Movie.mkv"]):
                with self.subTest(paths=paths), self.assertRaises(HTTPException) as raised:
                    create_job(JobRequest(paths=paths))
                self.assertEqual(raised.exception.status_code, 400)

            response = create_job(JobRequest(paths=["B.mkv", "A.mkv"]))
            self.assertEqual([item.path for item in JOBS[response["jobId"]].items], ["B.mkv", "A.mkv"])
            self.assertTrue(all(item.started_at is None for item in JOBS[response["jobId"]].items))
            queued = create_job(JobRequest(paths=["C.mkv"], mode="target"))
            self.assertEqual([item.path for item in JOBS[queued["jobId"]].items], ["C.mkv"])
            self.assertEqual(JOBS[queued["jobId"]].target_language, "es")
            self.assertEqual(JOBS[response["jobId"]].subtitle_mode, "bilingual")
            self.assertEqual(JOBS[queued["jobId"]].subtitle_mode, "target")
            self.assertEqual(submit.call_count, 2)
            with self.assertRaises(HTTPException) as raised:
                create_job(JobRequest(paths=["A.mkv"]))
            self.assertEqual(raised.exception.status_code, 409)
            with self.assertRaises(HTTPException) as raised:
                create_job(JobRequest(paths=["D.mkv"], mode="invalid"))
            self.assertEqual(raised.exception.status_code, 400)

    def test_smart_rename_uses_title_and_moves_selected_files(self):
        class RenameWebDAV:
            def __init__(self):
                self.moves = []

            def file_info(self, path):
                return FileEntry(Path(path).name, path, "video", 100)

            def exists(self, _):
                return False

            def sidecars(self, _):
                return []

            def move(self, source, destination):
                self.moves.append((source, destination))

        class RenameCompletions:
            def __init__(self):
                self.calls = []

            def create(self, **kwargs):
                self.calls.append(kwargs)
                filename = json.loads(kwargs["messages"][1]["content"])["files"][0]["filename"]
                episode = "01" if ".01." in filename else "02"
                content = json.dumps({"renames": [
                    {"id": 0, "name": f"Game.of.Thrones.S01E{episode}.1080p.mkv"},
                ]})
                return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])

        webdav = RenameWebDAV()
        completions = RenameCompletions()
        ai = SimpleNamespace(chat=SimpleNamespace(completions=completions))
        rename_config = replace(
            config(),
            openai_reasoning_effort="minimal",
            openai_rename_reasoning_effort="high",
        )
        paths = ["Shows/obscure.01.1080p.mkv", "Shows/obscure.02.1080p.mkv"]
        services = (rename_config, webdav, webdav, object())
        with (
            patch("backend.app.require_services", return_value=services),
            patch("backend.app.OpenAI", return_value=ai),
            patch("backend.app.EXECUTOR.submit") as submit,
            patch("backend.app.update_job", wraps=update_job) as progress,
        ):
            result = rename_files(RenameRequest(paths=paths, title="  game of throne  "))
            job = JOBS[result["jobId"]]
            self.assertEqual(job.kind, "rename")
            self.assertEqual(job.rename_title, "game of throne")
            submit.assert_called_once_with(run_job, job.id, services)
            run_job(job.id, services)

        self.assertEqual(webdav.moves, [
            (paths[0], "Shows/Game.of.Thrones.S01E01.1080p.mkv"),
            (paths[1], "Shows/Game.of.Thrones.S01E02.1080p.mkv"),
        ])
        self.assertEqual([item.status for item in job.items], ["completed", "completed"])
        self.assertEqual([call.args[2] for call in progress.call_args_list], ["renaming", "renaming"])
        self.assertIn("2 of 2 files renamed", job.message)
        request = json.loads(completions.calls[0]["messages"][1]["content"])
        self.assertEqual(request["title"], "game of throne")
        self.assertEqual(request["files"][0]["path"], f"Media/{paths[0]}")
        prompt = completions.calls[0]["messages"][0]["content"]
        self.assertTrue(all(example in prompt for example in (
            "The.Wandering.Earth.1080p.x264.mp4",
            "Nirvana.in.Fire.S01E01.1080p.AMZN.WEB.DL.mkv",
            "Shameless.S03E02.1080p.AMZN.WEB.DL.mkv",
            "Shameless.S00E01.1080p.AMZN.WEB.DL.mkv",
        )))
        self.assertEqual(completions.calls[0]["response_format"]["type"], "json_schema")
        self.assertEqual(completions.calls[0]["reasoning_effort"], "high")
        self.assertEqual(len(completions.calls), 2)

    def test_batch_continues_after_item_failure(self):
        JOBS["batch"] = Job(id="batch", items=[JobItem(path=path) for path in ("A.mkv", "B.mkv", "C.mkv")])
        calls = []

        def process(path, *_args, **_kwargs):
            calls.append(path)
            if path == "B.mkv":
                raise PipelineError("No subtitle")
            return {"existing": False, "outputPath": path.replace(".mkv", ".srt")}

        with (
            patch("backend.app.require_services", return_value=(config(), object(), object(), object())),
            patch("backend.app.OpenAI"),
            patch("backend.app.process_video", side_effect=process),
        ):
            run_job("batch")

        self.assertEqual(calls, ["A.mkv", "B.mkv", "C.mkv"])
        self.assertEqual(JOBS["batch"].status, "completed")
        self.assertIsNotNone(JOBS["batch"].finished_at)
        self.assertEqual([item.status for item in JOBS["batch"].items], ["completed", "failed", "completed"])
        self.assertTrue(all(item.started_at is not None for item in JOBS["batch"].items))
        self.assertTrue(all(item.finished_at is not None for item in JOBS["batch"].items))
        self.assertTrue(all(item.started_at <= item.finished_at for item in JOBS["batch"].items))
        self.assertIn("2 of 3", JOBS["batch"].message)

    def test_all_failed_batch_is_failed(self):
        JOBS["batch"] = Job(id="batch", items=[JobItem(path="A.mkv"), JobItem(path="B.mkv")])
        with (
            patch("backend.app.require_services", return_value=(config(), object(), object(), object())),
            patch("backend.app.OpenAI"),
            patch("backend.app.process_video", side_effect=PipelineError("No subtitle")),
        ):
            run_job("batch")

        self.assertEqual(JOBS["batch"].status, "failed")
        self.assertIsNotNone(JOBS["batch"].finished_at)
        self.assertTrue(all(item.status == "failed" for item in JOBS["batch"].items))
        self.assertEqual(JOBS["batch"].error, "Every video in the batch failed")


class FakeCompletions:
    def __init__(self, invalid=False):
        self.calls = []
        self.invalid = invalid

    def create(self, **kwargs):
        self.calls.append(kwargs)
        requested = json.loads(kwargs["messages"][1]["content"])
        translations = [] if self.invalid else [{"id": row["id"], "text": "你好。"} for row in requested["cues"]]
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps({"translations": translations})))],
            usage=SimpleNamespace(prompt_tokens=11, completion_tokens=7, total_tokens=18),
        )


class FakeAI:
    def __init__(self, invalid=False):
        self.chat = SimpleNamespace(completions=FakeCompletions(invalid))
        self.closed = False

    def close(self):
        self.closed = True

    @property
    def responses(self):
        raise AssertionError("Responses API must never be accessed")


class TranslationTests(unittest.TestCase):
    def test_chat_completions_shape_and_bilingual_output(self):
        fake = FakeAI()
        with tempfile.TemporaryDirectory() as directory, patch("backend.app.OpenAI", return_value=fake) as factory:
            source = Path(directory) / "in.srt"
            output = Path(directory) / "out.srt"
            source.write_bytes(SRT)
            usage = translate_srt(source, output, config())
            rendered = output.read_text(encoding="utf-8")

        self.assertIn("Hello there.\n你好。", rendered)
        self.assertEqual(usage["totalTokens"], 18)
        call = fake.chat.completions.calls[0]
        self.assertEqual([message["role"] for message in call["messages"]], ["developer", "user"])
        self.assertEqual(call["response_format"]["type"], "json_schema")
        self.assertEqual(call["reasoning_effort"], "low")
        self.assertIs(call["store"], False)
        self.assertNotIn("input", call)
        self.assertEqual(factory.call_args.kwargs["max_retries"], 2)

    def test_target_only_replaces_english_and_names_target_language(self):
        fake = FakeAI()
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "in.srt"
            output = Path(directory) / "out.srt"
            source.write_bytes(SRT)
            translate_srt(
                source,
                output,
                config(),
                fake,
                target_language="es",
                subtitle_mode="target",
            )
            rendered = output.read_text(encoding="utf-8")

        self.assertNotIn("Hello there.", rendered)
        self.assertIn("Spanish", fake.chat.completions.calls[0]["messages"][0]["content"])

    def test_invalid_translation_ids_fail_after_three_chat_calls(self):
        fake = FakeAI(invalid=True)
        with tempfile.TemporaryDirectory() as directory, patch("backend.app.time.sleep"):
            source = Path(directory) / "in.srt"
            source.write_bytes(SRT)
            with self.assertRaisesRegex(PipelineError, "three attempts"):
                translate_srt(source, Path(directory) / "out.srt", config(), fake)
        self.assertEqual(len(fake.chat.completions.calls), 3)

    def test_transport_failure_is_not_retried_by_schema_loop(self):
        calls = []

        class BrokenCompletions:
            def create(self, **_):
                calls.append(1)
                raise RuntimeError("transport failed")

        fake = SimpleNamespace(chat=SimpleNamespace(completions=BrokenCompletions()))
        with tempfile.TemporaryDirectory() as directory, patch("backend.app.time.sleep") as sleep:
            source = Path(directory) / "in.srt"
            source.write_bytes(SRT)
            with self.assertRaisesRegex(PipelineError, "request failed"):
                translate_srt(source, Path(directory) / "out.srt", config(), fake)
        self.assertEqual(len(calls), 1)
        sleep.assert_not_called()

    def test_empty_translation_is_retried_even_when_all_ids_match(self):
        fake = FakeAI()
        create = fake.chat.completions.create

        def empty_once(**kwargs):
            completion = create(**kwargs)
            if len(fake.chat.completions.calls) == 1:
                result = json.loads(completion.choices[0].message.content)
                result["translations"][-1]["text"] = " \n "
                completion.choices[0].message.content = json.dumps(result)
            return completion

        with tempfile.TemporaryDirectory() as directory, patch("backend.app.time.sleep"):
            source = Path(directory) / "in.srt"
            output = Path(directory) / "out.srt"
            source.write_bytes(SRT)
            with patch.object(fake.chat.completions, "create", side_effect=empty_once):
                usage = translate_srt(source, output, config(), fake)
            self.assertTrue(all("你好。" in cue.content for cue in srt.parse(output.read_text())))
        self.assertEqual(len(fake.chat.completions.calls), 2)
        self.assertEqual(usage["totalTokens"], 36)
        retry_prompt = fake.chat.completions.calls[1]["messages"][0]["content"]
        self.assertIn("previous response failed validation", retry_prompt)
        self.assertIn("must be a non-empty string", retry_prompt)
        self.assertEqual(
            fake.chat.completions.calls[0]["messages"][1],
            fake.chat.completions.calls[1]["messages"][1],
        )

    def test_batches_preserve_ids_timestamps_and_use_short_context(self):
        fake = FakeAI()
        create = fake.chat.completions.create

        def translate(**kwargs):
            completion = create(**kwargs)
            requested = json.loads(kwargs["messages"][1]["content"])
            completion.choices[0].message.content = json.dumps({"translations": [
                {"id": row["id"], "text": f"译文{row['id']}"}
                for row in reversed(requested["cues"])
            ]})
            return completion

        with tempfile.TemporaryDirectory() as directory:
            source, output = Path(directory) / "in.srt", Path(directory) / "out.srt"
            original = [srt.Subtitle(index=10 + i, start=timedelta(seconds=i * 3),
                                    end=timedelta(seconds=i * 3 + 2), content=f"Line {i}...")
                        for i in range(23)]
            source.write_text(srt.compose(original, reindex=False))
            with patch.object(fake.chat.completions, "create", side_effect=translate):
                usage = translate_srt(source, output, config(), fake, subtitle_mode="bilingual")
            rendered = list(srt.parse(output.read_text()))
        self.assertEqual(len(rendered), 23)
        self.assertEqual(usage["totalTokens"], 3 * 18)
        requests = sorted((json.loads(c["messages"][1]["content"]) for c in fake.chat.completions.calls),
                          key=lambda request: request["cues"][0]["id"])
        self.assertEqual([len(request["cues"]) for request in requests], [10, 10, 3])
        for request in requests:
            i = request["cues"][0]["id"]
            self.assertEqual(request["context_before"], "\n".join(c.content for c in original[max(0, i-2):i]))
        for i, (before, after) in enumerate(zip(original, rendered)):
            self.assertEqual((before.index, before.start, before.end), (after.index, after.start, after.end))
            self.assertEqual(after.content, before.content + f"\n译文{i}")

    def test_invalid_batches_split_to_singletons_and_count_failed_usage(self):
        fake = FakeAI()
        create = fake.chat.completions.create

        def reject_batches(**kwargs):
            completion = create(**kwargs)
            requested = json.loads(kwargs["messages"][1]["content"])
            if len(requested["cues"]) > 1:
                completion.choices[0].message.content = '{"translations": []}'
            return completion

        with tempfile.TemporaryDirectory() as directory, patch("backend.app.time.sleep"):
            source, output = Path(directory) / "in.srt", Path(directory) / "out.srt"
            source.write_text(srt.compose([
                srt.Subtitle(index=i+1, start=timedelta(seconds=i), end=timedelta(seconds=i+1), content="Hello.")
                for i in range(4)
            ]))
            with patch.object(fake.chat.completions, "create", side_effect=reject_batches):
                usage = translate_srt(source, output, config(), fake)
            self.assertEqual(len(list(srt.parse(output.read_text()))), 4)
        sizes = [len(json.loads(call["messages"][1]["content"])["cues"]) for call in fake.chat.completions.calls]
        self.assertEqual(sizes, [4, 4, 4, 2, 2, 2, 1, 1, 2, 2, 2, 1, 1])
        self.assertEqual(usage["totalTokens"], len(sizes) * 18)

    def test_batches_are_bounded_by_characters(self):
        cues = [(i, srt.Subtitle(index=i, start=timedelta(), end=timedelta(seconds=1), content="x"*1500))
                for i in range(11)]
        self.assertEqual([len(batch) for batch in batch_cues(cues)], [4, 4, 3])

    def test_persistent_empty_translation_does_not_write_output(self):
        fake = FakeAI()
        create = fake.chat.completions.create

        def empty(**kwargs):
            completion = create(**kwargs)
            result = json.loads(completion.choices[0].message.content)
            result["translations"][-1]["text"] = ""
            completion.choices[0].message.content = json.dumps(result)
            return completion

        with tempfile.TemporaryDirectory() as directory, patch("backend.app.time.sleep"):
            source = Path(directory) / "in.srt"
            output = Path(directory) / "out.srt"
            source.write_bytes(SRT)
            with patch.object(fake.chat.completions, "create", side_effect=empty):
                with self.assertRaisesRegex(PipelineError, "three attempts"):
                    translate_srt(source, output, config(), fake)
            self.assertFalse(output.exists())
        self.assertEqual(len(fake.chat.completions.calls), 3)

    def test_single_cue_response_rejects_extra_duplicate_and_noninteger_ids(self):
        for invalid_ids in ([0, 1], [0, 0], [False], [0.0]):
            with self.subTest(ids=invalid_ids):
                fake = FakeAI()
                create = fake.chat.completions.create

                def invalid_response(**kwargs):
                    completion = create(**kwargs)
                    completion.choices[0].message.content = json.dumps({"translations": [
                        {"id": cue_id, "text": "你好。"} for cue_id in invalid_ids
                    ]})
                    return completion

                with tempfile.TemporaryDirectory() as directory, patch("backend.app.time.sleep"):
                    source = Path(directory) / "in.srt"
                    output = Path(directory) / "out.srt"
                    source.write_bytes(SRT)
                    with patch.object(fake.chat.completions, "create", side_effect=invalid_response):
                        with self.assertRaisesRegex(PipelineError, "three attempts"):
                            translate_srt(source, output, config(), fake)
                    self.assertFalse(output.exists())
                self.assertEqual(len(fake.chat.completions.calls), 3)


class OpenFolderTests(unittest.TestCase):
    def test_credential_file_is_revealed_in_file_manager(self):
        with tempfile.TemporaryDirectory() as root:
            credentials_file = Path(root, "config.json")
            credentials_file.write_text("{}", encoding="utf-8")
            with patch("backend.app.CONFIG_PATH", credentials_file), patch(
                "backend.app.sys.platform", "darwin"
            ), patch("backend.app.subprocess.run") as run:
                self.assertEqual(open_credentials_file(), {"opened": True})
                self.assertEqual(run.call_args.args[0], ["open", "-R", str(credentials_file.resolve())])

    def test_missing_credential_file_cannot_be_opened(self):
        with tempfile.TemporaryDirectory() as root, patch(
            "backend.app.CONFIG_PATH", Path(root, "missing.json")
        ), patch("backend.app.subprocess.run") as run:
            with self.assertRaises(HTTPException) as caught:
                open_credentials_file()
            self.assertEqual(caught.exception.status_code, 404)
            run.assert_not_called()

    def test_remote_location_encodes_current_directory(self):
        source = WebDAV(config())
        try:
            with patch("backend.app.require_services", return_value=(None, source, None, None)), patch.object(source, "list", return_value=[]):
                result = files("Films & TV/电影 #1", False)
            self.assertEqual(result["location"], "https://example.test/dav/Media%20Library/Films%20%26%20TV/%E7%94%B5%E5%BD%B1%20%231/")
        finally:
            source.client.close()
            source.media_client.close()

    def test_local_folder_opens_and_rejects_escape_and_files(self):
        with tempfile.TemporaryDirectory() as root:
            folder = Path(root, "Films & TV")
            folder.mkdir()
            Path(root, "video.mkv").touch()
            source = LocalStorage(root)
            with patch("backend.app.require_services", return_value=(None, source, None, None)), patch("backend.app.sys.platform", "darwin"), patch("backend.app.subprocess.run") as run:
                self.assertEqual(open_local_folder("Films & TV"), {"opened": True})
                self.assertEqual(run.call_args.args[0], ["open", str(folder.resolve())])
                run.reset_mock()
                for path in ("../outside", "video.mkv", "missing"):
                    with self.assertRaises(HTTPException) as caught:
                        open_local_folder(path)
                    self.assertEqual(caught.exception.status_code, 400)
                run.assert_not_called()

    def test_opener_failure_is_reported(self):
        with tempfile.TemporaryDirectory() as root:
            with patch("backend.app.require_services", return_value=(None, LocalStorage(root), None, None)), patch("backend.app.sys.platform", "darwin"), patch("backend.app.subprocess.run", side_effect=OSError("Unavailable")):
                with self.assertRaises(HTTPException) as caught:
                    open_local_folder("")
                self.assertEqual(caught.exception.status_code, 500)


if __name__ == "__main__":
    unittest.main()
