/** 运行这个交互 and its reason: what stands in for a program whose source the server did not confirm. One press runs that one block. */
import { h } from "../ui/dom";
import { WIKI_COPY } from "./copy";
import { qTip } from "./q-tip";

/** How long a block waits for the server's answer before it offers to run: no longer than a local server takes. */
export const OFFER_AFTER_MS = 800;

export const runOffer = (run: () => void): HTMLElement => h("div", { class: "ew evowiki-evoui-offer" }, h("button", { class: "ew-btn evowiki-evoui-run", onclick: run }, () => (WIKI_COPY.evouiRun)), qTip(() => (WIKI_COPY.evouiRunWhy), () => (WIKI_COPY.evouiRunWhyLabel)));
