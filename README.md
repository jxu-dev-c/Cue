# Cue

**Find, sync, and translate subtitles for your video library.**

Cue is a local web app for videos on WebDAV or your own filesystem. Choose a language, select your videos, and let Cue handle the subtitles.

- **Find subtitles** from existing sidecars, complete indexed MKV text tracks, or OpenSubtitles.
- **Sync locally** using short audio samples.
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

Browse to a folder, select one or more videos, and start a job. Cue prefers existing target subtitles, then complete indexed MKV text tracks, before searching OpenSubtitles. Embedded text keeps the video's own timestamps and needs no guessed synchronization.

| Video source | Subtitle destination |
| --- | --- |
| WebDAV | Beside the remote video or in one flat local output folder |
| Local folder | Beside each video |

Output names include the language: `Movie.es.srt` or `Movie.es.en.srt` for bilingual subtitles. Name collisions receive a numeric suffix.

- Smart Rename also renames matching subtitle files beside the video, preserving language and forced/SDH tags. It checks all destination names before moving files and attempts to restore earlier moves if a later move fails. NFO files, artwork, and subtitles in a separate output folder are not renamed.
- Queued jobs keep the media source, output destination, and credentials selected when submitted, even if Settings changes before they run.
- Translation sends up to 10 cues per AI request (6,000 source characters), with two preceding cues as context (1,000 characters total). Invalid replies are retried, then split into smaller batches down to individual cues. Four batches run concurrently. IDs and timestamps are preserved by the app; validation cannot guarantee that a translation has the correct meaning.
- Local paths belong to the **machine running Cue**. Use absolute, existing, readable and writable directories. Local Smart Rename requires hard-link support.
- Indexed MKV extraction uses Enzyme to read the track and seek index, then fetches only the selected text subtitle blocks. It checks the index against the track's frame/byte statistics and packet timestamps before accepting it as complete. Disabled, forced, commentary, and signs-only tracks are excluded. Supported text codecs are SRT/UTF-8, ASS and SSA; missing or incomplete indexes fall back to the normal pipeline.
- Without complete embedded or exact-match subtitles, Cue uses its own **best-effort short-sample alignment** (up to three 8-second dialogue samples). It selects a default non-commentary audio track and estimates a constant offset from the observed intervals. Ambiguous estimates are allowed and jobs are labelled “approximate timing.” If there is no positive match or the shift would lose opening cues, original timestamps are retained. Text, IDs and cue counts are preserved. Frame-rate drift is not corrected. FFmpeg/ffprobe decode and inspect media; ffsubsync is not used or required.
- **No video or audio cache files are written to disk.** Indexed subtitle reads use 16 KiB blocks, at most 16 MiB of cache and a separate **64 MiB or one quarter of the video size** transfer limit. Audio synchronization uses up to 16 MiB of cache and **256 MiB or one quarter of the video size**, whichever is smaller, including rereads. Audio reads use 1 MiB ranges. Subtitle hashing separately reads 128 KiB. Small subtitle files are saved normally; the operating system may swap RAM.
- Indexed subtitle extraction has a five-minute deadline for its many small requests; audio range reads have a 120-second deadline. Servers must support byte ranges; failures never trigger a full video download. Incomplete audio or media-read failures still stop the job. Local and remote videos use the same short-sample approximation when needed.
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
