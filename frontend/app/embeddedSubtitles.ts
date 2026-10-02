export type EmbeddedSubtitleStatus = "available" | "unsupported" | "unavailable";

export type EmbeddedSubtitleMetadata = {
  path: string;
  status: EmbeddedSubtitleStatus;
  languages: Array<string | null>;
};

export type EmbeddedSubtitleCellState =
  | { status: "loading" }
  | EmbeddedSubtitleMetadata;

function parseMetadataLine(line: string): EmbeddedSubtitleMetadata {
  const value = JSON.parse(line) as Partial<EmbeddedSubtitleMetadata>;
  if (
    typeof value.path !== "string"
    || !["available", "unsupported", "unavailable"].includes(value.status ?? "")
    || !Array.isArray(value.languages)
    || value.languages.some((language) => language !== null && typeof language !== "string")
  ) {
    throw new Error("Embedded subtitle metadata response is invalid");
  }
  return value as EmbeddedSubtitleMetadata;
}

export async function consumeEmbeddedSubtitleStream(
  response: Response,
  onMetadata: (metadata: EmbeddedSubtitleMetadata) => void,
): Promise<void> {
  if (!response.ok) {
    const body = await response.json().catch(() => ({})) as { detail?: string };
    throw new Error(body.detail || `Request failed (${response.status})`);
  }
  if (!response.body) throw new Error("Embedded subtitle metadata stream is unavailable");

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffered = "";
  while (true) {
    const { done, value } = await reader.read();
    buffered += decoder.decode(value, { stream: !done });
    let newline = buffered.indexOf("\n");
    while (newline >= 0) {
      const line = buffered.slice(0, newline).trim();
      buffered = buffered.slice(newline + 1);
      if (line) onMetadata(parseMetadataLine(line));
      newline = buffered.indexOf("\n");
    }
    if (done) break;
  }
  const finalLine = buffered.trim();
  if (finalLine) onMetadata(parseMetadataLine(finalLine));
}

export async function fetchEmbeddedSubtitleStream(
  url: string,
  signal: AbortSignal,
  onMetadata: (metadata: EmbeddedSubtitleMetadata) => void,
): Promise<void> {
  const response = await fetch(url, {
    cache: "no-store",
    headers: { Accept: "application/x-ndjson" },
    signal,
  });
  await consumeEmbeddedSubtitleStream(response, onMetadata);
}

export function shouldShowLanguageDash(
  sidecarCount: number,
  embedded?: EmbeddedSubtitleCellState,
): boolean {
  if (sidecarCount > 0 || embedded?.status === "loading") return false;
  return embedded?.status !== "available" || embedded.languages.length === 0;
}
