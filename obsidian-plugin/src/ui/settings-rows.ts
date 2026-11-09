/**
 * The demo's settings helpers (full-demo/src/app.js renderSettings: `row`, `input`, `select`, `sw`) with `ew-` classes:
 * a row is `div.ew-srow` > (div > `.ew-n` name + `.ew-d` description) + the control; inputs are type=text; a switch is
 * `button.ew-switch` role=switch aria-checked. The demo's element ids are left out (the 设置 view and the settings tab
 * may be open together): every control carries aria-label = its row's name instead. A row's left column may also hold
 * its `.ew-muted` status line (hidden while empty), so a failure is said in the row it concerns.
 */
import { el, type Label } from "./ew";
import { localizedAttribute } from "../i18n";
const labelAttribute = (node: HTMLElement, label: Label): void => {
  if (typeof label === "function") localizedAttribute(node, "aria-label", label);
  else node.setAttribute("aria-label", label);
};

export interface SettingRow { row: HTMLDivElement; left: HTMLDivElement }

/** app.js `row(box, name, desc, ctl)`: appended to `box`. */
export function settingRow(box: HTMLElement, name: Label, desc: Label | null, ctl: HTMLElement): SettingRow {
  const r = el("div", "srow");
  const l = el("div");
  l.append(el("div", "n", name));
  if (desc) l.append(el("div", "d", desc));
  r.append(l, ctl);
  box.append(r);
  return { row: r, left: l };
}

/** app.js `input(val, id)`: a type=text input. */
export function textInput(val: string, label: Label): HTMLInputElement {
  const i = el("input");
  i.type = "text";
  i.value = val;
  labelAttribute(i, label);
  return i;
}

/** app.js `select(opts, val, id, fn)` without the handler: one option per [value, text] in order. */
export function selectBox(opts: readonly (readonly [string, Label])[], val: string | null, label: Label): HTMLSelectElement {
  const x = el("select");
  labelAttribute(x, label);
  for (const [v, t] of opts) {
    const op = el("option", "", t);
    op.value = v;
    op.selected = v === val;
    x.append(op);
  }
  return x;
}

/** app.js `sw(on, fn)`: flips aria-checked on click, then `fn(new value)`. */
export function switchButton(on: boolean, label: Label, fn: (v: boolean) => void): HTMLButtonElement {
  const b = el("button", "switch");
  b.type = "button";
  b.setAttribute("role", "switch");
  b.setAttribute("aria-checked", String(on));
  labelAttribute(b, label);
  b.onclick = () => {
    const v = b.getAttribute("aria-checked") !== "true";
    b.setAttribute("aria-checked", String(v));
    fn(v);
  };
  return b;
}

/** A `.ew-muted` line (aria-live polite), hidden while it says nothing. */
export function statusLine(): HTMLDivElement {
  const s = el("div", "muted");
  s.setAttribute("aria-live", "polite");
  s.hidden = true;
  return s;
}

export function setStatus(line: HTMLElement, text: string): void {
  line.textContent = text;
  line.hidden = text === "";
}
