"""Openly licensed images for the page writer (deep@v24): licence filter, the Commons / Openverse
parsers, the download guard and re-encode, find_image / place_image in the workspace, the commit
(asset kind image + the evowiki-image fence), the durable wrapper and the prompt. Fake HTTP only: no
network."""

import base64
import io
import json
from dataclasses import dataclass, field

import pytest
from PIL import Image

from evowiki.bot.diffs import shown_body
from evowiki.deep.agent import IMAGES_RULE, write_prompt_stable
from evowiki.deep.contracts import RefineDraft, RefinePlan, image_fence
from evowiki.deep.durable import DurableImages
from evowiki.deep.images import (
    COMMONS_API,
    OPENVERSE_API,
    CommonsSearch,
    ImageCandidate,
    ImageRejected,
    OpenImages,
    OpenverseSearch,
    PreparedImage,
    prepare,
    reusable_license,
)
from evowiki.deep.tools import Workspace, build_tools, without_interactives
from evowiki.wikiapp.refine_apply import apply_draft, make_assets
from evowiki.wikiapp.refine_spec import TeachingSpec
from tests.deep.fakes import PLAN, DeepScript, call, finish, section, turn
from tests.deep.test_series_prompt import _page
from tests.deep.test_service import env, record, service  # noqa: F401 — the fixture
from tests.wikiapp.test_goal import NOW
from tests.wikiapp.test_page import organized


def png(w: int = 64, h: int = 48, mode: str = "RGB") -> bytes:
    buf = io.BytesIO()
    Image.new(mode, (w, h), (200, 30, 30) if mode == "RGB" else (200, 30, 30, 128)).save(buf, "PNG")
    return buf.getvalue()


@dataclass
class Resp:
    status_code: int = 200
    headers: dict = field(default_factory=lambda: {"content-type": "application/json"})
    content: bytes = b""


class FakeHttp:
    """Answers by URL prefix; records every GET."""

    def __init__(self, routes: dict[str, Resp | Exception]):
        self.routes, self.got = routes, []

    def get(self, url, params=None):
        self.got.append((url, dict(params or {})))
        for prefix, r in self.routes.items():
            if url.startswith(prefix):
                if isinstance(r, Exception):
                    raise r
                return r
        raise AssertionError(f"unexpected GET {url}")


def commons_json(*files: tuple[str, str, str]) -> bytes:
    """files: (title, licence short name, artist HTML)."""
    pages = [{"index": i, "title": f"File:{t}.png", "imageinfo": [{
        "thumburl": f"https://thumb.wikimedia.org/x/{i}.png", "thumbwidth": 1280, "thumbheight": 960,
        "descriptionurl": f"https://commons.wikimedia.org/wiki/File:{t}.png",
        "extmetadata": {"ObjectName": {"value": t}, "LicenseShortName": {"value": lic},
                        "LicenseUrl": {"value": "https://creativecommons.org/licenses/by-sa/4.0/"},
                        "Artist": {"value": artist},
                        "ImageDescription": {"value": "Diagram <b>of</b> the &amp; heart"}}}]}
        for i, (t, lic, artist) in enumerate(files)]
    return json.dumps({"query": {"pages": pages}}).encode()


OV = {"results": [
    {"title": "Heart model", "foreign_landing_url": "https://www.flickr.com/photos/1", "creator": "moon",
     "license": "by", "license_version": "2.0", "license_url": "https://creativecommons.org/licenses/by/2.0/",
     "thumbnail": "https://api.openverse.org/v1/images/abc/thumb/", "source": "flickr",
     "tags": [{"name": "anatomy"}], "width": 500, "height": 400},
    {"title": "No derivatives", "foreign_landing_url": "https://www.flickr.com/photos/2", "creator": "x",
     "license": "by-nd", "license_version": "2.0", "thumbnail": "https://api.openverse.org/v1/images/d/thumb/"}]}


def cand(**kw) -> ImageCandidate:
    return ImageCandidate(**{"provider": "commons", "title": "Heart diagram", "description": "the heart",
                             "author": "Wapcaplet", "license": "CC BY-SA 3.0",
                             "license_url": "https://creativecommons.org/licenses/by-sa/3.0/",
                             "source_url": "https://commons.wikimedia.org/wiki/File:Heart.png",
                             "file_url": "https://upload.wikimedia.org/h.png", "width": 640,
                             "height": 480, **kw})


class FakeImages:
    def __init__(self, hits=None, reject: str | None = None):
        self.hits = [cand()] if hits is None else hits
        self.reject, self.searched, self.fetched = reject, [], []

    def search(self, query, limit):
        self.searched.append(query)
        return self.hits[:limit]

    def thumbnail(self, c):
        return None

    def fetch(self, c):
        self.fetched.append(c.title)
        if self.reject:
            raise ImageRejected(self.reject)
        return prepare(png())


# ---- licences ----

@pytest.mark.parametrize("name,version,want", [
    ("CC BY-SA 4.0", "", "CC BY-SA 4.0"), ("CC BY 2.0", "", "CC BY 2.0"),
    ("cc-by-sa-3.0", "", "CC BY-SA 3.0"), ("CC BY-SA 2.5,2.0,1.0", "", "CC BY-SA 2.5"),
    ("by-sa", "4.0", "CC BY-SA 4.0"), ("by", "", "CC BY"), ("CC0", "", "CC0"), ("cc0", "1.0", "CC0"),
    ("Public domain", "", "Public domain"), ("PD-USGov", "", "Public domain"), ("pdm", "", "Public domain"),
    ("CC BY-NC 2.0", "", None), ("CC BY-NC-SA 4.0", "", None), ("CC BY-ND 4.0", "", None),
    ("by-nc", "2.0", None), ("GFDL", "", None), ("Attribution", "", None), ("", "", None),
    ("Fair use", "", None), ("CC BY-SA 4.0 or something else", "", None),
])
def test_only_reusable_licences_pass(name, version, want):
    assert reusable_license(name, version) == want


# ---- providers ----

def test_commons_parses_licence_author_and_drops_what_cannot_be_reused():
    http = FakeHttp({COMMONS_API: Resp(content=commons_json(
        ("Heart", "CC BY-SA 4.0", '<a href="/u">Wapcaplet</a>'),
        ("Nope", "CC BY-NC 2.0", "x"), ("Plain", "Public domain", "")))})
    got = CommonsSearch(http).search("human heart", 5)
    assert [c.title for c in got] == ["Heart", "Plain"]
    h = got[0]
    assert (h.author, h.license, h.provider) == ("Wapcaplet", "CC BY-SA 4.0", "commons")
    assert h.description == "Diagram of the & heart"                  # HTML stripped, entity decoded
    assert h.source_url == "https://commons.wikimedia.org/wiki/File:Heart.png"
    assert h.file_url.startswith("https://thumb.wikimedia.org/") and (h.width, h.height) == (1280, 960)
    params = http.got[0][1]
    assert params["generator"] == "search" and params["gsrnamespace"] == "6"
    assert "human heart" in params["gsrsearch"] and "LicenseShortName" in params["iiextmetadatafilter"]


def test_openverse_asks_for_reusable_licences_and_checks_them_again():
    http = FakeHttp({OPENVERSE_API: Resp(content=json.dumps(OV).encode())})
    got = OpenverseSearch(http).search("heart", 5)
    assert [(c.title, c.license, c.author) for c in got] == [("Heart model", "CC BY 2.0", "moon")]
    assert got[0].file_url == "https://api.openverse.org/v1/images/abc/thumb/"   # its own thumbnail
    assert http.got[0][1]["license"] == "cc0,pdm,by,by-sa"


def test_commons_first_then_openverse_and_a_failing_provider_is_skipped():
    ov = Resp(content=json.dumps(OV).encode())
    one = Resp(content=commons_json(("Heart", "CC0", "")))
    got = OpenImages(FakeHttp({COMMONS_API: one, OPENVERSE_API: ov})).search("heart", 2)
    assert [c.provider for c in got] == ["commons", "openverse"]
    down = OpenImages(FakeHttp({COMMONS_API: RuntimeError("down"), OPENVERSE_API: ov})).search("heart", 3)
    assert [c.provider for c in down] == ["openverse"]
    full = FakeHttp({COMMONS_API: Resp(content=commons_json(("A", "CC0", ""), ("B", "CC0", "")))})
    assert len(OpenImages(full).search("x", 2)) == 2 and len(full.got) == 1   # enough: no Openverse


# ---- download ----

def test_fetch_downloads_from_the_provider_host_and_re_encodes_as_webp():
    http = FakeHttp({"https://upload.wikimedia.org/": Resp(headers={"content-type": "image/png"},
                                                           content=png(2000, 1000, "RGBA"))})
    img = OpenImages(http).fetch(cand())
    assert img.data[:4] == b"RIFF" and img.data[8:12] == b"WEBP"
    assert (img.width, img.height) == (1280, 640)                      # scaled to MAX_SIDE
    assert base64.b64decode(img.data_b64) == img.data


@pytest.mark.parametrize("url,resp,why", [
    ("https://evil.example/x.png", None, "允许的图库地址"),
    ("http://upload.wikimedia.org/x.png", None, "允许的图库地址"),
    ("https://upload.wikimedia.org/x.png", Resp(status_code=302), "HTTP 302"),
    ("https://upload.wikimedia.org/x.png", Resp(headers={"content-type": "text/html"}), "不是图片"),
    ("https://upload.wikimedia.org/x.svg", Resp(headers={"content-type": "image/svg+xml"}), "不是图片"),
    ("https://upload.wikimedia.org/x.png", Resp(headers={"content-type": "image/png"}, content=b"<svg/>"),
     "不是能用的图片"),
])
def test_fetch_refuses(url, resp, why):
    http = FakeHttp({"https://upload.wikimedia.org/": resp} if resp else {})
    with pytest.raises(ImageRejected, match=why):
        OpenImages(http).fetch(cand(file_url=url))


def test_prepare_refuses_other_formats_and_tiny_images():
    buf = io.BytesIO()
    Image.new("RGB", (64, 64)).save(buf, "BMP")
    with pytest.raises(ImageRejected, match="BMP"):
        prepare(buf.getvalue())
    with pytest.raises(ImageRejected, match="尺寸"):
        prepare(png(8, 8))


# ---- the writer's tools ----

def _plan() -> RefinePlan:
    return RefinePlan(known="已知", observable=PLAN["observable"], one_at_a_time=tuple(PLAN["one_at_a_time"]),
                      contrast=PLAN["contrast"], spoken_answer=PLAN["spoken_answer"])


def _tools(ws: Workspace):
    from unittest.mock import MagicMock

    from tests.deep.fakes import FakeChecker
    return {t.spec.name: t for t in build_tools(
        ws, user_id="u1", page_id="p1", title="t", kview=MagicMock(), names={},
        teaching=TeachingSpec(version=1, items=()), checker=FakeChecker(), known="已知")}


def _ws(images=None) -> Workspace:
    ws = Workspace(_page("p1", "心脏怎么泵血", ("heart",), ("heart",)), image_search=images)
    ws.plan = _plan()                    # plan_page accepted
    return ws


def test_no_image_search_no_image_tools_and_nothing_in_the_prompt():
    ws = _ws()
    assert not {"find_image", "place_image"} & set(_tools(ws))
    assert IMAGES_RULE not in write_prompt_stable(ws, ())
    assert IMAGES_RULE in write_prompt_stable(_ws(FakeImages()), ())


def test_find_then_place_an_image_into_the_draft():
    from evowiki.agent.tools import ToolContext
    ctx = ToolContext(user_id="u1", session_id="t")
    fake = FakeImages([cand(), cand(title="Heart photo", source_url="https://commons.wikimedia.org/wiki/File:P.jpg")])
    ws = _ws(fake)
    t = _tools(ws)
    got = t["find_image"].run({"query": "human heart anatomy", "purpose": "看清四个腔室"}, ctx)
    assert got.error is None and "[img1] Heart diagram" in got.content and "[img2] Heart photo" in got.content
    assert "CC BY-SA 3.0" in got.content and "Wapcaplet" in got.content
    assert t["find_image"].spec.parallel and t["find_image"].spec.effect == "network"

    bad = t["place_image"].run({"candidate": "img9", "image_id": "heart", "section_id": "s1", "alt": "心脏"}, ctx)
    assert bad.error is not None and "find_image" in bad.content
    no_sec = t["place_image"].run({"candidate": "img1", "image_id": "heart", "section_id": "zz", "alt": "心脏"}, ctx)
    assert no_sec.error is not None
    ok = t["place_image"].run({"candidate": "img1", "image_id": "heart", "section_id": "s1",
                               "alt": "心脏剖面，四个腔室"}, ctx)
    assert ok.error is None and "{{image:heart}}" in ok.content
    img = ws.images["heart"]
    assert (img.title, img.author, img.license, img.source) == ("Heart diagram", "Wapcaplet", "CC BY-SA 3.0",
                                                                "Wikimedia Commons")
    assert base64.b64decode(img.data_b64)[8:12] == b"WEBP"

    ws_sec = t["write_section"].run({"section_id": "s1", "heading": "心脏长什么样", "role": "explain",
                                     "depth": "example", "cue": "心脏有几个腔？", "answer": "一句答案",
                                     "body": "先看图。\n\n{{image:heart}}\n\n左边两个腔，右边两个腔。"}, ctx)
    assert ws_sec.error is None
    draft = ws.draft("p1")
    assert [m.image_id for m in draft.images] == ["heart"] and draft.needs_assets


def test_a_rejected_download_places_nothing():
    from evowiki.agent.tools import ToolContext
    ctx = ToolContext(user_id="u1", session_id="t")
    ws = _ws(FakeImages(reject="下载失败（HTTP 404）"))
    t = _tools(ws)
    t["find_image"].run({"query": "heart", "purpose": "看清腔室"}, ctx)
    r = t["place_image"].run({"candidate": "img1", "image_id": "heart", "section_id": "s1", "alt": "心脏"}, ctx)
    assert r.error is not None and "HTTP 404" in r.content and not ws.images


def test_no_hits_is_not_an_error():
    from evowiki.agent.tools import ToolContext
    ws = _ws(FakeImages([]))
    r = _tools(ws)["find_image"].run({"query": "xyz", "purpose": "p"}, ToolContext(user_id="u1", session_id="t"))
    assert r.error is None and "没有找到" in r.content


# ---- draft, commit, note ----

def _draft_with_image() -> RefineDraft:
    ws = _ws(FakeImages())
    from evowiki.agent.tools import ToolContext
    ctx = ToolContext(user_id="u1", session_id="t")
    t = _tools(ws)
    t["find_image"].run({"query": "heart", "purpose": "看腔室"}, ctx)
    t["place_image"].run({"candidate": "img1", "image_id": "heart", "section_id": "s1", "alt": "心脏剖面"}, ctx)
    t["write_section"].run({"section_id": "s1", "heading": "心脏", "role": "explain", "depth": "example",
                            "cue": "心脏有几个腔？", "answer": "一句答案", "body": "看图。\n\n{{image:heart}}\n\n四个腔。"}, ctx)
    return ws.draft("p1")


def test_draft_refuses_an_unplaced_or_clashing_image():
    d = _draft_with_image()
    with pytest.raises(ValueError, match="never placed"):
        RefineDraft(page_id="p1", plan=d.plan,
                    sections=(d.sections[0].model_copy(update={"body": "没有图"}),), images=d.images)
    with pytest.raises(ValueError, match="unknown section"):
        RefineDraft(page_id="p1", plan=d.plan, sections=d.sections,
                    images=(d.images[0].model_copy(update={"section_id": "zz"}),))


def test_commit_stores_an_image_asset_and_the_note_carries_the_fence():
    d = _draft_with_image()
    ids = iter(["ia_1", "ia_2"])
    assets = make_assets(d, {}, user_id="u1", job_id="j1", now=NOW, new_id=lambda p: next(ids))
    (a,) = assets
    assert (a.meta.kind, a.meta.interactive_id, a.meta.check.ok) == ("image", "heart", True)
    assert a.html == d.images[0].data_b64
    page, changed = apply_draft(_page("p1", "心脏", ("heart",), ("heart",)), d, assets, job_id="j1")
    body = page.texts["s1"].body
    assert "{{image:" not in body and changed == {"s1": "rewritten"}
    assert image_fence("ia_1", d.images[0]) in body
    lines = ["```evowiki-image", "asset: ia_1", "alt: 心脏剖面", "title: Heart diagram", "author: Wapcaplet",
             "license: CC BY-SA 3.0", "license_url: https://creativecommons.org/licenses/by-sa/3.0/",
             "source: Wikimedia Commons", "source_url: https://commons.wikimedia.org/wiki/File:Heart.png",
             "width: 64", "height: 48", "```"]
    assert image_fence("ia_1", d.images[0]) == "\n".join(lines)
    assert shown_body(body, {}) == "看图。\n\n〔图片：Heart diagram〕\n\n四个腔。"
    assert "〔图片：Heart diagram〕" in shown_body(d.sections[0].body, {"heart": "Heart diagram"})


def test_without_interactives_drops_an_image_and_its_line():
    d = without_interactives(_draft_with_image(), {"heart"})
    assert not d.images and "{{image:heart}}" not in d.sections[0].body


def test_durable_images_outside_a_workflow_is_a_plain_call_and_keeps_rejections():
    fake = FakeImages()
    d = DurableImages(fake)
    assert [c.title for c in d.search("heart", 3)] == ["Heart diagram"]
    img = d.fetch(cand())
    assert isinstance(img, PreparedImage) and img.data[8:12] == b"WEBP"
    with pytest.raises(ImageRejected, match="no"):
        DurableImages(FakeImages(reject="no")).fetch(cand())


# ---- end to end: a refine that places an image ----

def test_refine_places_an_image_and_commits_it(env):  # noqa: F811 — the fixture
    pid = organized(env)
    fake = FakeImages()
    body = "s2 精修正文：先看图。\n\n{{image:heart}}\n\n图里左右各两个腔。"
    ds = DeepScript().plan().write(
        turn(call("find_image", 1, query="human heart anatomy", purpose="看清四个腔")),
        turn(section("s2", 1, body=body),
             call("place_image", 1, candidate="img1", image_id="heart", section_id="s2", alt="心脏剖面图")),
        turn(finish()))
    svc = service(env, ds)
    svc.images = fake
    res = svc.run("u1", svc.start("u1", pid, "user"))
    assert (res.job.stage, res.job.failure) == ("done", None), res.job.failure_detail
    assert fake.searched == ["human heart anatomy"] and fake.fetched == ["Heart diagram"]
    page = record(env, pid).page
    (asset_id,) = res.job.interactives
    assert f"```evowiki-image\nasset: {asset_id}\nalt: 心脏剖面图\n" in page.texts["s2"].body
    a = svc.assets.get("u1", asset_id)
    assert a.meta.kind == "image" and base64.b64decode(a.html)[8:12] == b"WEBP"
    stable = next(r for r in ds.requests if r.purpose == "deep.write").messages[0].text
    assert IMAGES_RULE in stable
