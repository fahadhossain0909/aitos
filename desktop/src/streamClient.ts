import { consoleFetch } from "./operatorSession";
import { parseSnapshot } from "./consoleClient";
import type { Snapshot } from "./data";

const MAX_BUFFER = 2 * 1024 * 1024;

/** Split a text buffer into complete SSE frames plus the unfinished remainder. */
export function splitFrames(buffer: string): { frames: string[]; rest: string } {
  const parts = buffer.replace(/\r\n/g, "\n").split("\n\n");
  return { frames: parts.slice(0, -1), rest: parts[parts.length - 1] ?? "" };
}

/** Decode one frame. Keep-alives yield null; anything unexpected throws. */
export function decodeFrame(frame: string): unknown | null {
  if (frame === ": keepalive") return null;
  const lines = frame.split("\n");
  if (
    lines.length !== 2 ||
    lines[0] !== "event: snapshot" ||
    !lines[1].startsWith("data: ")
  )
    throw new Error("Unexpected stream frame");
  return JSON.parse(lines[1].slice(6));
}

export type StreamEnd = "ended" | "unsupported" | "failed";

/**
 * Read one bounded snapshot stream. Every snapshot goes through the same strict
 * parser as polling, so streaming never widens what the console will accept.
 */
export async function readSnapshotStream(
  signal: AbortSignal,
  onSnapshot: (snapshot: Snapshot) => void,
): Promise<StreamEnd> {
  const response = await consoleFetch("/console/v1/stream", {
    signal,
    headers: { Accept: "text/event-stream" },
  });
  if (response.status === 404 || response.status === 405) return "unsupported";
  if (
    !response.ok ||
    !response.body ||
    !(response.headers.get("content-type") ?? "").startsWith("text/event-stream")
  )
    return "failed";
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let rest = "";
  for (;;) {
    const { done, value } = await reader.read();
    if (done) return "ended";
    const parsed = splitFrames(rest + decoder.decode(value, { stream: true }));
    rest = parsed.rest;
    if (rest.length > MAX_BUFFER) throw new Error("Stream frame too large");
    for (const frame of parsed.frames) {
      const payload = decodeFrame(frame);
      if (payload !== null) onSnapshot(parseSnapshot(payload));
    }
  }
}
