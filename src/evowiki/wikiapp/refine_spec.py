"""Where deep refine reads the user's teaching spec and exemplar pages (plan rev 3.3 §10.2 `read_spec`).

`WikiSpecSource` reads R1's per-user spec: the effective spec (`wikiapp.teachspec.effective_spec`, the
virtual v1 when nothing is stored) or an old version from the event log (`wikiapp.teach.read_version`,
what RSI-B's gate compares). `refine_input` then keeps only the items in force for the page
(`teachspec.applicable`) — those are the rules the reviewer must judge and the ids the SectionLog records.

Exemplars come from `spec.exemplars`. A spec without any (R1's default v1 has none) falls back to the
bundled `default_exemplar.md`: a short original page that demonstrates the teaching method
(familiar way → problem made visible → plain-words idea → minimal example → step trace → formal terms →
transferable rule) on a tiny topic. It replaces a verbatim copy of the user's own exemplar, which carried
vault-only syntax (front matter, wiki links, embedded-html with a vault path); the method itself now lives
in `teach.standard`, and the compare experiment showed that calibration against a concrete page still
lifts the writing, so one is always supplied."""

from functools import cache
from pathlib import Path
from typing import Protocol

from evowiki.teach.contracts import Exemplar, TeachingSpec
from evowiki.wikiapp.repo import Repo, Snapshot

EXEMPLAR_PATH = Path(__file__).with_name("default_exemplar.md")


class SpecSource(Protocol):
    def spec(self, snap: Snapshot, version: int | None = None) -> TeachingSpec:
        """The user's teaching spec; `version` None = the current one. LookupError: unknown version."""
        ...


class OnlySpec:
    """A `SpecSource` that serves one spec, whatever version is asked for (a dry run under a given
    spec: RSI-B's Gate rewriter, harness replays)."""

    def __init__(self, spec: TeachingSpec):
        self._spec = spec

    def spec(self, snap: Snapshot, version: int | None = None) -> TeachingSpec:
        return self._spec


@cache
def default_exemplar() -> Exemplar:
    return Exemplar(exemplar_id="ex_default_method", kind="wiki_page",
                    ref="bundled:teaching-method", title="讲法样板：从熟悉的做法一步步走到新办法",
                    text=EXEMPLAR_PATH.read_text(encoding="utf-8"))


def with_exemplar(spec: TeachingSpec) -> TeachingSpec:
    """`spec`, with the bundled exemplar when it has none of its own."""
    return spec if spec.exemplars else spec.model_copy(update={"exemplars": (default_exemplar(),)})


class WikiSpecSource:
    def __init__(self, repo: Repo):
        self._repo = repo

    def spec(self, snap: Snapshot, version: int | None = None) -> TeachingSpec:
        from evowiki.wikiapp.teach import read_version
        from evowiki.wikiapp.teachspec import effective_spec, effective_version

        if version is None or version == effective_version(snap.state):
            return effective_spec(snap.state)
        return read_version(self._repo, snap, version)
