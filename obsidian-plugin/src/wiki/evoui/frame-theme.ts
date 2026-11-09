/**
 * What a frame needs to look like the note around it. A frame cannot read the page's styles, so the main window sends the
 * plugin's rules for the renderer's class names, Obsidian's base rules for form elements and the resolved value of every CSS variable those rules read.
 */
import type { FrameTheme } from "./protocol";

const OWN_RULE = /evoui|evowiki-math-slot/;
/** Obsidian's base look of the elements the renderer builds: sliders, buttons, selects, inputs, tables. */
const BASE_RULE = /(^|,\s*|body:not\(\.is-mobile\) )(\*|button|select|textarea|input|fieldset|label|table|thead|tbody|tr|td|th|code|details|summary)(\[[^\]]*\]|:[^\s,]+)*\s*(,|$)/;
/** evoui.js APP_TOKENS: what a block's `app` reads for its theme inside the frame. */
const APP_VARS = ["--eu-blue", "--eu-miss", "--eu-right", "--eu-shaky", "--eu-wrong", "--eu-fg", "--eu-fg2", "--eu-bg", "--eu-surface", "--eu-line", "--eu-accent"];

let cached: { css: string; names: string[] } | null = null;

function ownRules(doc: Document): { css: string; names: string[] } {
  if (cached !== null) return cached;
  const parts: string[] = [];
  for (const sheet of Array.from(doc.styleSheets)) {
    let rules: CSSRuleList;
    try { rules = sheet.cssRules; } catch { continue; }       // a cross-origin sheet: none of ours
    for (const rule of Array.from(rules)) {
      if (OWN_RULE.test(rule.cssText) || BASE_RULE.test((rule as CSSStyleRule).selectorText ?? "")) parts.push(rule.cssText);
    }
  }
  const css = parts.join("\n");
  const names = new Set(APP_VARS);
  for (const m of css.matchAll(/var\(\s*(--[\w-]+)/g)) names.add(m[1]!);
  cached = { css, names: [...names] };
  return cached;
}

export function frameTheme(root: HTMLElement): FrameTheme {
  const doc = root.ownerDocument;
  const { css, names } = ownRules(doc);
  const cs = (doc.defaultView ?? window).getComputedStyle(root);
  const vars: Record<string, string> = {};
  for (const n of names) { const v = cs.getPropertyValue(n).trim(); if (v !== "") vars[n] = v; }
  return {
    vars, css,
    dark: doc.body.classList.contains("theme-dark"),
    page: root.closest(".evowiki-page") !== null,
    font: { family: cs.fontFamily, size: cs.fontSize, lineHeight: cs.lineHeight, color: cs.color },
  };
}

/** The theme changed (css-change): read the rules again. */
export function forgetThemeRules(): void { cached = null; }
