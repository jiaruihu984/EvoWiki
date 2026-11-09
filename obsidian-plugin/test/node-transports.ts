import { OfflineError } from "../src/api/errors";
import type { JsonTransport } from "../src/api/client";

/** Contract tests run in Node, where requestUrl does not exist; same contract as wiki/transport.ts obsidianWikiHttp. */
export const nodeJsonTransport: JsonTransport = {
  async request(req) {
    try {
      // eslint-disable-next-line no-restricted-globals -- test-only transport
      const r = await fetch(req.url, { method: req.method, headers: req.headers, body: req.body });
      return { status: r.status, text: await r.text() };
    } catch (e) { throw new OfflineError(e); }
  },
};
