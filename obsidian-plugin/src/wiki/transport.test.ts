import { requestUrl } from "obsidian";
import { beforeEach, expect, it, vi } from "vitest";
import { OfflineError } from "../api/errors";
import { obsidianWikiHttp } from "./transport";

beforeEach(() => { vi.mocked(requestUrl).mockReset(); });

it("passes status and text through without throwing on HTTP errors", async () => {
  vi.mocked(requestUrl).mockResolvedValueOnce({ status: 409, text: '{"detail":"x"}' } as never);
  await expect(obsidianWikiHttp.request({ method: "POST", url: "http://h/wiki/curate", headers: { a: "b" }, body: "{}" }))
    .resolves.toEqual({ status: 409, text: '{"detail":"x"}' });
  expect(vi.mocked(requestUrl).mock.calls[0]![0]).toEqual(
    { url: "http://h/wiki/curate", method: "POST", headers: { a: "b" }, body: "{}", throw: false });
});

it.each(["PUT", "DELETE"] as const)("passes %s through", async (method) => {
  vi.mocked(requestUrl).mockResolvedValueOnce({ status: 204, text: "" } as never);
  await obsidianWikiHttp.request({ method, url: "http://h/wiki/x", headers: {} });
  expect(vi.mocked(requestUrl).mock.calls[0]![0]).toMatchObject({ method, throw: false });
});

it("turns a network failure into OfflineError", async () => {
  vi.mocked(requestUrl).mockRejectedValueOnce(new Error("net::ERR_CONNECTION_REFUSED"));
  await expect(obsidianWikiHttp.request({ method: "GET", url: "http://h/wiki", headers: {} })).rejects.toBeInstanceOf(OfflineError);
});

it("passes an ArrayBuffer body and its content type through (multipart upload)", async () => {
  vi.mocked(requestUrl).mockResolvedValueOnce({ status: 201, text: "{}" } as never);
  const body = new Uint8Array([1, 2, 3]).buffer;
  await obsidianWikiHttp.request({ method: "POST", url: "http://h/wiki/sources", headers: { "Content-Type": "multipart/form-data; boundary=b" },
    body, contentType: "multipart/form-data; boundary=b" });
  expect(vi.mocked(requestUrl).mock.calls[0]![0]).toEqual({ url: "http://h/wiki/sources", method: "POST",
    headers: { "Content-Type": "multipart/form-data; boundary=b" }, body, contentType: "multipart/form-data; boundary=b", throw: false });
});

it("binary: returns the response bytes (text only for an error body)", async () => {
  const bytes = new Uint8Array([37, 80, 68, 70]).buffer;
  vi.mocked(requestUrl).mockResolvedValueOnce({ status: 200, get text(): string { throw new Error("not text"); }, arrayBuffer: bytes } as never);
  await expect(obsidianWikiHttp.request({ method: "GET", url: "http://h/wiki/sources/s/file", headers: {}, binary: true }))
    .resolves.toEqual({ status: 200, text: "", bytes });
  vi.mocked(requestUrl).mockResolvedValueOnce({ status: 404, text: '{"detail":"x"}', arrayBuffer: new ArrayBuffer(0) } as never);
  await expect(obsidianWikiHttp.request({ method: "GET", url: "http://h/wiki/sources/s/file", headers: {}, binary: true }))
    .resolves.toMatchObject({ status: 404, text: '{"detail":"x"}' });
});
