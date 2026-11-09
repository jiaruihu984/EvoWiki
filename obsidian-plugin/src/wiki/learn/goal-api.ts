/**
 * 学点新的: the big-goal routes as the plugin calls them (types: schema.ts, from wiki_schemas.py). The plugin runs on
 * HttpGoalApi; goal-fake.ts (big-goal-demo.html's scripted flow behind the same contract) is used only with plugin data
 * `fixture: "demo"` (the screenshot comparison).
 *
 * A goal is made only in the 助手 (its goal_new action); 学点新的 follows it from the draft id on (getDraft).
 *
 * - POST /wiki/goals/{draft_id}/answers {answers} (aligned, −1 = 不知道) or {skip: true} (stop now) → GoalDraftOut
 *   (the next round: same draft_id, round + 1, `results` for the round just answered; finished: map_id / path_id / page_id)
 * - GET /wiki/maps/{map_id} → GoalMapOut (404 unknown)
 * - POST /wiki/maps/{map_id}/regions/{node_id}/enter → GoalDraftOut, any map node (spec 2026-10-28): a block / sub-block
 *   split one level or shown (map_id: its parts are on the map), a point opened (a round over it, or its path); 409 known,
 *   404 unknown, 502 the split failed, 503 no model
 * - POST /wiki/nodes/{node_id}/split {map_id} → SplitOut (继续拆细 on a point: its parts go under it on that map)
 * - GET /wiki/paths/{path_id} → PathOut; POST /wiki/paths/{path_id}/next → PathNextOut
 * - GET /wiki/writes?page_ids=… → WriteOut[] (where each page's writing is: a block's page cards fill with it)
 * - GET /wiki/progress → ProgressOut (every goal's next step as the server decides it: a reloaded goal run resumes there)
 * - GET /wiki/goals/{draft_id} → GoalDraftOut (an open draft's current round, or what it finished as): 学点新的 after a
 *   goal confirmed in the chat (the assistant's goal_new row gives the draft id)
 * - POST /wiki/maps/{map_id}/edits {ops} → MapEditOut (the map + `undo`: 撤销 sends {op: "restore", map: undo}); 404 / 409 /
 *   422 refused, nothing changed
 * - POST /wiki/paths/{path_id}/edits (move / remove / unremove / add), …/rows/{key}/split and …/split/undo → PathEditOut
 *   (a block's page list, the same routes as the 知识图谱 path card)
 *
 * - POST /wiki/readings/{reading_id}/units/{unit_id}/open {start} → ReadingUnitOpenOut (读懂这一篇: a unit entered —
 *   「看这一段」 once — and with `start` its reading page started; 409 skipped / 404 unknown)
 *
 * Every route but GET makes LLM calls on the real server: tests use a recording transport only.
 */
import { jsonCall } from "../../api/client";
import type { WikiConfig, WikiHttp } from "../api";
import type {
  GraphPlacesOut, GoalDraftOut, GoalMapOut, MapEditOpIn, MapEditOut, PathEditIn, PathEditOut, PathNextOut, PathOut, ProgressOut, ReadingUnitOpenOut, SplitOut, WriteOut,
} from "../schema";

/** 提交 (answers aligned with the questions, −1 = 不知道) or 跳过 (stop now with the evidence so far). */
export type GoalAnswer = { answers: number[] } | { skip: true };

export interface GoalApi {
  /** True for the HTTP client: 生成第一页 then really writes the page through the wiki controller. */
  readonly live: boolean;
  answer(draftId: string, body: GoalAnswer): Promise<GoalDraftOut>;
  getMap(mapId: string): Promise<GoalMapOut>;
  /** Any map node; 409 when it is known; re-entering with an open round in it returns that round. */
  enterRegion(mapId: string, regionId: string): Promise<GoalDraftOut>;
  /** 继续拆细: the point split one level (one model call), its parts added under it on map `mapId`. */
  splitNode(nodeId: string, mapId: string): Promise<SplitOut>;
  getPath(pathId: string): Promise<PathOut>;
  pathNext(pathId: string): Promise<PathNextOut>;
  /** GET /wiki/goals/{draft_id}: the draft's current round, or its result (map / path / page). */
  getDraft(draftId: string): Promise<GoalDraftOut>;
  /** The 学习路径 card's edits (reorder / drop / rename / add / restore), all or none. */
  editMap(mapId: string, ops: MapEditOpIn[]): Promise<MapEditOut>;
  /** A block's page list: move / remove / unremove / add. */
  editPath(pathId: string, edit: PathEditIn): Promise<PathEditOut>;
  /** 拆成多页 of a page row (may call the model); `undo` in the answer takes it back. */
  splitRow(pathId: string, pageKey: string): Promise<PathEditOut>;
  undoSplitRow(pathId: string, pageKey: string, changeId: string): Promise<PathEditOut>;
  /** Where a goal's blocks stand among his groups, similar ones near (POST /wiki/graph/places; read only). Absent: not placed. */
  graphPlaces?(blocks: { id: string; name: string; scope: string }[]): Promise<GraphPlacesOut>;
  /** GET /wiki/writes: where each page's writing is (a block's page cards show it). Absent: no progress is shown. */
  writes?(pageIds: string[]): Promise<WriteOut[]>;
  /** GET /wiki/progress: where the learner is on each goal (the one source of progress). Absent (the demo): not read. */
  progress?(): Promise<ProgressOut>;
  /** 读懂这一篇: enter a unit (its points and uses once it is read the first time); `start` also starts its page. Absent: no reading. */
  openUnit?(readingId: string, unitId: string, start: boolean): Promise<ReadingUnitOpenOut>;
}

const enc = encodeURIComponent;

/** The live client on the wiki HTTP transport (jsonCall: X-EvoWiki-User, non-2xx → ApiError, unreachable → OfflineError). */
export class HttpGoalApi implements GoalApi {
  readonly live = true;
  graphPlaces(blocks: { id: string; name: string; scope: string }[]): Promise<GraphPlacesOut> { return this.call("POST", "/wiki/graph/places", { blocks }); }
  constructor(private readonly config: () => WikiConfig, private readonly http: WikiHttp) {}
  private call<T>(method: "GET" | "POST", path: string, body?: unknown): Promise<T> {
    return jsonCall<T, "GET" | "POST">(this.http, this.config(), method, path, body);
  }
  answer(draftId: string, body: GoalAnswer): Promise<GoalDraftOut> {
    const wire = "skip" in body ? { skip: true } : { answers: body.answers };
    return this.call("POST", `/wiki/goals/${enc(draftId)}/answers`, wire);
  }
  getMap(mapId: string): Promise<GoalMapOut> { return this.call("GET", `/wiki/maps/${enc(mapId)}`); }
  enterRegion(mapId: string, regionId: string): Promise<GoalDraftOut> {
    return this.call("POST", `/wiki/maps/${enc(mapId)}/regions/${enc(regionId)}/enter`);
  }
  splitNode(nodeId: string, mapId: string): Promise<SplitOut> {
    return this.call("POST", `/wiki/nodes/${enc(nodeId)}/split`, { map_id: mapId, reason: "requested" });
  }
  getPath(pathId: string): Promise<PathOut> { return this.call("GET", `/wiki/paths/${enc(pathId)}`); }
  pathNext(pathId: string): Promise<PathNextOut> { return this.call("POST", `/wiki/paths/${enc(pathId)}/next`); }
  getDraft(draftId: string): Promise<GoalDraftOut> { return this.call("GET", `/wiki/goals/${enc(draftId)}`); }
  progress(): Promise<ProgressOut> { return this.call("GET", "/wiki/progress"); }
  openUnit(readingId: string, unitId: string, start: boolean): Promise<ReadingUnitOpenOut> {
    return this.call("POST", `/wiki/readings/${enc(readingId)}/units/${enc(unitId)}/open`, { start });
  }
  editMap(mapId: string, ops: MapEditOpIn[]): Promise<MapEditOut> { return this.call("POST", `/wiki/maps/${enc(mapId)}/edits`, { ops }); }
  editPath(pathId: string, edit: PathEditIn): Promise<PathEditOut> { return this.call("POST", `/wiki/paths/${enc(pathId)}/edits`, edit); }
  splitRow(pathId: string, pageKey: string): Promise<PathEditOut> {
    return this.call("POST", `/wiki/paths/${enc(pathId)}/rows/${enc(pageKey)}/split`);
  }
  writes(pageIds: string[]): Promise<WriteOut[]> {
    return this.call("GET", `/wiki/writes?page_ids=${enc(pageIds.join(","))}&unwritten=true`);
  }
  undoSplitRow(pathId: string, pageKey: string, changeId: string): Promise<PathEditOut> {
    return this.call("POST", `/wiki/paths/${enc(pathId)}/rows/${enc(pageKey)}/split/undo`, { change_id: changeId });
  }
}
