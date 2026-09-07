# Cue

**Find, sync, and translate subtitles for your video library.**

Cue is a local web app for videos on WebDAV or your own filesystem. Choose a language, select your videos, and let Cue handle the subtitles.

- **Find subtitles** from existing sidecar files or OpenSubtitles.
- **Sync locally** using a short audio sample (15 seconds for WebDAV).
- **Translate English** through a configurable Chat Completions endpoint, with target-only or bilingual output.
- **Process multiple videos** in sequence; one failed video won't stop the rest.
- **Keep your library organized** with language-tagged filenames and Smart Rename. Existing subtitle files are never overwritten.

![Cue media library with WebDAV browsing and the jobs panel](assets/cue-app-preview.png)

## Quick start

Install **Python 3.10–3.14**, **uv**, **Node.js 20+**, and **FFmpeg**, then run from the repository root:

```sh
uv sync
npm --prefix frontend install
npm run prod
```

Any Python version in this range works; you do not need Python 3.13 specifically. To choose an installed version explicitly, use `uv sync --python 3.12` (replace `3.12` with your version) in place of `uv sync`.

Open **<http://127.0.0.1:3666/>** and follow the setup wizard. Have these ready:

- A WebDAV connection or a local video folder.
- An OpenSubtitles consumer API key and account credentials for downloads.
- An API key, base URL, and model for a Chat Completions endpoint.

Choose your subtitle language and output mode during setup. You can change everything later in [Settings](http://127.0.0.1:3666/settings/).

## Using Cue

Browse to a folder, select one or more videos, and start a job. Cue prefers existing subtitles before searching OpenSubtitles.

| Video source | Subtitle destination |
| --- | --- |
| WebDAV | Beside the remote video or in one flat local output folder |
| Local folder | Beside each video |

Output names include the language: `Movie.es.srt` or `Movie.es.en.srt` for bilingual subtitles. Name collisions receive a numeric suffix.

- Smart Rename also renames matching subtitle files beside the video, preserving language and forced/SDH tags. It checks all destination names before moving files and attempts to restore earlier moves if a later move fails. NFO files, artwork, and subtitles in a separate output folder are not renamed.
- Queued jobs keep the media source, output destination, and credentials selected when submitted, even if Settings changes before they run.
- Translation sends up to 10 cues per AI request (6,000 source characters), with two preceding cues as context (1,000 characters total). Invalid replies are retried, then split into smaller batches down to individual cues. Four batches run concurrently. IDs and timestamps are preserved by the app; validation cannot guarantee that a translation has the correct meaning.
- Local paths belong to the **machine running Cue**. Use absolute, existing, readable and writable directories. Local Smart Rename requires hard-link support.
- WebDAV synchronization decodes **15 seconds of 8 kHz mono audio once**, then aligns subtitles using that audio in memory. Four bounded range reads overlap to reduce network latency, and signed download URLs are reused within a job. When readable SRT cues indicate a silent intro, the sample starts five seconds before the first cue (up to three minutes into the video). This fast mode corrects timing offsets; it does not attempt frame-rate drift correction from a short sample.
- **No video or audio cache files are written to disk.** Remote reads use up to 16 MiB of RAM cache and at most **32 MiB or one quarter of the video size**, whichever is smaller, including rereads. Subtitle matching separately reads 128 KiB for the hash. Small subtitle files are still saved normally. The operating system manages RAM and may swap it.
- Remote audio extraction stops after 45 seconds of wall time; subsequent alignment has a 20-second limit. Servers must support byte ranges. Unsupported servers or videos that exceed the limits fail without falling back to a full download. Interleaved video/audio still requires some video bytes; WebDAV does not provide server-side quality conversion. Local videos retain the five-minute synchronization behavior.
- Settings and credentials are stored in `~/.config/subtitle-maker/config.json`. Saved credentials are not returned to the browser.
- Cue runs on **localhost / 127.0.0.1**. Keep the server running while using it.

To use Cue in its own window, open the production app and choose your browser's **Install Cue** action, or **File → Add to Dock** in macOS Safari. The installed app still requires the server; uninstalling it preserves server settings.

## Running on a trusted private network

Cue has no application login. Restrict access to your own trusted devices using your private network's access controls. Run one server process, preferably under a dedicated non-root account with access only to the required media folders.

Build once with `npm run build`, then bind to the server's private-network IP (replace the example address and hostname):

```sh
UVICORN_HOST=100.101.102.103 \
CUE_ALLOWED_HOSTS=100.101.102.103,media-server.example.ts.net \
npm start
```

Open `http://100.101.102.103:3666/` or the configured hostname. `CUE_ALLOWED_HOSTS` is a comma-separated list of exact hostnames/IPs without schemes or ports. Localhost remains allowed; the default bind address remains `127.0.0.1`. Production builds use the page's origin for API requests; leave `NEXT_PUBLIC_API_BASE_URL` unset when building.

For a reverse proxy on the same machine, keep the localhost bind and configure the public hostname. If the proxy rewrites the upstream Host header to localhost, also explicitly allow the browser's origin:

```sh
CUE_ALLOWED_HOSTS=media-server.example.ts.net \
CUE_ALLOWED_ORIGINS=https://media-server.example.ts.net \
npm start
```

Origins include the scheme and any non-default port, separated by commas. Keep the proxy's upstream connection private and serve Cue at `/` on its own hostname. Network allowlists do not provide user authentication. Use HTTPS for installed-app/offline support on remote devices.

Local folders refer to the server's filesystem. For containers, mount the media folders with the required write permissions and persist the service user's `~/.config/subtitle-maker/` directory. The “open folder” and “open credentials” actions launch a file manager on the server and require a desktop session.

Jobs remain in memory: restarting loses their status and queue, and multiple workers/replicas are unsupported. Other browser sessions do not automatically discover existing jobs.

## Development

After installing the dependencies above, start the backend:

```sh
uv run uvicorn backend.app:app --host 127.0.0.1 --port 3666
```

In another terminal, start the frontend:

```sh
NEXT_PUBLIC_API_BASE_URL=http://127.0.0.1:3666 npm --prefix frontend run dev
```

Open <http://127.0.0.1:3000>. For production, `npm run prod` builds the static frontend and starts FastAPI to serve the app and API together. Use `npm start` to restart an already-built app.

Run the standard checks:

```sh
uv run python -m unittest discover -s tests
npm --prefix frontend run typecheck
npm --prefix frontend run lint
npm --prefix frontend test
npm --prefix frontend run build
```

To check the reported split-sentence translation failure against your configured AI model, run:

```sh
RUN_TRANSLATION_LIVE=1 uv run python -m unittest tests.test_translation_live
```

This opt-in test translates the reported failure cases and fresh examples across three runs (12 batch requests before retries). It consumes API tokens and prints translations for review. Passing these targeted checks does not establish general translation quality.

See [AGENTS.md](AGENTS.md) for a guide to the source files and tests.
