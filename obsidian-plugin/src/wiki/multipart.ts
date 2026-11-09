/**
 * multipart/form-data built by hand (requestUrl takes a string or an ArrayBuffer body, no FormData): the framing of
 * RFC 7578, every header and value in UTF-8, the file's bytes as-is.
 *
 * A file part names its file twice: `filename="…"` with the raw UTF-8 name (what browsers send and what the backend's
 * parser, python-multipart, reads; it ignores starred parameters) with `"`, CR and LF percent-escaped as the HTML
 * standard does, and `filename*=UTF-8''…` (RFC 5987) for parsers that prefer the extended form.
 */

export interface MultipartFile { field: string; name: string; data: ArrayBuffer; type?: string }
export interface Multipart { body: ArrayBuffer; contentType: string }

const enc = new TextEncoder();
const CRLF = "\r\n";

function randomBoundary(): string {
  const bytes = new Uint8Array(16);
  globalThis.crypto.getRandomValues(bytes);
  return `----EvoWikiForm${Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("")}`;
}

function checkName(name: string): string {
  if (!/^[A-Za-z0-9_.-]+$/.test(name)) throw new Error(`multipart: bad field name ${JSON.stringify(name)}`);
  return name;
}

/** RFC 5987 value-chars: encodeURIComponent leaves ! ' ( ) * unescaped, which attr-char does not allow. */
const ext = (s: string): string => encodeURIComponent(s).replace(/[!'()*]/g, (c) => `%${c.charCodeAt(0).toString(16).toUpperCase()}`);
const quoted = (s: string): string => s.replace(/"/g, "%22").replace(/\r/g, "%0D").replace(/\n/g, "%0A");

/**
 * Fields (in order), then the file when given. `boundary` is for tests; by default a fresh random one (128 bits). Throws
 * when a field name is not a plain token or the boundary occurs in a field value or the file name (fail closed instead of
 * a corrupt body). The file's bytes are not scanned (up to 50 MB in the renderer): a random 128-bit boundary does not
 * turn up in them by chance.
 */
export function buildMultipart(fields: ReadonlyArray<readonly [string, string]>, file?: MultipartFile, boundary?: string): Multipart {
  const b = boundary ?? randomBoundary();
  const parts: Uint8Array[] = [];
  const delim = `--${b}`;
  const clash = (text: string): void => { if (text.includes(delim)) throw new Error("multipart: the boundary occurs in the content"); };
  for (const [name, value] of fields) {
    const v = enc.encode(value);
    clash(value);
    parts.push(enc.encode(`--${b}${CRLF}Content-Disposition: form-data; name="${checkName(name)}"${CRLF}${CRLF}`), v, enc.encode(CRLF));
  }
  if (file) {
    const data = new Uint8Array(file.data);
    clash(file.name);
    parts.push(enc.encode(`--${b}${CRLF}Content-Disposition: form-data; name="${checkName(file.field)}"; filename="${quoted(file.name)}"; `
      + `filename*=UTF-8''${ext(file.name)}${CRLF}Content-Type: ${file.type ?? "application/octet-stream"}${CRLF}${CRLF}`), data, enc.encode(CRLF));
  }
  parts.push(enc.encode(`--${b}--${CRLF}`));
  const out = new Uint8Array(parts.reduce((n, p) => n + p.length, 0));
  let at = 0;
  for (const p of parts) { out.set(p, at); at += p.length; }
  return { body: out.buffer, contentType: `multipart/form-data; boundary=${b}` };
}
