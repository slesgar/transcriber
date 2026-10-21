import json
from datetime import datetime

from transcriber.agent_qa import qa_filename, read_qa, render_qa, write_agent_qa
from transcriber.config import Config
from transcriber.session import Meta, Session, State
from transcriber.vault import write_raw


def meta(private=False):
    return Meta(id="2026-09-15-1430", started_at=datetime.fromisoformat("2026-09-15T14:30:00+03:00"),
                app="kontur.talk", title="Синк по витрине", participants=["Иван Сидоров"], private=private)


def qa_line(question, answer, **kw):
    item = {"asked_at": "2026-09-15T14:52:10+03:00", "question": question, "answer": answer,
            "agent": "Агент", "with_context": False, "seconds": 9}
    item.update(kw)
    return json.dumps(item, ensure_ascii=False)


def test_filename_is_not_a_transcript_name():
    name = qa_filename(meta())
    assert name == "Agent-2026-09-15-1430-Синк-по-витрине.md"
    assert not name.startswith("Local-")   # рутины вольта ищут транскрипты по «Local-»


def test_read_skips_broken_and_unanswered_lines(tmp_path):
    (tmp_path / "qa.jsonl").write_text("\n".join([
        qa_line("Что решили?", "Пилот на одну группу."),
        '{"question": "оборвано',
        qa_line("Кто отвечает?", "", error="таймаут"),
        qa_line("  ", "ответ без вопроса"),
    ]) + "\n")
    items = read_qa(tmp_path)
    assert [i["question"] for i in items] == ["Что решили?"]
    assert read_qa(tmp_path / "нет") == []


def test_render_links_meeting_and_warns_it_is_not_a_source():
    text = render_qa(meta(), "Local-x", "MTG-x", [json.loads(qa_line("Что\nрешили?", "Пилот.", with_context=True))])
    assert text.startswith("---\ntype: raw\nsource: other\nsource_date: 2026-09-15\nprojects: []\ntags: [raw, agent-qa]\n---\n")
    assert "К встрече [[MTG-x]], транскрипт [[Local-x]]" in text and "не источник фактов" in text
    assert "## 14:52 · Что решили?\n\nПилот.\n\n_Агент · с контекстом проектов · 9 с_" in text


def test_written_next_to_raw_only_when_there_were_questions(vault, tmp_path):
    cfg = Config(vault_root=vault)
    d = tmp_path / "sessions" / "2026-09-15-1430"
    d.mkdir(parents=True)
    s = Session(d, meta(), State())
    write_raw(cfg, s, status_text="финализирован")
    assert write_agent_qa(cfg, s) is None
    assert not list((vault / "Raw/Transcripts").glob("Agent-*"))
    (d / "qa.jsonl").write_text(qa_line("Что решили?", "Пилот.") + "\n")
    p = write_agent_qa(cfg, s)
    assert p == vault / "Raw/Transcripts/Agent-2026-09-15-1430-Синк-по-витрине.md"
    assert "[[Local-2026-09-15-1430-Синк-по-витрине]]" in p.read_text()


def test_private_recording_never_gets_a_file(vault, tmp_path, private_dir):
    cfg = Config(vault_root=vault, private_dir=private_dir)
    d = tmp_path / "sessions" / "2026-09-15-1430"
    d.mkdir(parents=True)
    (d / "qa.jsonl").write_text(qa_line("Что решили?", "Пилот.") + "\n")
    s = Session(d, meta(private=True), State())
    assert write_agent_qa(cfg, s) is None


def test_line_separator_inside_an_answer_does_not_split_the_record(tmp_path):
    (tmp_path / "qa.jsonl").write_text(qa_line("Что решили?", "Пилот. Потом раскат.\u0085Всё.") + "\n")
    assert read_qa(tmp_path)[0]["answer"] == "Пилот. Потом раскат.\u0085Всё."


def test_name_follows_the_pinned_raw_not_the_title(vault, tmp_path):
    from transcriber.agent_qa import qa_path_for_raw
    cfg = Config(vault_root=vault)
    d = tmp_path / "sessions" / "2026-09-15-1430"
    d.mkdir(parents=True)
    s = Session(d, meta(), State())
    write_raw(cfg, s, status_text="финализирован")
    (d / "qa.jsonl").write_text(qa_line("Что решили?", "Пилот.") + "\n")
    s.meta.title = "Новая тема"
    p = write_agent_qa(cfg, s)
    assert p == qa_path_for_raw(vault / "Raw/Transcripts/Local-2026-09-15-1430-Синк-по-витрине.md")
    assert p.name == "Agent-2026-09-15-1430-Синк-по-витрине.md"


def test_failure_does_not_escape(vault, tmp_path, monkeypatch):
    from transcriber import agent_qa
    cfg = Config(vault_root=vault)
    s = Session(tmp_path, meta(), State())
    monkeypatch.setattr(agent_qa, "write_agent_qa", lambda *a: (_ for _ in ()).throw(OSError("диск")))
    assert agent_qa.write_agent_qa_safely(cfg, s) is None
