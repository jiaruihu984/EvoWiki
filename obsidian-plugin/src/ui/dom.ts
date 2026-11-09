import { localizedText } from "../i18n";
type Child = Node | string | (() => string) | null | undefined | false;
type Attr = string | boolean | ((e: Event) => void);

/** Element builder. Strings become text nodes (server text is never parsed as HTML). */
export function h<K extends keyof HTMLElementTagNameMap>(
  tag: K, attrs: Record<string, Attr> = {}, ...children: Child[]
): HTMLElementTagNameMap[K] {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (typeof v === "function") el.addEventListener(k.replace(/^on/, ""), v);
    else if (typeof v === "boolean") el.toggleAttribute(k, v);
    else if (k === "class") el.className = v;
    else el.setAttribute(k, v);
  }
  for (const c of children) {
    if (c === null || c === undefined || c === false) continue;
    if (typeof c === "function") el.append(localizedText(document.createTextNode(""), c));
    else el.append(typeof c === "string" ? document.createTextNode(c) : c);
  }
  return el;
}
