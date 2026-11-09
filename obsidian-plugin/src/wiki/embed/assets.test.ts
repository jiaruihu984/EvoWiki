import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "../../api/errors";
import { FakeWikiApi } from "../fake";
import { DEMO_ASSET, DEMO_INTERACTIVE_HTML, embedBlock, sha256Hex } from "../fixtures";
import type { AssetMeta } from "../schema";
import { DEMO_IMG_B64 } from "../fixtures-demo-img";
import {
  AssetFetcher, type AssetSource, type AssetStore, assetIdsIn, assetsFolder, evouiAssetIdsIn, imageAssetIdsIn, MAX_ASSET_BYTES, MAX_IMAGE_B64,
  webpBytes,
} from "./assets";

/** An in-memory vault: create rejects an existing path (like vault.create); every write is recorded. */
function memStore(initial: Record<string, string> = {}) {
  const files = new Map(Object.entries(initial));
  const folders: string[] = [];
  const writes: string[] = [];
  const binary = new Map<string, Uint8Array>();
  const store: AssetStore = {
    occupied: (p) => Promise.resolve(files.has(p) || folders.includes(p)),
    ensureFolder: (p) => { folders.push(p); return Promise.resolve(); },
    create: (p, t) => {
      if (files.has(p)) return Promise.reject(new Error("exists"));
      files.set(p, t); writes.push(p); return Promise.resolve();
    },
    createBinary: (p, d) => {
      if (files.has(p) || binary.has(p)) return Promise.reject(new Error("exists"));
      binary.set(p, new Uint8Array(d)); writes.push(p); return Promise.resolve();
    },
  };
  return { store, files, folders, writes, binary };
}

const PATH = "EvoWiki/assets/demo-z-steps.html";
let warn: ReturnType<typeof vi.spyOn>;
beforeEach(() => { warn = vi.spyOn(console, "warn").mockImplementation(() => undefined); });
afterEach(() => { warn.mockRestore(); });

function fetcher(api: AssetSource, initial: Record<string, string> = {}, onWritten = vi.fn()) {
  const m = memStore(initial);
  return { ...m, onWritten, f: new AssetFetcher(() => api, m.store, () => "EvoWiki", onWritten) };
}
const source = (meta: Partial<AssetMeta>, html: string): AssetSource & { assetMeta: ReturnType<typeof vi.fn>; assetHtml: ReturnType<typeof vi.fn> } => ({
  assetGenerated: () => Promise.resolve({ generated: false }),
  assetMeta: vi.fn(() => Promise.resolve({ ...DEMO_ASSET, ...meta })),
  assetHtml: vi.fn(() => Promise.resolve(html)),
});

describe("assetIdsIn", () => {
  it("the asset ids of embedded-html blocks whose src is assets/<id>.html, each once", () => {
    const other = embedBlock({ ...DEMO_ASSET, asset_id: "b_2", interactive_id: "b-two" });
    const md = `intro\n\n${embedBlock(DEMO_ASSET)}\n\ntext\n\n${other}\n\n${embedBlock(DEMO_ASSET)}\n`
      + "```embedded-html\ntype: embedded-html\nid: x-1\nsrc: mine/x.html\ntitle: t\nheight: 400\n```\n"
      + "```embedded-html\ntype: embedded-html\nid: x-2\nsrc: assets/bad.html\ntitle: t\nheight: 9\n```\n"
      + "```js\nsrc: assets/nope.html\n```\n";
    expect(assetIdsIn(md)).toEqual(["demo-z-steps", "b_2"]);
    expect(assetIdsIn(md.replace(/\n/g, "\r\n"))).toEqual(["demo-z-steps", "b_2"]);
    expect(assetIdsIn("no blocks")).toEqual([]);
  });

  it("the assets folder sits under a valid wiki folder only", () => {
    expect(assetsFolder("EvoWiki")).toBe("EvoWiki/assets");
    expect(assetsFolder("/Wiki")).toBeNull();
    expect(assetsFolder("../Wiki")).toBeNull();
    expect(assetsFolder("")).toBeNull();
  });
});

describe("AssetFetcher", () => {
  it("the fake's demo interactive: meta, html, sha256 matches → written to <notes>/assets/<id>.html only", async () => {
    const api = new FakeWikiApi();
    const r = fetcher(api);
    expect(await r.f.retrieve("demo-z-steps")).toBe("written");
    expect(r.writes).toEqual([PATH]);
    expect(r.files.get(PATH)).toBe(DEMO_INTERACTIVE_HTML);
    expect(r.folders).toEqual(["EvoWiki/assets"]);
    expect(r.onWritten).toHaveBeenCalledOnce();
    expect(r.onWritten).toHaveBeenCalledWith("demo-z-steps");
    expect(r.f.pathFor("demo-z-steps")).toBe(PATH);
  });

  it("a sha256 mismatch writes nothing and warns", async () => {
    const r = fetcher(source({}, DEMO_INTERACTIVE_HTML + " "));
    expect(await r.f.retrieve("demo-z-steps")).toBe("failed");
    expect(r.writes).toEqual([]);
    expect(warn).toHaveBeenCalled();
    expect(r.onWritten).not.toHaveBeenCalled();
  });

  it("meta for another id or without a sha256 writes nothing", async () => {
    expect(await fetcher(source({ asset_id: "other" }, DEMO_INTERACTIVE_HTML)).f.retrieve("demo-z-steps")).toBe("failed");
    expect(await fetcher(source({ sha256: "nope" }, DEMO_INTERACTIVE_HTML)).f.retrieve("demo-z-steps")).toBe("failed");
  });

  it("more than 200000 bytes (UTF-8) writes nothing, even with a matching sha256", async () => {
    const big = "é".repeat(MAX_ASSET_BYTES / 2 + 1); // 2 bytes each
    const r = fetcher(source({ sha256: sha256Hex(big) }, big));
    expect(await r.f.retrieve("demo-z-steps")).toBe("failed");
    expect(r.writes).toEqual([]);
    const ok = "a".repeat(MAX_ASSET_BYTES);
    expect(await fetcher(source({ sha256: sha256Hex(ok) }, ok)).f.retrieve("demo-z-steps")).toBe("written");
  });

  it("an existing file is not fetched again (no request, nothing written)", async () => {
    const api = source({}, DEMO_INTERACTIVE_HTML);
    const r = fetcher(api, { [PATH]: "old" });
    expect(await r.f.retrieve("demo-z-steps")).toBe("present");
    expect(api.assetMeta).not.toHaveBeenCalled();
    expect(r.files.get(PATH)).toBe("old");
  });

  it("503 (refine off on the server) is silent; another failure warns; nothing written either way", async () => {
    const off: AssetSource = { assetGenerated: () => Promise.resolve({ generated: false }), assetMeta: () => Promise.reject(new ApiError(503, "off")), assetHtml: () => Promise.reject(new Error("x")) };
    const r = fetcher(off);
    expect(await r.f.retrieve("demo-z-steps")).toBe("unavailable");
    expect(warn).not.toHaveBeenCalled();
    const gone: AssetSource = { assetGenerated: () => Promise.resolve({ generated: false }), assetMeta: () => Promise.reject(new ApiError(404, "none")), assetHtml: () => Promise.reject(new Error("x")) };
    const g = fetcher(gone);
    expect(await g.f.retrieve("demo-z-steps")).toBe("failed");
    expect(warn).toHaveBeenCalledOnce();
    expect([...r.writes, ...g.writes]).toEqual([]);
  });

  it("an invalid id or wiki folder: no request, nothing written", async () => {
    const api = source({}, DEMO_INTERACTIVE_HTML);
    const r = fetcher(api);
    for (const id of ["../x", "a/b", "a.b", "", "x".repeat(101), "con", "NUL", "com3", "lpt1"]) expect(await r.f.retrieve(id)).toBe("failed");
    const m = memStore();
    expect(await new AssetFetcher(() => api, m.store, () => "../Wiki").retrieve("demo-z-steps")).toBe("failed");
    expect(api.assetMeta).not.toHaveBeenCalled();
    expect([...r.writes, ...m.writes]).toEqual([]);
  });

  it("two fetches of one id share one request; retrieveAll goes through every id", async () => {
    const api = source({}, DEMO_INTERACTIVE_HTML);
    const r = fetcher(api);
    const [a, b] = await Promise.all([r.f.retrieve("demo-z-steps"), r.f.retrieve("demo-z-steps")]);
    expect([a, b]).toEqual(["written", "written"]);
    expect(api.assetMeta).toHaveBeenCalledOnce();
    await r.f.retrieveAll(["demo-z-steps", "missing-one"]);
    expect(r.writes).toEqual([PATH]);
  });
});

describe("evoui assets (kind evoui, saved as <id>.json)", () => {
  const BLOCK = JSON.stringify({ type: "evoui", title: "t" });
  const JSON_PATH = "EvoWiki/assets/ia_9.json";
  const evoSource = (kind: "html" | "evoui", text = BLOCK) => source({ asset_id: "ia_9", kind, sha256: sha256Hex(text) }, text);

  it("evouiAssetIdsIn lists the asset: references of evoui fences, each once; inline JSON blocks are not assets", () => {
    const md = "```evoui\nasset: ia_9\n```\ntext\n```evoui\nasset: ia_9\n```\n```evoui\n{\"type\":\"evoui\"}\n```\n```evoui\nasset: ../x\n```\n";
    expect(evouiAssetIdsIn(md)).toEqual(["ia_9"]);
    expect(evouiAssetIdsIn(md.replace(/\n/g, "\r\n"))).toEqual(["ia_9"]);
    expect(assetIdsIn(md)).toEqual([]);
  });

  it("meta.kind evoui, sha256 matches → written to assets/<id>.json, never overwritten", async () => {
    const r = fetcher(evoSource("evoui"));
    expect(r.f.pathFor("ia_9", "evoui")).toBe(JSON_PATH);
    expect(await r.f.retrieve("ia_9", "evoui")).toBe("written");
    expect(r.writes).toEqual([JSON_PATH]);
    expect(r.files.get(JSON_PATH)).toBe(BLOCK);
    const again = fetcher(evoSource("evoui"), { [JSON_PATH]: "old" });
    expect(await again.f.retrieve("ia_9", "evoui")).toBe("present");
    expect(again.files.get(JSON_PATH)).toBe("old");
  });

  it("a sha256 mismatch, an oversized file or the wrong kind writes nothing", async () => {
    const bad = fetcher(source({ asset_id: "ia_9", kind: "evoui" }, BLOCK));
    expect(await bad.f.retrieve("ia_9", "evoui")).toBe("failed");
    expect(await fetcher(evoSource("html")).f.retrieve("ia_9", "evoui")).toBe("failed");
    expect(await fetcher(evoSource("evoui")).f.retrieve("ia_9", "html")).toBe("failed");
    const big = "a".repeat(MAX_ASSET_BYTES + 1);
    expect(await fetcher(evoSource("evoui", big)).f.retrieve("ia_9", "evoui")).toBe("failed");
    expect(bad.writes).toEqual([]);
  });

  it("retrieveAll takes the kind", async () => {
    const r = fetcher(evoSource("evoui"));
    await r.f.retrieveAll(["ia_9"], "evoui");
    expect(r.writes).toEqual([JSON_PATH]);
  });
});

describe("image assets (kind image, saved as the binary <id>.webp)", () => {
  const IMG_PATH = "EvoWiki/assets/ia_7.webp";
  const imgSource = (kind: "html" | "image", text = DEMO_IMG_B64) => source({ asset_id: "ia_7", kind, sha256: sha256Hex(text) }, text);
  const fence = (asset: string) => "```evowiki-image\nasset: " + asset + "\nalt: a\ntitle: t\nlicense: CC0\nsource: Wikimedia Commons\n"
    + "source_url: https://commons.wikimedia.org/wiki/File:X.png\nwidth: 10\nheight: 10\n```";

  it("imageAssetIdsIn lists the evowiki-image fences' assets, each once; a fence that does not parse is skipped", () => {
    const md = `${fence("ia_7")}\ntext\n${fence("ia_7")}\n${fence("ia_8")}\n\`\`\`evowiki-image\nasset: ia_9\n\`\`\`\n`;
    expect(imageAssetIdsIn(md)).toEqual(["ia_7", "ia_8"]);
    expect(imageAssetIdsIn(md.replace(/\n/g, "\r\n"))).toEqual(["ia_7", "ia_8"]);
    expect(assetIdsIn(md)).toEqual([]);
    expect(evouiAssetIdsIn(md)).toEqual([]);
  });

  it("meta.kind image, sha256 of the base64 matches, a WebP → the bytes written to assets/<id>.webp, never overwritten", async () => {
    const r = fetcher(imgSource("image"));
    expect(r.f.pathFor("ia_7", "image")).toBe(IMG_PATH);
    expect(await r.f.retrieve("ia_7", "image")).toBe("written");
    expect(r.writes).toEqual([IMG_PATH]);
    expect(Buffer.from(r.binary.get(IMG_PATH)!).toString("base64")).toBe(DEMO_IMG_B64);
    expect(r.onWritten).toHaveBeenCalledWith("ia_7");
    const again = fetcher(imgSource("image"), { [IMG_PATH]: "old" });
    expect(await again.f.retrieve("ia_7", "image")).toBe("present");
    expect(again.writes).toEqual([]);
  });

  it("a sha256 mismatch, too much text, not a WebP or the wrong kind writes nothing", async () => {
    const bad = fetcher(source({ asset_id: "ia_7", kind: "image" }, DEMO_IMG_B64));
    expect(await bad.f.retrieve("ia_7", "image")).toBe("failed");
    expect(await fetcher(imgSource("html")).f.retrieve("ia_7", "image")).toBe("failed");
    const png = Buffer.from("\x89PNG\r\n\x1a\n0000000000000000", "latin1").toString("base64");
    expect(await fetcher(imgSource("image", png)).f.retrieve("ia_7", "image")).toBe("failed");
    const big = "A".repeat(MAX_IMAGE_B64 + 4);
    expect(await fetcher(imgSource("image", big)).f.retrieve("ia_7", "image")).toBe("failed");
    expect(bad.writes).toEqual([]);
  });

  it("webpBytes: only base64 of a RIFF…WEBP file", () => {
    expect(webpBytes(DEMO_IMG_B64)?.length).toBeGreaterThan(1000);
    expect(webpBytes("not base64!")).toBeNull();
    expect(webpBytes(Buffer.from("RIFF0000WAVEfmt ").toString("base64"))).toBeNull();
  });
});
