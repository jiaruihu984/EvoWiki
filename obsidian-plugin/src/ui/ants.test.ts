import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { antsFrame } from "./ants";

describe("antsFrame (app.js antsFrame)", () => {
  beforeEach(() => { vi.useFakeTimers(); });
  afterEach(() => { vi.useRealTimers(); document.body.replaceChildren(); });

  function row(): HTMLElement {
    const r = document.createElement("div");
    Object.defineProperty(r, "offsetWidth", { value: 200 });
    Object.defineProperty(r, "offsetHeight", { value: 40 });
    document.body.append(r);
    return r;
  }

  it("appends one svg.ew-ants sized offset+10 with the rect attributes", () => {
    const r = row();
    antsFrame(r);
    const svgs = r.querySelectorAll("svg.ew-ants");
    expect(svgs.length).toBe(1);
    const svg = svgs[0]!;
    expect(svg.getAttribute("width")).toBe("210");
    expect(svg.getAttribute("height")).toBe("50");
    expect(svg.getAttribute("aria-hidden")).toBe("true");
    const rect = svg.querySelector("rect")!;
    expect(["x", "y", "width", "height", "rx"].map((k) => rect.getAttribute(k))).toEqual(["1", "1", "208", "48", "13"]);
  });
  it("a second call removes the earlier frame anywhere in the document", () => {
    const a = row(), b = row();
    antsFrame(a);
    antsFrame(b);
    expect(document.querySelectorAll(".ew-ants").length).toBe(1);
    expect(a.querySelector(".ew-ants")).toBeNull();
    expect(b.querySelector(".ew-ants")).not.toBeNull();
  });
  it("gets ew-out at 6000 ms and is removed 900 ms later", () => {
    const r = row();
    antsFrame(r);
    const svg = r.querySelector(".ew-ants")!;
    vi.advanceTimersByTime(5999);
    expect(svg.classList.contains("ew-out")).toBe(false);
    vi.advanceTimersByTime(1);
    expect(svg.classList.contains("ew-out")).toBe(true);
    vi.advanceTimersByTime(899);
    expect(svg.isConnected).toBe(true);
    vi.advanceTimersByTime(1);
    expect(svg.isConnected).toBe(false);
  });
});
