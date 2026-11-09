import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";

// User ruling 2026-11-04: 学点新的 has each group's band glow, the 知识图谱 page the pastel territories, the thumbnail no background.
const optionsOf = (file: string): string => {
  const s = readFileSync(resolve(__dirname, file), "utf-8"), call = s.indexOf("= GraphCloud(");
  return s.slice(call, s.indexOf("});", call));
};

describe("the option each view passes to GraphCloud", () => {
  it("学点新的: the band glow", () => { expect(optionsOf("../learn/learn-view.ts")).toContain("washByBand: true"); });
  it("知识图谱: the pastel territories", () => {
    const o = optionsOf("../starmap/starmap-view.ts");
    expect(o).toContain("tokens: root");
    expect(o).not.toContain("washByBand");
    expect(o).not.toContain("small: true");
  });
  it("今天's thumbnail: no background", () => {
    const o = optionsOf("../cloud/mini-cloud.ts");
    expect(o).toContain("small: true");
    expect(o).not.toContain("washByBand");
  });
});
