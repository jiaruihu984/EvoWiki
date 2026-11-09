from pydantic import BaseModel

from evowiki.llm.gateway import _parse


class Out(BaseModel):
    text: str


BODY = '{\n  "text": "回想：x\\n\\n正文"}'          # a complete object; the JSON string holds \n escapes


def test_a_complete_object_with_stray_closing_braces_is_accepted():
    assert _parse(Out, BODY + "\n}").text == "回想：x\n\n正文"          # the calibration failure
    assert _parse(Out, '{"text": "a"} ]}  ').text == "a"
    assert _parse(Out, BODY).text == "回想：x\n\n正文"                 # the normal path is unchanged


def test_anything_else_still_fails_closed():
    assert _parse(Out, '{"text": "a"} trailing words') is None
    assert _parse(Out, '{"text": "a"') is None                  # truncated
    assert _parse(Out, '[{"text": "a"}]') is None               # not an object
    assert _parse(Out, '{"other": 1} }') is None                # wrong shape
    assert _parse(Out, '{"text": "a"} {"text": "b"}') is None   # two objects
