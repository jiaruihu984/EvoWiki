import { describe, expect, it, vi } from "vitest";
import { buildCheckCard, buildRoughCard, checkBlock, roughBlocks, roughReady, roughText, type RoughState } from "./diag-cards";

const BLOCKS = [{ id: "id", name: "分类与鉴别" }, { id: "wq", name: "水质与环境" }];

describe("大体摸底 (ask_rough): the grid", () => {
  it("reads the payload's blocks, skipping what it cannot show", () => {
    expect(roughBlocks({ draft_id: "d", blocks: [...BLOCKS, { id: "", name: "x" }, "y", { id: "k" }] })).toEqual(BLOCKS);
    expect(roughBlocks({})).toEqual([]);
  });

  it("a row per block, 掌握 / 熟悉 / 不会 one tap each, 提交 once every block is marked; his line names each mark", () => {
    const st: RoughState = { blocks: BLOCKS, marks: {}, sent: false };
    const mark = vi.fn((id: string, v: "know" | "partly" | "dont") => { st.marks[id] = v; });
    const submit = vi.fn();
    let c = buildRoughCard(st, { mark, submit }, true);
    expect([...c.querySelectorAll(".ew-dg-row.ew-hd span")].map((x) => x.textContent)).toEqual(["", "掌握", "熟悉", "不会"]);
    expect([...c.querySelectorAll(".ew-dg-row:not(.ew-hd) > span")].map((x) => x.textContent)).toEqual(["分类与鉴别", "水质与环境"]);
    const go = c.querySelector<HTMLButtonElement>(".ew-dg-btn.ew-pri")!;
    expect([go.textContent, go.disabled]).toEqual(["提交", true]);
    c.querySelector<HTMLButtonElement>('[aria-label="分类与鉴别：掌握"]')!.click();
    c.querySelector<HTMLButtonElement>('[aria-label="水质与环境：不会"]')!.click();
    expect(mark.mock.calls).toEqual([["id", "know"], ["wq", "dont"]]);
    c = buildRoughCard(st, { mark, submit }, true);
    expect(c.querySelector('[aria-label="分类与鉴别：掌握"]')!.className).toBe("ew-dg-cell ew-know");
    expect(c.querySelector('[aria-label="水质与环境：不会"]')!.getAttribute("aria-pressed")).toBe("true");
    expect(roughReady(st)).toBe(true);
    c.querySelector<HTMLButtonElement>(".ew-dg-btn")!.click();
    expect(submit).toHaveBeenCalledTimes(1);
    expect(roughText(st)).toBe("分类与鉴别 掌握、水质与环境 不会");
  });

  it("sent: a row of squares in the blocks' colours and 已提交", () => {
    const c = buildRoughCard({ blocks: BLOCKS, marks: { id: "partly", wq: "dont" }, sent: true }, { mark: () => {}, submit: () => {} }, true);
    expect(c.className).toBe("ew-dg-mini");
    expect([...c.querySelectorAll(".ew-dg-sq")].map((x) => x.className)).toEqual(["ew-dg-sq ew-some", "ew-dg-sq ew-gap"]);
    expect(c.querySelector(".ew-dg-mt")!.textContent).toBe("已提交");
  });
});

describe("细问 (ask_check): the question card", () => {
  it("the block, the question, its options (不知道 quieter); an option is sent as his answer; the rating row last", () => {
    expect(checkBlock({ question: "q", block_id: "wq", block_name: "水质与环境" })).toEqual({ id: "wq", name: "水质与环境" });
    expect(checkBlock({ question: "q" })).toBeNull();
    const pick = vi.fn(), rating = document.createElement("div");
    const c = buildCheckCard({ blockName: "水质与环境", question: "新开的缸第二天就放虾，最可能出什么问题？", options: ["氨氮升高，虾中毒", "水太干净", "不知道"], waiting: true }, pick, rating);
    expect(c.querySelector(".ew-dg-qk")!.textContent).toBe("水质与环境");
    expect(c.querySelector(".ew-dg-qk .ew-dg-sq")!.className).toBe("ew-dg-sq ew-asking");
    expect(c.querySelector(".ew-dg-qq")!.textContent).toBe("新开的缸第二天就放虾，最可能出什么问题？");
    const opts = [...c.querySelectorAll<HTMLButtonElement>(".ew-dg-ob")];
    expect(opts.map((b) => [b.textContent, b.className])).toEqual([["氨氮升高，虾中毒", "ew-dg-ob"], ["水太干净", "ew-dg-ob"], ["不知道", "ew-dg-ob ew-idk"]]);
    opts[0]!.click();
    expect(pick).toHaveBeenCalledWith("氨氮升高，虾中毒");
    expect(c.querySelector(".ew-dg-foot")!.firstChild).toBe(rating);
    const done = buildCheckCard({ blockName: "水质与环境", question: "q", options: ["a"], waiting: false }, pick, null);
    expect([done.querySelectorAll(".ew-dg-ob").length, done.querySelector(".ew-dg-qk .ew-dg-sq")!.className]).toEqual([0, "ew-dg-sq"]);
  });
});

describe("the sent grid says only what the server has", () => {
  const st = (outcome?: "sending" | "failed") => ({ blocks: [{ id: "a", name: "甲" }], marks: { a: "partly" as const }, sent: true, ...(outcome ? { outcome } : {}) });
  it("已提交 after the answer came back; 正在提交 meanwhile; 没有记下，重试 when it did not go up", () => {
    const h = { mark: () => {}, submit: () => {} };
    expect(buildRoughCard(st(), h, true).textContent).toContain("已提交");
    expect(buildRoughCard(st("sending"), h, true).textContent).toContain("正在提交");
    expect(buildRoughCard(st("failed"), h, true).textContent).toContain("没有记下，重试");
  });
});
