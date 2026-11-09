# EvoUI 块的通用写法（core）

一个交互块是一个 JSON 对象。它不是 HTML，也不能含脚本；渲染器只认识下面列出的节点和表达式（唯一的例外是
沙盒里的小程序 `app`，见 evoui/app）。
所有文字都按纯文字显示（写 `<b>` 就会原样显示成 `<b>`）。不合规则的块不会被放到页面上。

## 块

```json
{"type": "evoui", "title": "标题", "question": "这个交互的用处：解决的理解障碍 / 要练熟的东西 / 回答的学习问题",
 "state": {"名字": 初始值},
 "derived": {"名字": "表达式"},
 "body": 节点,
 "fallback": "渲染不了时显示的一段文字",
 "probes": [{"set": {"名字": 值}}]}
```

- `state`：读者能改的值（数字、字符串、布尔、对象）。只能被控件改。名字用字母、数字、下划线或汉字，
  不能和内置函数同名。
- `derived`：由 state 和其他 derived 算出的值，按书写顺序计算，只能用前面已经有的名字。
- `fallback`：一段完整的纯文字，渲染不了时（或读者的环境不支持时）代替整个交互，要把结论讲清楚。
- `probes`（可选）：检查用的试操作，「把这些 state 设成这些值之后，块里的文字或图应该变」。不写时，系统会把
  每个控件各试两个值（滑块 / 数字框 / 播放器：最小和最大；选择 / 下拉 / 写了 `bind` 的 route：第一项和最后一项；开关 / 勾选：
  开和关）。
  所以每个控件都要真的影响块里显示的东西。
- 除 `probes` 外所有字段都必填（`derived` 可以是 `{}`）。不能有表里没列的字段。

## 节点

每个节点是 `{"kind": "...", ...}`。「模板」是可以带 `{表达式}` 的文字。

布局：

| kind | 字段 | 说明 |
|---|---|---|
| `box` | `children`、`gap`(0–4)、`border`(bool)、`tone`("plain"/"surface") | 竖排容器；surface 是浅底卡片 |
| `row` | `children`、`gap`、`justify`("start"/"between"/"center")、`wrap`(bool) | 横排容器 |
| `grid` | `children`、`columns`(1–4)、`gap` | 网格；很窄时 3、4 列自动变成 1 列 |
| `grid-item` | `children`、`span`(1–4)、`gap`、`border`、`tone`("plain"/"surface") | 网格里占几列的一格 |
| `divider` | （无） | 分隔线 |

文字：

| kind | 字段 | 说明 |
|---|---|---|
| `text` | `text`（模板）、`size`("sm"/"md"/"lg")、`weight`("normal"/"bold")、`tone` | 一段文字 |
| `title` | `text`（模板）、`size`("md"/"lg"/"xl"/"2xl")、`tone` | 大号数字或结论（等宽数字） |
| `caption` | `text`（模板） | 小号灰字：说明、数值的名称 |
| `badge` | `text`（模板）、`tone` | 小标签（随状态变的结论标签） |
| `math` | `tex`（LaTeX，纯文字，不是模板）、`display`(bool，单独一行) | 公式；只写不变的公式，变化的数用 text |
| `icon` | `name`、`tone` | 小图标：`arrow-down` `arrow-up` `arrow-left` `arrow-right` `check` `cross` `dot` `plus` `minus` `info` `warning` |
| `table` | `columns`（列名，一般不超过 6 个）、`rows`、`caption`（模板） | 表格；`rows` 是行的数组（每行每列一个模板，一般不超过 20 行），或一个算出 `[[…], …]` 的表达式（只显示前 200 行） |
| `timeline` | `events`：`[{"when","title","text","tone"}]`（至少 2 项，按先后顺序） | 时间线：竖排的事件或阶段（evoui/timeline） |
| `deep-dive` | `children`（至少 1 个节点）、`label`（纯文字，默认「深入一点」） | 折叠起来的「深入一点」，读者自己点开才看（evoui/timeline） |

控件（`bind` 是 state 名）：

| kind | 字段 | 说明 |
|---|---|---|
| `slider` | `bind`（数字）、`min`、`max`、`step`、`label`（模板） | 滑块 |
| `input` | `bind`（数字）、`min`、`max`、`step`、`label` | 数字输入框，输入会夹在 min–max 之间 |
| `choice` | `bind`、`options`：`[{"label": "...", "value": 值}]`、`style`("segmented"/"buttons") | 分段选择或一排预设按钮 |
| `select` | `bind`、`options`、`label` | 下拉选择（选项多时用） |
| `toggle` | `bind`（布尔）、`label` | 开关 |
| `checkbox` | `bind`（布尔）、`label` | 勾选框 |
| `button` | `label`（模板）、`set`：`{"state 名": 表达式}`、`tone` | 按一下把几个 state 设成这些值（预设、重置、下一步） |

逻辑、图和测验：

| kind | 字段 | 说明 |
|---|---|---|
| `if` | `cond`（表达式）、`then`（节点）、`else`（节点或 null） | 条件显示 |
| `plot` | `xmin` `xmax` `ymin` `ymax`（数字）、`grid`(bool)、`height`(160–420)、`items`（见下）、`legend`：`[{"label","tone"}]` | 坐标平面：向量、几何、时序图、递归树 |
| `chart` | `chart`("line"/"area"/"bar"/"scatter"/"pie")、`data`（表达式，结果是对象数组）、`x`（字段名）、`series`：`[{"key","label"}]`（至少 1 个，一般不超过 4 个；pie 只能 1 个）、`x_label` `y_label` `caption`（模板）、`y_min`（数字）；可选 `stack` `curve` `gradient` `hover` `visible_points` `annotations` `donut`（evoui/chart） | 图表；scatter 的 x 字段要是数字；pie 用 x 字段做名称、series 的字段做数值 |
| `quiz` | `questions`：`[{"q","options":[...],"correct":序号,"why","type","feedback","hint","case","cell"}]`、`cases`：`[{"id","label","advice"}]`、`layout`("list"/"paged")、`title`、`id` | 一组选择题（evoui/quiz） |
| `flashcards` | `cards`：`[{"front","back","hint","cell"}]`（至少 2 张，一般不超过 40 张）、`title`、`shuffle`(bool)、`id` | 一叠闪卡：翻面看答案，自评记住 / 没记住（evoui/flashcard） |

揭开、动效、计时（evoui/reveal）和播放（evoui/player）：

| kind | 字段 | 说明 |
|---|---|---|
| `reveal` | `children`（1–12 个节点）、`mode`("click"/"steps"/"after")、`after`（练习节点的 `id`）、`label`（模板）、`id` | 先遮住，点一下、一步一步、或做完某个练习后再显示 |
| `animate` | `children`、`effect`("rise"/"fade") | 出现时淡入（rise 再带一点上移） |
| `transition` | `key`（表达式）、`children` | `key` 的值一变，旧内容淡出、新内容淡入 |
| `celebrate` | `when`（表达式） | `when` 从假变成真的那一刻撒一次彩纸 |
| `timer` | `mode`("countdown"/"stopwatch")、`seconds`（倒计时秒数 1–3600）、`bind`（数字 state，可选）、`autostart`(bool)、`label`（模板）、`id` | 倒计时 / 秒表 |
| `player` | `bind`（数字）、`min`、`max`、`step`、`interval_ms`(100–5000)、`loop`(bool)、`label`（模板）、`id` | 播放器：按时间一步一步推进一个 state（算法动画、参数扫描） |

练习卡、翻页和选择路线（evoui/practice）：

| kind | 字段 | 说明 |
|---|---|---|
| `fill-blank` | `sentence`（纯文字，正好一个 `___`）、`options`：`[{"label","why"}]`（2–8 个）、`correct`（序号）、`why`、`cell`、`id` | 填空：点一个候选填进空里，当场判对错 |
| `sentence` | `sentence`（纯文字，1–8 个 `___`）、`words`（空数到 12 个词）、`answer`（每个空一个词的序号）、`why`、`cell`、`id` | 拼句：按顺序点词填满所有空，填满就判 |
| `pager` | `children`（至少 1 页，一般不超过 20 页，每页一个节点）、`gate`(bool)、`id` | 练习翻页器：一次显示一页，「‹ n/N ›」翻页，最后一页「完成」 |
| `route` | `prompt`（模板）、`options`：`[{"label","value","next","desc"}]`（至少 2 个，一般不超过 6 个）、`bind`（可选）、`preference`（可选）、`id` | 选择路线：读者选接下来怎么学，选中的会告诉系统 |

自绘图和循环（用来画概念的是 draw_visual，这里是节点本身）：

| kind | 字段 | 说明 |
|---|---|---|
| `svg` | `viewBox`("0 0 640 300")、`children`（图形）、`tween`（ms） | 里面每项 `{"el": "rect", "x": "{i*5}", "width": 9, "fill": "{i<3?'blue':'gray-soft'}", "click": {"状态": 表达式}, "drag": {"状态": "clamp(mx,0,9)"}}`；`el` 是 path rect circle ellipse line polyline polygon text image g node arrow plot；也可以放 `each`、`if` |
| `each` | `in`（表达式，结果是列表）、`as`（默认 d）、`index`（默认 i）、`children` | 每项一份 `children`，放在 box、row 或 svg 里都行；不能放 quiz 这些带进度的节点 |

`box` `row` `grid-item` 还可以有 `background` `width` `height` `flex` `radius` `padding` `align` `color` `clip` `click`（模板）。`text` 还可以有 `color` `align` `rich`（`**粗体**`）。

`plot.items` 里每项 `{"shape": ..., ...}`，坐标都可以是表达式（数据坐标）：

| shape | 字段 |
|---|---|
| `arrow` | `x1` `y1` `x2` `y2`、`tone`、`label`（模板）、`dashed`(bool) |
| `segment` | `x1` `y1` `x2` `y2`、`tone`、`dashed` |
| `point` | `x` `y`、`tone`、`label` |
| `polyline` | `points`（表达式，结果是 `[[x,y],...]`）、`tone`、`dashed` |
| `rect` | `x` `y`（左下角）、`w` `h`、`tone`、`fill`(bool：实心渐变填充，像柱子；同时 `dashed` 时是浅色区域，用来圈出一段范围)、`dashed`、`label`（写在中间） |
| `circle` | `cx` `cy`、`r`（按 x 轴的单位）、`tone`、`fill`、`dashed`、`label` |
| `text` | `x` `y`、`text`（模板）、`anchor`("start"/"middle"/"end")、`size`("sm"/"md")、`tone` |
| `label` | `x` `y`、`text`（模板）、`tone` |

时序图：两条竖的虚线 `segment` 当参与方，`arrow` 当报文，`text` 写编号和名称。
递归树：`circle` 当结点、`segment` 当边、`text` 写每层的代价。

chart 铺满块的宽度（高约 220–260）；plot 保持 360 × `height` 的比例，最宽 720（居中），宽屏上按比例变高。两者的字都不会
被缩小。想让 plot 里的圆是正圆、角度看起来对，让 (xmax − xmin) : (ymax − ymin) 接近 (360 − 44) : (height − 44)。图里的颜色：`accent` 蓝、`warning` 橙、`success` 绿、
`danger` 红（数据配色），`default` / `secondary` 是中性色。

`tone` 只能是 `"default"` `"secondary"` `"accent"` `"success"` `"warning"` `"danger"`（box / grid-item
只能是 "plain" / "surface"）。不能写颜色值，也不能是模板。

## 表达式

- 字面量：数字、`"字符串"`、`true` `false`、数组 `[a, b]`、对象 `{k: v}`（键不加引号）。
- 名字：state 和 derived 的名字。
- 运算：`+ - * / % **`、`== != < <= > >=`、`&& || !`、`条件 ? a : b`、`a.b`、`a[i]`。
- 单参数箭头函数：`x => 表达式`，只能直接写在 `map` / `iterate` 的参数里。
- 函数：`sin cos tan atan2 sqrt abs min max round floor ceil exp log pow`、常量 `PI`；
  `fmt(x, 位数)`（保留小数的字符串）；`range(n)`（0..n-1）；`map(数组, f)`；`sum(数组)`；
  `iterate(n, 初值, f)`（返回 `[初值, f(初值), f(f(初值)), ...]` 共 n 项）；`len(数组)`；`deg2rad(度)`。
- 不能访问任何别的东西（没有 window、document、网络）。
- 读者那边有安全检查，不通过就只显示 fallback（检查结果会说哪里不行）：`iterate` 的表达式里不能有字符串；值里有文字的名字只能写
  `名字.字段`、`名字[0]`，不能传给 `map` / `iterate`，不能用变量下标（只含数字的数组可以，如 `map(range(5), i => dx[i] * dy[i])`）；所以选项值能用数字就用数字。
- 块被拒时可以换一种写法继续计算。换不出来就不要把算出来的结果改成写死的数字，让块看起来还在计算：算不了的部分用文字讲，块也可以只有文字和静态图。

## 上限

一个块最多 400 个节点、嵌套 16 层、32 个 state、48 个 derived；一个表达式或模板最多 500 / 600 字；
plot 最多 60 项；chart 的 annotations 最多 8 个、visible_points 至少 3；quiz 每题 2–8 个选项、6 种情况；flashcards 至少 2 张卡；timeline 至少 2 项；deep-dive 至少 1 个子节点；reveal 1–12 步；timer 倒计时 1–3600 秒；player 的 interval_ms 100–5000；
fill-blank 2–8 个候选；sentence 1–8 个空、最多 12 个词（每个最多 60 字）；route 至少 2 个选项；整个块的 JSON 不超过
40000 字（不算 app 的 `html`）；一个块最多一个 app，它的 `html` 最多 60000 字、`items` 最多 40 项。

数量建议（不是硬性限制，读者要求更多时照他的要求写，只要整个块不超过上面的字数和节点上限）：quiz 一般不超过 15 题；flashcards
一般不超过 40 张；pager 一般不超过 20 页；route 一般不超过 6 个选项；chart 一般不超过 4 个 series；表格一般不超过 6 列 20 行。

## 写好一个交互的规则

1. 常见结构：状态 → 控件 → 图 → 实时读数 → 一句随状态变化的结论（用 `if`、条件表达式或 `badge`）。
2. 一个交互只讲一件事。数字要真实合理。检查结果里有试操作后的屏幕读数，拿它核对块和正文写的结论。
3. 优先照着那一类的模板写（结构照抄，内容、数字和题目全部换成这一页的；模板里的 `cell` 只是占位，换成这一页给出的格子，
   没有格子就不写 `cell`）。
