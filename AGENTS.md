# Agent guide

Start with [README.md](README.md) for setup, usage, and development commands. Look up behavior in the implementation and its tests; keep this file a navigation guide.

| Question | Where to look |
| --- | --- |
| API, storage, subtitle selection, sync, translation, or renaming? | [backend/app.py](backend/app.py) and [tests/test_app.py](tests/test_app.py). Start with `process_video`, `WebDAV`, `LocalStorage`, `translate_srt`, or `smart_rename`. |
| Embedded tracks, remote reads, or synchronization? | [backend/embedded_subtitles.py](backend/embedded_subtitles.py), [backend/media_range.py](backend/media_range.py), [backend/subtitle_sync.py](backend/subtitle_sync.py), [tests/test_embedded_subtitles.py](tests/test_embedded_subtitles.py), [tests/test_subtitle_sync.py](tests/test_subtitle_sync.py), and [tests/test_media_integration.py](tests/test_media_integration.py). |
| Settings, credentials, or config migration? | `Config` and settings handlers in [backend/app.py](backend/app.py); [backend/config_storage.py](backend/config_storage.py) and [tests/test_config_storage.py](tests/test_config_storage.py). |
| UI or setup wizard? | Read [frontend/AGENTS.md](frontend/AGENTS.md) first, then [Home.tsx](frontend/app/Home.tsx) or [Settings.tsx](frontend/app/Settings.tsx). |
| Translations or styling? | `frontend/app/i18n/locales/`, [globals.css](frontend/app/globals.css), and [tokens.scss](frontend/app/styles/tokens.scss). |
| Routing or production serving? | [backend/frontend.py](backend/frontend.py), [next.config.ts](frontend/next.config.ts), and [tests/test_frontend.py](tests/test_frontend.py). |
| Installed app or offline behavior? | [ServiceWorker.tsx](frontend/app/ServiceWorker.tsx), `frontend/public/`, and [serviceWorker.test.mjs](frontend/app/serviceWorker.test.mjs). |
| Dependencies or available commands? | [pyproject.toml](pyproject.toml), [package.json](package.json), and [frontend/package.json](frontend/package.json). |

Run the relevant checks from the README. Frontend unit tests live in `frontend/app/*.test.mjs`. For opt-in media integration coverage, see [tests/test_media_integration.py](tests/test_media_integration.py) and run:

```sh
RUN_MEDIA_INTEGRATION=1 uv run python -m unittest tests.test_media_integration
```
