// @vitest-environment node
import { createServer, type Server } from "node:http";
import type { AddressInfo } from "node:net";
import { afterAll, beforeAll, expect, it } from "vitest";
import { AbortedError, OfflineError } from "./errors";
import { nodeStreamTransport } from "./transport-node-sse";

let server: Server; let base = "";
beforeAll(async () => {
  server = createServer((req, res) => {
    if (req.url === "/ok") {
      let body = ""; req.on("data", (c) => (body += String(c)));
      req.on("end", () => {
        res.writeHead(200, { "Content-Type": "text/event-stream" });
        res.write(`event: echo\ndata: ${JSON.stringify({ body, user: req.headers["x-evowiki-user"] })}\n\n`);
        res.write("event: done\ndata: {}\n\n"); res.end();
      });
    } else if (req.url === "/hang") { res.writeHead(200, { "Content-Type": "text/event-stream" }); res.write("event: a\ndata: {}\n\n"); }
    else { res.writeHead(404, { "Content-Type": "application/json" }); res.end('{"detail":"nope"}'); }
  });
  await new Promise<void>((r) => server.listen(0, "127.0.0.1", r));
  base = `http://127.0.0.1:${(server.address() as AddressInfo).port}`;
});
afterAll(() => new Promise<void>((r) => { server.closeAllConnections(); server.close(() => r()); }));

it("streams 2xx chunks and sends headers and body", async () => {
  let got = "";
  const res = await nodeStreamTransport.stream({ method: "POST", url: `${base}/ok`, headers: { "X-EvoWiki-User": "u" }, body: '{"q":1}' },
    (c) => (got += c));
  expect(res.status).toBe(200);
  expect(got).toContain('"body":"{\\"q\\":1}"');
  expect(got).toContain('"user":"u"');
});

it("returns the error body as text without calling onChunk", async () => {
  let called = false;
  const res = await nodeStreamTransport.stream({ method: "GET", url: `${base}/missing`, headers: {} }, () => (called = true));
  expect(res).toEqual({ status: 404, text: '{"detail":"nope"}' });
  expect(called).toBe(false);
});

it("rejects with AbortedError when the caller aborts mid-stream", async () => {
  const ac = new AbortController();
  const p = nodeStreamTransport.stream({ method: "GET", url: `${base}/hang`, headers: {}, signal: ac.signal }, () => ac.abort());
  await expect(p).rejects.toBeInstanceOf(AbortedError);
});

it("rejects with OfflineError when nothing listens", async () => {
  await expect(nodeStreamTransport.stream({ method: "GET", url: "http://127.0.0.1:1/x", headers: {} }, () => {}))
    .rejects.toBeInstanceOf(OfflineError);
});
