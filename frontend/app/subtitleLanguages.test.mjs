import assert from "node:assert/strict";
import test from "node:test";
import { getSubtitleLanguage, normalizeSubtitleLanguage } from "./subtitleLanguages.ts";

test("maps detected subtitle languages to accessible country flags", () => {
  assert.deepEqual(getSubtitleLanguage("en"), { flag: "🇬🇧", label: "English" });
  assert.deepEqual(getSubtitleLanguage("zh-tw"), { flag: "🇹🇼", label: "Traditional Chinese" });
  assert.deepEqual(getSubtitleLanguage("zh-cn+en"), {
    flag: "🇨🇳🇬🇧",
    label: "Simplified Chinese & English",
  });
  assert.deepEqual(getSubtitleLanguage(null), { flag: "🏳️", label: "Unknown language" });
});

test("canonicalizes legacy and BCP-47 container language tags", () => {
  assert.equal(normalizeSubtitleLanguage("eng"), "en");
  assert.equal(normalizeSubtitleLanguage("fra"), "fr");
  assert.equal(normalizeSubtitleLanguage("zh_Hans"), "zh-Hans");
  assert.equal(normalizeSubtitleLanguage("pt_BR"), "pt-BR");
  assert.equal(normalizeSubtitleLanguage("und"), null);
  assert.deepEqual(getSubtitleLanguage("eng"), { flag: "🇬🇧", label: "English" });
  assert.deepEqual(getSubtitleLanguage("zh-Hans"), { flag: "🇨🇳", label: "Simplified Chinese" });
});
