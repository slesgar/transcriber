import json

import yaml

from transcriber import config as config_module
from transcriber.cli import main
from transcriber.firstrun import Answers, QUALITY_LOG_HEADER, ask, init_data_dir, set_yaml_keys


def test_init_creates_files_from_examples_and_applies_answers(tmp_path):
    created = init_data_dir(tmp_path / "data", Answers(owner_name="Анна", owner_talk_name="Анна Петрова",
                            vault_root="~/V", private_dir="~/P", meeting_hosts=["talk.example.com"]))
    assert set(created) >= {"config.yaml", "app-config.json", "glossary.txt", "quality-log.md", "sessions", "voices", "bench"}
    data = yaml.safe_load((tmp_path / "data" / "config.yaml").read_text())
    assert data["owner_name"] == "Анна" and data["owner_talk_name"] == "Анна Петрова"
    assert data["owner_thoughts_heading"] == "## Мысли владельца"  # заголовок только в файле
    assert data["vault_root"] == "~/V" and data["private_dir"] == "~/P"
    assert data["speaker_similarity"] == 0.4            # остальное из примера
    app = json.loads((tmp_path / "data" / "app-config.json").read_text())
    assert app["privacy"]["workHosts"] == ["talk.example.com"] and app["recon"]["enabled"] is True
    assert (tmp_path / "data").stat().st_mode & 0o777 == 0o700


def test_init_is_idempotent(tmp_path):
    init_data_dir(tmp_path, Answers(owner_name="Анна"))
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    assert init_data_dir(tmp_path, Answers(owner_name="Пётр", meeting_hosts=["x.example.com"])) == []
    assert {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()} == before


def test_set_yaml_keys_keeps_comments_and_replaces_in_place():
    text = "# шапка\nowner_name: Owner   # кто\nspeaker_similarity: 0.3\n"
    out = set_yaml_keys(text, {"owner_name": "Анна", "talk_shared_names": ["Переговорная 5"]})
    assert out.splitlines()[0] == "# шапка" and 'owner_name: "Анна"' in out and "speaker_similarity: 0.3" in out
    assert out.rstrip().endswith('talk_shared_names: ["Переговорная 5"]')
    assert yaml.safe_load(out)["owner_name"] == "Анна"


def test_ask_keeps_defaults_on_empty_input():
    answers = iter(["", "Анна Петрова", "", "", "talk.example.com, ktalk.ru"])
    got = ask(Answers(), read=lambda prompt: next(answers), write=lambda s: None)
    assert got.owner_name == "Owner" and got.owner_talk_name == "Анна Петрова"
    assert got.meeting_hosts == ["talk.example.com", "ktalk.ru"]


def test_cli_init_yes(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(config_module, "DATA_DIR", tmp_path / "d")
    main(["init", "--yes", "--owner", "Анна", "--hosts", "talk.example.com"])
    assert yaml.safe_load((tmp_path / "d" / "config.yaml").read_text())["owner_name"] == "Анна"
    assert "config.yaml" in capsys.readouterr().out


def test_quality_log_header_forbids_names_and_topics():
    assert "без имён" in QUALITY_LOG_HEADER and "- HH:MM — " in QUALITY_LOG_HEADER
