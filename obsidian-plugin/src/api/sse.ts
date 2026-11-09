import { createParser, type EventSourceParser } from "eventsource-parser";

/** One server-sent event: `event` defaults to "message"; `data` is the parsed JSON; `id` when the server sent one. */
export interface SseMessage { event: string; data: unknown; id?: string }

/** An event whose `data` is not JSON (the backend sends every payload as one JSON `data:` line). */
export class SseDataError extends Error {
  constructor() { super("event data is not JSON"); this.name = "SseDataError"; }
}

/**
 * Incremental SSE decoding with eventsource-parser (spec framing: LF / CR / CRLF, a leading BOM, fields split
 * across chunks). Comment frames (the server's `: ping` keep-alive) and blocks without data are skipped.
 * `push` returns the events the chunk completed; it throws SseDataError for an event whose data is not JSON.
 */
export class SseDecoder {
  private ready: SseMessage[] = [];
  private bad = false;
  private readonly parser: EventSourceParser = createParser({
    onEvent: (m) => {
      if (this.bad) return;
      let data: unknown;
      try { data = JSON.parse(m.data); } catch { this.bad = true; return; }
      const msg: SseMessage = { event: m.event ?? "message", data };
      if (m.id !== undefined) msg.id = m.id;
      this.ready.push(msg);
    },
  });

  push(chunk: string): SseMessage[] {
    this.parser.feed(chunk);
    if (this.bad) throw new SseDataError();
    const out = this.ready;
    this.ready = [];
    return out;
  }
}
