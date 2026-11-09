import { describe, expect, it, vi } from "vitest";
import { WIKI_COPY } from "../copy";
import { EmbedHub } from "./embed-block";
import { captionEl, type ImagePort, mountImageBlock, parseImageBlock, safeUrl } from "./image-block";

/** The fence body exactly as deep/contracts.py image_fence writes it (tests/deep/test_images.py pins the same text). */
const SRC = ["asset: ia_7", "alt: 左边是原图，右边是错切以后", "title: Mona Lisa with eigenvector", "author: J. Finkelstein",
  "license: CC BY-SA 4.0", "license_url: https://creativecommons.org/licenses/by-sa/4.0/", "source: Wikimedia Commons",
  "source_url: https://commons.wikimedia.org/wiki/File:Mona_Lisa_with_eigenvector.png", "width: 640", "height: 544"].join("\n");
const PATH = "EvoWiki/Wiki/assets/ia_7.webp";

const flush = () => new Promise((r) => setTimeout(r, 0));

function port(present: boolean, over: Partial<ImagePort> = {}): ImagePort & { files: Set<string> } {
  const files = new Set(present ? [PATH] : []);
  return { files, resource: (p) => (files.has(p) ? `app://local/${p}` : null), canFetch: () => true,
    assetPath: (id) => `EvoWiki/Wiki/assets/${id}.webp`,
    retrieve: vi.fn(() => { files.add(PATH); return Promise.resolve(true); }), ...over };
}

describe("parseImageBlock", () => {
  it("reads every field; optional author / license_url may be absent", () => {
    expect(parseImageBlock(SRC)).toEqual({ asset: "ia_7", alt: "左边是原图，右边是错切以后", title: "Mona Lisa with eigenvector",
      author: "J. Finkelstein", license: "CC BY-SA 4.0", licenseUrl: "https://creativecommons.org/licenses/by-sa/4.0/",
      source: "Wikimedia Commons", sourceUrl: "https://commons.wikimedia.org/wiki/File:Mona_Lisa_with_eigenvector.png",
      width: 640, height: 544 });
    const bare = SRC.split("\n").filter((l) => !/^(author|license_url):/.test(l)).join("\n");
    expect(parseImageBlock(bare)).toMatchObject({ author: "", licenseUrl: "" });
  });

  it("null for a missing required key, a repeated key, a bad asset id, a non-http source URL, a line that is not key: value", () => {
    for (const k of ["asset", "alt", "title", "license", "source", "source_url", "width", "height"]) {
      expect(parseImageBlock(SRC.split("\n").filter((l) => !l.startsWith(`${k}:`)).join("\n"))).toBeNull();
    }
    expect(parseImageBlock(SRC + "\nasset: ia_8")).toBeNull();
    expect(parseImageBlock(SRC.replace("asset: ia_7", "asset: ../x"))).toBeNull();
    expect(parseImageBlock(SRC.replace("source_url: https", "source_url: javascript"))).toBeNull();
    expect(parseImageBlock(SRC + "\njust text")).toBeNull();
  });

  it("safeUrl keeps http(s) only", () => {
    expect(safeUrl("https://a.org/x")).toBe("https://a.org/x");
    expect(safeUrl("javascript:alert(1)")).toBe("");
    expect(safeUrl("https://a.org/x\"onclick")).toBe("");
  });
});

describe("mountImageBlock", () => {
  it("the vault file as an <img> (no remote URL), under it title · author · licence · source with the licence and source linked", () => {
    const el = document.createElement("div");
    mountImageBlock(el, SRC, port(true), new EmbedHub());
    const img = el.querySelector("img")!;
    expect(img.getAttribute("src")).toBe(`app://local/${PATH}`);
    expect(img.getAttribute("alt")).toBe("左边是原图，右边是错切以后");
    const cap = el.querySelector("figcaption")!;
    expect(cap.textContent).toBe("Mona Lisa with eigenvector · J. Finkelstein · CC BY-SA 4.0 · Wikimedia Commons");
    expect([...cap.querySelectorAll("a")].map((a) => a.getAttribute("href"))).toEqual([
      "https://creativecommons.org/licenses/by-sa/4.0/", "https://commons.wikimedia.org/wiki/File:Mona_Lisa_with_eigenvector.png"]);
  });

  it("a missing file: one line and 取回; after the fetch the image is drawn", async () => {
    const el = document.createElement("div");
    const p = port(false);
    const retrieve = vi.fn((id: string) => { p.files.add(`EvoWiki/Wiki/assets/${id}.webp`); return Promise.resolve(true); });
    mountImageBlock(el, SRC, { ...p, retrieve }, new EmbedHub());
    expect(el.textContent).toContain(WIKI_COPY.imageMissing);
    el.querySelector("button")!.click();
    await flush();
    expect(retrieve).toHaveBeenCalledWith("ia_7");
    expect(el.querySelector("img")?.getAttribute("src")).toBe(`app://local/${PATH}`);
  });

  it("no 取回 when it cannot fetch; a written asset redraws it through the hub", () => {
    const el = document.createElement("div");
    const hub = new EmbedHub();
    const p = port(false, { canFetch: () => false });
    mountImageBlock(el, SRC, p, hub);
    expect(el.querySelector("button")).toBeNull();
    p.files.add(PATH);
    hub.assetWritten("ia_7");
    expect(el.querySelector("img")).not.toBeNull();
  });

  it("a block that does not parse: one plain line", () => {
    const el = document.createElement("div");
    mountImageBlock(el, "asset: ia_7", port(true), new EmbedHub());
    expect(el.textContent).toBe(WIKI_COPY.imageUnreadable);
    expect(captionEl(parseImageBlock(SRC)!).querySelectorAll("a")).toHaveLength(2);
  });
});
