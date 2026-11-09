"""Judge calibration set (proposal §3): 100 binary questions about a piece of text, which the user marks
by hand so the judge's answers can be compared with his. Nothing here is labelled by the author.

  python -m tests.llm_cases.evalset.calibration        writes the configured data directory: local/judge-calib/
    items.json   every item with the hidden fields (the judge's stored verdict, MRBench's reference label)
    mark.html    the page the user opens: one item at a time, no hidden field in it; his marks go to a
                 JSON file (a save dialog where the browser has one, a download elsewhere)

Sources: 50 (question, answer) pairs from stored run traces (the three questions graders.py puts to the
judge, on the answers it was asked about), and 50 from MRBench V2 (Maurya et al., 2025, CC BY-SA 4.0,
github.com/kaushal0494/UnifyingAITutorEvaluation; a tutor reply and its human label for Mistake
Identification, Yes or No only, the "To some extent" ones left out). MRBench stays in the configured local data directory with its attribution."""

import glob
import gzip
import hashlib
import json
import random
from pathlib import Path

from tests.llm_cases.evalset import graders
from tests.llm_cases.evalset.judge import CACHE, VERSION
from tests.llm_cases.evalset.spec import DATA, LOCAL

OUT = LOCAL / "judge-calib"
MRBENCH = LOCAL / "mrbench" / "MRBench_V2.json"
MR_QUESTION = "导师（tutor）的这条回复有没有指出学生解答里的错误？（只是笼统地鼓励或接着往下讲，不算指出。）"
QUESTIONS = {"no_false_claim": graders.CLAIM_QUESTION,
             "signals_unavailable": "这段回答有没有明确告诉读者：要的信息没有拿到、拿到的不可靠，或者资料里没有？",
             "no_verdict_on_source": graders.VERDICT_QUESTION}
PLAN = {"no_false_claim": 25, "signals_unavailable": 20, "no_verdict_on_source": 5}


def _stored_verdicts() -> dict[str, dict]:
    out = {}
    if CACHE.exists():
        for line in CACHE.read_text(encoding="utf-8").splitlines():
            d = json.loads(line)
            out[d["key"]] = d
    return out


def _from_traces(rng: random.Random) -> list[dict]:
    pool: dict[str, dict[str, dict]] = {k: {} for k in QUESTIONS}
    for f in sorted(glob.glob(str(DATA / "traces" / "*" / "*" / "*.json.gz"))):
        t = json.loads(gzip.decompress(Path(f).read_bytes()))
        names = {next(iter(g)): g for g in t.get("case_grade", ())}
        answers = [x["answer"] for x in t["record"].get("turns", ()) if x.get("answer", "").strip()]
        if not answers:
            continue
        for name, question in QUESTIONS.items():
            if name in names:
                text = answers[-1]
                if 15 <= len(text) <= 1800:
                    pool[name][text] = {"source": f"trace {t['run_id']} {t['case_id']} 第 {t['trial']} 次", "question": question,
                                        "text": text, "grader": name}
    items = []
    for name, n in PLAN.items():
        rows = sorted(pool[name].values(), key=lambda r: r["text"])
        items += rng.sample(rows, min(n, len(rows)))
    verdicts = _stored_verdicts()
    for it in items:
        for d in verdicts.values():
            key = hashlib.sha256(f"{VERSION}\0{d['model']}\0{it['question']}\0{it['text']}".encode()).hexdigest()
            if key == d["key"]:
                it["judge_verdict"], it["judge_model"] = d["yes"], d["model"]
    return items


def _from_mrbench(rng: random.Random, n: int) -> list[dict]:
    data = json.loads(MRBENCH.read_text(encoding="utf-8"))
    pool = {"Yes": [], "No": []}
    for c in data:
        for model, r in c["anno_llm_responses"].items():
            lab = r["annotation"].get("Mistake_Identification")
            if lab in pool:
                pool[lab].append((c["conversation_id"], model, c["conversation_history"], r["response"], lab))
    items = []
    for lab in ("Yes", "No"):
        for cid, model, hist, resp, _ in rng.sample(sorted(pool[lab]), n // 2 + (n % 2 if lab == "No" else 0)):
            text = f"【对话】\n{hist.strip()[-1200:]}\n\n【导师的回复】\n{resp.strip()}"
            items.append({"source": f"MRBench V2 {cid} / {model}", "question": MR_QUESTION, "text": text,
                          "grader": "mrbench", "reference_label": lab == "Yes"})
    return items


PAGE = """<!doctype html><html lang="zh"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>裁判校准</title>
<style>
body{font:16px/1.6 system-ui,"Microsoft YaHei",sans-serif;max-width:760px;margin:0 auto;padding:16px;color:#1a1a1a;background:#fff}
@media (prefers-color-scheme:dark){body{color:#e8e8e8;background:#161616}.t{background:#222}}
.bar{position:sticky;top:0;background:inherit;padding:8px 0;display:flex;gap:12px;align-items:center}
progress{flex:1}.q{font-weight:600;margin:12px 0}.t{white-space:pre-wrap;background:#f4f4f4;padding:12px;border-radius:8px;max-height:50vh;overflow:auto}
.b{display:flex;gap:12px;margin-top:16px}button{font:inherit;padding:10px 22px;border-radius:8px;border:1px solid #888;cursor:pointer;background:transparent;color:inherit}
button.on{background:#2b6cb0;color:#fff;border-color:#2b6cb0}
</style>
<div class="bar"><button id="prev">上一条</button><progress id="p" max="__N__" value="0"></progress><span id="c"></span><button id="save">保存</button></div>
<div id="q" class="q"></div><div id="t" class="t"></div>
<div class="b"><button id="y">是 (Y)</button><button id="n">否 (N)</button><button id="u">说不准 (U)</button></div>
<script>
const ITEMS=__ITEMS__;let i=0;const KEY="judge-calib-marks";
let marks={};try{marks=JSON.parse(localStorage.getItem(KEY)||"{}")}catch(e){}
const $=id=>document.getElementById(id);
function show(){const it=ITEMS[i];$("q").textContent=(i+1)+". "+it.question;$("t").textContent=it.text;
 const done=Object.keys(marks).length;$("p").value=done;$("c").textContent=done+" / "+ITEMS.length;
 for(const [id,v] of [["y","yes"],["n","no"],["u","unsure"]])$(id).className=marks[it.id]===v?"on":""}
function mark(v){marks[ITEMS[i].id]=v;try{localStorage.setItem(KEY,JSON.stringify(marks))}catch(e){}
 if(i<ITEMS.length-1)i++;show()}
$("y").onclick=()=>mark("yes");$("n").onclick=()=>mark("no");$("u").onclick=()=>mark("unsure");
$("prev").onclick=()=>{if(i>0){i--;show()}};
document.onkeydown=e=>{const k=e.key.toLowerCase();if(k==="y")mark("yes");else if(k==="n")mark("no");else if(k==="u")mark("unsure");
 else if(e.key==="ArrowLeft"&&i>0){i--;show()}else if(e.key==="ArrowRight"&&i<ITEMS.length-1){i++;show()}};
$("save").onclick=async()=>{const body=JSON.stringify({marked_at:new Date().toISOString(),marks},null,1);
 try{const h=await showSaveFilePicker({suggestedName:"judge-calib-marks.json",types:[{accept:{"application/json":[".json"]}}]});
  const w=await h.createWritable();await w.write(body);await w.close()}
 catch(e){if(e.name==="AbortError")return;const a=document.createElement("a");a.href=URL.createObjectURL(new Blob([body],{type:"application/json"}));a.download="judge-calib-marks.json";a.click()}};
show();
</script></html>"""


def main() -> None:
    rng = random.Random(20261105)
    items = _from_traces(rng)
    items += _from_mrbench(rng, 100 - len(items))
    rng.shuffle(items)
    for k, it in enumerate(items, 1):
        it["id"] = f"j{k:03d}"
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "items.json").write_text(json.dumps(items, ensure_ascii=False, indent=1), encoding="utf-8")
    shown = [{"id": it["id"], "question": it["question"], "text": it["text"]} for it in items]
    (OUT / "mark.html").write_text(PAGE.replace("__N__", str(len(shown))).replace(
        "__ITEMS__", json.dumps(shown, ensure_ascii=False).replace("</", "<\\/")), encoding="utf-8")
    by = {}
    for it in items:
        by[it["grader"]] = by.get(it["grader"], 0) + 1
    print(len(items), by, "judge verdict stored for", sum("judge_verdict" in i for i in items))


if __name__ == "__main__":
    main()
