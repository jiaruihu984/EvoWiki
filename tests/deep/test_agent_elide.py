from evowiki.deep.agent import KEEP_READS, _elide
from evowiki.deep.tools import READ_STUB
from evowiki.llm.chat import ChatMessage


def _history(echo):
    return [ChatMessage(role="user", text="写"),
            ChatMessage(role="assistant", text="", echo=echo),
            ChatMessage(role="tool", tool_call_id="t1", text="很长的页面" * 100),
            ChatMessage(role="assistant", text="继续", echo=echo)]


def test_old_reads_are_stubbed_without_signed_thinking():
    out = _elide(_history(None), {2: 1}, KEEP_READS + 2)
    assert out[2].text == READ_STUB


def test_history_is_sent_verbatim_with_signed_thinking():
    echo = [{"type": "thinking", "thinking": "…", "signature": "sig"}, {"type": "text", "text": "x"}]
    hist = _history(echo)
    assert _elide(hist, {2: 1}, KEEP_READS + 2) == tuple(hist)


def test_anthropic_history_without_thinking_is_sent_verbatim():
    # an Anthropic echo is a list even with no thinking: never shortened (cache prefix, signatures)
    hist = _history([{"type": "text", "text": "x"}])
    assert _elide(hist, {2: 1}, KEEP_READS + 2) == tuple(hist)


def test_deepseek_history_is_still_elided():
    out = _elide(_history({"reasoning_content": "想一想"}), {2: 1}, KEEP_READS + 2)
    assert out[2].text == READ_STUB
