import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import test from "node:test";
import { runInNewContext } from "node:vm";
import { JSDOM } from "jsdom";
import React, { act } from "react";
import ts from "typescript";

const require = createRequire(import.meta.url);
const messages = JSON.parse(readFileSync(new URL("./i18n/locales/en/common.json", import.meta.url), "utf8"));
const translation = {
  i18n: { language: "en" },
  t(key, values = {}) {
    const message = key.split(".").reduce((value, part) => value?.[part], messages) ?? key;
    return message.replace(/{{(\w+)}}/g, (_, name) => values[name] ?? "");
  },
};
const homeSource = ts.transpileModule(
  readFileSync(new URL("./Home.tsx", import.meta.url), "utf8"),
  { compilerOptions: { module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX } },
).outputText;

// Mount the real Home component, replacing only its Next shell and HTTP boundary.
async function mountHome(t, kind, terminalStatus, error, embeddedLanguages = []) {
  const dom = new JSDOM("<div id='root'></div>", { url: "http://localhost/Series/" });
  dom.window.HTMLDialogElement.prototype.showModal = function () { this.setAttribute("open", ""); };
  dom.window.HTMLDialogElement.prototype.close = function () { this.removeAttribute("open"); };
  const restoreGlobals = [];
  for (const [key, value] of Object.entries({
    window: dom.window,
    document: dom.window.document,
    IS_REACT_ACT_ENVIRONMENT: true,
  })) {
    const original = Object.getOwnPropertyDescriptor(globalThis, key);
    Object.defineProperty(globalThis, key, { configurable: true, writable: true, value });
    restoreGlobals.push(() => {
      if (original) Object.defineProperty(globalThis, key, original);
      else delete globalThis[key];
    });
  }
  dom.window.sessionStorage.setItem("cue-jobs", "job-1");
  const timers = new Map();
  const metadataTimers = new Map();
  let timerId = 0;
  dom.window.setTimeout = (callback, delay) => {
    (delay === 0 ? metadataTimers : timers).set(++timerId, callback);
    return timerId;
  };
  dom.window.clearTimeout = (id) => { timers.delete(id); metadataTimers.delete(id); };
  let status = "queued";
  const fileRequests = [];
  const video = { name: "Episode.mkv", path: "Series/Episode.mkv", type: "video", subtitles: [] };
  const exports = {};
  runInNewContext(homeSource, {
    exports,
    require(name) {
      if (name === "next-i18next/client") return { useT: () => translation };
      if (name === "next/navigation") return { usePathname: () => "/Series/" };
      if (name === "./embeddedSubtitles") return {
        ...require("./embeddedSubtitles.ts"),
        async fetchEmbeddedSubtitleStream(_url, signal, onMetadata) {
          if (!signal.aborted) onMetadata({ path: video.path, status: "available", languages: embeddedLanguages });
        },
      };
      if (name === "./AppHeader") return { AppHeader: ({ jobsControl }) => jobsControl };
      return require(name.startsWith("./") ? `${name}.ts` : name);
    },
    process: { env: {} },
    window: dom.window,
    document: dom.window.document,
    sessionStorage: dom.window.sessionStorage,
    localStorage: dom.window.localStorage,
    Event: dom.window.Event,
    AbortController,
    URLSearchParams,
    async fetch(url) {
      let body;
      if (url === "/api/health") body = { ready: true };
      else if (url === "/api/quota") body = { remaining: 10 };
      else if (url === "/api/settings") body = {
        setup_required: false,
        values: { source_type: "local", target_language: "zh-cn" },
        options: { target_languages: [], subtitle_modes: [] },
      };
      else if (url === "/api/jobs/job-1") body = {
        id: "job-1", kind, status, message: "", created_at: 1,
        items: [{ path: video.path, status, message: "", error: status === "failed" ? error : undefined }],
      };
      else if (url.startsWith("/api/files?")) {
        fileRequests.push(url);
        const finished = status === terminalStatus;
        body = { path: "Series", location: "/media/Series", entries: [{
          ...video,
          name: finished && kind === "rename" ? "Renamed.mkv" : video.name,
          subtitles: finished && kind !== "rename"
            ? [{ name: "Episode.zh-Hans.srt", path: "Series/Episode.zh-Hans.srt", language: "zh-cn" }]
            : [],
        }] };
      } else throw new Error(`Unexpected request: ${url}`);
      return { ok: true, json: async () => body };
    },
  });
  const { createRoot } = await import("react-dom/client");
  const root = createRoot(dom.window.document.getElementById("root"));
  t.after(async () => {
    await act(async () => root.unmount());
    dom.window.close();
    restoreGlobals.forEach((restore) => restore());
  });
  async function flushMetadata() {
    const callbacks = [...metadataTimers.values()];
    metadataTimers.clear();
    await act(async () => callbacks.forEach((callback) => callback()));
  }
  await act(async () => root.render(React.createElement(exports.Home)));
  await flushMetadata();
  return {
    document: dom.window.document,
    fileRequests,
    async poll(nextStatus) {
      status = nextStatus;
      const callbacks = [...timers.values()];
      timers.clear();
      await act(async () => callbacks.forEach((callback) => callback()));
      await flushMetadata();
    },
  };
}

for (const kind of ["subtitles", undefined, "rename"]) {
  for (const status of ["completed", "failed"]) {
    test(`${kind ?? "legacy subtitle"} job ${status} refreshes the visible media automatically`, async (t) => {
      const home = await mountHome(t, kind, status);
      assert.ok(home.document.querySelector('[aria-label="No sidecar subtitle"]'));
      assert.deepEqual(home.fileRequests, ["/api/files?path=Series"]);

      await home.poll("running");
      assert.equal(home.fileRequests.length, 1, "active jobs do not refresh the directory");

      await home.poll(status);
      assert.deepEqual(home.fileRequests, ["/api/files?path=Series", "/api/files?path=Series&refresh=true"]);
      if (kind === "rename") {
        assert.ok(home.document.querySelector('[title="Renamed.mkv"]'));
      } else {
        assert.equal(home.document.querySelector('[aria-label="No sidecar subtitle"]'), null);
        assert.ok(home.document.querySelector('[role="img"][title="Simplified Chinese · Episode.zh-Hans.srt"]'));
      }
      await home.poll(status);
      assert.equal(home.fileRequests.length, 2, "terminal jobs are not polled or refreshed again");
    });
  }
}

for (const [error, showHelp] of [
  ["No reliable requested subtitle was found", true],
  ["OpenSubtitles search failed (400): The query is rejected", true],
  ["OpenSubtitles search failed (422): query is invalid", true],
  ["OpenSubtitles search failed (401): Unauthorized", false],
  ["OpenSubtitles search failed (429): Too many requests", false],
  ["OpenSubtitles search could not connect", false],
  ["OpenSubtitles download request failed (400): The query is rejected", false],
]) {
  test(`rename help for ${error}`, async (t) => {
    const home = await mountHome(t, "subtitles", "failed", error);
    await home.poll("failed");
    const jobs = home.document.querySelector('[aria-controls="job-queue"]');
    if (jobs.getAttribute("aria-expanded") !== "true") await act(async () => jobs.click());
    assert.equal(home.document.querySelector(".queueError").textContent, error);
    const help = home.document.querySelector(".renameHelpLink");
    assert.equal(Boolean(help), showHelp);
    if (!showHelp) return;
    await act(async () => help.click());
    const dialog = home.document.getElementById("rename-help");
    assert.equal(dialog.open, true);
    assert.equal(dialog.querySelectorAll("li").length, 4);
    assert.match(dialog.textContent, /English movie or series title/);
    assert.match(dialog.textContent, /Create subtitles/);
    await act(async () => dialog.querySelector(".dialogActions button").click());
    assert.equal(dialog.open, false);
    await act(async () => help.click());
    await act(async () => dialog.querySelector('[aria-label="Close rename help"]').click());
    assert.equal(dialog.open, false);
    await act(async () => help.click());
    await act(async () => dialog.click());
    assert.equal(dialog.open, false);
  });
}

test("embedded languages appear beside bilingual sidecars after a job refresh", async (t) => {
  const home = await mountHome(t, "subtitles", "completed", undefined, ["eng", "zh-Hans"]);
  assert.ok(home.document.querySelector('[aria-label="English embedded subtitle"]'));
  assert.ok(home.document.querySelector('[aria-label="Simplified Chinese embedded subtitle"]'));
  assert.equal(home.document.querySelector('[aria-label="No sidecar subtitle"]'), null);
  await home.poll("completed");
  assert.ok(home.document.querySelector('[title="Simplified Chinese · Episode.zh-Hans.srt"]'));
  assert.ok(home.document.querySelector('[aria-label="English embedded subtitle"]'));
});
