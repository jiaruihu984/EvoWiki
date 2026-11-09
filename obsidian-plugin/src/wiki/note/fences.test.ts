import { describe, expect, it } from "vitest";
import { FenceScanner, fenceCloses, fenceOpen } from "./fences";

const kinds = (lines: string[]): string[] => { const s = new FenceScanner(); return lines.map((l) => s.step(l)); };

describe("fences (CommonMark fenced code)", () => {
  it("opens on ≥3 backticks or tildes indented ≤3 spaces; a backtick info string with a backtick is not a fence", () => {
    expect(fenceOpen("```js")).toEqual({ ch: "`", n: 3 });
    expect(fenceOpen("   ~~~~")).toEqual({ ch: "~", n: 4 });
    expect(fenceOpen("    ```")).toBeNull();
    expect(fenceOpen("``")).toBeNull();
    expect(fenceOpen("``` a`b")).toBeNull();
  });
  it("closes only on the same character, at least as long, with nothing after it", () => {
    expect(fenceCloses("```", { ch: "`", n: 4 })).toBe(false);
    expect(fenceCloses("~~~", { ch: "`", n: 3 })).toBe(false);
    expect(fenceCloses("`````  ", { ch: "`", n: 4 })).toBe(true);
    expect(fenceCloses("``` js", { ch: "`", n: 3 })).toBe(false);
  });
  it("a ```` fence wrapping ``` lines: the inner fences and a # line are code", () => {
    expect(kinds(["a", "````md", "```", "# h", "```", "````", "b"])).toEqual(["text", "open", "code", "code", "code", "close", "text"]);
  });
  it("an unclosed fence runs to the end", () => {
    const s = new FenceScanner();
    expect(["```", "# h"].map((l) => s.step(l))).toEqual(["open", "code"]);
    expect(s.fence).toEqual({ ch: "`", n: 3 });
  });
});
