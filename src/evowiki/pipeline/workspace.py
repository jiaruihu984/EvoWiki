import json
from collections.abc import Iterable
from pathlib import Path

from pydantic import BaseModel

from evowiki.content.models import Passage
from evowiki.llm.log import AttemptLog, CallLog
from evowiki.pipeline.review import ReviewQueue
from evowiki.pipeline.sources import SourceRegistration, load_registry, save_registry


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


class Workspace:
    """On-disk pipeline state under one root directory (default `content/`)."""

    def __init__(self, root: Path):
        self.root = Path(root)

    def load[T: BaseModel](self, name: str, model: type[T]) -> list[T]:
        path = self.root / f"{name}.json"
        if not path.exists():
            return []
        return [model.model_validate(x) for x in json.loads(path.read_text(encoding="utf-8"))]

    def save(self, name: str, items: Iterable[BaseModel]) -> None:
        doc = [i.model_dump(mode="json") for i in items]
        _atomic_write(self.root / f"{name}.json",
                      json.dumps(doc, ensure_ascii=False, indent=1) + "\n")

    def load_passages(self) -> list[Passage]:
        path = self.root / "passages.jsonl"
        if not path.exists():
            return []
        return [Passage.model_validate_json(line)
                for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]

    def save_passages(self, passages: Iterable[Passage]) -> None:
        lines = [p.model_dump_json() + "\n" for p in sorted(passages, key=lambda p: p.id)]
        _atomic_write(self.root / "passages.jsonl", "".join(lines))

    def registry(self) -> list[SourceRegistration]:
        path = self.root / "sources.json"
        return load_registry(path) if path.exists() else []

    def save_registry(self, regs: Iterable[SourceRegistration]) -> None:
        save_registry(self.root / "sources.json", regs)

    def queue(self) -> ReviewQueue:
        return ReviewQueue(self.root / "review_queue.jsonl")

    def call_log(self) -> CallLog:
        return CallLog(self.root / "llm_calls.jsonl")

    def connection_request_paths(self) -> list[list[str]]:
        """Connection paths requested by the personal view for new hubs (spec §6.2.4), in file
        order without duplicates. Written by evowiki.view.grounding.append_connection_requests.
        A to-do list only: generated connections still go through review before they publish."""
        path = self.root / "connection_requests.jsonl"
        if not path.exists():
            return []
        out: list[list[str]] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                p = list(json.loads(line)["path"])
                if p not in out:
                    out.append(p)
        return out

    def attempt_log(self) -> AttemptLog:
        return AttemptLog(self.root / "llm_attempts.jsonl")

    @property
    def versions_dir(self) -> Path:
        return self.root / "versions"
