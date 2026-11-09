import { describe, expect, it } from "vitest";
import type { Band } from "../schema";
import { concept, protoGoals, protoK } from "./fixture";
import { frontier } from "./frontier";
import { buildStarMap } from "./model";

const none = { goals: [], membership: {} };
const mk = (rows: [string, string, Band, string[]][]) =>
  buildStarMap({ concepts: rows.map(([id, n, b, p]) => concept(id, n, [b], p)) }, none);

describe("frontier", () => {
  it("matches the hand-worked prototype result, ordered by unlocked followers then name", () => {
    const map = buildStarMap(protoK(), protoGoals());
    // Hand-worked: cov (prereq var likely) unlocks corr + pca = 2; eig (mm likely), nuc (dna likely), orth (vec known)
    // unlock 1 each, so they tie and sort by name code point. corr (cov shaky) and pca (cov shaky) are blocked.
    const all = frontier(map, { max: 10 });
    const tied = ["特征值与特征向量", "核苷酸", "正交"].sort();
    expect(all.map((r) => r.name)).toEqual(["协方差", ...tied, "DNA 复制"]); // rep: pair is likely, unlocks nothing
    expect(frontier(map).map((r) => r.name)).toEqual(["协方差", ...tied.slice(0, 2)]);
    expect(all[0]!.reason).toBe("已掌握「方差」");
  });
  it("concepts without prerequisites are not on the frontier", () => {
    expect(frontier(mk([["a", "A", "none", []], ["b", "B", "gap", []]]))).toEqual([]);
  });
  it("truncates to max and sorts by code point on ties", () => {
    const map = mk([["r", "根", "known", []], ["x", "乙", "none", ["r"]], ["y", "甲", "none", ["r"]], ["z", "丙", "none", ["r"]]]);
    const expected = ["乙", "甲", "丙"].sort().slice(0, 2);
    expect(frontier(map, { max: 2 }).map((r) => r.name)).toEqual(expected);
    expect(frontier(map, { max: 0 })).toEqual([]);
  });
  it("reason lists at most two prerequisites, then 等", () => {
    const map = mk([["a", "甲", "known", []], ["b", "乙", "likely", []], ["c", "丙", "known", []], ["t", "目标", "none", ["a", "b", "c"]]]);
    expect(frontier(map)[0]!.reason).toBe("已掌握「甲」和「乙」等知识");
    const two = mk([["a", "甲", "known", []], ["b", "乙", "likely", []], ["t", "目标", "none", ["a", "b"]]]);
    expect(frontier(two)[0]!.reason).toBe("已掌握「甲」和「乙」");
  });
  it("empty when everything is known or nothing is", () => {
    expect(frontier(mk([["a", "A", "known", []], ["b", "B", "known", ["a"]]]))).toEqual([]);
    expect(frontier(mk([["a", "A", "none", []], ["b", "B", "none", ["a"]]]))).toEqual([]);
  });
});
