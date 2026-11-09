import json
import urllib.request
from collections.abc import Callable, Iterable, Mapping
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, Field, model_validator

from evowiki.content.models import LicenseObligations, Passage, Source

DENYLIST_PATTERNS: tuple[str, ...] = ("mml-book", "mathematics for machine learning")

Fetcher = Callable[[str], str]


class IneligibleSourceError(ValueError):
    pass


class SourceDocument(BaseModel):
    locator: str
    title: str
    # Substrings marking paragraphs of third-party quoted text (e.g. a non-derivative paper
    # abstract embedded in an eligible page); matching paragraphs are dropped at ingestion.
    exclude: list[str] = Field(default_factory=list)


class SourceRegistration(BaseModel):
    source: Source
    license_marker: str = ""
    documents: list[SourceDocument] = Field(default_factory=list)
    notes: str = ""

    @model_validator(mode="after")
    def _registration_rules(self) -> "SourceRegistration":
        s = self.source
        haystack = f"{s.title} {s.locator}".lower()
        if s.derivative_allowed and any(p in haystack for p in DENYLIST_PATTERNS):
            raise ValueError(f"source {s.id} matches the denylist and may not enter generation")
        if s.derivative_allowed and not s.license_url:
            raise ValueError(f"source {s.id} needs a license_url to be verified")
        if s.derivative_allowed and not self.license_marker:
            raise ValueError(f"source {s.id} needs a license_marker to be verified")
        return self


def http_fetch(url: str, timeout: float = 30.0) -> str:
    req = urllib.request.Request(
        url, headers={"User-Agent": "evowiki-content-pipeline/0.1 (license verification)"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        charset = resp.headers.get_content_charset() or "utf-8"
        return resp.read().decode(charset, errors="replace")


def verify_license(reg: SourceRegistration, fetch: Fetcher, now: datetime) -> SourceRegistration:
    """Check the license page and record the outcome in `license_check_status` /
    `license_checked_at`. Only success moves `license_verified_at` (history); an unreachable
    page (`OSError`: URLError, HTTPError 404/410, timeouts) or a missing marker keeps it."""
    if not reg.source.derivative_allowed:
        return reg
    assert reg.source.license_url is not None  # guaranteed by the registration validator
    update: dict[str, object] = {"license_checked_at": now}
    try:
        text = fetch(reg.source.license_url)
    except OSError:
        update["license_check_status"] = "unavailable"
    else:
        if reg.license_marker.lower() in text.lower():
            update.update(license_check_status="verified", license_verified_at=now)
        else:
            update["license_check_status"] = "mismatch"
    checked = reg.source.model_copy(update=update)
    return reg.model_copy(update={"source": Source.model_validate(checked.model_dump())})


def is_eligible(source: Source) -> bool:
    return (source.derivative_allowed and source.license_verified_at is not None
            and source.license_check_status in (None, "verified"))


def eligible_sources(regs: Iterable[SourceRegistration]) -> list[Source]:
    return [r.source for r in regs if is_eligible(r.source)]


def require_eligible(source: Source) -> None:
    if not is_eligible(source):
        raise IneligibleSourceError(
            f"source {source.id} is not eligible (derivative_allowed and a currently verified "
            f"license required; last check: {source.license_check_status or 'none'})")


def load_registry(path: Path) -> list[SourceRegistration]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return [SourceRegistration.model_validate(r) for r in data]


def save_registry(path: Path, regs: Iterable[SourceRegistration]) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    doc = [r.model_dump(mode="json") for r in regs]
    Path(path).write_text(json.dumps(doc, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


def license_obligations(
    passage_ids: Iterable[str], passages: Mapping[str, Passage], sources: Mapping[str, Source]
) -> LicenseObligations:
    cited = [sources[sid] for sid in sorted({passages[p].source_id for p in passage_ids})]
    return LicenseObligations(
        attribution=[s.id for s in cited if s.attribution_required],
        share_alike=any(s.share_alike for s in cited),
        non_commercial=any(not s.commercial_allowed for s in cited),
    )
