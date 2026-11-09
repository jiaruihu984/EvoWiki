import { describe, expect, it } from "vitest";
import { buildMultipart } from "./multipart";

interface Part { headers: Record<string, string>; body: Uint8Array }

/** A small multipart/form-data reader (RFC 7578 framing): enough to read back what buildMultipart wrote. */
function parse(body: ArrayBuffer, contentType: string): Part[] {
  const m = /^multipart\/form-data; boundary=(.+)$/.exec(contentType);
  if (!m) throw new Error(`not multipart: ${contentType}`);
  const bytes = new Uint8Array(body);
  const latin = Array.from(bytes, (b) => String.fromCharCode(b)).join("");   // 1 char per byte
  const delim = `--${m[1]!}`;
  const chunks = latin.split(delim);
  expect(chunks[0]).toBe("");
  expect(chunks[chunks.length - 1]).toBe("--\r\n");
  return chunks.slice(1, -1).map((c) => {
    expect(c.startsWith("\r\n")).toBe(true);
    expect(c.endsWith("\r\n")).toBe(true);
    const inner = c.slice(2, -2);
    const cut = inner.indexOf("\r\n\r\n");
    const headers: Record<string, string> = {};
    for (const line of inner.slice(0, cut).split("\r\n")) {
      const i = line.indexOf(":");
      // header values are UTF-8 bytes: decode them back
      headers[line.slice(0, i).toLowerCase()] = utf8(line.slice(i + 1).trim());
    }
    return { headers, body: Uint8Array.from(inner.slice(cut + 4), (ch) => ch.charCodeAt(0)) };
  });
}
const utf8 = (latin: string): string => new TextDecoder().decode(Uint8Array.from(latin, (ch) => ch.charCodeAt(0)));
const text = (b: Uint8Array): string => new TextDecoder().decode(b);
/** The `key="value"` / `key*=UTF-8''pct` parameters of a Content-Disposition value. */
function params(cd: string): Record<string, string> {
  const out: Record<string, string> = {};
  for (const seg of cd.split(/;\s*/).slice(1)) {
    const i = seg.indexOf("=");
    const k = seg.slice(0, i);
    const v = seg.slice(i + 1);
    out[k] = k.endsWith("*") ? decodeURIComponent(v.replace(/^UTF-8''/, "")) : v.replace(/^"|"$/g, "");
  }
  return out;
}

describe("buildMultipart", () => {
  it("fields only: each a form-data part with its UTF-8 value", () => {
    const { body, contentType } = buildMultipart([["text", "笔记\n第二行"], ["title", "标题"]], undefined, "XyZ123");
    expect(contentType).toBe("multipart/form-data; boundary=XyZ123");
    const parts = parse(body, contentType);
    expect(parts.map((p) => p.headers["content-disposition"])).toEqual(['form-data; name="text"', 'form-data; name="title"']);
    expect(parts.map((p) => text(p.body))).toEqual(["笔记\n第二行", "标题"]);
  });

  it("a file part: name, an ASCII-safe filename, filename* in UTF-8, Content-Type, and the bytes as-is", () => {
    const data = Uint8Array.from([0x25, 0x50, 0x44, 0x46, 0x00, 0xff, 0x0d, 0x0a, 0x2d, 0x2d]).buffer;
    const { body, contentType } = buildMultipart([["use", "learn"]],
      { field: "file", name: "线性代数 讲义.pdf", data, type: "application/pdf" }, "b0und");
    const parts = parse(body, contentType);
    expect(parts).toHaveLength(2);
    expect(text(parts[0]!.body)).toBe("learn");
    const f = parts[1]!;
    const p = params(f.headers["content-disposition"]!);
    expect(f.headers["content-disposition"]!.startsWith("form-data; ")).toBe(true);
    expect(p.name).toBe("file");
    expect(p.filename).toBe("线性代数 讲义.pdf");        // raw UTF-8 (RFC 7578 §4.2; what the backend's parser reads)
    expect(p["filename*"]).toBe("线性代数 讲义.pdf");    // RFC 5987 form for parsers that prefer it
    expect(f.headers["content-disposition"]).toContain("filename*=UTF-8''%E7%BA%BF");
    expect(f.headers["content-type"]).toBe("application/pdf");
    expect([...f.body]).toEqual([...new Uint8Array(data)]);
  });

  it("escapes quote, CR and LF in a filename; defaults the type to application/octet-stream", () => {
    const { body, contentType } = buildMultipart([], { field: "file", name: 'a"b\r\nc.md', data: new ArrayBuffer(0) }, "B");
    const [f] = parse(body, contentType);
    expect(f!.headers["content-disposition"]).toContain('filename="a%22b%0D%0Ac.md"');
    expect(params(f!.headers["content-disposition"]!)["filename*"]).toBe('a"b\r\nc.md');
    expect(f!.headers["content-type"]).toBe("application/octet-stream");
    expect(f!.body.length).toBe(0);
  });

  it("a fresh random boundary that does not occur in the content", () => {
    const a = buildMultipart([["x", "1"]]);
    const b = buildMultipart([["x", "1"]]);
    const ba = /boundary=(.+)$/.exec(a.contentType)![1]!;
    expect(ba).toMatch(/^[A-Za-z0-9-]{20,70}$/);
    expect(ba).not.toBe(/boundary=(.+)$/.exec(b.contentType)![1]);
    expect(parse(a.body, a.contentType).map((p) => text(p.body))).toEqual(["1"]);
  });

  it("refuses a boundary that occurs in the content, and field names it cannot quote", () => {
    expect(() => buildMultipart([["x", "--zz"]], undefined, "zz")).toThrow(/boundary/);
    expect(() => buildMultipart([['a"b', "1"]], undefined, "q")).toThrow(/field name/);
  });
});

describe("buildMultipart: the file's bytes are not scanned for the boundary (a random 128-bit one)", () => {
  it("the file name still is", () => {
    expect(() => buildMultipart([], { field: "file", name: "x--q.pdf", data: new ArrayBuffer(1) }, "q")).toThrow(/boundary/);
    const data = new TextEncoder().encode("--q in the bytes").buffer;
    expect(() => buildMultipart([], { field: "file", name: "x.pdf", data }, "q")).not.toThrow();
  });
});
