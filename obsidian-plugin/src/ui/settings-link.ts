/** A spending-limit sentence from the server: its 「设置」 opens the settings. */
import { el } from "./ew";
import { SETTINGS_COPY } from "../copy";

export function withSettingsLink(text: string, open: (() => void) | undefined, action = false): (string | HTMLElement)[] {
  if (!action || open === undefined) return [text];
  const link = el("button", "elink", () => SETTINGS_COPY.viewTitle);
  link.type = "button";
  link.dataset.action = "open-settings";
  link.onclick = (e) => { e.stopPropagation(); open(); };
  return [text, " · ", link];
}
