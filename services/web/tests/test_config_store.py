import pytest

import config_store


def test_load_rejects_non_mapping_yaml(tmp_path, monkeypatch):
    cfg_path = tmp_path / "scarguard.yml"
    cfg_path.write_text("- not\n- a\n- mapping\n")
    monkeypatch.setattr(config_store, "CONFIG_PATH", cfg_path)

    with pytest.raises(ValueError, match="Config root must be a mapping"):
        config_store.load()


def test_set_armed_does_not_overwrite_non_mapping_config(tmp_path, monkeypatch):
    cfg_path = tmp_path / "scarguard.yml"
    original = "- temporary\n- invalid\n"
    cfg_path.write_text(original)
    monkeypatch.setattr(config_store, "CONFIG_PATH", cfg_path)

    with pytest.raises(ValueError, match="Config root must be a mapping"):
        config_store.set_armed(True)

    assert cfg_path.read_text() == original
