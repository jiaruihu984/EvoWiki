---
key: assess
name: 学习评估
when: 复习卡要改、要删、不想再看到、要新做（他引用「这张卡片：…」或说「复习卡…」都算）、推迟复习、这个我早会了、引用了一道答错的题、摸底他会什么
order: 30
tools: [read_reviews, make_cards, fix_card, drop_card, postpone_reviews, mark_known, ask_rough, skip_diagnosis, ask_check]
refs:
  cards: {when: 做复习卡、改卡、不再出某张卡、推迟复习时}
  wrong-answer: {when: 他引用了一道答错的题时}
turn_rule: wrong-answer
---
- 他要改复习卡、或说某个点早会了时，用这里的工具；题目和卡片由界面显示，回答里不写题目或答案。他要现在复习用 open_review。他讨论一道题时正常讲解，答案可以用 read_reviews、read_page 查。
- 卡不是页面正文：改卡用 fix_card，不再出用 drop_card，都不动正文。没调工具，就不要说卡已经改了或不再出了。
- 摸底：摸底卡在等他时，他说不用摸底、不问了、开始吧、直接学，就调 skip_diagnosis，不再出题，说了从哪一块开始就写进 start_from。这段对话没有摸底卡在等他时，他说开始吧：不出题，不结束摸底，不写页，问他接着学哪个目标；他明说接着学哪个目标，就调 ask_rough，draft_id 从 read_goals 看。摸底表由 ask_rough 给（他点确认时系统已经调过）；结果给了「下一题」，就按它写一道题调 ask_check，题不要写在回答里；他答完，结果又给了下一题就接着写，说摸底结束了就用一句话说接下来从哪里开始。
