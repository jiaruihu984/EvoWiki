/**
 * The walkthrough's static interactives note (test/fixtures/embed/interactives.md, src/wiki/WALKTHROUGH.md section O):
 * its blocks parse with the plugin's own parser (the backend's embed_block format) and point at asset files that exist
 * where the fetcher would write them (<notes>/assets/<id>.html). The counter asset is the fake's demo interactive.
 */
import { existsSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import { DEMO_ASSET, DEMO_INTERACTIVE_HTML, embedBlock } from "../fixtures";
import { assetIdsIn } from "./assets";
import { parseEmbedBlock } from "./embed-block";

/** Resolved from this file, so the test does not depend on the directory vitest runs in. */
const WIKI = join(__dirname, "..", "..", "..", "test", "fixtures", "embed");
/** A checkout with core.autocrlf turns the fixture files into CRLF: compare the text. */
const read = (p: string): string => readFileSync(p, "utf8").split("\r\n").join("\n");

describe("embedded HTML fixture", () => {
  const md = read(`${WIKI}/interactives.md`);
  const blocks = [...md.matchAll(/```embedded-html\n([\s\S]*?)```/g)].map((m) => m[1]!);

  it("two blocks in the backend's format, both parsed", () => {
    expect(blocks).toHaveLength(2);
    expect(blocks.map((b) => parseEmbedBlock(b))).toEqual([
      { id: "z-steps", src: "assets/demo-z-steps.html", title: "点一下，看 z 怎么变", height: 400 },
      { id: "sandbox-probe", src: "assets/sandbox-probe.html", title: "沙箱探针", height: 560 },
    ]);
    // The first block is exactly what embed_block writes for the fake's demo asset.
    expect(md).toContain(embedBlock(DEMO_ASSET));
  });

  it("the asset files are where the fetcher writes them; the counter is the demo interactive", () => {
    expect(assetIdsIn(md)).toEqual(["demo-z-steps", "sandbox-probe"]);
    for (const id of assetIdsIn(md)) expect(existsSync(`${WIKI}/assets/${id}.html`), id).toBe(true);
    expect(read(`${WIKI}/assets/demo-z-steps.html`)).toBe(DEMO_INTERACTIVE_HTML);
  });

  it("the probe tries every escape the walkthrough checks, and navigates only on a click", () => {
    const probe = read(`${WIKI}/assets/sandbox-probe.html`);
    for (const s of ["parent.document", "require", "top.location", "fetch(", "location.href", "localStorage", "window.open"]) {
      expect(probe, s).toContain(s);
    }
    expect(probe).toMatch(/addEventListener\("click", function \(\) \{\s*location\.href/);
  });
});
