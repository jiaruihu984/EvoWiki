import { describe, expect, it } from "vitest";
import { cssLoadsRemote, RENDER_WAIT_MS, renderMarkdownSafely, stripRemoteMedia } from "./safe-markdown";

const add = (root: HTMLElement, tag: string, attrs: Record<string, string>): HTMLElement => {
  const el = root.ownerDocument.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, v);
  root.append(el);
  return el;
};

describe("remote media in rendered Markdown", () => {
  it("a remote image never reaches the panel; it becomes a link that loads nothing", async () => {
    const el = document.createElement("div");
    let target: HTMLElement | null = null;
    await renderMarkdownSafely((_md, into) => {
      target = into;
      const p = add(into, "p", {}); p.append("看图：");
      add(p, "img", { src: "https://x/?q=secret", alt: "图" });
      return Promise.resolve();
    }, el, "![图](https://x/?q=secret)");
    // Rendered into a document without a window, so nothing could start loading before the clean-up.
    expect(target!.ownerDocument.defaultView).toBeNull();
    expect(el.querySelector("img")).toBeNull();
    const a = el.querySelector("a")!;
    expect(a.getAttribute("href")).toBe("https://x/?q=secret");
    expect(a.getAttribute("rel")).toBe("noopener"); expect(a.getAttribute("target")).toBe("_blank");
    expect(a.textContent).toBe("图");
    expect(el.textContent).toBe("看图：图");
  });

  it("strips every remote media kind; local and data media stay", () => {
    const root = document.createElement("div");
    add(root, "img", { src: "//cdn.example/x.png" });
    add(root, "img", { src: "data:image/png;base64,AAAA" });
    add(root, "img", { src: "app://local/vault/a.png" });
    add(root, "img", { src: "a.png", srcset: "a.png 1x, https://x/b.png 2x" });
    add(root, "iframe", { src: "https://x/frame" });
    add(root, "video", { src: "local.mp4", poster: "HTTPS://x/poster.png" });
    const audio = add(root, "audio", {}); add(audio, "source", { src: "https://x/a.mp3" });
    add(root, "embed", { src: "http://x/e" });
    add(root, "object", { data: " https://x/o" });
    stripRemoteMedia(root);
    const left = [...root.querySelectorAll("img,iframe,video,audio,source,embed,object")].map((e) => e.outerHTML);
    expect(left).toEqual(['<img src="data:image/png;base64,AAAA">', '<img src="app://local/vault/a.png">']);
    expect(root.querySelector('a[href="https://x/a.mp3"]')).not.toBeNull(); // the whole <audio> became a link
    expect(root.querySelector('a[href="//cdn.example/x.png"]')).toBeNull(); // protocol-relative → plain text, not a link
    expect(root.textContent).toContain("//cdn.example/x.png");
  });

  it("a failing renderer falls back to plain text", async () => {
    const el = document.createElement("div");
    await renderMarkdownSafely(() => Promise.reject(new Error("boom")), el, "**粗体**");
    expect(el.textContent).toBe("**粗体**");
  });
});

/** A container of a window-less document holding `html`, parsed the way a renderer would produce it. */
const parse = (html: string): HTMLElement => {
  const doc = new DOMParser().parseFromString(html, "text/html");
  const root = doc.createElement("div");
  root.append(...doc.head.childNodes, ...doc.body.childNodes);
  return root;
};
const clean = (html: string): HTMLElement => { const r = parse(html); stripRemoteMedia(r); return r; };
const sleep = (ms: number): Promise<void> => new Promise((r) => setTimeout(r, ms));

describe("more remote-load vectors", () => {
  it("<input type=image> with a remote src becomes a link; a local one and other inputs stay", () => {
    const r = clean('<input type="image" src="https://x/?q=1" alt="go"><input type="IMAGE" src="//x/y"><input type="image" src="app://local/b.png"><input type="checkbox" checked disabled>');
    expect(r.querySelectorAll("input")).toHaveLength(2);
    expect(r.querySelector("input[type=image]")!.getAttribute("src")).toBe("app://local/b.png");
    expect(r.querySelector("input[type=checkbox]")!.hasAttribute("checked")).toBe(true);
    expect(r.querySelector('a[href="https://x/?q=1"]')!.textContent).toBe("go");
    expect(r.textContent).toContain("//x/y");
  });

  it("SVG <image>, <feImage> and <use> that load a remote file are removed; a local fragment stays", () => {
    const r = clean('<svg><image href="https://x/a.png"/><image xlink:href="//x/b.png"/><feImage href="https://x/c.png"/><use href="https://x/s.svg#a"/><use href="#local"/><rect width="4" height="4"/></svg>');
    expect(r.querySelectorAll("image,feImage,use")).toHaveLength(1);
    expect(r.querySelector("use")!.getAttribute("href")).toBe("#local");
    expect(r.querySelector("rect")).not.toBeNull();
    expect(new XMLSerializer().serializeToString(r)).not.toContain("x/");
  });

  it("<link> is removed (remote or not), and so is a <style> that fetches; a plain <style> stays", () => {
    const r = clean('<link rel="stylesheet" href="https://x/a.css"><link rel="stylesheet" href="local.css"><style>@import url(https://x/b.css);</style><style>p{background:url("//x/c.png")}</style><style>p{color:red}</style><p>t</p>');
    expect(r.querySelector("link")).toBeNull();
    expect([...r.querySelectorAll("style")].map((s) => s.textContent)).toEqual(["p{color:red}"]);
  });

  it("a style attribute that fetches a remote URL loses it (escapes, image-set, comments included); local url() and plain styles stay", () => {
    const r = clean([
      '<p id="a" style="background:url(https://x/a.png)">1</p>',
      '<p id="b" style="background:URL( &quot;//x/b.png&quot; )">2</p>',
      '<p id="c" style="background:u\\72l(https://x/c.png)">3</p>',
      '<p id="d" style="background:image-set(&quot;a.png&quot; 1x, &quot;https://x/d.png&quot; 2x)">4</p>',
      '<p id="e" style="background:/* c */url(//x/e.png)">5</p>',
      '<p id="f" style="color:red;background:url(app://local/f.png)">6</p>',
      '<p id="g" style="color:red">7</p>',
    ].join(""));
    for (const id of ["a", "b", "c", "d", "e"]) expect(r.querySelector(`#${id}`)!.hasAttribute("style"), id).toBe(false);
    expect(r.querySelector("#f")!.getAttribute("style")).toBe("color:red;background:url(app://local/f.png)");
    expect(r.querySelector("#g")!.getAttribute("style")).toBe("color:red");
    expect(cssLoadsRemote("background:url(http://x/y)")).toBe(true);
    expect(cssLoadsRemote("background:url(data:image/png;base64,AA)")).toBe(false);
  });

  it("a remote src / srcset / poster / href on anything but a link is dropped; links and Obsidian's attributes stay", () => {
    const r = clean('<img class="x" alt="a" src="a.png" srcset="https://x/b.png 2x"><table background="//x/bg.png" id="t"><tr><td>c</td></tr></table><video src="v.mp4"></video><a href="https://x/page" target="_blank" rel="noopener" data-href="Note" class="internal-link">l</a><a href="app://local/n.md">m</a><span contenteditable="false" data-x="1" id="i">s</span><mjx-container class="MathJax"><svg><g/></svg></mjx-container>');
    expect(r.querySelector("img")).toBeNull();
    expect(r.querySelector("a[href='https://x/b.png']")).not.toBeNull();
    expect(r.querySelector("table")!.hasAttribute("background")).toBe(false);
    expect(r.querySelector("table")!.id).toBe("t");
    expect(r.querySelector("video")!.getAttribute("src")).toBe("v.mp4");
    const a = r.querySelector("a[href='https://x/page']")!;
    expect([a.getAttribute("target"), a.getAttribute("rel"), a.getAttribute("data-href"), a.className]).toEqual(["_blank", "noopener", "Note", "internal-link"]);
    expect(r.querySelector("a[href='app://local/n.md']")).not.toBeNull();
    expect(r.querySelector("span")!.outerHTML).toBe('<span contenteditable="false" data-x="1" id="i">s</span>');
    expect(r.querySelector("mjx-container svg g")).not.toBeNull();
  });

  it("scripts, event handlers and script / html URLs are gone", () => {
    const r = clean('<script>alert(1)</script><p onclick="x()" title="t">p</p><a href="javascript:alert(1)">j</a><a href=" JaVa\tScript:alert(1)">k</a><iframe src="data:text/html,<b>x</b>"></iframe><iframe src="app://local/frame.html"></iframe>');
    expect(r.querySelector("script")).toBeNull();
    expect(r.querySelector("p")!.hasAttribute("onclick")).toBe(false);
    expect(r.querySelector("p")!.getAttribute("title")).toBe("t");
    for (const a of r.querySelectorAll("a")) expect(a.hasAttribute("href")).toBe(false);
    expect([...r.querySelectorAll("iframe")].map((f) => f.getAttribute("src"))).toEqual([null, "app://local/frame.html"]);
  });

  it("content added after the render promise resolved is sanitized too (a late processor, a late src change)", async () => {
    const el = document.createElement("div");
    document.body.append(el);
    await renderMarkdownSafely((_md, into) => {
      const p = add(into, "p", {}); p.append("早");
      add(p, "img", { src: "local.png", id: "later" });
      return Promise.resolve();
    }, el, "x");
    // the panel element now holds the rendered nodes; a late processor adds to it / changes it
    expect(el.querySelector("#later")!.getAttribute("src")).toBe("local.png");
    add(el, "img", { src: "https://x/late.png", alt: "晚" });
    add(el, "link", { rel: "stylesheet", href: "https://x/late.css" });
    const wrap = add(el, "div", {});
    add(wrap, "input", { type: "image", src: "https://x/i.png" });
    add(wrap, "p", { style: "background:url(//x/z.png)" }).append("z");
    el.querySelector("#later")!.setAttribute("src", "https://x/changed.png");
    await sleep(30);
    expect(el.querySelector("img,link,input")).toBeNull();
    expect(el.querySelector('a[href="https://x/changed.png"]')).not.toBeNull();
    expect(el.querySelector('a[href="https://x/late.png"]')!.textContent).toBe("晚");
    expect(el.querySelector("p[style]")).toBeNull();
    expect(el.textContent).toContain("早");
    el.remove();
  });

  it("a renderer that never resolves (real Obsidian waits for an image to load in a window-less document) is sanitized as it stands", async () => {
    const el = document.createElement("div");
    const t0 = Date.now();
    await renderMarkdownSafely((_md, into) => {
      const p = add(into, "p", {}); p.append("见图");
      add(p, "img", { src: "https://x/slow.png", alt: "慢图" });
      return new Promise<void>(() => undefined);
    }, el, "x");
    expect(Date.now() - t0).toBeGreaterThanOrEqual(RENDER_WAIT_MS - 50);
    expect(el.querySelector("img")).toBeNull();
    expect(el.textContent).toBe("见图慢图");
  });
});
