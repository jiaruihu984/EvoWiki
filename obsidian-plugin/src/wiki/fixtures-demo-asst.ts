/**
 * Dev-only (demo fixture): the 助手's five action flows of the approved demo (full-demo/src/asst.js `replyFor`: split, path,
 * review, merge, rule) as scripted assistant events and ledger rows, so real Obsidian shows the same cards as the demo
 * (assistant-actions Task 16, frame-by-frame comparison). Same titles, page lists, diffs, questions and results as asst.js.
 */
import type { ActionOut, DiffLine, MadeOut } from "./bot/bot-schema";
import type { ActionDraft, ActionItem, AssistantEvent, MessageIn } from "../assistant/types";

/** asst.js replyFor: which flow a message starts (null = the demo's default answer). */
export type DemoFlow = "split" | "path" | "review" | "merge" | "rule";
export function demoFlowOf(text: string): DemoFlow | null {
  if (/笼统|拆解|拆成|拆开/.test(text)) return "split";
  if (/路径|顺序/.test(text)) return "path";
  if (/考考?我|出几道|测一测|(带我|帮我|开始).{0,8}复习/.test(text)) return "review";
  if (/合成|合并|并成/.test(text)) return "merge";
  if (/以后/.test(text) && /例子|公式/.test(text)) return "rule";
  return null;
}

/** app.js PAGES titles the flows name. */
const T = { p1: "均值和方差", p2: "特征值与特征向量", p3: "协方差矩阵", p6: "降维丢了多少信息", p5: "主成分怎么选", pj: "投影（补讲）" } as const;

/** splitFlow's three pages (id, name, the question the page answers: a short name and one plain line, no plan). */
const SPLIT: readonly [string, string, string][] = [
  ["q1", "特征向量", "哪些方向转完还朝原来那边"],
  ["q2", "特征值", "这个方向被拉长了几倍"],
  ["q3", "求特征值", "给一个矩阵，怎么把它们算出来"],
];
/** pathFlow's draft: GPATH with 「降维丢了多少信息」 moved after 「主成分怎么选」. */
const PATH_ORDER: readonly (keyof typeof T)[] = ["p1", "p2", "p3", "p5", "p6", "pj"];

interface FlowSpec { tools: [string, string, string?][]; text: string; action?: ActionOut }

const row = (o: Pick<ActionOut, "action_id" | "kind" | "scope" | "summary" | "why"> & Partial<ActionOut>, now: Date): ActionOut => ({
  level: "ask", status: "proposed", source: "assistant", content: {}, page_id: null, detail: "", created_at: now.toISOString(), decided_at: null,
  expires_at: null, can_undo: false, diff: [], confirm: true, cost_usd: null, made: [], revise: null, result: {}, section_id: null, ...o,
});
const pages = (items: ActionItem[]): Record<string, unknown>[] => items.map((i) => ({ key: i.key, title: i.title, page_id: i.page_id ?? null, covers: i.covers ?? "" }));

function flow(f: DemoFlow, now: Date): FlowSpec {
  switch (f) {
    case "split": {
      const items: ActionItem[] = SPLIT.map(([key, title, covers]) => ({ key, title, covers, page_id: null, n: null, level: null }));
      const diff: DiffLine[] = [{ kind: "del", text: `路径上的「${T.p2}」（页面留在 Wiki 里）` }, ...items.map((i) => ({ kind: "add" as const, text: `新页面「${i.title}」` }))];
      return { tools: [["read_page", "read", T.p2], ["read_path", "read"]],
        text: "这一页一口气讲了三件事：特征向量是什么、特征值是什么、怎么求。拆成三页，每页只讲一件，各带一个能动手的例子。",
        action: row({ action_id: "act_asst_split", kind: "page_split", scope: "new_page", summary: `把「${T.p2}」拆成 3 页`, why: "你在助手里说「这一页太笼统了」",
          content: { pages: pages(items), title: `把「${T.p2}」拆成 3 页` }, diff }, now) };
    }
    case "path": {
      const items: ActionItem[] = PATH_ORDER.map((id) => ({ key: id, title: T[id], page_id: id, covers: null, n: null, level: null }));
      return { tools: [["read_path", "read", "学会 PCA"], ["read_k", "read"]],
        text: "有一处顺序不对：「降维丢了多少信息」算的是没选上的主成分带走了多少方差，得先知道「主成分怎么选」，现在它排在前面了。",
        action: row({ action_id: "act_asst_path", kind: "path_edit", scope: "path", summary: "调整「学会 PCA」的学习路径", why: "你在助手里说「这个路径不合理」",
          content: { pages: pages(items) }, diff: [{ kind: "add", text: "新顺序：" + items.map((i) => i.title).join(" → ") }] }, now) };
    }
    case "review":
      return { tools: [["open_review", "read"]], text: "复习卡在下面，一张张做。" };
    case "merge":
      return { tools: [["read_page", "read", "方差"], ["read_page", "read", "协方差"]],
        text: "这两页有一半在讲同一件事：协方差就是两个量一起的方差。合成一页「方差和协方差」，先讲一个量，再推到两个量，复习时也只要看一页。",
        action: row({ action_id: "act_asst_merge", kind: "page_merge", scope: "delete_merge", summary: "把「方差」和「协方差」合成一页「方差和协方差」",
          why: "你在助手里说「把这两页合成一页」", content: { title: "方差和协方差" },
          diff: [{ kind: "del", text: "页面「方差」" }, { kind: "del", text: "页面「协方差」" },
            { kind: "add", text: "新页面「方差和协方差」：先讲一个量有多分散，再讲两个量怎么一起变" }] }, now) };
    case "rule":
      return { tools: [["read_rules", "read"]],
        text: "你已经有一条「先用具体例子解释新概念」，不过没说公式。我记一条新的：先给一个能算出结果的小例子，再讲公式。",
        action: row({ action_id: "act_asst_rule", kind: "rule", scope: "teaching_params", summary: "新增偏好「先给一个能算出结果的小例子，再讲公式」",
          why: "你在助手里说「以后都先给例子再讲公式」" }, now) };
  }
}

/** The row the server sends as each demo tool starts (assistant/status_line.py's wording for these tools). */
function demoStatus(name: string, summary: string | undefined): string {
  switch (name) {
    case "read_page": return summary ? `正在读《${summary}》` : "正在读页面";
    case "read_path": return "正在看学习路径";
    case "read_k": return "正在看你的掌握情况";
    case "read_graph": return "正在看知识图谱";
    case "read_rules": return "正在看你的讲解规则";
    default: return "正在处理…";
  }
}

/** assistant/tool_rows.py `_past`: what a running line says once done (正在看学习路径 → 看了学习路径). */
function demoDone(running: string): string {
  return running.startsWith("正在") && running.length > 3 ? `${running[2]}了${running.slice(3)}` : running;
}

/** The turn's events: each tool's row (running, then done), the paragraph, then the card. */
export function demoFlowEvents(f: DemoFlow, n: number, now: Date): { events: AssistantEvent[]; action: ActionOut | null } {
  const s = flow(f, now);
  const ev: AssistantEvent[] = [];
  s.tools.forEach(([name, effect, summary], k) => {
    const call_id = `df${n}_${k}`;
    const running = demoStatus(name, summary), page = name === "read_page" && summary ? [summary] : [];
    ev.push({ summary: summary ?? "", type: "tool_start", call_id, name, effect: effect as "read", label: running },
      { type: "tool_end", call_id, ok: true, sources: [], label: demoDone(running), group: page.length ? "read_page" : "", group_label: page.length ? "读了 {k} 页" : "",
        items: page, refs: [] });
  });
  ev.push({ type: "text_delta", text: s.text, at: null });
  if (s.action) {
    const a = s.action;
    const items = (a.content.pages as ActionItem[] | undefined) ?? [];
    ev.push({ type: "action", goal_title: a.kind === "goal_new" && typeof a.content.title === "string" ? a.content.title : null, page_id: null, action_id: a.action_id, kind: a.kind, title: a.summary, items: items.map((i) => ({ key: i.key, title: i.title,
      page_id: i.page_id ? i.page_id : null, covers: i.covers ? i.covers : null, n: null, level: null })), status: "proposed", confirm: true, can_undo: false,
      replaces: "" });
  }
  ev.push({ type: "done", stopped: false, confirmed: false, message_id: `m_demo_flow_${n}`, tool_calls: s.tools.length + 1, llm_calls: 2, cost_usd: null, attachments: [], session_attachments: [] });
  return { events: ev, action: s.action ?? null };
}

/**
 * 确认 on a flow's row: executed, with the pages it made (asst.js apply): a split's pages (edited draft order, added pages too)
 * take the path places of 「特征值与特征向量」 (2, 3, …); a path edit keeps its list; a merge makes 「方差和协方差」 in 统计.
 */
export function demoApprove(a: ActionOut, draft: ActionDraft | undefined, now: Date): ActionOut {
  const out: ActionOut = { ...structuredClone(a), status: "executed", can_undo: true, decided_at: now.toISOString() };
  const was = (a.content.pages as { key: string; title: string; page_id: string | null; covers: string }[] | undefined) ?? [];
  if (draft && "text" in draft && a.kind === "rule") out.content = { ...out.content, text: draft.text };
  const list = draft && "pages" in draft ? draft.pages.map((p, k) => {
    const old = p.key !== undefined ? was.find((w) => w.key === p.key) : undefined;
    return old ?? { key: `new${k}`, title: p.title ?? "", page_id: null, covers: "" };
  }) : was;
  if (a.kind === "page_split") {
    const made: MadeOut[] = list.map((p, k) => ({ key: p.key, page_id: `pg_${p.key}`, n: 2 + k, folder: "线性代数" }));
    out.content = { ...out.content, pages: list.map((p) => ({ ...p, page_id: `pg_${p.key}` })) };
    out.made = made;
  } else if (a.kind === "path_edit") {
    out.content = { ...out.content, pages: list.map((p) => ({ ...p, page_id: p.page_id ?? `pg_${p.key}` })) };
    out.made = [];
  } else if (a.kind === "page_merge") {
    out.made = [{ key: null, page_id: "pvc", n: null, folder: "统计" }];
  }
  return out;
}

// ---- 学点新的 → 开始: the big blocks talked over in the chat (app.js startBlocks / blocksReply / blocksAct, 2026-10-23) ----

/** A block of the draft as the fixture's ledger row keeps it (`from`: the demo topic's regions it carries; `level`: 基础 / 核心 / 进阶 / 拓展, "" when none). */
interface DemoBlock { key: string; name: string; covers: string; level: string; from: string[]; target?: boolean }
/** One 学点新的 conversation: the goal, and the newest draft row (executed once confirmed). */
export interface DemoBlocksConv { goal: string; background: string; topic: string; act: ActionOut; blocks: DemoBlock[] }

/** app.js BLOCK_WORDS: the other words a message may name a block by. */
const BLOCK_WORDS: Record<string, string[]> = {
  keep: ["开缸", "日常", "养起来"], wq: ["水质", "环境"], id: ["分类", "鉴别", "认识"], food: ["营养", "摄食", "吃什么", "喂"], br: ["繁殖", "生长", "蜕皮"], sick: ["疾病", "风险", "生病"],
};

/**
 * app.js FIRST_REPLY / firstReply: the first reply for each demo topic — `paras` above the draft card, `tail` under it (the
 * real one is written by the model from the blocks call's result and what it found on the web); the generic one only
 * frames and asks.
 */
const FIRST_REPLY: Record<string, { paras: string[]; tail: string[] }> = {
  "黑壳虾": {
    paras: ["这份大纲不是一份养虾教程，而是先把它认清楚，再把它养活，最后能自己判断出了什么问题。",
      "先提醒一句：「黑壳虾」是水族店里的俗称，一般指新米虾属（Neocaridina）的一种，常和别的米虾混着卖，所以单开了「分类与鉴别」，放在最前面。"],
    tail: ["主线是 分类与鉴别 → 水质与环境 → 开缸与日常，「营养与摄食」可以和开缸一起学，「生长与繁殖」「疾病与风险」留到虾养稳以后。",
      "建议先从「硝化」「开缸」「过水」「喂多少」这四个点开始。",
      "你是想自己把虾养好，还是把它当成一种生物来研究？前一种会把操作排前面，后一种会多讲分类和生理。"] },
};
export function firstReply(topic: string, n: number): { paras: string[]; tail: string[] } {
  return FIRST_REPLY[topic] ?? { paras: [`按你说的目标，为「${topic}」拆解学习路径，先安排 ${n} 个学习主题，再逐个细化为学习页面。`],
    tail: ["你是想先能用起来，还是想把原理弄懂？两种排法不一样。"] };
}

/**
 * The goal of a message: 开始's (intent learn_goal) as it carries it; else a typed message read as `goal`, then 背景：… when
 * given (null: it does not say 我要学 / 想学).
 */
export function demoGoalOf(body: MessageIn): { goal: string; background: string } | null {
  const [goal = "", ...rest] = (body.text ?? "").split("\n");
  const bg = rest.join("\n").replace(/^背景：/, "");
  // 开始's (learn_goal): the form's goal, its 背景 line in the text (the message carries his words)
  if (body.intent === "learn_goal") return { goal: (body.goal ?? "").trim(), background: bg.trim() };
  if (!/^(我(要|想)学|想学)/.test(goal.trim())) return null;
  return { goal: goal.trim(), background: bg.trim() };
}

function blocksRow(c: { goal: string; background: string; topic: string }, blocks: DemoBlock[], said: string | null, id: string, now: Date): ActionOut {
  const title = `为「${c.topic}」拆解学习路径`;
  return row({ action_id: id, kind: "goal_new", scope: "goal", summary: title,
    why: said !== null ? `你在助手里说「${said}」` : `你在学点新的里说「${c.goal}」`,
    content: { goal: c.goal, background: c.background, title: c.topic, confirm: true,
      blocks: blocks.map((b) => ({ key: b.key, name: b.name, covers: b.covers === "" ? [] : b.covers.split("、"), level: b.level, from: b.from })),
      pages: blocks.map((b) => ({ key: b.key, title: b.name, covers: b.covers, level: b.level })) },
    diff: [{ kind: "add", text: blocks.map((b) => b.name).join(" → ") }] }, now);
}

/**
 * The turn's events in stream order: the tools, the text before the card, the card, the text after it (the live loop
 * commits each model call's text as one text_delta at its position, the card's action event in between).
 */
function blocksEvents(n: number, tools: [string, string, string?][], text: string, a: ActionOut | null, tail = ""): AssistantEvent[] {
  const ev: AssistantEvent[] = [];
  tools.forEach(([name, effect, summary], k) => {
    const call_id = `db${n}_${k}`;
    ev.push({ summary: summary ?? "", type: "tool_start", call_id, name, effect: effect as "read", label: "" }, { type: "tool_end", call_id, ok: true, sources: [], label: "", group: "", group_label: "", items: [], refs: [] });
  });
  ev.push({ type: "text_delta", text, at: null });
  if (a !== null) {
    const pages = a.content.pages as { key: string; title: string; covers: string; level: string }[];
    ev.push({ type: "action", goal_title: a.kind === "goal_new" && typeof a.content.title === "string" ? a.content.title : null, page_id: null, action_id: a.action_id, kind: a.kind, title: a.summary, items: pages.map((p) => ({ key: p.key, title: p.title,
      page_id: null, covers: p.covers !== "" ? p.covers : null, n: null, level: p.level !== "" ? p.level : null })), status: "proposed", confirm: true, can_undo: false,
      replaces: typeof a.content.replaces === "string" ? a.content.replaces : "" });
  }
  if (tail !== "") ev.push({ type: "text_delta", text: `\n\n${tail}`, at: text.length });
  ev.push({ type: "done", stopped: false, confirmed: false, message_id: `m_demo_blocks_${n}`, tool_calls: tools.length + (a ? 1 : 0), llm_calls: 2, cost_usd: null, attachments: [], session_attachments: [] });
  return ev;
}

/**
 * app.js startBlocks: 开始's first message (「我要学…」, one of the demo's topics) gets the goal's blocks as a draft card,
 * firstReply's paragraphs above it and its tail under it (搜了网页 / 读了你的背景 / 看了你的 Wiki first). null: not a 学点新的
 * start (the other flows answer it).
 */
export function demoBlocksStart(body: MessageIn, topics: Record<string, { regions: { id: string; name: string; scope: string; level?: string; target?: boolean }[] }>, n: number, now: Date):
  { conv: DemoBlocksConv; events: AssistantEvent[] } | null {
  const asked = demoGoalOf(body);
  if (asked === null) return null;
  const { goal, background } = asked;
  const topic = Object.keys(topics).find((k) => goal.includes(k));
  if (topic === undefined) return null;
  const blocks: DemoBlock[] = topics[topic]!.regions.map((r) => ({ key: r.id, name: r.name, covers: r.scope, level: r.level ?? "", from: [r.id], target: r.target === true }));
  const conv: DemoBlocksConv = { goal, background, topic, blocks, act: blocksRow({ goal, background, topic }, blocks, null, `act_blocks_${n}`, now) };
  const fr = firstReply(topic, blocks.length);
  const events = blocksEvents(n, [["web_search", "network", topic], ["read_goals", "read"], ["search_wiki", "read", topic]],
    fr.paras.join("\n\n"), conv.act, fr.tail.join("\n\n"));
  return { conv, events };
}

/** The draft's blocks the text names, in the order it names them (app.js blockHits). */
function blockHits(d: DemoBlock[], text: string): DemoBlock[] {
  return d.map((b) => [Math.min(...[b.name, ...(BLOCK_WORDS[b.from[0] ?? ""] ?? [])].map((w) => { const i = text.indexOf(w); return i < 0 ? 1e9 : i; })), b] as const)
    .filter(([at]) => at < 1e9).sort((x, y) => x[0] - y[0]).map(([, b]) => b);
}

/**
 * app.js blocksReply: 合 / 并 (two blocks or more), 不学 / 去掉, 改名 (with 「…」), 加, and the answer to the first reply's
 * question (了解 / 研究 → the biology blocks first; 养 → the order stays, one line). Each change gives a NEW draft row and the
 * old one is rejected as replaced (the fixture's edit_goal_blocks); after 确认 the 学习路径 is where to change it.
 */
export function demoBlocksReply(conv: DemoBlocksConv, text: string, n: number, now: Date): { events: AssistantEvent[]; replaced: ActionOut | null } {
  const plain = (t: string, tools: [string, string][] = []): { events: AssistantEvent[]; replaced: null } => ({ events: blocksEvents(n, tools, t, null), replaced: null });
  if (conv.act.status !== "proposed") return plain("已经按这个分法排好了。要改就在学习路径里直接改，或者撤销后重新分。", [["read_path", "read"]]);
  const d = conv.blocks.map((b) => ({ ...b, from: [...b.from] }));
  const hits = blockHits(d, text), q = (text.match(/「([^」]+)」/g) ?? []).map((x) => x.slice(1, -1));
  let say: string;
  if (/合|并/.test(text) && hits.length >= 2) {
    const byPos = (x: DemoBlock, y: DemoBlock): number => d.indexOf(x) - d.indexOf(y);
    const into = /(并|放|合)(到|进)/.test(text) ? hits[hits.length - 1]! : hits.find((b) => b.target === true) ?? [...hits].sort(byPos)[0]!;
    const parts = [...hits].sort(byPos), rest = parts.filter((b) => b !== into), at = d.indexOf(parts[0]!);
    const was = rest.map((b) => b.covers.split("、").slice(0, 3).join("、")).join("、");
    into.from = parts.flatMap((b) => b.from); into.covers = parts.map((b) => b.covers).filter((c) => c !== "").join("、");
    for (const b of parts) d.splice(d.indexOf(b), 1);
    d.splice(at, 0, into);
    say = `可以，「${rest.map((b) => b.name).join("」「")}」并进了「${into.name}」，原来的${was}都在这一块里，排在前面讲。`;
  } else if (/不(用|要|学|需要)|去掉|删|跳过/.test(text) && hits.length) {
    for (const b of hits) d.splice(d.indexOf(b), 1);
    say = `可以，「${hits.map((b) => b.name).join("」「")}」先不学，以后想学再加回来。`;
  } else if (/改(名|成|叫)|叫/.test(text) && hits.length && q.length) {
    const old = hits[0]!.name; hits[0]!.name = q[q.length - 1]!; say = `可以，「${old}」改叫「${hits[0]!.name}」。`;
  } else if (/加/.test(text)) {
    const name = q[0] ?? text.replace(/.*加(一块|上)?[:：]?\s*/, "").replace(/[。！!]$/, "").trim();
    if (!name) return plain("要加哪一块？");
    d.push({ key: `u${n}`, name, covers: "", level: "", from: [] }); say = `可以，加了一块「${name}」，排在最后。`;
  } else if (/了解|研究|原理/.test(text) && !/养/.test(text)) {
    const bio = d.filter((b) => ["id", "br"].includes(b.from[0] ?? "")), rest = d.filter((b) => !bio.includes(b));
    d.splice(0, d.length, ...bio, ...rest);
    say = "可以，按研究这种生物来排：「分类与鉴别」「生长与繁殖」放到前面，开缸和日常照顾只讲到能看懂原理为止。";
  } else if (/养/.test(text)) return plain("可以，就按自己养来排，顺序不变；分类和生理只讲到能看懂操作为止。");
  else return plain("要改哪一块？比如「水质和开缸合成一块」「繁殖先不学」。");
  const old: ActionOut = { ...conv.act, status: "rejected", detail: "replaced", decided_at: now.toISOString() };
  conv.blocks = d;
  conv.act = blocksRow(conv, d, text, `act_blocks_${n}`, now);
  conv.act.content = { ...conv.act.content, replaces: old.action_id };       // edit_goal_blocks names the row it replaced
  return { events: blocksEvents(n, [["read_goals", "read"]], say, conv.act), replaced: old };
}

/** 确认 of a blocks row: the blocks as confirmed (the card's edited list: kept keys, added titles). */
export function demoConfirmedBlocks(conv: DemoBlocksConv, draft: ActionDraft | undefined): DemoBlock[] {
  if (!draft || !("pages" in draft)) return conv.blocks;
  return draft.pages.map((p, k) => conv.blocks.find((b) => b.key === p.key) ?? { key: `new${k}`, name: p.title ?? "", covers: "", level: "", from: [] });
}
