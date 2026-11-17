import json
from pathlib import Path

import main


def test_load_invalid_config_returns_default(tmp_path, monkeypatch):
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text("{not json")
    monkeypatch.setattr(main, "CONFIG_PATH", cfg_path)
    cfg = main.load_config()
    assert cfg["mappings"] == []
    assert cfg["listen"]["port"] == 9001


def test_validate_and_save_config(tmp_path, monkeypatch):
    cfg_path = tmp_path / "config.json"
    monkeypatch.setattr(main, "CONFIG_PATH", cfg_path)
    cfg = main._default_config()
    cfg["mappings"].append({"osc_path": "/p", "controller": "c", "motor": 2})
    cfg["mappings"].append({"osc_path": "", "controller": "c", "motor": 0})  # invalid
    main.save_config(cfg)
    loaded = json.loads(cfg_path.read_text())
    assert len(loaded["mappings"]) == 1
    assert loaded["mappings"][0]["motor"] == 2


def test_clamp_strength():
    assert main.clamp_strength(1.5) == 1.0
    assert main.clamp_strength(-1) == 0.0
    assert main.clamp_strength("0.25") == 0.25
    assert main.clamp_strength("bad") == 0.0
    assert main.clamp_strength(float("nan")) == 0.0
