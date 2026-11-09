import i18next from "i18next";
import zh from "./zh.json";
import en from "./en.json";
import extraZh from "./extra-zh.json";
import extraEn from "./extra-en.json";

export type UiLanguage = "zh" | "en";
export const localeOf = (value: string): UiLanguage => value.toLowerCase().startsWith("zh") ? "zh" : "en";
export const i18n = i18next.createInstance();
void i18n.init({ lng: "zh", fallbackLng: false, resources: { zh: { translation: { ...zh, ...extraZh } }, en: { translation: { ...en, ...extraEn } } },
  initImmediate: false, keySeparator: false, interpolation: { escapeValue: false }, returnEmptyString: false });

export function tr(key: string, values: Record<string, unknown> = {}): string {
  return String(i18n.t(key, values));
}
export const uiLanguage = (): UiLanguage => localeOf(i18n.language);
export function setUiLanguage(language: UiLanguage): void { if (i18n.language !== language) void i18n.changeLanguage(language); }
export function onLanguageChange(refresh: () => void): () => void {
  i18n.on("languageChanged", refresh);
  return () => { i18n.off("languageChanged", refresh); };
}

const labels = new Set<WeakRef<Node>>();
const bindings = new WeakMap<Node, Map<string, (target: Node) => void>>();
/** Only explicit EvoWiki label bindings are refreshed; never inspect or rewrite user text. */
export function bindLabel<T extends Node>(target: T, update: (target: T) => void, slot = "text"): T {
  update(target);
  let fields = bindings.get(target);
  if (!fields) { fields = new Map(); bindings.set(target, fields); labels.add(new WeakRef(target)); }
  fields.set(slot, (node) => update(node as T));
  return target;
}
onLanguageChange(() => {
  for (const reference of labels) {
    const node = reference.deref();
    if (node) for (const update of bindings.get(node)?.values() ?? []) update(node);
    else labels.delete(reference);
  }
});
const textNodes = new WeakMap<Node, Text>();
export function localizedText<T extends Node>(target: T, text: () => string | null): T {
  if (target.nodeType === 3) return bindLabel(target, (node) => { node.textContent = text(); });
  let label = textNodes.get(target);
  if (!label || (label.parentNode as Node | null) !== target) {
    for (const child of [...target.childNodes]) if (child.nodeType === 3) target.removeChild(child);
    label = (target.ownerDocument ?? document).createTextNode("");
    target.insertBefore(label, target.firstChild); textNodes.set(target, label);
  }
  const owned = label;
  return bindLabel(target, (node) => { if ((owned.parentNode as Node | null) === node) owned.textContent = text(); });
}
export function labelNode(text: () => string): Text { return localizedText(document.createTextNode(""), text); }
export function localizedAttribute<T extends Element>(target: T, name: string, text: () => string): T {
  return bindLabel(target, (node) => { node.setAttribute(name, text()); }, name);
}
