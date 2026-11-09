# Writing a configurable library component

A component is one generic interaction that can teach many topics; the topic comes from a **config**.
The model that places it only fills the config (JSON) — it never writes HTML. Two files per component,
in this directory:

- `<id>.html` — standalone UTF-8 HTML, inline CSS/JS only.
- `<id>.json` — a `SeedComponent` (see `distribution-line.json`): `component_id`, `name`, `description`,
  `learning_question`, `scenarios`, `params`, `html_path`, `height`, `probes`, `source`, `config_schema`,
  `example_configs`.

Reference implementation: `distribution-line.html` / `.json`. Copy its `/* ---- EvoWiki component base */`
CSS block **unchanged** (tokens, dark scheme, buttons, focus, `.lab` layout, `.read`, `#result`) and its
`readConfig()` / `$` / `fmt` / `esc` / `el` helpers; write only the component part yourself.

## Rules (all checked or reviewed)

1. **Config.** Read it with `readConfig()`: `<script type="application/json" id="evowiki-config">` (injected
   when placed), falling back to a built-in `DEFAULT` equal to example 1. Never `eval`, never build HTML
   from config strings without `esc()` (or use `textContent`). Clamp every number you read (the schema
   has no numeric bounds) and cope with short/long arrays (the validator allows up to 60 items, strings
   up to 600 chars).
2. **Schema** (`config_schema`) — the subset Anthropic structured outputs accept: keys `type`,
   `properties`, `required`, `additionalProperties`, `items`, `enum`, `const`, `description`, `title`,
   `anyOf`, `minItems` (0 or 1 only). No `minimum`/`maximum`/`maxLength`/`pattern`/`maxItems`/`default`.
   Every object: `"additionalProperties": false` and **all** its properties in `required` (optional =
   `"type": ["string","null"]` or an `anyOf` with `{"type":"null"}`). Top level must have string
   `title` and `question` (shown as the component's heading and learning question). Put a short
   `description` on fields a model needs help with (units, how many items, what a field means).
3. **Examples.** `example_configs`: at least 2, **different subjects** (e.g. statistics and biology), each
   a realistic teaching use. Both must pass the check below.
4. **Probes** (`probes`): 1–4 scripted tries that work for **every** config (stable ids like `#next`,
   `#reset`, `#slider-0`, `[data-probe=…]`). Each operates a real control; afterwards the text of
   `expect_change` (normally `#result`) must differ. A probe may reference the config as
   `{{config.items[0].id}}` if needed. `#result` is an `aria-live="polite"` sentence saying what the
   last action changed and why — a teaching sentence, not "updated".
5. **Offline & safe.** No network (no fonts, CDNs, images by URL, fetch). No `localStorage`, no
   `alert`, no timers that run forever when idle (animations stop when done or when paused).
6. **Theme.** Colours only through the base tokens (`--accent`, `--tx`, `--good`/`--warn`/`--bad`, …).
   Must look right in light and dark (the check takes a dark screenshot too).
7. **Not colour alone.** Every state also has a label, shape, line style, position or icon drawn in SVG.
   No emoji or Unicode symbols as icons (✔ ✘ ▶ etc.) — write the word or draw an SVG shape.
8. **Keyboard.** Everything is operable with Tab/Enter/Space/arrows: native `<button>`, `<input
   type=range>`, `<select>`. Anything draggable also has a keyboard or button alternative.
9. **375 px.** No horizontal overflow at 375 px wide (the check measures it). Draw SVG at its real
   pixel width (`viewBox` from the container's `clientWidth`, redraw on `ResizeObserver`) so text stays
   ≥ 12px; never let a fixed-width SVG scale text down. `height` in the JSON = the desktop content height
   (400–1600; mobile may scroll vertically inside the frame).
10. **Craft.** Use a quiet Obsidian-like look: no cards inside cards, no eyebrow labels above
    headings, no coloured `border-left` callouts, no gradient text, numbers in `--mono` with tabular
    numerals, one purposeful transition at most. Chinese UI copy, plain words.

11. **Right and wrong.** A component that grades the reader holds its key in the config: `targets[].id`,
    `items[].id`, `items[].answer` and `items[].cell` (the K cell, `string | null`). It posts each item's first
    placement to the host once, `parent.postMessage({evowikiComponentItem: {item, target}}, "*")`, with
    `target: null` when the answer was shown first. The server grades it against the stored config (`deep.components.graded_config`,
    `wikiapp.block_results`) and records one result per item, ever.

## Check (no database, no LLM)

```
uv run python scripts/check_component.py src/evowiki/deep/components/<id>.json --shots the product specification
```

It validates the schema and the examples, opens component + each example in headless Chromium at 375 and
1280 px (0 console/page errors, 0 network requests, 0 overflow, every probe passes) and writes
`the product specification` and
`ex<n>-1280-dark.png`. Look at the screenshots (Read tool) and fix what looks wrong.
