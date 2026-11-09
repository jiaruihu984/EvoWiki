import { expect, it } from "vitest";
import { SseDataError, SseDecoder } from "./sse";

const feed = (chunks: string[]) => {
  const d = new SseDecoder();
  return chunks.flatMap((c) => d.push(c));
};

it("returns complete events and keeps the incomplete tail for the next chunk", () => {
  const d = new SseDecoder();
  expect(d.push('event: sentence\ndata: {"text":"a","cites":[]}\n\nevent: done\ndata: {"acc')).toEqual([{ event: "sentence", data: { text: "a", cites: [] } }]);
  expect(d.push('epted":1}\n\n')).toEqual([{ event: "done", data: { accepted: 1 } }]);
});

it("skips the server's keep-alive comment frames, alone or between events", () => {
  expect(feed([": ping\n\n", 'event: outline\ndata: {"n":1}\n\n: ping\n\n', ": ping\n\n", 'event: done\ndata: {}\n\n'])
    .map((e) => e.event)).toEqual(["outline", "done"]);
  expect(feed([": ping\n\n: ping\n\n"])).toEqual([]);
});

it("handles CRLF, lone CR, a leading BOM and the default event name", () => {
  expect(feed(['data: {"x":1}\r\n\r\n'])).toEqual([{ event: "message", data: { x: 1 } }]);
  expect(feed(['event: a\rdata: {"x":2}\r\r'])).toEqual([{ event: "a", data: { x: 2 } }]);
  expect(feed(['﻿event: a\ndata: {"x":3}\n\n'])).toEqual([{ event: "a", data: { x: 3 } }]);
  expect(feed(["event: a\r", '\ndata: {"x":4}\r', "\n\r", "\n"])).toEqual([{ event: "a", data: { x: 4 } }]);   // CRLF split across chunks
});

it("decodes events split across arbitrary chunk boundaries, multi-byte text included", () => {
  const wire = ': ping\n\nevent: context\ndata: {"depth":2,"t":"主成分"}\nid: m1:0\n\nevent: done\ndata: {"accepted":0,"dropped":0}\n\n';
  const out = feed([...wire]);
  expect(out).toEqual([{ event: "context", data: { depth: 2, t: "主成分" }, id: "m1:0" }, { event: "done", data: { accepted: 0, dropped: 0 } }]);
});

it("throws SseDataError for an event whose data is not JSON, including empty data", () => {
  expect(() => new SseDecoder().push("event: a\ndata: {nope\n\n")).toThrow(SseDataError);
  expect(() => new SseDecoder().push("event: a\ndata:\n\n")).toThrow(SseDataError);
});
