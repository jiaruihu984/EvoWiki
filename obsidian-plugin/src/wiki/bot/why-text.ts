/** The 为什么？ chain as text (app.js `whyChain` rows: 触发 → 条件 → 产出 → 你的反应) from GET /wiki/bot/why. A rule card has no 规划: no model ran. */
import { BOT_KIND, BOT_REACT, GATE_REASON, reactLabel, RULE_ZH, RUNS_COPY as C } from "./bot-copy";
import type { ActivityOut, WhyOut } from "./bot-schema";

export interface WhyLines { 触发: string; 条件: string; 规划: string; 产出: string; 你的反应: string }

/** A skipped row's reason: the gate's own detail when it has one, else the label of its reason code. */
export function skipReason(a: ActivityOut): string {
  if (a.gate_detail != null && a.gate_detail !== "") return a.gate_detail;
  return (a.gate_reason !== null ? GATE_REASON[a.gate_reason] : undefined) ?? a.gate_reason ?? C.unknownReason;
}

export function whyLines(w: WhyOut): Omit<WhyLines, "规划"> {
  const t = w.trigger;
  const trig = t === null ? "" : C.rule(RULE_ZH[t.rule_id] ?? t.rule_id, t.rule_version, t.reason);
  const g = w.gate;
  let gate = "";
  if (g !== null) {
    if (g.detail != null && g.detail !== "") gate = g.detail;
    else if (g.outcome === "pass") gate = C.gatePass;
    else {
      const why = (g.reason !== null ? GATE_REASON[g.reason] : undefined) ?? g.reason ?? C.unknownReason;
      gate = g.outcome === null ? C.gateNone : g.outcome === "defer" ? C.gateDefer(why) : C.gateSkip(why);
    }
  }
  const chats = w.outputs.filter((o) => o.type === "suggestion" && o.kind === "chat");
  const outs = w.outputs.flatMap((o) => {
    if (o.type === "action") return [C.outAction(o.title)];
    if (o.type === "rsi_change") return [C.outRsi(o.title)];
    if (o.kind === "chat") return chats.length >= 2 ? (o === chats[0] ? [C.outChats(chats.length)] : []) : [C.outChat(o.title)];
    return [C.outToday(o.title) + (o.kind === "wrong_answers" && (o.status === "pending" || o.status === "shown") ? C.openRemedial : "")];
  });
  const r = w.reaction;
  const out = w.outputs.find((o) => o.type === "suggestion");
  return {
    触发: trig, 条件: gate, 产出: outs.length > 0 ? outs.join(C.outSep) : C.noOutput,
    你的反应: reactLabel(out?.kind ?? t?.kind ?? "", r?.status ?? "", out?.title ?? ""),
  };
}

/** The 为什么？ chain of a page-write row, from the row itself (the demo's `trig / gate / plan / out / react`). `running`: the write is not finished. */
export function writeWhyLines(a: ActivityOut, cost: number, running: boolean): WhyLines {
  const w = a.write;
  if (w == null) throw new Error("not a page-write row");
  const idle = a.kind === "idle_write";
  return {
    触发: idle ? C.writeTrigIdle(w.goal, w.n) : C.writeTrigUser(w.asked, w.origin),
    条件: idle ? C.writeGateIdle : C.writeGateUser,
    规划: running ? C.writePlanRunning(cost) : C.writePlan(cost),
    产出: C.writeOut(w.title, w.folder),
    你的反应: C.writeReact(w.opened, w.undone),
  };
}

/**
 * The "?" tooltip of a Bot run (app.js botRuns `qTip([r.why, det, when · react])`): the reason in plain words, then the
 * execution details (rule, model, calls, cost), then when · what the learner did. `why` is the read chain when it is
 * there; without it only the reason and the time show. Empty pieces are left out.
 */
export function runTipLines(run: ActivityOut, why: WhyOut | null, when: string): string[] {
  const t = why?.trigger ?? null;
  const reason = run.reason.trim() !== "" ? run.reason.trim() : t?.reason ?? "";
  const bits: string[] = t !== null ? [C.ruleOnly(RULE_ZH[t.rule_id] ?? t.rule_id, t.rule_version)] : [];
  const kind = run.kind === "chat" && run.suggestion_status === "accepted" ? "chat" : "";
  const react = kind === "chat" ? reactLabel(kind, run.suggestion_status ?? "", run.suggestion_title ?? "")
    : BOT_REACT[run.suggestion_status ?? ""] ?? "";
  return [reason, bits.join(" · "), C.whenReact(when, react)].filter((x) => x !== "");
}

/** The tooltip of a page-write row: what asked for it, how it ran, then when · what happened to the page. */
export function writeTipLines(a: ActivityOut, cost: number, running: boolean, when: string): string[] {
  const l = writeWhyLines(a, cost, running);
  return [l.触发, `${BOT_KIND[a.kind] ?? ""} · ${l.规划}`, C.whenReact(when, running ? "" : l.你的反应)].filter((x) => x !== "" && x !== " · ");
}
