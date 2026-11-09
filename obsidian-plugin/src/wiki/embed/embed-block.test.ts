import { setUiLanguage } from "../../i18n";
import { afterEach, describe, expect, it, vi } from "vitest";
import { WIKI_COPY } from "../copy";
import { DEMO_ASSET, embedBlock } from "../fixtures";
import {
  assetIdOfSrc, buildSrcdoc, componentItemOf, EMBED_CSP, EmbedHub, type EmbedPort, frameHeightOf, mountEmbedBlock, parseEmbedBlock, resolveSrc, safeSrc,
  THEME_VARS, themeStyle,
} from "./embed-block";

const SRC = "type: embedded-html\nid: z-steps\nsrc: assets/demo-z-steps.html\ntitle: 点一下: 看 z\nheight: 400\n";
const NOTE = "EvoWiki/Wiki/PCA.md";
const ASSET = "EvoWiki/Wiki/assets/demo-z-steps.html";
/** The fenced block's inner text, as Obsidian hands it to a code block processor. */
const inner = (block: string): string => block.split("\n").slice(1, -1).join("\n");

describe("parseEmbedBlock", () => {
  it("reads the block exactly as deep/contracts.py embed_block writes it (fixtures.embedBlock)", () => {
    expect(parseEmbedBlock(inner(embedBlock(DEMO_ASSET)))).toEqual(
      { id: "z-steps", src: "assets/demo-z-steps.html", title: DEMO_ASSET.title, height: 400 });
    expect(parseEmbedBlock(SRC)).toEqual({ id: "z-steps", src: "assets/demo-z-steps.html", title: "点一下: 看 z", height: 400 });
    expect(parseEmbedBlock(SRC.replace(/\n/g, "\r\n") + "\nother: x\n")?.height).toBe(400);
  });

  it("null for a missing key, a repeated key, a bad type, a line that is not key: value", () => {
    for (const k of ["type", "id", "src", "title", "height"]) {
      expect(parseEmbedBlock(SRC.split("\n").filter((l) => !l.startsWith(`${k}:`)).join("\n"))).toBeNull();
    }
    expect(parseEmbedBlock(SRC + "id: other\n")).toBeNull();
    expect(parseEmbedBlock(SRC.replace("type: embedded-html", "type: html"))).toBeNull();
    expect(parseEmbedBlock(SRC + "just text\n")).toBeNull();
    expect(parseEmbedBlock("")).toBeNull();
  });

  it("null for a height outside 400–1600 or not an integer (no default is guessed)", () => {
    for (const h of ["399", "1601", "0", "500.5", "-500", "abc", "", "4e2"]) {
      expect(parseEmbedBlock(SRC.replace("height: 400", `height: ${h}`))).toBeNull();
    }
    expect(parseEmbedBlock(SRC.replace("height: 400", "height: 1600"))?.height).toBe(1600);
  });

  it("null for an id that is not kebab-case", () => {
    for (const id of ["Z-steps", "-z", "z", "z_steps", "z steps", "a".repeat(65), "z/../x"]) {
      expect(parseEmbedBlock(SRC.replace("id: z-steps", `id: ${id}`))).toBeNull();
    }
  });

  it("null for an unsafe src: .., absolute, URL schemes, backslash, not .html", () => {
    for (const s of ["../x.html", "assets/../../x.html", "/abs/x.html", "http://evil/x.html", "https:x.html", "file:///c/x.html",
      "javascript:alert(1).html", "data:text/html,x.html", "assets\\x.html", "assets/x.htm", "assets/x.html?y", "assets//x.html",
      "./x.html", "C:/x.html", "assets/%2e%2e/x.html", "app://obsidian.md/x.html"]) {
      expect(safeSrc(s), s).toBe(false);
      expect(parseEmbedBlock(SRC.replace("src: assets/demo-z-steps.html", `src: ${s}`)), s).toBeNull();
    }
    expect(safeSrc("assets/demo-z-steps.html")).toBe(true);
    expect(safeSrc("sub/dir/x.HTML")).toBe(true);
  });
});

describe("paths", () => {
  it("src resolves against the note's folder; the asset id only for assets/<id>.html", () => {
    expect(resolveSrc("assets/a.html", NOTE)).toBe("EvoWiki/Wiki/assets/a.html");
    expect(resolveSrc("assets/a.html", "Top.md")).toBe("assets/a.html");
    expect(resolveSrc("../a.html", NOTE)).toBeNull();
    expect(assetIdOfSrc("assets/demo-z-steps.html")).toBe("demo-z-steps");
    expect(assetIdOfSrc("assets/sub/x.html")).toBeNull();
    expect(assetIdOfSrc("other/x.html")).toBeNull();
    expect(assetIdOfSrc("assets/a.b.html")).toBeNull();
    for (const n of ["con", "PRN", "Aux", "nul", "com1", "LPT9"]) expect(assetIdOfSrc(`assets/${n}.html`), n).toBeNull();
    expect(assetIdOfSrc("assets/com10.html")).toBe("com10");
  });
});

describe("srcdoc", () => {
  it("starts with the CSP meta (the backend's ASSET_CSP without sandbox), then the theme style, then the file", () => {
    expect(EMBED_CSP).toBe("default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data: blob:; "
      + "font-src data:; media-src data: blob:; connect-src 'none'; form-action 'none'; base-uri 'none'");
    const doc = buildSrcdoc("<!doctype html><html><head><title>x</title></head><body>b</body></html>", "<style>S</style>");
    expect(doc.startsWith(`<meta http-equiv="Content-Security-Policy" content="${EMBED_CSP}">`)).toBe(true);
    expect(doc.indexOf("<style>S</style>")).toBeLessThan(doc.indexOf("<!doctype html>"));
  });

  it("theme style: computed values on :root, color-scheme by theme; unsafe values are left out", () => {
    const vals: Record<string, string> = { "--background-primary": " #1e1e1e ", "--text-normal": "rgb(1, 2, 3)",
      "--font-text": "'Inter', sans-serif", "--text-muted": "red; } body { x: y", "--text-faint": "</style><script>", "--interactive-accent": "" };
    const css = themeStyle((n) => vals[n] ?? "", true);
    expect(css).toBe("<style>:root { --background-primary: #1e1e1e; --text-normal: rgb(1, 2, 3); --font-text: 'Inter', sans-serif; "
      + "--evowiki-bg: #1e1e1e; --evowiki-tx: rgb(1, 2, 3); color-scheme: dark; }</style>");
    expect(themeStyle(() => "", false)).toContain("color-scheme: light;");
  });

  it("theme style also hands over the demo's tokens (surfaces, text, data palette, mastery colours) next to Obsidian's names", () => {
    for (const n of ["--background-primary", "--text-normal", "--interactive-accent", "--bg-0", "--bg-1", "--border", "--tx-1",
      "--tx-2", "--tx-3", "--viz-1", "--viz-2", "--known", "--shaky", "--gap"]) expect(THEME_VARS, n).toContain(n);
    const vals: Record<string, string> = { "--viz-1": "#2f6fdf", "--viz-2": "#e07b2a", "--known": "#2f9e5b", "--gap": "#d9483b",
      "--font-ui": 'system-ui,"Segoe UI",sans-serif' };
    const css = themeStyle((n) => vals[n] ?? "", false);
    expect(css).toContain("--viz-1: #2f6fdf; --viz-2: #e07b2a; --known: #2f9e5b; --gap: #d9483b;");
    expect(css).toContain('--font-ui: system-ui,"Segoe UI",sans-serif;');
  });
});

function port(over: Partial<EmbedPort> = {}, files: Record<string, string> = {}): EmbedPort {
  return {
    read: (p) => Promise.resolve(files[p] ?? null),
    isGenerated: () => Promise.resolve(true),
    canFetch: () => true,
    assetPath: (id) => `EvoWiki/Wiki/assets/${id}.html`,
    retrieve: vi.fn(() => Promise.resolve(true)),
    theme: () => ({ read: (n) => (n === "--text-normal" ? "#ddd" : ""), dark: true }),
    ...over,
  };
}
const flush = () => new Promise((r) => setTimeout(r, 0));

describe("mountEmbedBlock", () => {
  it("an unreadable block: one plain line, nothing read", async () => {
    const el = document.createElement("div");
    const read = vi.fn(() => Promise.resolve(null));
    mountEmbedBlock(el, SRC.replace("height: 400", "height: 99"), NOTE, port({ read }), new EmbedHub());
    await flush();
    expect(el.textContent).toBe(WIKI_COPY.embedUnreadable);
    expect(el.querySelector("iframe")).toBeNull();
    expect(read).not.toHaveBeenCalled();
  });

  it("the file: a title and an iframe with sandbox exactly allow-scripts, no allow attribute, srcdoc = CSP + theme + file", async () => {
    const el = document.createElement("div");
    mountEmbedBlock(el, SRC, NOTE, port({}, { [ASSET]: "<p>hi</p>" }), new EmbedHub());
    await flush();
    expect(el.querySelector(".evowiki-embed-title")).toBeNull();   // the interactive shows its own title
    expect(el.querySelector("iframe")?.getAttribute("title")).toBe("点一下: 看 z");
    const f = el.querySelector("iframe")!;
    expect(f.getAttribute("sandbox")).toBe("allow-scripts");
    expect(f.getAttribute("sandbox")).not.toContain("allow-same-origin");
    expect(f.hasAttribute("allow")).toBe(false);
    expect(f.hasAttribute("src")).toBe(false);
    expect(f.getAttribute("referrerpolicy")).toBe("no-referrer");
    expect(f.getAttribute("loading")).toBe("lazy");
    expect(f.getAttribute("title")).toBe("点一下: 看 z");
    expect(f.style.height).toBe("400px");
    const gate = new DOMParser().parseFromString(f.getAttribute("srcdoc")!, "text/html");
    const doc = gate.querySelector("iframe")!.getAttribute("srcdoc")!;
    expect(doc.startsWith('<meta http-equiv="Content-Security-Policy"')).toBe(true);
    expect(doc).toContain("--text-normal: #ddd;");
    expect(doc).toContain("color-scheme: dark;");
    expect(doc.endsWith("<p>hi</p>")).toBe(true);
  });

  it("no file: 「还没取回」 with 取回; no button when fetching is not allowed or the block points elsewhere", async () => {
    const draw = async (p: EmbedPort, src = SRC, note = NOTE) => {
      const el = document.createElement("div");
      mountEmbedBlock(el, src, note, p, new EmbedHub());
      await flush();
      return el;
    };
    const el = await draw(port());
    expect(el.textContent).toContain(WIKI_COPY.embedMissing);
    expect(el.querySelector("button.evowiki-embed-fetch")?.textContent).toBe(WIKI_COPY.embedFetch);
    expect((await draw(port({ canFetch: () => false }))).querySelector("button")).toBeNull();
    // A note outside the wiki folder: its assets/ is not where a fetch would write.
    const away = await draw(port(), SRC, "Elsewhere/Note.md");
    expect(away.textContent).toContain(WIKI_COPY.embedMissing);
    expect(away.querySelector("button")).toBeNull();
    // Not an assets/<id>.html src: nothing to fetch.
    expect((await draw(port(), SRC.replace("assets/demo-z-steps.html", "mine/x.html"))).querySelector("button")).toBeNull();
  });

  it("an EvoWiki asset is read from the wiki's assets folder for a note in a topic folder and for a draft without a note path", async () => {
    const draw = async (note: string) => {
      const el = document.createElement("div");
      mountEmbedBlock(el, SRC, note, port({}, { [ASSET]: "<p>shown</p>" }), new EmbedHub());
      await flush();
      return el;
    };
    for (const note of ["EvoWiki/Wiki/特征值、特征向量等/节点与边.md", ""]) {
      const el = await draw(note);
      expect(el.querySelector("iframe")).not.toBeNull();
      expect(el.textContent).not.toContain(WIKI_COPY.embedMissing);
    }
    // a note outside the wiki keeps its own relative assets/ (nothing there)
    expect((await draw("Elsewhere/Note.md")).textContent).toContain(WIKI_COPY.embedMissing);
  });

  it("a missing file is fetched by itself, then drawn; a failed fetch says so and keeps 取回", async () => {
    const files: Record<string, string> = {};
    const retrieve = vi.fn((id: string) => { files[`EvoWiki/Wiki/assets/${id}.html`] = "<p>got</p>"; return Promise.resolve(true); });
    const el = document.createElement("div");
    mountEmbedBlock(el, SRC, NOTE, port({ retrieve }, files), new EmbedHub());
    await flush(); await flush(); await flush();
    expect(retrieve).toHaveBeenCalledTimes(1);
    expect(retrieve).toHaveBeenCalledWith("demo-z-steps");
    expect(el.querySelector("iframe")?.getAttribute("srcdoc")).toContain("<p>got</p>");

    const bad = document.createElement("div");
    mountEmbedBlock(bad, SRC, NOTE, port({ retrieve: () => Promise.resolve(false) }), new EmbedHub());
    await flush(); await flush(); await flush();
    expect(bad.textContent).toContain(WIKI_COPY.embedMissing);
    expect(bad.textContent).toContain(WIKI_COPY.embedFetchFailed);
    expect(bad.querySelector("button")).not.toBeNull();
  });

  it("theme change: a frame is redrawn only when the theme values it got differ; nothing after the cleanup", async () => {
    const hub = new EmbedHub();
    let dark = true;
    const p = port({ theme: () => ({ read: () => "", dark }) }, { [ASSET]: "x" });
    const el = document.createElement("div");
    const stop = mountEmbedBlock(el, SRC, NOTE, p, hub);
    await flush();
    const first = el.querySelector("iframe")!;
    expect(first.getAttribute("srcdoc")).toContain("color-scheme: dark");
    hub.themeChanged(); await flush();
    expect(el.querySelector("iframe")).toBe(first);          // same values: the running interactive is left alone
    dark = false; hub.themeChanged(); await flush();
    expect(el.querySelector("iframe")).not.toBe(first);
    expect(el.querySelector("iframe")!.getAttribute("srcdoc")).toContain("color-scheme: light");
    stop();
    dark = true; hub.themeChanged(); await flush();
    expect(el.querySelector("iframe")!.getAttribute("srcdoc")).toContain("color-scheme: light");
  });

  it("an asset written: only blocks waiting for that asset redraw; running interactives are not reset", async () => {
    const hub = new EmbedHub();
    const files: Record<string, string> = { "EvoWiki/Wiki/assets/other.html": "o" };
    const p = port({}, files);
    const waiting = document.createElement("div");
    const running = document.createElement("div");
    mountEmbedBlock(waiting, SRC, NOTE, p, hub);
    mountEmbedBlock(running, SRC.replace("demo-z-steps", "other"), NOTE, p, hub);
    await flush();
    const frameBefore = running.querySelector("iframe")!;
    files[ASSET] = "<p>new</p>";
    hub.assetWritten("someone-else"); await flush();
    expect(waiting.querySelector("iframe")).toBeNull();
    hub.assetWritten("demo-z-steps"); await flush();
    expect(waiting.querySelector("iframe")?.getAttribute("srcdoc")).toContain("<p>new</p>");
    hub.assetWritten("other"); await flush();
    expect(running.querySelector("iframe")).toBe(frameBefore);
  });

  it("the file sits in a gate: its navigation goes nowhere, and the gate's report takes the frame away", async () => {
    const el = document.createElement("div");
    document.body.append(el);
    mountEmbedBlock(el, SRC, NOTE, port({}, { [ASSET]: "<p>hi</p>" }), new EmbedHub());
    await flush();
    const f = el.querySelector("iframe")!;
    const gate = new DOMParser().parseFromString(f.getAttribute("srcdoc")!, "text/html");
    expect(gate.querySelector("meta")?.getAttribute("content")).toBe(EMBED_CSP);
    expect(EMBED_CSP).toContain("default-src 'none'");
    expect(gate.querySelector("iframe")?.getAttribute("sandbox")).toBe("allow-scripts");
    window.dispatchEvent(new MessageEvent("message", { data: { evowikiFrameNavigated: true }, source: f.contentWindow }));
    expect(el.querySelector("iframe")).toBeNull();
    expect(el.textContent).toContain(WIKI_COPY.embedPaused);
  });

  it("a file the server did not store, or no answer: the title and 运行这个交互; nothing runs until the press, which holds for this mount", async () => {
    for (const isGenerated of [() => Promise.resolve(false), () => Promise.reject(new Error("offline")), undefined]) {
      const el = document.createElement("div");
      const hub = new EmbedHub();
      const asked: string[] = [];
      mountEmbedBlock(el, SRC, NOTE, port({ isGenerated: isGenerated === undefined ? undefined : (h) => { asked.push(h); return isGenerated(); } }, { [ASSET]: "<p>hi</p>" }), hub);
      await flush();
      expect(el.querySelector("iframe")).toBeNull();
      expect(el.querySelector(".evowiki-embed-title")?.textContent).toBe("点一下: 看 z");
      el.querySelector<HTMLButtonElement>(".evowiki-evoui-run")!.click();
      expect(el.querySelector("iframe")).not.toBeNull();
      if (isGenerated !== undefined) expect(asked).toEqual(["<p>hi</p>"]);
    }
  });

  it("a write landing while the block's first read is pending still draws the frame", async () => {
    const hub = new EmbedHub();
    let release: (v: string | null) => void = () => undefined;
    const reads: string[] = [];
    let present = false;
    const read = vi.fn((p: string) => {
      reads.push(p);
      if (reads.length === 1) return new Promise<string | null>((r) => { release = r; });   // looked before the write
      return Promise.resolve(present ? "<p>now</p>" : null);
    });
    const el = document.createElement("div");
    mountEmbedBlock(el, SRC, NOTE, port({ read }), hub);
    present = true;
    hub.assetWritten("demo-z-steps");                         // lands while the first read is in flight
    release(null);                                            // the stale answer: no file yet
    await flush(); await flush();
    expect(read).toHaveBeenCalledTimes(2);
    expect(el.querySelector("iframe")?.getAttribute("srcdoc")).toContain("<p>now</p>");
  });
});

describe("frameHeightOf (the opt-in height handshake's message shape)", () => {
  it("exactly { evowikiFrameHeight: <finite number> }, clamped to 80–1600 and rounded up", () => {
    expect(frameHeightOf({ evowikiFrameHeight: 312 })).toBe(312);
    expect(frameHeightOf({ evowikiFrameHeight: 312.2 })).toBe(313);
    expect(frameHeightOf({ evowikiFrameHeight: 10 })).toBe(80);
    expect(frameHeightOf({ evowikiFrameHeight: -5 })).toBe(80);
    expect(frameHeightOf({ evowikiFrameHeight: 99_999 })).toBe(1600);
  });
  it("anything else is null", () => {
    for (const bad of [null, undefined, 312, "312", [312], { evowikiFrameHeight: "312" }, { evowikiFrameHeight: Number.NaN },
      { evowikiFrameHeight: Number.POSITIVE_INFINITY }, { evowikiFrameHeight: 312, other: 1 }, { height: 312 }, {},
      { evowikiFrameHeight: null }, new Date()]) expect(frameHeightOf(bad), JSON.stringify(bad) ?? "undefined").toBeNull();
  });
});

describe("componentItemOf (a graded component's report)", () => {
  it("exactly { evowikiComponentItem: { item, target } }, target a string or null", () => {
    expect(componentItemOf({ evowikiComponentItem: { item: "1", target: "a" } })).toEqual({ item: "1", target: "a" });
    expect(componentItemOf({ evowikiComponentItem: { item: "1", target: null } })).toEqual({ item: "1", target: null });
  });
  it("anything else is null", () => {
    for (const bad of [null, "x", 1, [], {}, new Date(), { evowikiFrameHeight: 3 },
      { evowikiComponentItem: "1" }, { evowikiComponentItem: { item: "1" } }, { evowikiComponentItem: { item: 1, target: "a" } },
      { evowikiComponentItem: { item: "", target: "a" } }, { evowikiComponentItem: { item: "1", target: 2 } },
      { evowikiComponentItem: { item: "1", target: "a", correct: true } }, { evowikiComponentItem: { item: "x".repeat(65), target: "a" } },
      { evowikiComponentItem: { item: "1", target: "a" }, more: 1 }]) expect(componentItemOf(bad), JSON.stringify(bad) ?? "undefined").toBeNull();
  });
});

describe("mountEmbedBlock: a graded component's report", () => {
  afterEach(() => { document.body.replaceChildren(); });
  const ANSWER = { evowikiComponentItem: { item: "1", target: "a" } };
  const mounted = async (notePath: string, source = SRC) => {
    const el = document.createElement("div");
    document.body.append(el);
    const onEvent = vi.fn();
    mountEmbedBlock(el, source, notePath, port({ onEvent }, { [ASSET]: "<p>hi</p>" }), new EmbedHub());
    await flush();
    return { f: el.querySelector("iframe")!, onEvent };
  };
  const send = (data: unknown, source: MessageEventSource | null): void => {
    window.dispatchEvent(new MessageEvent("message", { data, source }));
  };

  it("from the shown frame's own window in a wiki note: a result event for the asset", async () => {
    const { f, onEvent } = await mounted(NOTE);
    send(ANSWER, f.contentWindow);
    expect(onEvent).toHaveBeenCalledTimes(1);
    expect(onEvent.mock.calls[0]).toEqual(["demo-z-steps",
      { type: "item", node: "component", kind: "component", item: "1", attempt: 1, response: "a", revision: 0 }]);
  });

  it("from any other window, or in another shape, nothing goes out", async () => {
    const { f, onEvent } = await mounted(NOTE);
    send(ANSWER, window);
    send(ANSWER, null);
    send({ evowikiComponentItem: { item: "1", target: "a", correct: true } }, f.contentWindow);
    expect(onEvent).not.toHaveBeenCalled();
  });

  it("a draft or a chat preview (rendered without a note path) reports nothing", async () => {
    const { f, onEvent } = await mounted("");
    send(ANSWER, f.contentWindow);
    expect(onEvent).not.toHaveBeenCalled();
  });
});

describe("mountEmbedBlock: the height handshake", () => {
  afterEach(() => { document.body.replaceChildren(); });
  const mounted = async () => {
    const el = document.createElement("div");
    document.body.append(el);
    const hub = new EmbedHub();
    const cleanup = mountEmbedBlock(el, SRC, NOTE, port({}, { [ASSET]: "<p>hi</p>" }), hub);
    await flush();
    const f = el.querySelector("iframe")!;
    const send = (data: unknown, source: MessageEventSource | null = f.contentWindow) => {
      window.dispatchEvent(new MessageEvent("message", { data, source }));
    };
    return { el, f, send, cleanup, hub };
  };

  it("the frame's own window reporting a height sets the frame to it (no 400 floor), clamped to 80–1600", async () => {
    const { f, send } = await mounted();
    expect(f.contentWindow).not.toBeNull();
    send({ evowikiFrameHeight: 130 });
    expect(f.style.height).toBe("130px");
    expect(f.getAttribute("height")).toBe("130");
    send({ evowikiFrameHeight: 5 });
    expect(f.style.height).toBe("80px");
    send({ evowikiFrameHeight: 5000 });
    expect(f.style.height).toBe("1600px");
  });

  it("a message from any other window (the host, another frame, none) is ignored", async () => {
    const { f, send } = await mounted();
    const other = document.createElement("iframe");
    document.body.append(other);
    send({ evowikiFrameHeight: 130 }, window);
    send({ evowikiFrameHeight: 130 }, other.contentWindow);
    send({ evowikiFrameHeight: 130 }, null);
    expect(f.style.height).toBe("400px");
  });

  it("bad shapes from the frame's own window are ignored", async () => {
    const { f, send } = await mounted();
    for (const bad of [130, "130", { evowikiFrameHeight: "130" }, { evowikiFrameHeight: Number.NaN }, { evowikiFrameHeight: 130, x: 1 }, null]) send(bad);
    expect(f.style.height).toBe("400px");
  });

  it("cleanup removes the listener: a later report changes nothing", async () => {
    const add = vi.spyOn(window, "addEventListener");
    const remove = vi.spyOn(window, "removeEventListener");
    const { f, send, cleanup } = await mounted();
    const listener = add.mock.calls.find((c) => c[0] === "message")?.[1];
    expect(listener).toBeDefined();
    cleanup();
    expect(remove.mock.calls.some((c) => c[0] === "message" && c[1] === listener)).toBe(true);
    send({ evowikiFrameHeight: 130 });
    expect(f.style.height).toBe("400px");
    add.mockRestore(); remove.mockRestore();
  });

  it("a redraw (theme change) drops the old frame's listener: only the new frame is listened to", async () => {
    let dark = true;
    const el = document.createElement("div");
    document.body.append(el);
    const hub = new EmbedHub();
    const remove = vi.spyOn(window, "removeEventListener");
    mountEmbedBlock(el, SRC, NOTE, port({ theme: () => ({ read: () => "", dark }) }, { [ASSET]: "<p>hi</p>" }), hub);
    await flush();
    const old = el.querySelector("iframe")!;
    const oldWin = old.contentWindow;
    dark = false; hub.themeChanged(); await flush();
    const now = el.querySelector("iframe")!;
    expect(now).not.toBe(old);
    expect(remove.mock.calls.filter((c) => c[0] === "message")).toHaveLength(1);
    window.dispatchEvent(new MessageEvent("message", { data: { evowikiFrameHeight: 200 }, source: oldWin }));
    expect(now.style.height).toBe("400px");
    window.dispatchEvent(new MessageEvent("message", { data: { evowikiFrameHeight: 200 }, source: now.contentWindow }));
    expect(now.style.height).toBe("200px");
    remove.mockRestore();
  });
});

it("managed HTML receives locale changes without replacing its running iframe", async () => {
  const target = document.createElement("div"); document.body.append(target);
  const stop = mountEmbedBlock(target, SRC, NOTE, port({}, { [ASSET]: '<html data-evowiki-locale="1"><p>managed</p></html>' }), new EmbedHub());
  await flush(); const first = target.querySelector("iframe")!;
  const send = vi.spyOn(first.contentWindow!, "postMessage");
  setUiLanguage("en"); expect(send).toHaveBeenCalledWith({ t: "locale", locale: "en" }, "*");
  expect(target.querySelector("iframe")).toBe(first); stop(); send.mockClear();
  setUiLanguage("zh"); expect(send).not.toHaveBeenCalled(); target.remove();
});
