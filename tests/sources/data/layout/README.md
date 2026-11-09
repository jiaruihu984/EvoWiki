# Layout parser fixtures

Recorded outputs of MinerU and Docling on two public documents, used by
`tests/sources/test_layout_adapt.py`. Only public PDFs: ResNet (arXiv 1512.03385) and a few pages
of the d2l-zh book. Embedded images are scrubbed (`image` / `img_path` / `uri` set to `""`).

| file | source | backend | call |
| --- | --- | --- | --- |
| `mineru_resnet_p1-3.json` | ResNet PDF, pages 1-3 | mineru 4.0.12 | `parse(pdf, tier="standard", page_range="1-3")` |
| `mineru_d2l_math_p1-2.json` | d2l-zh excerpt (`zh_math_nobm.pdf`), pages 1-2 | mineru 4.0.12 | `parse(pdf, tier="standard", page_range="1-2")` |
| `docling_resnet_p1-3.json` | ResNet PDF, pages 1-3 | docling 2.144.0 | `DocumentConverter().convert(pdf, page_range=(1, 3)).document.export_to_dict()` |

MinerU files are `mineru.render.render_content_list(result.middle_json)`; Docling files are the
dict above. Recorded with the bake-off venvs (`mineru-env`, `docling-env`), `HF_HUB_DISABLE_TELEMETRY=1`,
one at a time on the GPU, each script under an `if __name__ == "__main__":` guard (MinerU spawns
worker processes on Windows).

## Findings

- MinerU `page_idx` is **absolute** (0-based in the whole PDF), not relative to the parsed range.
  Checked by parsing ResNet pages 4-5: the items carried `page_idx` 3 and 4. `from_mineru` therefore
  uses `page_idx + 1` as the page; `first_page` only sets where the page list starts. (The ranges
  recorded here start at page 1, where both readings agree.)
- No table lies in the recorded ranges (the first ResNet table is on page 4), so tables are covered
  by a synthetic item in the tests.
- Docling formulas come back with an empty `text` here (formula enrichment is off); the adapter
  keeps `orig` as the block text and leaves `latex` empty.
