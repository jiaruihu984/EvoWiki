import base64
import hashlib
from io import BytesIO

import pytest
from PIL import Image

from evowiki.llm.chat import ChatTurn, ImagePart
from evowiki.llm.gateway import Gateway
from evowiki.llm.log import CallLog
from evowiki.llm.models import GatewayConfig, ModelRef
from evowiki.llm.prices import PriceTable
from evowiki.llm.providers import ProviderRejectedError
from evowiki.llm.scripted import ScriptedChatProvider
from evowiki.llm.usage import Account, NullMeter
from evowiki.llm.usage_control import UsageControl
from evowiki.sources.claude_pages import ClaudePages

REF = ModelRef(provider="anthropic", model="claude-sonnet-5-5")
CFG = GatewayConfig(generator=REF, verifier=REF, allow_single_provider=True)
ACCOUNT = Account(user_id="u1", group="other")


def png(color):
    buf = BytesIO()
    Image.new("RGB", (40, 30), color).save(buf, "PNG")
    return buf.getvalue()


def reader(tmp_path, chat):
    prov = ScriptedChatProvider("anthropic", chat)
    gw = Gateway(CFG, CallLog(tmp_path / "c.jsonl"), mode="record", providers={"anthropic": prov},
                 usage=UsageControl(meter=NullMeter(), prices=PriceTable({})))
    return ClaudePages(gw, lambda: ACCOUNT), prov


def test_reads_each_page_with_its_image(tmp_path):
    texts = iter(["# 第一页", "  $x^2$  "])
    r, prov = reader(tmp_path, lambda m, req: ChatTurn(text=next(texts)))
    pages = [png((255, 0, 0)), png((0, 0, 255))]
    out = r.read(pages)
    assert [(o.text, o.figure) for o in out] == [("# 第一页", False), ("$x^2$", False)]
    assert r.name == "claude" and r.available()
    assert len(prov.chat_calls) == 2
    for req, raw in zip(prov.chat_calls, pages, strict=True):
        msg = req.messages[0]
        (part,) = msg.parts
        assert isinstance(part, ImagePart) and msg.text.startswith("第 ")
        assert Image.open(BytesIO(base64.b64decode(part.data_b64))).size == (40, 30)
        assert part.sha256 == hashlib.sha256(raw).hexdigest()


def test_empty_output_is_none_with_reason(tmp_path):
    r, _ = reader(tmp_path, lambda m, req: ChatTurn(text="  "))
    (o,) = r.read([png((1, 2, 3))])
    assert o.text is None and o.reason == "empty" and o.figure is False


def test_rejection_is_raised(tmp_path):
    def boom(m, req):
        raise ProviderRejectedError("402", status_code=402)
    r, _ = reader(tmp_path, boom)
    with pytest.raises(ProviderRejectedError):
        r.read([png((1, 2, 3))])


def test_unavailable_without_gateway():
    assert not ClaudePages(None, lambda: None).available()



def test_the_printed_only_variant_has_its_own_prompt_and_version(tmp_path):
    from evowiki.sources.claude_pages import (
        PRINTED_PROMPT_VERSION,
        PRINTED_SYSTEM,
        PROMPT_VERSION,
        SYSTEM,
    )
    r, prov = reader(tmp_path, lambda m, req: ChatTurn(text="印刷的字"))
    p = r.printed_only().for_user("u1")              # the metered copy stays printed-only
    assert p.printed and not r.printed
    p.read([png((1, 1, 1))])
    r.read([png((2, 2, 2))])
    printed, plain = prov.chat_calls
    from evowiki.llm.writing_style import DEFAULT_LANGUAGE_STYLE, prepend_style

    assert (printed.system, printed.prompt_version) == (
        prepend_style(PRINTED_SYSTEM, DEFAULT_LANGUAGE_STYLE), PRINTED_PROMPT_VERSION)
    assert (plain.system, plain.prompt_version) == (
        prepend_style(SYSTEM, DEFAULT_LANGUAGE_STYLE), PROMPT_VERSION)
    assert printed.purpose == plain.purpose == "source.vision"
