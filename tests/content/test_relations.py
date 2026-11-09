import copy

import pytest

from evowiki.content.io import graph_from_document
from evowiki.content.relations import (
    RelationCatalogError,
    build_relation_catalog,
    c_test,
    c_test_hash,
    catalog_hash,
    endpoints_of,
    relation_id,
)
from tests.fixtures.devcontent import build_dev_content, dev_document


def test_relation_id_is_order_free():
    assert relation_id("svd", "pca") == relation_id("pca", "svd") == "rel:pca~svd"
    assert endpoints_of("rel:pca~svd") == ("pca", "svd")
    with pytest.raises(ValueError):
        relation_id("pca", "pca")


def test_catalog_groups_connections_by_endpoints():
    cat = build_relation_catalog(build_dev_content().graph)
    e = cat.get("rel:lora~pca")                         # conn.pca.svd.lora.en
    assert e.hops == 2 and e.path == ("lora", "svd", "pca")
    assert e.connection_segment_ids == ("conn.pca.svd.lora.en",)
    assert cat.get("rel:pca~svd").primary_connection_segment_id == "conn.pca.svd.en"
    assert [x.relation_id for x in cat.for_concept("pca")] == sorted(
        x.relation_id for x in cat.entries if "pca" in x.endpoints)
    assert cat.relations_of_segment("conn.pca.svd.en") == ("rel:pca~svd",)
    assert cat.relations_of_segment("pca.definition.en") == ()


def _with_covers(covers, status="human_approved"):
    doc = copy.deepcopy(dev_document())
    for p in doc["perspectives"]:
        if p["segment_id"] == "pca.perspective.linear_autoencoder.en":
            p["covers_relation_ids"] = covers
            p["covers_review_status"] = status
    return graph_from_document(doc)


def test_reviewed_covers_bind_perspective_segments():
    cat = build_relation_catalog(_with_covers(["rel:autoencoder~pca"]))
    assert cat.get("rel:autoencoder~pca").perspective_segment_ids == (
        "pca.perspective.linear_autoencoder.en",)
    assert cat.relations_of_segment("pca.perspective.linear_autoencoder.en") == (
        "rel:autoencoder~pca",)


def test_flagged_covers_are_ignored():
    cat = build_relation_catalog(_with_covers(["rel:autoencoder~pca"], status="flagged"))
    assert cat.get("rel:autoencoder~pca").perspective_segment_ids == ()


@pytest.mark.parametrize("covers", [["rel:matrix~vector"],      # not touching pca
                                    ["rel:eigenvalue~pca"],     # eigenvalue not in A(p)
                                    ["rel:lora~neural_network"]])  # no published connection
def test_invalid_covers_fail_closed(covers):
    with pytest.raises(RelationCatalogError):
        build_relation_catalog(_with_covers(covers))


def test_c_test_and_hashes_are_content_only():
    g = build_dev_content().graph
    cat = build_relation_catalog(g)
    route = [c for c in g.concept_ids if c != "linear_algebra"]
    ids = c_test(cat, route)
    assert ids == tuple(sorted(ids)) and all(set(endpoints_of(r)) <= set(route) for r in ids)
    assert catalog_hash(cat) == catalog_hash(build_relation_catalog(g))
    assert len(c_test_hash(ids)) == 64 and c_test_hash(ids) == c_test_hash(reversed(ids))
