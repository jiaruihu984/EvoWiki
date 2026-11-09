/** The personal sidebar contains only preferences, pending preferences, activity and sources. */
import type { WikiActions } from "./actions";
import { renderPersonal } from "./render-personal";
import { sideBody, statusLines } from "./render-today";
import type { WikiState } from "./state";

export function renderEngine(s: WikiState, a: WikiActions, newTools = 0): HTMLDivElement {
  const b = sideBody("side-body pz");
  b.dataset.tab = "engine";
  b.append(...statusLines(s));
  renderPersonal(b, s, a, new Date(), newTools);
  return b;
}
