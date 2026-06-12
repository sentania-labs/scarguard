"""Unit tests for class-list handling in prepare_dataset.py."""

import prepare_dataset as pd
import pytest


@pytest.fixture(autouse=True)
def _reset_active_classes():
    yield
    pd.set_active_classes(list(pd.DEFAULT_CLASSES))


def test_parse_classes_strips_lowercases_dedupes():
    assert pd._parse_classes(" Duck, heron ,DUCK,plant ") == ["duck", "heron", "plant"]


def test_parse_classes_empty_input():
    assert pd._parse_classes("") == []
    assert pd._parse_classes(" , ,") == []


def test_default_classes_include_distractors():
    assert pd.DEFAULT_CLASSES == [
        "duck", "heron", "raccoon", "person", "dog", "cat", "plant",
    ]


def test_resolve_class_defaults():
    assert pd._resolve_class("person") == "person"
    assert pd._resolve_class("Potted Plant") == "plant"
    assert pd._resolve_class("houseplant") == "plant"
    assert pd._resolve_class("great_blue_heron") == "heron"
    assert pd._resolve_class("herons") == "heron"
    assert pd._resolve_class("kitten") == "cat"


def test_resolve_class_token_matching_blocks_substrings():
    assert pd._resolve_class("cattle") is None
    assert pd._resolve_class("bobcat") is None
    assert pd._resolve_class("eggplant") is None


def test_resolve_class_hyphenated_labels():
    # Roboflow Universe class names frequently use hyphens.
    assert pd._resolve_class("blue-heron") == "heron"
    assert pd._resolve_class("duck-male") == "duck"
    assert pd._resolve_class("great blue heron") == "heron"


def test_resolve_class_exact_active_name_beats_alias():
    # "flower" normally aliases to plant; as an active class it wins.
    pd.set_active_classes(["duck", "heron", "flower"])
    assert pd._resolve_class("flower") == "flower"
    assert pd._resolve_class("houseplant") is None


def test_resolve_class_restricted_to_active_classes():
    pd.set_active_classes(["duck", "heron", "raccoon"])
    assert pd._resolve_class("person") is None
    assert pd._resolve_class("plant") is None
    assert pd._resolve_class("great_blue_heron") == "heron"
    assert pd._resolve_class("mallard") == "duck"


def test_set_active_classes_filters_oid_labels():
    pd.set_active_classes(["duck", "heron", "raccoon"])
    assert pd.OID_CLASSES == {"/m/09ddx": "duck", "/m/0dq75": "raccoon"}

    pd.set_active_classes(list(pd.DEFAULT_CLASSES))
    assert pd.OID_CLASSES["/m/01g317"] == "person"
    assert pd.OID_CLASSES["/m/0bt9lr"] == "dog"
    assert pd.OID_CLASSES["/m/01yrx"] == "cat"
    # Houseplant and Plant both fold into "plant"
    assert pd.OID_CLASSES["/m/03fp41"] == "plant"
    assert pd.OID_CLASSES["/m/05s2s"] == "plant"


def test_set_active_classes_defines_index_order():
    pd.set_active_classes(["heron", "person", "duck"])
    assert pd.UNIFIED_CLASSES.index("heron") == 0
    assert pd.UNIFIED_CLASSES.index("person") == 1
    assert pd.UNIFIED_CLASSES.index("duck") == 2


def test_build_remap_drops_inactive_classes():
    pd.set_active_classes(["duck", "heron", "raccoon"])
    remap = pd._build_remap(["heron", "person", "cattle"])
    assert remap == {0: pd.UNIFIED_CLASSES.index("heron")}

    pd.set_active_classes(list(pd.DEFAULT_CLASSES))
    remap = pd._build_remap(["heron", "person", "cattle"])
    assert remap == {
        0: pd.UNIFIED_CLASSES.index("heron"),
        1: pd.UNIFIED_CLASSES.index("person"),
    }


def test_merge_and_split_writes_absolute_dataset_path(tmp_path):
    """ultralytics resolves a relative ``path`` against the process cwd,
    so data.yaml must carry the absolute dataset directory."""
    img = tmp_path / "img.jpg"
    img.write_bytes(b"\xff\xd8\xff\xe0fake-jpeg")
    samples = [
        pd.Sample(image=img, label_lines=["0 0.5 0.5 0.2 0.2"], source="test"),
    ]
    out = tmp_path / "merged"
    pd.merge_and_split(samples, out, val_split=0.5, seed=1)
    text = (out / "data.yaml").read_text()
    assert f"path: {out}\n" in text
    assert "path: .\n" not in text
