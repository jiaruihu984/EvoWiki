"""Deterministic scripted provider for the offline spine fixture. Not model output."""
import json
import re
from itertools import pairwise
from typing import Any

from evowiki.llm.models import LLMRequest
from evowiki.pipeline.linking import mentions
from evowiki.pipeline.support import zh_id
from evowiki.pipeline.translate import Glossary
from tests.pipeline.helpers import MINI_CONCEPTS, MINI_PASSAGES

CONCEPTS = {c.id: c for c in MINI_CONCEPTS}
PASSAGES = sorted(MINI_PASSAGES, key=lambda p: p.id)
P = {p.id: p for p in PASSAGES}
# (src, dst, relation, kind, citation)
SPINE_EDGES = [
    ("vector", "matrix", "prerequisite_of", "pedagogical", "p_mat"),
    ("matrix", "eigenvalue", "prerequisite_of", "formal", "p_eig"),
    ("variance", "covariance", "prerequisite_of", "formal", "p_cov"),
    ("eigenvalue", "pca", "prerequisite_of", "formal", "p_pca1"),
    ("covariance", "pca", "prerequisite_of", "formal", "p_pca1"),
    ("matrix", "neural_network", "prerequisite_of", "pedagogical", "p_nn"),
    ("neural_network", "autoencoder", "prerequisite_of", "formal", "p_ae"),
    ("svd", "lora", "prerequisite_of", "pedagogical", "p_lora"),
    ("neural_network", "lora", "prerequisite_of", "pedagogical", "p_lora"),
    ("pca", "svd", "related_to", None, "p_svd"),
    ("pca", "autoencoder", "related_to", None, "p_ae"),
]


def passages_for(*cids: str) -> list:
    return [p for p in PASSAGES if all(mentions(p.text, CONCEPTS[c]) for c in cids)]


class SpineScript:
    def __init__(self) -> None:
        self._glossary = Glossary.from_concepts(MINI_CONCEPTS)
        self._en_by_zh_subject: dict[str, list[str]] = {}

    def responder(self, model: str, req: LLMRequest) -> str:
        handler = getattr(self, "_" + req.purpose)
        return json.dumps(handler(req), ensure_ascii=False)

    def _edges_propose(self, req: LLMRequest) -> dict[str, Any]:
        return {"edges": [
            {"src": s, "dst": d, "relation": rel, "kind": kind, "strength": 0.8,
             "citations": [cite], "rationale": "scripted fixture"}
            for s, d, rel, kind, cite in SPINE_EDGES if req.subject in (s, d)]}

    def _segments_generate(self, req: LLMRequest) -> dict[str, Any]:
        ps = passages_for(req.subject)
        if not ps:
            return {"segments": []}
        return {"segments": [
            {"kind": "one_line", "sentences": [{"text": ps[0].text, "citations": [ps[0].id]}]},
            {"kind": "definition", "sentences": [{"text": p.text, "citations": [p.id]}
                                                 for p in ps[:2]]}]}

    def _segments_repair(self, req: LLMRequest) -> dict[str, Any]:
        wanted = set(re.findall(r"one_line|definition",
                                req.prompt.split("Write only these segments:", 1)[1]
                                .split(".", 1)[0]))
        out = self._segments_generate(req)
        return {"segments": [s for s in out["segments"] if s["kind"] in wanted]}

    def _support_verify(self, req: LLMRequest) -> dict[str, Any]:
        n = len(re.findall(r"^Sentence \d+:", req.prompt, re.MULTILINE))
        return {"verdicts": [{"index": i, "supported": True} for i in range(n)]}

    def _perspectives_generate(self, req: LLMRequest) -> dict[str, Any]:
        if req.subject != "pca":
            return {"perspectives": []}
        return {"perspectives": [
            {"framing_label": "Variance maximization", "framing_phase": "ML", "sentences": [
                {"text": P["p_pca2"].text, "citations": ["p_pca2"]}]},
            {"framing_label": "Linear autoencoder", "framing_phase": "DL", "sentences": [
                {"text": P["p_ae"].text, "citations": ["p_ae"]},
                {"text": P["p_pca1"].text, "citations": ["p_pca1"]}]}]}

    def _perspective_phase_verify(self, req: LLMRequest) -> dict[str, Any]:
        [phase] = re.findall(r"proposed framing_phase (LA|ML|DL)", req.prompt)
        return {"phase": phase, "agrees": True, "reason": "scripted"}

    def _perspective_covers_propose(self, req: LLMRequest) -> dict[str, Any]:
        return {"relation_ids": re.findall(r"^- (rel:[^:\s]+):", req.prompt, re.MULTILINE)}

    def _perspective_covers_verify(self, req: LLMRequest) -> dict[str, Any]:
        rids = re.findall(r"^- (rel:[^:\s]+):", req.prompt, re.MULTILINE)
        return {"verdicts": [{"relation_id": r, "covers": True, "reason": "scripted"}
                             for r in rids]}

    def _presumes_verify(self, req: LLMRequest) -> dict[str, Any]:
        cids = re.findall(r"^- (\w+):", req.prompt, re.MULTILINE)
        return {"verdicts": [{"concept_id": c, "level": "helpful"} for c in cids]}

    def _connections_generate(self, req: LLMRequest) -> dict[str, Any]:
        path = req.subject.split("->")
        sentences, hops = [], []
        for a, b in pairwise(path):
            p = passages_for(a, b)[0]
            sentences.append({"text": p.text, "citations": [p.id]})
            hops.append([p.id])
        return {"sentences": sentences, "hop_citations": hops}

    def _quiz_generate(self, req: LLMRequest) -> dict[str, Any]:
        subject = req.subject
        cid = subject[len("conn."):].split(".")[0] if subject.startswith("conn.") else subject
        p = passages_for(cid)[0]

        def item(stem: str, **extra: Any) -> dict[str, Any]:
            return {"stem": stem, "answer_key": p.text, "kind": "recall", "difficulty": 2,
                    "kc_tags": [f"{cid}/basics"], "misconception_tags": [],
                    "citations": [p.id], **extra}

        name = CONCEPTS[cid].name_en
        if subject.startswith("conn."):
            return {"items": [
                item(f"Explain {name} [{subject}].", kind="transfer", transfer_kind="seen"),
                item(f"Apply {name} to a related case [{subject}].", kind="transfer",
                     transfer_kind="near")]}
        return {"items": [item(f"Explain {name} [{subject}].")]}

    def _quiz_answer_verify(self, req: LLMRequest) -> dict[str, Any]:
        return {"answer": "scripted", "key_is_correct": True}

    def _translate_zh(self, req: LLMRequest) -> dict[str, Any]:
        lines = req.prompt.split("Sentences:\n", 1)[1].splitlines()
        en_texts, out = [], []
        for line in lines:
            idx, _, text = line.partition(": ")
            terms = self._glossary.terms_in(text)
            zh = "".join(f"{e.term_zh}（{e.term_en}）" for e in terms) or "说明"
            out.append({"index": int(idx), "text": zh + "。"})
            en_texts.append(text)
        self._en_by_zh_subject[zh_id(req.subject)] = en_texts
        return {"sentences": out}

    def _back_translate_en(self, req: LLMRequest) -> dict[str, Any]:
        texts = self._en_by_zh_subject[req.subject]
        return {"sentences": [{"index": i, "text": t} for i, t in enumerate(texts)]}
