import { describe, expect, it } from "vitest";
import { decodeFrame, splitFrames } from "./streamClient";

describe("snapshot stream framing", () => {
  it("keeps partial frames until they are complete", () => {
    const first = splitFrames('event: snapshot\ndata: {"a":1}\n\nevent: sn');
    expect(first.frames).toEqual(['event: snapshot\ndata: {"a":1}']);
    expect(first.rest).toBe("event: sn");
    expect(splitFrames(first.rest + 'apshot\ndata: {}\n\n').frames).toHaveLength(1);
  });
  it("normalises CRLF framing", () => {
    expect(splitFrames("a\r\n\r\nb").frames).toEqual(["a"]);
  });
  it("ignores keep-alives and decodes snapshot payloads", () => {
    expect(decodeFrame(": keepalive")).toBeNull();
    expect(decodeFrame('event: snapshot\ndata: {"schema_version":1}')).toEqual({ schema_version: 1 });
  });
  it("rejects anything that is not exactly a snapshot frame", () => {
    for (const bad of [
      "event: order\ndata: {}",
      "data: {}",
      'event: snapshot\ndata: {}\nretry: 1',
      "event: snapshot\ndata: not-json",
      ": comment",
    ])
      expect(() => decodeFrame(bad), bad).toThrow();
  });
});
