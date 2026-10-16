import importlib
from pathlib import Path

import yaml as _yaml

from transcriber import config as config_module
from transcriber.config import Config, load_config


def test_defaults_when_no_yaml(tmp_path):
    cfg = load_config(yaml_path=tmp_path / "missing.yaml", glossary_path=tmp_path / "missing.txt")
    assert cfg.sample_rate == 16000
    assert cfg.min_silence_ms == 700
    assert cfg.max_utterance_s == 30.0
    assert cfg.glossary == []
    assert cfg.vault_root.name == "Vault"


def test_yaml_and_glossary_override(tmp_path):
    (tmp_path / "c.yaml").write_text("vault_root: /tmp/vault\nmin_silence_ms: 500\nhallucinations: ['Спасибо за просмотр']\n")
    (tmp_path / "g.txt").write_text("CDP\n\nОмни\n")
    cfg = load_config(yaml_path=tmp_path / "c.yaml", glossary_path=tmp_path / "g.txt")
    assert cfg.vault_root == Path("/tmp/vault")
    assert cfg.min_silence_ms == 500
    assert cfg.hallucinations == ["Спасибо за просмотр"]
    assert cfg.glossary == ["CDP", "Омни"]


def test_unknown_yaml_keys_are_skipped_with_warning(tmp_path, caplog):
    (tmp_path / "c.yaml").write_text("min_silence_ms: 400\nnope: 1\n")
    with caplog.at_level("WARNING", logger="transcriber"):
        cfg = load_config(yaml_path=tmp_path / "c.yaml", glossary_path=tmp_path / "missing.txt")
    assert cfg.min_silence_ms == 400
    assert "nope" in caplog.text


def test_data_dir_can_be_overridden_by_env(tmp_path, monkeypatch):
    monkeypatch.setenv("TRANSCRIBER_DATA", str(tmp_path))
    try:
        reloaded = importlib.reload(config_module)
        assert reloaded.DATA_DIR == tmp_path
        cfg = reloaded.Config()
        assert (cfg.sessions_dir, cfg.voices_dir, cfg.bench_dir) == (tmp_path / "sessions", tmp_path / "voices", tmp_path / "bench")
    finally:
        monkeypatch.delenv("TRANSCRIBER_DATA")
        importlib.reload(config_module)


def test_empty_data_env_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("TRANSCRIBER_DATA", "")
    try:
        reloaded = importlib.reload(config_module)
        assert reloaded.DATA_DIR == reloaded.DEFAULT_DATA_DIR.expanduser()
    finally:
        monkeypatch.delenv("TRANSCRIBER_DATA")
        importlib.reload(config_module)


def test_default_data_dir_is_application_support():
    assert config_module.DEFAULT_DATA_DIR == Path("~/Library/Application Support/Transcriber")


def test_load_config_reads_from_data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(config_module, "DATA_DIR", tmp_path)
    (tmp_path / "config.yaml").write_text("min_silence_ms: 321\n")
    (tmp_path / "glossary.txt").write_text("Толк\n")
    cfg = load_config()
    assert cfg.min_silence_ms == 321 and cfg.glossary == ["Толк"]


def test_load_config_with_missing_data_dir_uses_defaults(tmp_path, monkeypatch):
    monkeypatch.setattr(config_module, "DATA_DIR", tmp_path / "nope")
    cfg = load_config()
    assert cfg.min_silence_ms == 700 and cfg.sessions_dir == tmp_path / "nope" / "sessions"


def test_owner_defaults_are_neutral():
    cfg = Config()
    assert cfg.owner_name == "Owner" and cfg.owner_thoughts_heading == "## Мысли владельца"
    assert cfg.owner_talk_name == "" and cfg.talk_shared_names == []
    assert cfg.vault_root == Path("~/Documents/Vault").expanduser()
    # раскладка вольта владельца живёт в его config.yaml, в коде — нейтральная
    assert (cfg.raw_subdir, cfg.meetings_subdir, cfg.reference_subdir) == ("Raw/Transcripts", "Meetings", "Raw/TalkBot")


def test_example_yaml_is_neutral():
    data = _yaml.safe_load((config_module.CODE_ROOT / "worker" / "config.example.yaml").read_text())
    assert data["owner_name"] == "Owner" and data["talk_shared_names"] == []
    assert data.get("owner_talk_name", "") == "" and data["owner_thoughts_heading"] == "## Мысли владельца"


def test_clustering_and_echo_defaults_match_tuned_values(tmp_path):
    cfg = load_config(yaml_path=tmp_path / "missing.yaml", glossary_path=tmp_path / "missing.txt")
    assert cfg.speaker_similarity == 0.4     # стенд 14.10: 0.3 склеивал людей, 0.55 дробил
    assert cfg.min_embed_s == 2.0
    assert cfg.echo_strong_similarity == 0.8


def test_shipped_yaml_matches_tuned_defaults():
    data = _yaml.safe_load((config_module.CODE_ROOT / "worker" / "config.example.yaml").read_text())
    assert data["speaker_similarity"] == 0.4 and data["min_embed_s"] == 2.0
    assert data["talk_split_share"] == 0.3 == Config().talk_split_share
    assert data["talk_split_min_count"] == 3 == Config().talk_split_min_count
    assert data["echo_strong_similarity"] == 0.8


def test_block_defaults_and_shipped_yaml_agree(tmp_path):
    cfg = load_config(yaml_path=tmp_path / "missing.yaml", glossary_path=tmp_path / "missing.txt")
    assert (cfg.block_min_s, cfg.block_max_s) == (45.0, 90.0)
    assert (cfg.block_gap_s, cfg.block_gap_keep_s, cfg.block_idle_s) == (1.5, 0.5, 4.0)
    assert cfg.carry_context is True
    data = _yaml.safe_load((config_module.CODE_ROOT / "worker" / "config.example.yaml").read_text())
    assert data["block_min_s"] == 45 and data["block_max_s"] == 90
    assert data["block_gap_s"] == 1.5 and data["carry_context"] is True


def test_voice_settings_have_defaults_and_expand_paths(tmp_path):
    cfg = load_config(yaml_path=tmp_path / "missing.yaml", glossary_path=tmp_path / "missing.txt")
    assert cfg.voices_dir.name == "voices"
    assert cfg.voice_match_similarity == 0.45
    assert cfg.voice_min_samples == 3

    (tmp_path / "c.yaml").write_text("voices_dir: ~/голоса\nvoice_match_similarity: 0.6\nvoice_min_samples: 5\n")
    cfg = load_config(yaml_path=tmp_path / "c.yaml", glossary_path=tmp_path / "missing.txt")
    assert cfg.voices_dir == Path("~/голоса").expanduser()
    assert cfg.voice_match_similarity == 0.6 and cfg.voice_min_samples == 5


def test_private_dir_has_a_default_and_is_expanded(tmp_path):
    cfg = load_config(yaml_path=tmp_path / "missing.yaml", glossary_path=tmp_path / "missing.txt")
    assert cfg.private_dir == Path("~/Documents/Transcriber Private").expanduser()
    assert cfg.private_dir.is_absolute()

    (tmp_path / "c.yaml").write_text("private_dir: ~/Приватное\n")
    cfg = load_config(yaml_path=tmp_path / "c.yaml", glossary_path=tmp_path / "missing.txt")
    assert cfg.private_dir == Path("~/Приватное").expanduser()


def test_shipped_yaml_keeps_private_dir_outside_the_vault():
    data = _yaml.safe_load((config_module.CODE_ROOT / "worker" / "config.example.yaml").read_text())
    assert "Transcriber Private" in data["private_dir"]
    assert data["vault_root"] not in data["private_dir"]


def test_mlx_cache_default_and_shipped_yaml_agree(tmp_path):
    cfg = load_config(yaml_path=tmp_path / "missing.yaml", glossary_path=tmp_path / "missing.txt")
    assert cfg.mlx_cache_mb == 2048
    data = _yaml.safe_load((config_module.CODE_ROOT / "worker" / "config.example.yaml").read_text())
    assert data["mlx_cache_mb"] == 2048


def test_memory_guards_default_and_shipped_yaml_agree(tmp_path):
    cfg = load_config(yaml_path=tmp_path / "missing.yaml", glossary_path=tmp_path / "missing.txt")
    data = _yaml.safe_load((config_module.CODE_ROOT / "worker" / "config.example.yaml").read_text())
    assert data["idle_restart_mb"] == cfg.idle_restart_mb == 3500
    assert data["abandoned_after_min"] == cfg.abandoned_after_min == 10
    assert data["busy_restart_mb"] == cfg.busy_restart_mb == 6000


def test_owner_talk_name_joins_shared_names():
    cfg = Config(owner_talk_name="Анна Петрова", talk_shared_names=["Переговорная 5"])
    assert cfg.talk_shared_names == ["Переговорная 5", "Анна Петрова"]
    assert Config(owner_talk_name="Анна Петрова", talk_shared_names=["Анна Петрова"]).talk_shared_names == ["Анна Петрова"]
