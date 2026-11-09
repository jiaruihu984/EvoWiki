"""Every production seed resolves from packaged assets, with no development documents."""

import shutil
from html.parser import HTMLParser
from pathlib import Path

from evowiki.deep.library_seed import SEED_FILE, load_seed, seed_html


class _Resources(HTMLParser):
    def __init__(self):
        super().__init__()
        self.external = []

    def handle_starttag(self, tag, attrs):
        for name, value in attrs:
            if ((tag, name) in {("script", "src"), ("link", "href"), ("img", "src")}
                    and value and not value.startswith("data:")):
                self.external.append(value)


def test_all_seed_components_resolve_without_docs(tmp_path):
    packaged = Path("src/evowiki/deep")
    target = tmp_path / packaged
    target.mkdir(parents=True)
    shutil.copyfile(SEED_FILE, target / SEED_FILE.name)
    shutil.copytree(SEED_FILE.parent / "components", target / "components")
    seeds = load_seed(target / SEED_FILE.name, root=tmp_path)
    assert len(seeds) == 18
    assert len({c.component_id for c in seeds}) == 18
    assert not (tmp_path / "docs").exists()
    for component in seeds:
        assert component.html_path.startswith("src/evowiki/deep/components/")
        for language in ("zh", "en"):
            html = seed_html(component, root=tmp_path, language=language)
            assert html.strip()
            assert '<script data-vendor="' not in html
            assert f'<script data-evowiki-locale="{component.component_id}"></script>' not in html
            resources = _Resources()
            resources.feed(html)
            assert not resources.external, (component.component_id, resources.external)
