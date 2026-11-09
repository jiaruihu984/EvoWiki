import { describe, expect, it, vi } from "vitest";
import { ApiError, OfflineError } from "../../api/errors";
import type { WikiHttp, WikiHttpRequest } from "../api";
import { HttpGoalApi } from "./goal-api";

/** A recording transport: never the real server (these routes make LLM calls). */
function http(status = 201, text = "{}"): WikiHttp & { calls: WikiHttpRequest[] } {
  const calls: WikiHttpRequest[] = [];
  return { calls, request: vi.fn((r: WikiHttpRequest) => { calls.push(r); return Promise.resolve({ status, text }); }) };
}
const cfg = { baseUrl: "http://h:1", userId: "jerry" };
const shape = (r: WikiHttpRequest): [string, string, unknown] => [r.method, r.url, r.body === undefined ? undefined : JSON.parse(r.body as string)];

describe("HttpGoalApi (the big-goal routes on master)", () => {
  it("sends each route with the user header and the contract's body", async () => {
    const t = http();
    const api = new HttpGoalApi(() => cfg, t);
    expect(api.live).toBe(true);
    await api.answer("d/1", { answers: [0, -1, 2] });
    await api.answer("d/1", { skip: true });
    await api.getMap("m 1");
    await api.enterRegion("m1", "r/2");
    await api.splitNode("c/3", "m1");
    await api.getPath("p1");
    await api.pathNext("p1");
    await api.writes(["pg/1"]);
    expect(t.calls.map(shape)).toEqual([
      ["POST", "http://h:1/wiki/goals/d%2F1/answers", { answers: [0, -1, 2] }],
      ["POST", "http://h:1/wiki/goals/d%2F1/answers", { skip: true }],
      ["GET", "http://h:1/wiki/maps/m%201", undefined],
      ["POST", "http://h:1/wiki/maps/m1/regions/r%2F2/enter", undefined],
      ["POST", "http://h:1/wiki/nodes/c%2F3/split", { map_id: "m1", reason: "requested" }],   // 继续拆细: its parts go on this goal's map
      ["GET", "http://h:1/wiki/paths/p1", undefined],
      ["POST", "http://h:1/wiki/paths/p1/next", undefined],
      ["GET", "http://h:1/wiki/writes?page_ids=pg%2F1&unwritten=true", undefined],
    ]);
    expect(t.calls.every((r) => r.headers["X-EvoWiki-User"] === "jerry")).toBe(true);
    // 跳过 is answers {skip: true}, never the /stop route.
    expect(t.calls.some((r) => r.url.endsWith("/stop"))).toBe(false);
  });

  it("the chat-first routes: GET /wiki/goals/{id}, map edits {ops}, the path edits / 拆成多页 / its undo", async () => {
    const t = http(200, "{}");
    const api = new HttpGoalApi(() => cfg, t);
    await api.getDraft("dr/1");
    await api.editMap("m1", [{ op: "reorder", ids: ["a", "b"] }, { op: "rename", id: "a", name: "水质" }]);
    await api.editMap("m1", [{ op: "add", name: "造景" }]);
    await api.editPath("p1", { op: "move", from: 0, to: 2 });
    await api.editPath("p1", { op: "unremove", page_key: "p1-p2" });
    await api.splitRow("p1", "p1-p1");
    await api.undoSplitRow("p1", "p1-p1", "ch9");
    expect(t.calls.map(shape)).toEqual([
      ["GET", "http://h:1/wiki/goals/dr%2F1", undefined],
      ["POST", "http://h:1/wiki/maps/m1/edits", { ops: [{ op: "reorder", ids: ["a", "b"] }, { op: "rename", id: "a", name: "水质" }] }],
      ["POST", "http://h:1/wiki/maps/m1/edits", { ops: [{ op: "add", name: "造景" }] }],
      ["POST", "http://h:1/wiki/paths/p1/edits", { op: "move", from: 0, to: 2 }],
      ["POST", "http://h:1/wiki/paths/p1/edits", { op: "unremove", page_key: "p1-p2" }],
      ["POST", "http://h:1/wiki/paths/p1/rows/p1-p1/split", undefined],
      ["POST", "http://h:1/wiki/paths/p1/rows/p1-p1/split/undo", { change_id: "ch9" }],
    ]);
  });

  it("reads the settings per request (a new server / user id applies to the next call)", async () => {
    const t = http();
    let c = cfg;
    const api = new HttpGoalApi(() => c, t);
    await api.getMap("m");
    c = { baseUrl: "http://other:2", userId: "amy" };
    await api.getMap("m");
    expect(t.calls.map((r) => [r.url, r.headers["X-EvoWiki-User"]])).toEqual([["http://h:1/wiki/maps/m", "jerry"], ["http://other:2/wiki/maps/m", "amy"]]);
  });

  it("a non-2xx is an ApiError with its status (409 / 404 / 503 / 429); unreachable stays an OfflineError", async () => {
    for (const status of [409, 404, 503, 429]) {
      const api = new HttpGoalApi(() => cfg, http(status, '{"detail":"x"}'));
      await expect(api.enterRegion("m", "r")).rejects.toMatchObject({ status });
      await expect(api.enterRegion("m", "r")).rejects.toBeInstanceOf(ApiError);
    }
    const off: WikiHttp = { request: () => Promise.reject(new OfflineError()) };
    await expect(new HttpGoalApi(() => cfg, off).getMap("m")).rejects.toBeInstanceOf(OfflineError);
  });
});
