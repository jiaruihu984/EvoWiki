import { describe, expect, it, vi } from "vitest";
import { detachRemovedViews, REMOVED_VIEW_TYPES } from "./removed-views";

describe("detachRemovedViews", () => {
  it("closes every leaf of the retired sidebar / graph view types and nothing else", () => {
    const leaf = () => ({ detach: vi.fn() });
    const byType: Record<string, ReturnType<typeof leaf>[]> = {
      "evowiki-sidebar": [leaf()], "evowiki-graph": [leaf(), leaf()], "evowiki-wiki": [leaf()],
    };
    const ws = { getLeavesOfType: vi.fn((t: string) => byType[t] ?? []) };
    expect(detachRemovedViews(ws)).toBe(3);
    for (const t of REMOVED_VIEW_TYPES) for (const l of byType[t]!) expect(l.detach).toHaveBeenCalledOnce();
    expect(byType["evowiki-wiki"]![0]!.detach).not.toHaveBeenCalled();
    expect(ws.getLeavesOfType).not.toHaveBeenCalledWith("evowiki-wiki");
  });

  it("a layout without them is left alone", () => {
    expect(detachRemovedViews({ getLeavesOfType: () => [] })).toBe(0);
  });
});
