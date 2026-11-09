# EvoUI：小程序（app）

`app` 是一个完整的 HTML 小程序，放在沙盒 iframe 里运行，是唯一能写 HTML 和脚本的节点。把概念画出来让人看懂的用 draw_visual，不用自己写；
这里手写的是像小游戏一样的练习。翻面记忆的卡片用 `flashcards`（它自带 3D 翻面），不要为它写 app。它同样要在块的 `question` 里写清理解障碍，块的 `fallback` 要能代替它
（小程序 8 秒内没有准备好、或者读者的环境跑不了它时，`fallback` 会显示在它下面）。

| 字段 | 说明 |
|---|---|
| `html` | 必填，1–60000 字的完整小程序：样式、标记和脚本都写在里面（可以只写 `<div>…<style>…<script>…`，也可以带 `<!doctype html><html><head>…`） |
| `height` | 初始高度（像素，200–900 的整数，默认 420）；小程序可以用 `evowiki.resize()` 改成 200–1200 |
| `items` | 小程序会回报的可评分项，0–40 个 `{"id", "label", "cell"}`：`id` 小写字母 / 数字 / `-` / `_`，1–32 字，不能重复；`label` 纯文字（≤ 120 字）；`cell` 可选，只能用这一页给出的格子，回报的对错会作为学习证据记下 |
| `id` | 可选，同 quiz 的 `id` |

一个块里最多一个 `app`。块的 40000 字上限不算 `html` 本身。

小程序里能用的：
- 内联的 `<style>` 和 `<script>`（包括 `type="module"`）、`data:` / `blob:` 图片、canvas、SVG、CSS 动画和 3D 变换。
- 外部文件只能从这三个 CDN 加载：`https://cdn.jsdelivr.net/`、`https://unpkg.com/`、`https://cdnjs.cloudflare.com/`（脚本、样式、
  字体）。不允许 `eval` / `new Function`（没有 'unsafe-eval'）：靠它们编译代码的库（某些模板引擎、表达式解析库）在这里跑不起来。别的网址一律不行：不能 `fetch`、不能连接网络、不能用外部图片 / 字体 / 样式，所有数据都写在小程序里。
- 不能写 `<iframe>` `<object>` `<embed>` `<base>` `<meta http-equiv>`，不能用 `document.cookie`、`localStorage`、`sessionStorage`、
  `indexedDB`，不能碰 `window.parent` / `window.top` / `parent.` / `top.`（变量也不要叫 `parent` 或 `top`）；弹窗、表单提交、
  跳转页面都会被拦下。不能改 `location`（`location = …`、`location.href = …`、`assign` / `replace` / `reload`；变量和属性也不要叫 `location`），不能用 WebRTC，
  不能写 `<link>` 的 dns-prefetch / preconnect / prefetch / prerender。小程序自己跳到别的页面或重新载入时，页面会把它移除，
  显示 `fallback` 和「重新载入」按钮。和页面打交道只通过下面的 `window.evowiki`。
- 颜色用页面的变量，深色模式自动跟着变：`--viz-1`（蓝，主数据色）、`--viz-2`（橙，第二数据色）、`--known` / `--shaky` / `--gap`
  （绿 / 黄 / 红：对、半对、错），`--text`、`--text-2`（次要文字）、`--bg`、`--surface`（浅底卡片）、`--line`（细线）、`--accent`；
  也可以用 `--viz-text` `--viz-muted` `--viz-panel` `--viz-card` `--viz-border`。`<html>` 上有 `data-theme="light"` 或 `"dark"`；
  换主题时会触发 `evowiki:theme` 事件（画布要重画）。小程序的背景是透明的，页面的底色会透出来。
- 尊重「减少动态效果」：`matchMedia("(prefers-reduced-motion: reduce)")` 为真时不放动画、不撒彩纸。窄屏（375 像素宽）也不能
  横向溢出。

`window.evowiki`（小程序的脚本一开始就能用）：
- `evowiki.ready()`：小程序画好、可以操作了就调用一次。**必须调用**，否则 8 秒后页面会在它下面显示 `fallback`，检查也不会通过。
- `evowiki.item(id, response, correct, hintUsed, latencyMs)`：读者完成了 `items` 里的一项。`id` 必须是声明过的；`response` 是读者的
  作答（任意 JSON，≤ 2000 字）；`correct` 是 `true` / `false`（小程序自己判），判不了就写 `null`（只记下来，不算分）；`hintUsed`
  可选（默认 false）；`latencyMs` 可选（0–3600000 的整数；不写或超过时由页面按上一次回报到这次的时间算）。同一项每次打开最多记 3 次，
  和上一次回报完全相同（同一项、同样的作答和对错）的会被跳过：只在读者真的又做了一次时回报。
- `evowiki.summary({total, right, missed})`：一轮做完时调用一次（`missed` 是没做对的项的 `id` 列表）。之后的回报算第二轮。
- `evowiki.save(progress)`：保存小程序自己的进度（任意 JSON，≤ 16000 字）。下次打开时从 `evowiki.onInit(fn)` 拿回来：`fn(progress)`
  在页面把进度交给小程序时调用（没有保存过就是 `null`），之后 `evowiki.progress` 也是它。
- 框的高度自动跟着内容走（200–1200）。`evowiki.resize(h)` 把高度定成 `h` 像素，之后不再跟着内容变。
- `evowiki.image(id)`：draw_visual 放进来的真实图片，返回一个 Promise，给出能放进 `<img>` 的地址。
- 格式不对、项没声明、太大的消息会被页面丢掉；一个小程序最多回报 500 条消息。

最小的例子：

```json
{"kind": "app", "id": "spin", "height": 300,
 "items": [{"id": "turn", "label": "把箭头转到 90°", "cell": "c1.interpret"}],
 "html": "<style>button{font:inherit;padding:8px 14px;border-radius:10px;border:1px solid var(--line);background:var(--bg);color:var(--text)} #a{transition:transform .3s;transform-origin:50% 100%}</style><svg width=\"120\" height=\"120\"><line id=\"a\" x1=\"60\" y1=\"110\" x2=\"60\" y2=\"20\" stroke=\"var(--viz-1)\" stroke-width=\"6\"/></svg><p id=\"t\">0°</p><button id=\"b\">转 30°</button><script>let deg = 0; const a = document.getElementById('a'), t = document.getElementById('t'); function show() { a.style.transform = 'rotate(' + deg + 'deg)'; t.textContent = deg + '°'; } document.getElementById('b').onclick = () => { deg = (deg + 30) % 360; show(); evowiki.save({deg}); if (deg === 90) evowiki.item('turn', deg, true); }; evowiki.onInit(p => { if (p) { deg = p.deg; show(); } }); evowiki.ready();</script>"}
```
