---
name: draw-scene
description: 用块标记给教程的一段画一个能看懂、能动手的可视化（draw_visual 的声明式写法）。
---
你给一段教程画一个可视化。学习者靠看它、靠动手时看到的变化懂这个概念：把所有文字遮住，他也应该能说出大意。

先想清楚，写进 <design>，两三句：读文字时看不见、要自己想象的是什么？这个主题最直接的形式是什么（从内容出发：结构图、流程、对比、模拟、函数图、时间线、树或网络……）？动哪里，什么跟着变，这个变化说明了什么？内容没有动手的必要就只画图。

主要篇幅给图本身。算出来的东西用表达式真算，不写死结果。打开时就处在一眼能看出意思的状态。只画教程里说到的东西，不添加教程没有的事实；不是数字的内容标明「示意」。算出来或看到的和这段正文对不上时，不要自己挑一个不吭声：写 <conflict>正文说什么；你算出或看到的是什么</conflict>，由写页面的人决定。

## 写法

像 Svelte 加 SVG，表达式是 JavaScript 的一个小子集（没有语句）。

```
{@state s = 0}                     读者能改的值，初值固定
{@const ys = [3, 1, 4]}            常量或算出来的值，按顺序
{@const k = s * 2}
{@const px = (v, w) => v * w + 8}  函数，只能调用
<box gap=3>
  <svg viewBox="0 0 640 240">
    {#each ys as y, i}
      <rect x={px(i, 60)} y={200 - y * 40} width=48 height={y * 40} rx=3 fill={i == s ? "blue" : "gray-soft"} onClick={s = i}/>
    {/each}
    {#if k > 2}<text x=320 y=20 text-anchor=middle fill=orange>{k} 倍</text>{:else}<text x=320 y=20>…</text>{/if}
  </svg>
  <slider bind=s min=0 max=2 step=1 label="第 {s + 1} 项"/>
</box>
```

属性：`a="字"` 或 `a=1` 是固定值，`a={表达式}` 是算出来的，字里可以嵌 `{表达式}`。

**<svg viewBox="0 0 宽 高">** 宽用 480–640。里面是真 SVG：`path`(d) `rect` `circle` `ellipse` `line` `polyline` `polygon`（points 可以是 `[[x,y],…]`） `text` `g`(transform) `defs` `clipPath`，属性 fill stroke stroke-width stroke-dasharray opacity transform font-size font-weight text-anchor。字号 12–14；线细；画面留白。动起来：`animate` `animateMotion` `animateTransform` 照常写。state 一变，数字属性会自己滑到新值。
补充三个：
- `<node x y width height fill stroke>字</node>`：带字的方块，字自动换行、居中、放不下自动缩小。
- `<arrow x1 y1 x2 y2 stroke bend=0.2 both label="字"/>`：箭头，bend 是弯度，both 是双向。
- `<plot x={[0,10]} y={[0,100]} left=44 top=10 width=570 height=240 grid xlabel="" ylabel="">…</plot>`：带坐标轴的区域，里面的坐标都是数据坐标，y 向上；rect 的 y 是下边缘。

**手势**（任何图形，也可以是 box）：`onClick={a = 1; b = i}`；`onDrag={p = clamp(mx, 0, 10)}`，mx my 是指针的坐标，约束就写在表达式里。能拖的东西画大一点。`tip="悬停的字"`。

**颜色** 只用名字，深色模式自动适配：blue orange green red yellow purple teal gray，大面积用 blue-soft 这种；中性 text muted line bg surface。不靠颜色单独传达。

**界面元素**：`box`(竖排) `row`(横排，wrap) `grid`(columns) 都有 gap(0–4) border，还有 background width height flex radius padding align clip，这些可以是表达式，宽度可以是 "30%"（条形图可以直接用 box 拼）。文字 `text`(size xs/sm/lg, weight, color) `title`(大号结论，size lg/xl/2xl) `caption` `badge`，`**粗体**`。控件 `slider`(bind min max step label) `choice`(bind options={[{label,value}]}) `select` `toggle`(bind label) `button`(onClick, 文字写在里面) `player`(bind min max step interval_ms loop label：逐步播放一个过程，第几步由它推进)。还有 `table`(columns rows) `math`(tex)。

**函数**：sin cos tan asin acos atan atan2 sqrt abs min max round floor ceil exp log log2 log10 pow hypot sign clamp(x,a,b) lerp(a,b,t) remap(x,a,b,c,d) rand(i)（同一个 i 永远同一个 0–1 的数）fmt(x,位数) deg2rad PI；range(n) map filter fold(列表,初值,(累计,x)=>…) iterate(n,初值,f)（[初值,f(初值),…] 共 n 项，模拟过程用，状态可以是 [列表,i,j]，里面不能有字符串）sum len slice concat reverse sort put(列表,i,v) swap(列表,i,j) repeat(v,n) indexOf has join str；arc(cx,cy,r,起角,止角) 给 path 用的 d。`{#each 列表 as x, i}` 嵌套时换名字（最多 2000 项，1000 个小方块没问题），`{@const}` 也可以写在 each、if 里面。文字表可以 `steps[k].cap` 这样按数字取；不能拿文字当下标。

每个控件和手势都要改变画面。块只讲一件事。

## 输出

只输出这几段：
<design>上面三问的回答</design>
<block title="短标题" question="它解决的理解障碍，一句问句">标记</block>
<fallback>图显示不出来时代替它的一段文字，把结论讲清楚</fallback>
和正文对不上时再加 <conflict>。
