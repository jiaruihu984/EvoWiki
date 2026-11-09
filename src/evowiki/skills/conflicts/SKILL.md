---
key: conflicts
name: 待确认的记忆
when: 他要处理待确认的记忆（几条可能互相矛盾的规则、档案或定下的决定）
order: 75
tools: [read_memory_conflicts, resolve_memory_conflict, undo]
---
- 一次只讲一组：说清这几条各是什么、哪里矛盾，问他保留哪条、都保留，还是换个说法。处理完这一组再讲下一组。
- 他这一轮说了怎么处理才调 resolve_memory_conflict，quote 照抄他这一轮的原话：保留一条给那条的 id，都保留给 both，他说了一条新的就放进 replace_with（关于他自己的事写上 section）。他说的能对上是哪条（「保留先给例子的那条」）就算说清了，直接调，不再让他回一遍「对」；对不上才问，不替他选。这几条的去留只用 resolve_memory_conflict 处理，不用别的工具改。
- 处理完用一句话说结果；保留一条或换了说法的，说一声可以撤销。
