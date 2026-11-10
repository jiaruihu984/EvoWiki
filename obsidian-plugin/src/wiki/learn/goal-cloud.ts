import { realId, type Scene } from "../graph-page/scene";
import { GraphCloud, type GraphCloudHandle, type Hover } from "../graph-page/sigma-cloud";
import type { LearnScene } from "./learn-scene";
import { learnScene } from "./learn-scene";
import { bandOf, topRegions } from "./learn-flow";
import type { LearnDiag } from "./learn-view";
import type { Band, GoalMapOut, GraphPlacesOut, MapNodeOut } from "../schema";

export type BlockState = "known" | "shaky" | "gap" | "asking" | "none";
const TINT: Record<BlockState, Band> = { known: "known", shaky: "shaky", gap: "gap", asking: "none", none: "none" };

/** The original learning view's rule, used by the list and both charts. */
export function regionState(r: MapNodeOut, d: LearnDiag | null, withAsking = true): BlockState {
  if (withAsking && d?.asking === r.id) return "asking";
  const m = d?.marks[r.id];
  const mark: BlockState | null = m === undefined ? null : m === "know" ? "known" : m === "partly" ? "shaky" : "gap";
  if (mark !== null && d?.open !== false) return mark;
  const b = bandOf(r);
  const learned = (r.mix.known ?? 0) + (r.mix.likely ?? 0) >= 0.999;
  const band: BlockState = r.state === "known" || learned ? "known" : b === "known" || b === "likely" || b === "shaky" ? "shaky"
    : b === "gap" ? "gap" : "none";
  return band === "none" && mark !== null ? (mark === "known" ? "shaky" : mark) : band;
}

export function goalScene(base: Scene | null, map: GoalMapOut | null, places: GraphPlacesOut | null, title: string, diag: LearnDiag | null): LearnScene {
  const scene = learnScene(base, map, places, title);
  tintGoalScene(scene, map, diag);
  return scene;
}

export function tintGoalScene(scene: LearnScene, map: GoalMapOut | null, diag: LearnDiag | null): void {
  const tint = goalTints(map, diag);
  for (const [id, region] of scene.goalOf) {
    const band = tint.get(region), node = scene.scene.nodes.get(id);
    if (band !== undefined && node !== undefined && realId(id) === region) node.band = band;
  }
}

export function goalTints(map: GoalMapOut | null, diag: LearnDiag | null): ReadonlyMap<string, Band> {
  return new Map(topRegions(map).map((r) => [r.id, TINT[regionState(r, diag, false)]]));
}

/** Both main-area views use the original learning chart. Only the colour source differs. */
export function goalCloud(container: HTMLElement, o: {
  root: HTMLElement;
  scene(): LearnScene | null;
  background: "mastery" | "domain";
  enter(region: string): boolean;
  canEnter(region: string): boolean;
  hover(h: Hover | null): void;
  inspect?(id: string | null): void;
}): GraphCloudHandle {
  return GraphCloud(container, {
    tokens: o.root,
    font: () => getComputedStyle(o.root).fontFamily,
    // Keep the learning graph animated without changing Windows UI animation settings.
    reduced: () => false,
    onSelect: (id) => {
      const region = id === null ? undefined : o.scene()?.goalOf.get(id);
      if (region !== undefined) o.enter(region);
      else o.inspect?.(id);
    },
    onGroup: (id) => o.enter(o.scene()?.goalOf.get(id) ?? id),
    onHover: (h) => { o.hover(h); },
    grows: (id) => o.scene()?.goalOf.has(id) === true,
    semantic: true,
    washByBand: o.background === "mastery",
    pinned: (id) => o.scene()?.goalOf.get(id) === realId(id),
    canClick: (id) => {
      const region = o.scene()?.goalOf.get(id);
      return region !== undefined ? o.canEnter(region) : o.inspect !== undefined;
    },
  });
}
