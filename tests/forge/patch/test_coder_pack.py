"""The L2 coder's tool-call cap from the user's harness pack (harness 3b, Task 10): the pack's
H4.forge.patch.max_tool_calls may only lower L2_MAX_TOOL_CALLS; without a pack the forge's own
limits apply. `PatchDeps.pack` reaches the coder."""

import dataclasses

import pytest

from evowiki.forge import limits
from evowiki.forge.models import PatchRequest
from evowiki.forge.patch import coder
from evowiki.forge.patch.coder import LIMITS, limits_for
from evowiki.harness.pack import EMPTY, HarnessPack

REQ = PatchRequest(goal="修好加法", title="修好加法", failing_case="add(1,2)==3")


def test_without_a_pack_the_forge_limits_apply():
    assert limits_for(None) is LIMITS
    assert LIMITS.max_tool_calls == limits.L2_MAX_TOOL_CALLS
    assert limits_for(EMPTY).max_tool_calls == limits.L2_MAX_TOOL_CALLS


def test_the_pack_may_only_lower_the_cap():
    low = limits_for(HarnessPack(2, {"H4.forge.patch.max_tool_calls": 20}))
    assert low.max_tool_calls == 20
    assert dataclasses.replace(low, max_tool_calls=LIMITS.max_tool_calls) == LIMITS
    top = HarnessPack(2, {"H4.forge.patch.max_tool_calls": limits.L2_MAX_TOOL_CALLS})
    assert limits_for(top).max_tool_calls == limits.L2_MAX_TOOL_CALLS
    # the L1 key does not touch the L2 coder
    assert limits_for(HarnessPack(2, {"H4.forge.max_tool_calls": 5})).max_tool_calls == \
        limits.L2_MAX_TOOL_CALLS


def test_run_patch_coder_runs_with_the_packs_cap(monkeypatch):
    seen = []

    def fake_run_loop(spec, converse, ctx, messages):
        seen.append(spec.limits)
        return
        yield                                 # a generator that ends at once

    monkeypatch.setattr(coder, "run_loop", fake_run_loop)
    monkeypatch.setattr(coder, "build_tools", lambda holder: ())
    holder = object()
    coder.run_patch_coder(REQ, lambda r: None, holder=holder, trace=None, run_id="",  # type: ignore[arg-type]
                          user_id="u1", pack=HarnessPack(1, {"H4.forge.patch.max_tool_calls": 12}))
    coder.run_patch_coder(REQ, lambda r: None, holder=holder, trace=None, run_id="",  # type: ignore[arg-type]
                          user_id="u1")
    assert [lim.max_tool_calls for lim in seen] == [12, limits.L2_MAX_TOOL_CALLS]


def test_patch_deps_carry_an_optional_pack():
    pytest.importorskip("jsonschema")            # forge.api's build step needs it
    from evowiki.forge.api import PatchDeps
    fields = {f.name: f for f in dataclasses.fields(PatchDeps)}
    assert fields["pack"].default is None
