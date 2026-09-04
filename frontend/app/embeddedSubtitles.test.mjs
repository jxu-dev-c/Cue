import assert from "node:assert/strict";
import test from "node:test";
import {
  consumeEmbeddedSubtitleStream,
  shouldShowLanguageDash,
} from "./embeddedSubtitles.ts";

test("decodes metadata records split across stream chunks", async () => {
  const encoder = new TextEncoder();
  const response = new Response(new ReadableStream({
    start(controller) {
      controller.enqueue(encoder.encode('{"path":"One.mkv","status":"available","lang'));
      controller.enqueue(encoder.encode('uages":["eng",null]}\n{"path":"Two.mp4","status":"unavailable",'));
      controller.enqueue(encoder.encode('"languages":[]}'));
      controller.close();
    },
  }), { status: 200 });
  const records = [];

  await consumeEmbeddedSubtitleStream(response, (metadata) => records.push(metadata));

  assert.deepEqual(records, [
    { path: "One.mkv", status: "available", languages: ["eng", null] },
    { path: "Two.mp4", status: "unavailable", languages: [] },
  ]);
});

test("rejects malformed stream records", async () => {
  const response = new Response('{"path":"Movie.mkv","status":"surprise","languages":[]}\n');
  await assert.rejects(
    consumeEmbeddedSubtitleStream(response, () => {}),
    /metadata response is invalid/,
  );
});

test("shows a dash only after loading when no language is available", () => {
  assert.equal(shouldShowLanguageDash(0, { status: "loading" }), false);
  assert.equal(shouldShowLanguageDash(0, { path: "Movie", status: "available", languages: [] }), true);
  assert.equal(shouldShowLanguageDash(0, { path: "Movie", status: "unavailable", languages: [] }), true);
  assert.equal(shouldShowLanguageDash(1, { path: "Movie", status: "unsupported", languages: [] }), false);
});
