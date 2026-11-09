import { requestUrl } from "obsidian";
import { OfflineError } from "../api/errors";
import type { WikiHttp } from "./api";

/**
 * The plugin's one HTTP transport (the account, wiki, assistant, 用量 and goal clients all use it; streams go through
 * nodeStreamTransport). requestUrl runs outside the renderer's CORS checks (the service has no CORS middleware). Any
 * method, a string or ArrayBuffer body with its content type (multipart upload), and the response bytes when the request
 * asks for them.
 */
export const obsidianWikiHttp: WikiHttp = {
  async request(req) {
    try {
      const r = await requestUrl({ url: req.url, method: req.method, headers: req.headers, body: req.body,
        ...(req.contentType === undefined ? {} : { contentType: req.contentType }), throw: false });
      if (!req.binary) return { status: r.status, text: r.text };
      const ok = r.status >= 200 && r.status < 300;
      return ok ? { status: r.status, text: "", bytes: r.arrayBuffer } : { status: r.status, text: r.text };
    } catch (e) {
      throw new OfflineError(e);
    }
  },
};
