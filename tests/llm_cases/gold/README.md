# Gold prerequisite sets for goal decomposition (`goals.intake`)

Small fixtures excerpted from two validated prerequisite graphs, used to score how much of the
expert prerequisite structure a decomposition names. Used by
`tests/llm_cases/live/test_live_gold_coverage.py` (paid, skipped unless `EVOWIKI_LIVE=1`) and
tested offline by `tests/llm_cases/test_gold_coverage.py`.

| file | source | license |
|---|---|---|
| `metacademy_ml.json` | [Metacademy](https://github.com/metacademy/metacademy-content) content repository, commit `2654877392cea3857e9fa91ddf896a4a2f5361c8` (`concepts/<tag>/{title,id,dependencies}.txt`, `courses/*/concepts.txt`) | **CC BY-SA 3.0** (`LICENSE.txt`: Creative Commons Attribution-ShareAlike 3.0 Unported). This fixture is a derived excerpt and is shared under the same license. |
| `lecturebank_ml.json` | [LectureBank](https://github.com/Yale-LILY/LectureBank), commit `b237565ded5b112295fd5bc39035f53b0dd4286b` (`LB-Paper/208topics.csv`, `LB-Paper/prerequisite_annotation.csv`) | **No license stated**: the repository has no LICENSE file and its READMEs only ask for a citation. Only about 28 topic names / Wikipedia URLs and their prerequisite pairs are excerpted, for evaluation, with the citation below. Ask the authors before redistributing more. |

Attribution:

- Metacademy: content from metacademy.org (Roger Grosse, Colorado Reed and contributors), an
  expert-curated machine-learning concept graph. The repository is deprecated (the site moved to
  a database) but still holds the graph.
- LectureBank: Irene Li, Alexander R. Fabbri, Robert R. Tung, Dragomir R. Radev. *What Should I
  Learn First: Introducing LectureBank for NLP Education and Prerequisite Chain Learning.* AAAI
  2019, arXiv:1811.12181. 208 NLP/ML topics with manual prerequisite labels for every pair
  (`source, target, 1` = source is a prerequisite of target).

## Goals

Each fixture has three goals: 机器学习 (machine learning), 神经网络 (neural networks), 奇异值分解
(singular value decomposition). A goal is a set of **anchor** concepts in the source:

- 机器学习: the supervised / unsupervised topics of a standard introductory ML course (Ng's
  CS229 / Coursera ML syllabus) that exist in the source. Metacademy (13): linear regression,
  logistic regression, gradient descent, regularization, cross validation, bias-variance
  decomposition, feed-forward neural nets, backpropagation, SVM, k-means, PCA, naive Bayes,
  decision trees. LectureBank (11): the same where a topic exists (no cross validation or naive
  Bayes; PCA is its "dimensionality reduction" topic).
- 神经网络: feed-forward neural nets + backpropagation (LectureBank: Artificial neural network +
  Backpropagation).
- 奇异值分解: singular value decomposition.

## Rule (closure and trimming)

1. **closure** = the anchors plus everything reachable through "is a prerequisite of" edges
   (stored per goal as `edges`: concept → its direct prerequisites inside the closure).
2. **core** = the anchors plus every closure concept that at least
   `support_min = max(1, round(0.25 × number of anchors))` anchors reach. For 机器学习 that is
   "needed by at least 3 of the 13 (11) ML topics"; for a one- or two-anchor goal it is the whole
   closure. Only core concepts are scored.

`coverage.core_of` implements the rule; the offline test recomputes every fixture's `core` from
its stored `edges`, so the fixture cannot drift from the rule.

| goal | Metacademy closure → core | LectureBank closure → core |
|---|---|---|
| 机器学习 | 70 → 42 (13 targets + 29 prerequisites) | 28 → 17 (11 + 6) |
| 神经网络 | 15 → 15 | 9 → 9 |
| 奇异值分解 | 24 → 24 | 4 → 4 |

Source cleaning (all recorded in the fixtures):

- Metacademy `tag_fixes`: three dependency tags name a renamed concept
  (`feed_forward_neural_networks` → `feed_forward_neural_nets`, `random variables` →
  `random_variables`, `variance` → `expectation_and_variance`); `-` in tags is read as `_`; tags
  with no concept folder are dropped (none reached these closures, `unresolved: []`). Metacademy's
  foundation courses (`linear_algebra`, `multivariable_calculus`, `probability_theory`) give each
  concept its `field`.
- LectureBank `excluded` (removed from the graph before the closure, reason stored):
  208 *relational databases* (labelled a prerequisite of regression / SVM / k-means / decision
  trees: annotation noise) and 130 *backpropagation through time* (labelled a prerequisite of
  *Artificial neural network*: direction reversed). `unnamed_ids` 210 and 211 appear in the pairs
  but not in `208topics.csv`; their pairs are dropped.

## Aliases (the curated part)

Each core concept carries `aliases`: the Chinese terms a Chinese decomposition would use for it,
then English ones (an intake output may keep English such as SVM / PCA). They are written by hand
in `build_gold.py` and kept literal: the usual textbook names (概率 / 概率论, 矩阵乘法, 梯度下降…),
plus terms whose meaning *is* the concept (过拟合 / 欠拟合 for generalization, 贝叶斯定理 for
conditional probability, 秩 for column space). Broad concepts get short aliases so a narrower
named point counts for them (「条件概率」 shows probability is covered). `exclude` keeps names
that only share a word out (「梯度下降」 is not the concept *gradient*; 「支持向量机」, 「特征向量」,
「词向量」 are not *vectors*; 「偏差与方差」 is not *expectation and variance*).

## Scoring (`coverage.py`)

`coverage(names, load_goal(fixture, goal))`: `names` are the decomposition's concept and region
names. Names and aliases are normalized (NFKC, casefold, no whitespace or punctuation). A gold
concept is matched when one of its aliases is a substring of a name and the name contains none of
its excludes. `score` = matched / core size; `core_score` the same over the anchors (the goal's
own core topics, role "target"); `prereq_score` the same over the non-anchor part (what must be
known before the goal);
`matched` (gold id → the names), `missing` / `missing_names`; `fields` (Metacademy only): a
foundation field counts when a name contains its alias (线性代数, 微积分, 概率论) or one of its core
concepts is matched. Field names alone do not raise `score`.

## Threshold

Three floors, 机器学习 only; the live case must clear all of them. A decomposition is judged in two
parts, separately (user ruling 2026-10-11): the goal's own core topics (for ML: training with a
loss, regularization, generalization / bias-variance, evaluation, the main model families incl.
neural networks) and its prerequisites (the math before them). Listing the math perfectly but
missing the core algorithms fails, and so does the reverse.

| floor | Metacademy | LectureBank |
|---|---|---|
| overall `MIN_ML_COVERAGE` (`score`) | **0.35** (15 / 42) | **0.5** (9 / 17) |
| core `MIN_ML_CORE` (`core_score`, anchors) | **0.5** (7 / 13) | **0.5** (6 / 11) |
| prerequisites `MIN_ML_PREREQ` (`prereq_score`) | **0.25** (8 / 29) | **0.5** (3 / 6) |

Grounding for the part floors, from the offline test (`REFERENCE_ML` and its two halves:
`MATH_ONLY_ML` = the three math regions with their points, `CORE_ONLY_ML` = the rest):

| decomposition | Metacademy core / prereq | LectureBank core / prereq |
|---|---|---|
| reference (34 names) | 0.692 (9 / 13) / 0.379 (11 / 29) | 0.818 (9 / 11) / 0.833 (5 / 6) |
| math only (18 names) | **0.0** / 0.345 | **0.0** / 0.667 |
| ML core only (16 names) | 0.692 / **0.034** | 0.818 / **0.167** |
| regions only | 0.0 / 0.034 | 0.0 / 0.5 |
| live run 2 (intake@v7, 15 names) | **0.231** / 0.276 | **0.273** / 0.667 |

- the reference clears each part floor by ≥ 0.1 (margins: core 0.192 / 0.318, prereq 0.129 /
  0.333);
- the math-only half fails the core floor in both sources (0.0), the core-only half fails the
  prereq floor in both (0.034 / 0.167). On LectureBank the core-only half even clears the overall
  floor (0.588 ≥ 0.5): the overall score alone would let it pass;
- live run 2 (`the product specification`: math regions,
  supervised learning with linear / logistic regression only) fails the core floor in both
  sources; it also names no calculus, which the live case checks as a field;
- core is "half the goal's own syllabus topics": 7 / 13 Metacademy anchors, 6 / 11 LectureBank.
  The Metacademy prereq floor is lower for the same reason as its overall floor (below: 12 of its
  29 core prerequisites are course internals).

Grounding for the overall floor, from the offline test:

- a hand-written goal-level reference decomposition (34 names: the three math regions with their
  headline points, the ML methods, loss / overfitting) scores 0.476 (20 / 42) on Metacademy and
  0.824 (14 / 17) on LectureBank; it must clear each floor by ≥ 0.1;
- a regions-only outline (线性代数, 微积分, 概率论, 机器学习) scores 0.024 / 0.176 and fails both.

Metacademy's floor is lower because its core follows a university course's granularity: 12 of the
29 core prerequisites are linear-algebra / calculus course internals (subspaces, bases, column
space and nullspace, the four fundamental subspaces, orthogonal subspaces, projection, Gaussian
elimination, linear systems, limits and continuity, multiple integrals, linear approximation,
functions of several variables) that a goal-level decomposition is not expected to list.
Independently of the score, the live case requires probability and linear algebra in both sources
and calculus (the field) in Metacademy.

## Rebuilding

Download the two sources into new, empty directories (Metacademy: the repository tarball at the
commit above; LectureBank: the two `LB-Paper` CSVs), then from the repository root:

```
python -I tests/llm_cases/gold/build_gold.py <metacademy-content dir> <LB-Paper dir>
```

The builder only parses the files (nothing from them is executed) and fails when a core concept
has no alias.

## LectureBank is local-only

LectureBank has no license file (its README only asks for a citation), so `lecturebank_ml.json` is
never committed (user ruling 2026-10-11; it is in `.gitignore`). Build it from a local clone with
`python -I tests/llm_cases/gold/build_gold.py <metacademy dir> <LB-Paper dir>`; without it the
LectureBank checks skip and the live case reports it as absent.
