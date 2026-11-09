"""The default teaching spec (θ_rep,u v1 for a new user): 22 generic Chinese teaching rules, ids
`d01`…`d22`, origin `default`, RSI's to tune (authority `rsi`; the protected ones excepted) — the per-user, editable layer of the EvoWiki teaching standard
(`teach.standard`, distilled from the user's own wiki contract: familiar way → the problem shows → the
new idea in plain words → a minimal complete example → step-by-step trace → formal terms → transferable
rules; v2 adds language organization and personal examples). 22 leaves the user 8 of the
30 slots. Nothing private or tool-specific."""

from evowiki.teach.contracts import PROTECTED_DEFAULTS, SpecItem, SpecScope, TeachingSpec

_DEFAULTS: tuple[tuple[SpecScope, str], ...] = (
    ("any", "开头先用一两句人话说清最重要的结论：这是什么、解决什么问题。"),
    ("concept", "从学习者已经会的普通做法、日常经验或代码讲起，用一个能观察到的结果（输出、时间、形状、状态或失败）让问题真实出现，再引出新方法。"),
    ("concept", "新方法先用人话说清它怎样改变了过程，再给正式名称。"),
    ("any", "学习者没见过的新词、新符号、新对象第一次出现前先讲清：为什么需要、输入什么、改变什么、产生什么；不用一个陌生术语解释另一个。"),
    ("concept", "一次只引入一个新东西；一行里有几个新概念就先逐个拆开讲，再拼回完整的一行；嵌套调用从里往外读并写出读法。"),
    ("concept", "尽早给一个最小但完整的例子，有具体输入和预期结果；代码要能独立运行并写出预期输出，再给一句人话自查。"),
    ("concept", "用一组对照（普通做法与新做法，或对与错）给出可观察的证据，说明新方法到底改变了什么。"),
    ("concept", "逐步追踪：把输入、发生变化的位置、当前状态和输出连起来；涉及公式时把公式、代码变量、数据形状和图示一一对应。"),
    ("concept", "直觉和例子之后再给正式术语；核心术语讲全：人话解释、正式定义、例子里的对应位置、容易混淆的相近概念、能直接复述的一两句表达。"),
    ("concept", "最后给可迁移的规则：学习者能用自己的话复述核心机制，并预测一个新情况。"),
    ("concept", "保持「如果他当面问你，你会怎么一句一句讲」的顺序，不压成定义列表、箭头流程或术语卡。"),
    ("any", "为好懂做的简化要明说是简化，并在后文补全。"),
    ("any", "已经确定会的内容只用一句提醒带过，不重讲；“可能会”或没有证据的先修要先讲清再用；类比只借用学习者已经会的东西。"),
    ("any", "表格、清单和箭头流程只放在具体例子之后，用来比较或复习，不作为主要讲解。"),
    ("concept", "只在空间、顺序、状态变化或数据流需要“看见变化”才能懂时用图或交互；每个图示回答一个写明的学习问题，表现真实过程，不只靠颜色，不加装饰。"),
    ("concept", "交互只从组件库里选，学习者改一个输入，结果跟着变并说明为什么。"),
    ("concept", "讲常见错误时说明错在哪里、为什么会这样错。"),
    ("exercise", "确认题只考本页讲过的一个知识点，必须有唯一正确答案，错误选项对应真实的误解。"),
    ("quick", "速查页只给一句人话结论和直接结果，不展开整条学习路径。"),
    ("any", "正文不出现系统内部的说法（验收、契约、测试、参数名）。"),
    ("concept", "结论先行，紧跟最容易误解的地方；小标题多用问题句；形式化的话后面跟一句“翻译成人话”；结尾一句话总结。"),
    ("concept", "例子优先用学习者已经会的概念和他学过的页面里的内容，把新东西接到熟悉的东西上。"),
)

DEFAULT_ITEMS: tuple[SpecItem, ...] = tuple(
    SpecItem(item_id=f"d{i:02d}", text=text, scope=scope, origin="default", authority="rsi",
             rev_by="system", protected=f"d{i:02d}" in PROTECTED_DEFAULTS)
    for i, (scope, text) in enumerate(_DEFAULTS, 1))


def default_spec() -> TeachingSpec:
    """The default spec alone, as version 1 (default params, no exemplars)."""
    return TeachingSpec(version=1, items=DEFAULT_ITEMS)
