import * as http from "node:http";
import * as https from "node:https";
import type { StreamTransport } from "./client";
import { AbortedError, OfflineError } from "./errors";

/** SSE over Node http (Electron main-world Node; the reason the plugin is desktop-only). */
export const nodeStreamTransport: StreamTransport = {
  stream(req, onChunk) {
    return new Promise((resolve, reject) => {
      const signal = req.signal;
      const fail = (e: unknown) => reject(signal?.aborted ? new AbortedError() : new OfflineError(e));
      if (signal?.aborted) { reject(new AbortedError()); return; }
      const url = new URL(req.url);
      const mod = url.protocol === "https:" ? https : http;
      const r = mod.request(url, { method: req.method, headers: { ...req.headers, Accept: "text/event-stream" } }, (res) => {
        res.setEncoding("utf8"); // decodes multi-byte characters split across chunks
        const status = res.statusCode ?? 0;
        const ok = status >= 200 && status < 300;
        let errText = "";
        let ended = false;
        res.on("data", (c: string) => { if (ok) onChunk(c); else errText += c; });
        res.on("end", () => { ended = true; resolve({ status, text: errText }); });
        res.on("error", fail);
        res.on("close", () => { if (!ended) fail(new Error("stream closed early")); });
      });
      const onAbort = () => r.destroy();
      r.on("error", fail);
      r.on("close", () => signal?.removeEventListener("abort", onAbort));
      signal?.addEventListener("abort", onAbort, { once: true });
      if (req.body !== undefined) r.write(req.body);
      r.end();
    });
  },
};
