/**
 * Round-2 final fix (G2): only the user's open of a page sends `open=1` (the backend may then start a page rewrite an
 * RSI rule change left waiting); the re-reads after a write and the reading assistant's edits send nothing.
 */
import { describe, expect, it } from "vitest";
import { firstPage, flush, setup } from "./controller-test-kit";
import type { FakeWikiApi } from "./fake";

const getPages = (api: FakeWikiApi, n: number): unknown[][] =>
  api.calls.slice(n).filter((x) => x.method === "openPage").map((x) => x.args);

describe("openPage open flag", () => {
  it("opening or following a page sends open; the refresh after a write does not", async () => {
    const { api, c } = setup();
    await firstPage(c);
    let n = api.calls.length;
    await c.openWikiPage("p1");
    expect(getPages(api, n)).toEqual([["p1", { rewrite: true }]]);
    n = api.calls.length;
    await c.followPage("p1");
    expect(getPages(api, n)).toEqual([["p1", { rewrite: true }]]);
    n = api.calls.length;
    await c.know("s3");
    await flush();
    expect(getPages(api, n)).toEqual([["p1"]]);
  });

  it("a page the reading assistant edited is peeked (no openPage: nothing is opened)", async () => {
    const { api, c } = setup();
    await firstPage(c);
    const n = api.calls.length;
    await c.pageEditedElsewhere("p1");
    expect(getPages(api, n)).toEqual([]);
    expect(api.calls.slice(n).filter((x) => x.method === "getPage").map((x) => x.args)).toEqual([["p1"]]);
  });
});
