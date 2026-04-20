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


def test_load_cached_returns_empty_dict_when_config_file_missing(tmp_path, monkeypatch):
    missing_cfg_path = tmp_path / "missing.yml"
    monkeypatch.setattr(config_store, "CONFIG_PATH", missing_cfg_path)

    assert config_store.load_cached() == {}


class TestActionRulesMigration:
    """v0.13.3 renamed per-camera action_rules → notification_rules."""

    def test_load_migrates_action_rules(self, tmp_path, monkeypatch):
        cfg_path = tmp_path / "scarguard.yml"
        cfg_path.write_text(
            "cameras:\n"
            "- name: pond\n"
            "  rtsp_url: rtsp://x\n"
            "  action_rules:\n"
            "  - class_name: bird\n"
            "    channels: [discord]\n"
        )
        monkeypatch.setattr(config_store, "CONFIG_PATH", cfg_path)
        loaded = config_store.load()
        cam = loaded["cameras"][0]
        assert "action_rules" not in cam
        assert cam["notification_rules"] == [
            {"class_name": "bird", "channels": ["discord"]}
        ]

    def test_save_strips_action_rules(self, tmp_path, monkeypatch):
        cfg_path = tmp_path / "scarguard.yml"
        monkeypatch.setattr(config_store, "CONFIG_PATH", cfg_path)
        config_store.save({
            "cameras": [{
                "name": "pond",
                "rtsp_url": "rtsp://x",
                "action_rules": [{"class_name": "*", "channels": ["x"]}],
            }],
        })
        reloaded = config_store.load()
        cam = reloaded["cameras"][0]
        assert "action_rules" not in cam
        assert cam["notification_rules"] == [
            {"class_name": "*", "channels": ["x"]}
        ]

    def test_load_preserves_existing_notification_rules_when_both_present(
        self, tmp_path, monkeypatch,
    ):
        """If both keys exist, notification_rules wins; action_rules is dropped."""
        cfg_path = tmp_path / "scarguard.yml"
        cfg_path.write_text(
            "cameras:\n"
            "- name: pond\n"
            "  rtsp_url: rtsp://x\n"
            "  action_rules: [{class_name: old, channels: [old]}]\n"
            "  notification_rules: [{class_name: new, channels: [new]}]\n"
        )
        monkeypatch.setattr(config_store, "CONFIG_PATH", cfg_path)
        loaded = config_store.load()
        cam = loaded["cameras"][0]
        assert "action_rules" not in cam
        assert cam["notification_rules"] == [
            {"class_name": "new", "channels": ["new"]}
        ]


class TestCameraConfidenceThreshold:
    """v0.13.3 added optional per-camera confidence_threshold override."""

    def test_round_trip_preserves_confidence(self, tmp_path, monkeypatch):
        cfg_path = tmp_path / "scarguard.yml"
        monkeypatch.setattr(config_store, "CONFIG_PATH", cfg_path)
        config_store.save({
            "cameras": [
                {"name": "pond", "rtsp_url": "rtsp://x", "confidence_threshold": 0.45},
                {"name": "yard", "rtsp_url": "rtsp://y"},  # no override
            ],
        })
        reloaded = config_store.load()
        assert reloaded["cameras"][0]["confidence_threshold"] == 0.45
        assert "confidence_threshold" not in reloaded["cameras"][1]
