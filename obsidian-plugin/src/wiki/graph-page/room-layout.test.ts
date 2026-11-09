import { describe, expect, it } from "vitest";
import { cloudRadius, fillBox, packGroups, type RoomCloud, type RoomGroup, sunflower, TITLE_GAP, titleWidth } from "./room-layout";

// his old regions in the corners, a goal's blocks in the middle: block names of the user's report
const OLD: RoomGroup[] = [
  { id: "o1", title: "随机图与网络韧性", titlePx: 16, points: 6, at: [-1, -1] },
  { id: "o2", title: "网络表示与度量", titlePx: 16, points: 5, at: [1, -1] },
];
const blocks = (names: string[]): RoomGroup[] => names.map((title, i) => {
  const a = (i / names.length) * 2 * Math.PI - Math.PI / 2;
  return { id: `b${i}`, title, titlePx: 13, points: 6, at: [0.55 * Math.cos(a), 0.45 * Math.sin(a)] };
});
const SEVEN = blocks(["分类与鉴别", "水质与环境", "开缸与日常", "营养与摄食", "生长与繁殖", "疾病与风险", "水族箱设备与选购"]);

type Rect = { x0: number; y0: number; x1: number; y1: number; id: string };
/** Each group's title box and its cloud's box, as drawn. */
function rects(gs: readonly RoomGroup[], W: number, H: number): { all: Rect[]; height: number } {
  const p = packGroups(gs, W, H), all: Rect[] = [];
  for (const g of gs) {
    const c = p.places.get(g.id)!, r = cloudRadius(g.points, p.spacing), w = titleWidth(g.title, g.titlePx), h = Math.ceil(g.titlePx * 1.3) + 4;
    all.push({ id: g.id + ":cloud", x0: c.x - r, x1: c.x + r, y0: c.y - r, y1: c.y + r });
    all.push({ id: g.id + ":title", x0: c.x - w / 2, x1: c.x + w / 2, y0: c.y - r - TITLE_GAP - h, y1: c.y - r - TITLE_GAP });
  }
  return { all, height: p.height };
}
const hits = (all: readonly Rect[]): string[] => all.flatMap((a, i) => all.slice(i + 1)
  .filter((b) => a.x0 < b.x1 && b.x0 < a.x1 && a.y0 < b.y1 && b.y0 < a.y1).map((b) => `${a.id}×${b.id}`));

describe("packGroups: no title touches a title or a cloud, in any box", () => {
  it.each([[290, 800], [650, 386], [436, 790], [840, 840], [1080, 840], [300, 500]])("box %i × %i", (W, H) => {
    const { all, height } = rects([...OLD, ...SEVEN], W, H);
    expect(hits(all)).toEqual([]);
    expect(height).toBeLessThanOrEqual(H);
    for (const r of all) { expect(r.x0).toBeGreaterThanOrEqual(0); expect(r.x1).toBeLessThanOrEqual(W); expect(r.y0).toBeGreaterThanOrEqual(0); }
  });
  it("a long title and a one-point block keep their room", () => {
    const gs = blocks(["一个很长很长很长很长很长很长的块标题", "短", "另一个也不算短的块标题"]).map((g, i) => ({ ...g, points: i + 1 }));
    expect(hits(rects(gs, 320, 400).all)).toEqual([]);
  });
  it("the picture fills the box: the larger the box, the wider the spacing; the rows use its height", () => {
    const at = (W: number, H: number) => packGroups([...OLD, ...SEVEN], W, H);
    expect(at(1080, 840).spacing).toBeGreaterThan(at(436, 500).spacing);
    expect(at(1080, 840).height).toBeCloseTo(840, 0);
    const ys = [...at(650, 386).places.values()].map((p) => p.y);
    expect(Math.max(...ys) - Math.min(...ys)).toBeGreaterThan(386 * 0.45);
  });
  it("his regions go to the top corners, the blocks under them", () => {
    const p = packGroups([...OLD, ...SEVEN], 840, 840).places;
    expect(p.get("o1")!.x).toBeLessThan(840 / 2);
    expect(p.get("o2")!.x).toBeGreaterThan(840 / 2);
    for (const g of SEVEN) expect(p.get(g.id)!.y).toBeGreaterThan(p.get("o1")!.y);
  });
  it("too much for the box: the rows run past it and say how far", () => {
    const many = blocks(Array.from({ length: 60 }, (_, i) => `第${i}块的名字有点长`));
    expect(packGroups(many, 200, 200).height).toBeGreaterThan(200);
  });
});

describe("sunflower", () => {
  it("the first point is the middle; the others stay within the cloud's radius and apart", () => {
    expect(sunflower(0, 20)).toEqual({ x: 0, y: 0 });
    const ps = Array.from({ length: 12 }, (_, i) => sunflower(i, 20));
    for (const p of ps) expect(Math.hypot(p.x, p.y)).toBeLessThanOrEqual(cloudRadius(12, 20));
    const least = Math.min(...ps.flatMap((a, i) => ps.slice(i + 1).map((b) => Math.hypot(a.x - b.x, a.y - b.y))));
    expect(least).toBeGreaterThan(15);
  });
});

describe("fillBox", () => {
  /** Clouds whose titles are as wide as a narrow box allows: a title keeps its pixels whatever the unit. */
  const clouds = (n: number): RoomCloud[] => Array.from({ length: n }, (_, i) => {
    const a = i * 2.4, tw = 110, ch = 12;
    return { x: Math.cos(a) * 100, y: Math.sin(a) * 100, size: (u: number) => ({ x0: -Math.max(ch, tw / u), x1: Math.max(ch, tw / u), y0: -ch, y1: ch + 34 / u }) };
  });
  it.each([[436, 860], [351, 1300], [751, 700], [1036, 860]])("the placement kept fits the box and nothing touches: %i × %i", (W, H) => {
    const all = clouds(12), U = fillBox(all, W, H);
    expect((U.x1 - U.x0) * U.unit).toBeLessThanOrEqual(W + 1);
    expect((U.y1 - U.y0) * U.unit).toBeLessThanOrEqual(H + 1);
    const rs = all.map((c) => { const b = c.size(U.unit); return { x0: c.x + b.x0, x1: c.x + b.x1, y0: c.y + b.y0, y1: c.y + b.y1 }; });
    const eps = 1e-6;
    rs.forEach((a, i) => rs.slice(i + 1).forEach((b) => { expect(a.x0 < b.x1 - eps && b.x0 < a.x1 - eps && a.y0 < b.y1 - eps && b.y0 < a.y1 - eps).toBe(false); }));
  });
});

describe("fillBox in a box too narrow to push wide titles apart sideways", () => {
  it("stands them in rows: no two touch and the picture stays inside the box (regression, 339 wide)", () => {
    // what 知识图谱 hit: a title of 170 px beside a goal of 150 px, side by side in 339 px, one hid the other
    const cloud = (x: number, y: number, w: number, h: number): RoomCloud => ({ x, y, size: (u) => ({ x0: -w / 2 / u, x1: w / 2 / u, y0: -h / 2 / u, y1: h / 2 / u }) });
    const all = [cloud(0, 0, 330, 300), cloud(1, 0, 170, 60), cloud(0, 1, 170, 60), cloud(1, 1, 300, 100)];
    const f = fillBox(all, 339, 1500);
    const px = all.map((c) => { const b = c.size(f.unit); return { x0: (c.x + b.x0) * f.unit, x1: (c.x + b.x1) * f.unit, y0: (c.y + b.y0) * f.unit, y1: (c.y + b.y1) * f.unit }; });
    px.forEach((a, i) => px.slice(i + 1).forEach((b) => { expect(a.x1 <= b.x0 + 1e-6 || b.x1 <= a.x0 + 1e-6 || a.y1 <= b.y0 + 1e-6 || b.y1 <= a.y0 + 1e-6).toBe(true); }));
    expect(Math.max(...px.map((b) => b.x1)) - Math.min(...px.map((b) => b.x0))).toBeLessThanOrEqual(339 + 1e-6);
  });
});
