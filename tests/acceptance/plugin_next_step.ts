/**
 * What the Obsidian plugin offers as the next step, by the plugin's own code from what the server returned
 * (tests/acceptance/plugin.py bundles this file with esbuild and runs it with node). stdin: {catalog, paths, page,
 * progress} (GET /wiki, the GET /wiki/paths/{id} the plugin has read, the GET /wiki/pages/{id} of the open page,
 * GET /wiki/progress). stdout: {today: 今天's 下一步 (todayStep, with the label its button shows), end: the open page's
 * 「读完了」 main button (endModel)}.
 */
import { endModel } from "../../obsidian-plugin/src/wiki/end-block/model";
import { stepLabel, todayStep } from "../../obsidian-plugin/src/wiki/progress";
import type { CatalogOut, PageOut, PathOut, ProgressOut } from "../../obsidian-plugin/src/wiki/schema";
import { INITIAL_WIKI_STATE, type WikiState } from "../../obsidian-plugin/src/wiki/state";

interface Input { catalog: CatalogOut; paths: PathOut[]; page: PageOut; progress: ProgressOut }

let raw = "";
process.stdin.setEncoding("utf8");
process.stdin.on("data", (c: string) => { raw += c; });
process.stdin.on("end", () => {
  const input = JSON.parse(raw) as Input;
  const state: WikiState = {
    ...INITIAL_WIKI_STATE,
    conn: "online",
    catalog: { kind: "ready", value: input.catalog },
    progress: { kind: "ready", value: input.progress },
    pathPlans: Object.fromEntries(input.paths.map((p) => [p.path_id, p])),
    // no review card is due: 下一步 is about learning only
    review: { ...INITIAL_WIKI_STATE.review, today: { kind: "ready", value: { items: [], confirms: [] } as never } },
    page: { kind: "open", page: input.page, streaming: {}, touched: {}, streamWriter: null } as never,
  };
  const end = endModel(state, input.page.page_id);
  const today = todayStep(state);
  process.stdout.write(JSON.stringify({ today: today === null ? null : { ...today, label: stepLabel(today) }, end: end?.primary ?? null }));
});
