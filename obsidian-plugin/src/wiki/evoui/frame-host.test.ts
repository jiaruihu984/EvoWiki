import { readFileSync } from "node:fs";
import { join } from "node:path";
import { afterEach, describe, expect, it, vi } from "vitest";
import { APP_CSP, declaredImages, gateDocument, hasAppNode, openBlockFrame, STRICT_CSP, useTransportForTests } from "./frame-host";
import { readFromFrame } from "./protocol";

const directives = (csp: string): Map<string, Set<string>> => new Map(csp.split(";").map((d) => {
  const [name, ...values] = d.trim().split(/\s+/);
  return [name!, new Set(values)];
}));

describe("the frames' policy", () => {
  it("the strict policy allows no network and no inline script but the frame's own", () => {
    const d = directives(STRICT_CSP);
    expect([...d.get("default-src")!]).toEqual(["'none'"]);
    expect([...d.get("script-src")!]).toEqual(["'sha256-stub'"]);
    for (const k of ["connect-src", "frame-src", "worker-src", "object-src", "form-action", "base-uri"]) expect([...d.get(k)!], k).toEqual(["'none'"]);
    expect(STRICT_CSP).not.toContain("unsafe-eval");
  });

  it("an app's policy allows what evoui.js gives an app (a nested frame inherits it), and no more network", () => {
    const js = readFileSync(join(process.cwd(), "..", "src/evowiki/deep/evoui/evoui.js"), "utf8");
    const appCsp = directives(/"csp": "([^"]+)"/.exec(js)![1]!);
    const ours = directives(APP_CSP);
    for (const k of ["script-src", "style-src", "img-src", "font-src", "media-src"]) expect(ours.get(k), k).toEqual(appCsp.get(k));
    for (const k of ["connect-src", "frame-src", "form-action", "base-uri"]) expect([...ours.get(k)!], k).toEqual(["'none'"]);
    expect(APP_CSP).not.toContain("unsafe-eval");
  });

  it("the gate holds the block frame, sandboxed, both with the policy, and no script of its own", () => {
    const parsed = new DOMParser().parseFromString(gateDocument(STRICT_CSP, 'var a = "<b>&amp;";'), "text/html");
    expect(parsed.querySelector("meta")?.getAttribute("content")).toBe(STRICT_CSP);
    expect(parsed.querySelector("script")).toBeNull();
    const inner = parsed.querySelector("iframe")!;
    expect(inner.getAttribute("sandbox")).toBe("allow-scripts");
    const block = new DOMParser().parseFromString(inner.getAttribute("srcdoc")!, "text/html");
    expect(block.querySelector("meta")?.getAttribute("content")).toBe(STRICT_CSP);
    expect(block.querySelector("script")?.textContent).toBe('var a = "<b>&amp;";');   // byte for byte: the CSP hash is over this text
  });

  it("finds an app node anywhere in the block", () => {
    expect(hasAppNode({ body: { kind: "box", children: [{ kind: "text" }] } })).toBe(false);
    expect(hasAppNode({ body: { kind: "box", children: [{ kind: "if", then: { kind: "app" } }] } })).toBe(true);
  });
});

describe("what the main window takes from a frame", () => {
  it("only the known shapes", () => {
    for (const bad of [null, 1, "up", [], {}, { t: "up" }, { t: "up", boot: "" }, { t: "up", boot: "x".repeat(129) }, { t: "nope" }, { t: "pong" }, { t: "height", h: Infinity }, { t: "height", h: -1 }, { t: "height", h: "9" },
      { t: "event", ev: { type: "item" } }, { t: "event", ev: { type: "write", node: "n", revision: 1 } }, { t: "progress", progress: [] },
      { t: "math", id: 1, tex: 3, display: true }, { t: "math", id: 1, tex: "x".repeat(5001), display: true },
      { t: "event", ev: { type: "item", node: "n", revision: 1, x: "y".repeat(9000) } }, { t: "progress", progress: { revision: 1, x: "y".repeat(200_001) } }]) {
      expect(readFromFrame(bad), JSON.stringify(bad).slice(0, 60)).toBeNull();
    }
  });

  it("a height is clamped; a message carries nothing but its own fields", () => {
    expect(readFromFrame({ t: "height", h: 1e9 })).toEqual({ t: "height", h: 6_000 });
    expect(readFromFrame({ t: "up", boot: "document", path: "../.env" })).toEqual({ t: "up", boot: "document" });
    expect(readFromFrame({ t: "math", id: 2, tex: "x", display: false, html: "<script>" })).toEqual({ t: "math", id: 2, tex: "x", display: false });
  });
});

describe("an app's images", () => {
  it("a frame may ask only for the images its block declares", () => {
    const block = { type: "evoui", body: { kind: "box", children: [{ kind: "app", html: "x", images: [{ id: "a", asset: "ia_1" }] }, { kind: "text", images: [{ asset: "ia_2" }] }] } };
    expect([...declaredImages(block)]).toEqual(["ia_1"]);
  });
  it("a request is one exact shape", () => {
    expect(readFromFrame({ t: "image", id: 1, asset: "ia_1" })).toEqual({ t: "image", id: 1, asset: "ia_1" });
    expect(readFromFrame({ t: "image", id: "1", asset: "ia_1" })).toBeNull();
    expect(readFromFrame({ t: "image", id: 1 })).toBeNull();
  });
});


afterEach(() => useTransportForTests(null));
describe("frame operation identity", () => {
  it("drains a closing generation snapshot before final acknowledgment and rejects late results", () => {
    let receive!: (m: import("./protocol").FromFrame) => void;
    const removed = vi.fn(), saved = vi.fn(), event = vi.fn();
    useTransportForTests((_host, on) => {
      receive = on;
      return { start: () => undefined, post: () => undefined, remove: removed, connected: () => true, setHeight: () => undefined };
    });
    const host = document.createElement("div");
    const frame = openBlockFrame({ host, root: host, source: "final-snapshot", block: {}, blockId: "a", progress: null,
      onEvent: event, onProgress: saved, onStart: () => undefined, onFallback: () => undefined });
    receive({ t: "up", boot: "document" });
    void frame.stop();
    receive({ t: "event", ev: { type: "item", node: "quiz", revision: 2 } });
    receive({ t: "progress", progress: { revision: 2 } });
    expect(saved).toHaveBeenCalledExactlyOnceWith({ revision: 2 });
    expect(event).not.toHaveBeenCalled();
    receive({ t: "stopped", progress: { revision: 3 } });
    receive({ t: "stopped", progress: { revision: 4 } });
    receive({ t: "progress", progress: { revision: 5 } });
    expect(saved.mock.calls).toEqual([[{ revision: 2 }], [{ revision: 3 }]]);
    expect(removed).toHaveBeenCalledTimes(1);
  });

  it("does not deliver evidence or progress before the frame handshake", () => {
    let receive!: (m: import("./protocol").FromFrame) => void;
    useTransportForTests((_host, on) => {
      receive = on;
      return { start: () => undefined, post: (m) => { if (m.t === "stop") receive({ t: "stopped", progress: null }); }, remove: () => undefined, connected: () => true, setHeight: () => undefined };
    });
    const host = document.createElement("div");
    const onEvent = vi.fn(), onProgress = vi.fn();
    const frame = openBlockFrame({ host, root: host, source: "before-handshake", block: {}, blockId: "a", progress: null,
      onEvent, onProgress, onStart: () => undefined, onFallback: () => undefined });
    receive({ t: "event", ev: { type: "item", node: "quiz", revision: 1 } });
    receive({ t: "progress", progress: { revision: 1 } });
    expect(onEvent).not.toHaveBeenCalled();
    expect(onProgress).not.toHaveBeenCalled();
    void frame.stop();
  });

  it("resends initialization when the same frame window boots a fresh srcdoc document", () => {
    const sent: { t: string }[] = [];
    let receive!: (m: import("./protocol").FromFrame) => void;
    useTransportForTests((_host, on) => {
      receive = on;
      return { start: () => undefined, post: (m) => { sent.push(m); if (m.t === "stop") receive({ t: "stopped", progress: null }); }, remove: () => undefined, connected: () => true, setHeight: () => undefined };
    });
    const host = document.createElement("div");
    const frame = openBlockFrame({ host, root: host, source: "duplicate-handshake", block: {}, blockId: "a", progress: null,
      onEvent: () => undefined, onProgress: () => undefined, onStart: () => undefined, onFallback: () => undefined });
    receive({ t: "up", boot: "document" });
    receive({ t: "up", boot: "document" });
    expect(sent.filter((m) => m.t === "init")).toHaveLength(1);
    receive({ t: "up", boot: "reloaded-document" });
    expect(sent.filter((m) => m.t === "init")).toHaveLength(2);
    void frame.stop();
  });
});
