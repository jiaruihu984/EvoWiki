from pathlib import Path

import pytest

from evowiki.forge import limits
from evowiki.forge.patch.junit import JunitError, read
from tests.forge.patch.fakes import write_junit

XML = """<?xml version="1.0" encoding="utf-8"?>
<testsuites><testsuite name="pytest" tests="5">
<testcase classname="tests.deep.test_a" name="test_x" file="tests/deep/test_a.py" time="0.01"/>
<testcase classname="tests.deep.test_a" name="test_f" file="tests/deep/test_a.py">
  <failure message="assert 1 == 2">boom</failure></testcase>
<testcase classname="tests.deep.test_a" name="test_e" file="tests/deep/test_a.py">
  <error message="fixture">err</error></testcase>
<testcase classname="tests.deep.test_a" name="test_s[1-2]" file="tests/deep/test_a.py">
  <skipped message="skip"/></testcase>
<testcase classname="tests.deep.test_a.TestK" name="test_x" file="tests/deep/test_a.py"/>
</testsuite></testsuites>
"""


def test_outcomes_by_nodeid(tmp_path: Path):
    p = tmp_path / "j.xml"
    p.write_text(XML, encoding="utf-8")
    assert read(p) == {
        "tests/deep/test_a.py::test_x": "passed",
        "tests/deep/test_a.py::test_f": "failed",
        "tests/deep/test_a.py::test_e": "error",
        "tests/deep/test_a.py::test_s[1-2]": "skipped",
        "tests/deep/test_a.py::TestK::test_x": "passed",
    }


def test_without_file_attribute_uppercase_parts_are_classes(tmp_path: Path):
    p = tmp_path / "j.xml"
    p.write_text('<testsuite><testcase classname="tests.deep.test_a.TestK" name="test_x"/>'
                 '<testcase classname="tests.deep.test_a" name="test_y"/></testsuite>', encoding="utf-8")
    assert read(p) == {"tests/deep/test_a.py::TestK::test_x": "passed",
                       "tests/deep/test_a.py::test_y": "passed"}


def test_too_large_is_refused(tmp_path: Path):
    p = tmp_path / "j.xml"
    p.write_bytes(b"<testsuite>" + b" " * (limits.JUNIT_MAX_BYTES + 1) + b"</testsuite>")
    with pytest.raises(JunitError):
        read(p)


def test_missing_is_refused(tmp_path: Path):
    with pytest.raises(JunitError):
        read(tmp_path / "nope.xml")


@pytest.mark.parametrize("body", [
    b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]><testsuite/>',
    b'<?xml version="1.0"?><!doctype testsuite><testsuite/>',
    b'<testsuite><testcase classname="a" name="b"/></testsuite><!ENTITY a "x">',
    b"<testsuite><testcase",                                       # truncated: not a pass
    b"",
    b"<html/>",                                                    # not a junit file at all
])
def test_malformed_or_dtd_is_refused(tmp_path: Path, body: bytes):
    p = tmp_path / "j.xml"
    p.write_bytes(body)
    with pytest.raises(JunitError):
        read(p)


def test_fake_writer_round_trips(tmp_path: Path):
    want = {"tests/deep/test_a.py::test_x": "passed", "tests/deep/test_a.py::TestK::test_y": "failed",
            "tests/b/test_c.py::test_z[x]": "error", "tests/b/test_c.py::test_w": "skipped"}
    p = tmp_path / "sub" / "j.xml"
    write_junit(p, want)
    assert read(p) == want
