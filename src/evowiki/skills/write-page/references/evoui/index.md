# EvoUI 索引

看懂（把看不见的画出来）用 draw_visual，它写的是块里的 app 节点。练熟和辨析用下面的：
- EvoUI 块用节点拼：闪卡、测验、填空、拼句、翻页、计时、播放器、选择路线这些学习节点，或者状态 → 控件 → chart 或 plot → 实时读数。

EvoUI 块是声明式 JSON，渲染器只认识列出的节点。写块之前用 read_ref 读 evoui/core（通用写法，读一次），再读你要用的那一类（每类带它的模板）：
- evoui/chart：图表和坐标平面；参数实验室、多参数 + 大号结果、计算器、情景切换 + 时序图、面积图 + 悬停读数
- evoui/quiz：选择题、分页诊断测验
- evoui/flashcard：闪卡，翻完接测验
- evoui/timeline：时间线、阶段链、「深入一点」
- evoui/reveal：揭开、动效、计时；限时练习
- evoui/player：播放器；步骤动画、播放参数扫描
- evoui/practice：填空、拼句、翻页练习、选择学习方式
- evoui/app：小程序（app）的写法
- evoui/progress：进度和作答结果
