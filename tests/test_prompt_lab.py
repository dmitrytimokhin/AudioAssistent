import shutil
from datetime import date
from pathlib import Path

import pytest

from audio_assistant.client.domain import Segment, Transcript
from audio_assistant.client.prompt_lab import PromptLab, PromptTrial, transcript_from_run

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def lab(tmp_path):
    shutil.copytree(ROOT / "prompts", tmp_path / "prompts", ignore=shutil.ignore_patterns(".ipynb_checkpoints"))
    kinds = tmp_path / "meeting_kinds.yaml"
    shutil.copy(ROOT / "config" / "meeting_kinds.yaml", kinds)
    return PromptLab(tmp_path / "prompts", kinds)


def test_existing_prompts_pass_check(lab):
    assert [r for r in lab.check_all() if r["уровень"] == "error"] == []


def test_new_meeting_registers_kind(lab):
    files = lab.new("meeting", "retro", title="Ретроспектива")
    assert {f.name for f in files} == {"system.md", "user.md"}
    assert "retro" in lab._registered_kinds()
    from audio_assistant.client.meeting_kinds import load_meeting_kinds
    assert load_meeting_kinds(lab.kinds_file).get("retro").title == "Ретроспектива"
    issues = lab.check("meeting", "retro")
    assert any("заготовка" in i.text for i in issues)          # шаблон нельзя забыть дописать


def test_new_refuses_overwrite_and_bad_name(lab):
    with pytest.raises(FileExistsError):
        lab.new("meeting", "daily")
    with pytest.raises(ValueError):
        lab.new("image", "Мой Промпт")


def test_new_from_existing_audience(lab):
    lab.new("audience", "investors", copy_from="management")
    assert (lab.folder("audience", "investors") / "person.md").read_text() == \
        (lab.folder("audience", "management") / "person.md").read_text()
    assert "investors" in lab.names("audience")


def test_check_finds_typos(lab):
    folder = lab.folder("meeting", "daily")
    (folder / "user.md").write_text("Дата {dat}. {transcrip}", encoding="utf-8")
    texts = [i.text for i in lab.check("meeting", "daily")]
    assert any("{dat}" in t and "date" in t for t in texts)
    assert any("нет {transcript}" in t for t in texts)

    person = lab.folder("audience", "team") / "person.md"
    person.write_text("Речь: ${utteranse}", encoding="utf-8")
    levels = {i.level: i.text for i in lab.check("audience", "team")}
    assert "utterance" in levels["warn"] and "${utterance}" in levels["error"]


def test_check_digest_and_missing(lab):
    (lab.folder("digest", "weekly_dailies") / "digest.yaml").write_text("columns: []\n", encoding="utf-8")
    assert any(i.level == "error" for i in lab.check("digest", "weekly_dailies"))
    assert lab.check("image", "нет_такого")[0].level == "error"


def test_preview_substitutes(lab):
    text = lab.preview("meeting", "daily", transcript="Иван: сделал X")
    assert "Иван: сделал X" in text and "{transcript}" not in text
    assert "Иван Иванов" in lab.preview("audience", "team")


def test_catalog_lists_all_kinds(lab):
    kinds = {r["вид"] for r in lab.catalog()}
    assert kinds == {"audience", "meeting", "digest", "image"}


def test_transcript_from_run():
    detail = {"run": {"source": "a.wav"}, "dialogue": [{"speaker": "Иван", "text": "привет", "start": 1, "end": 2}]}
    tr = transcript_from_run(detail)
    assert tr.segments[0].speaker == "Иван" and tr.source == "a.wav"


def test_trial_isolates_failures():
    class Analyzer:
        def analyze(self, run, transcript, source, prompt=None):
            if prompt == "bad":
                raise KeyError("нет промпта")
            return type("R", (), {"text": f"ответ {prompt}"})()

    class P:
        analyzer = Analyzer()
    out = PromptTrial(P()).meeting(Transcript([Segment(0, 1, "x", "Иван")]), ["a", "bad"])
    assert out[0].text == "ответ a" and out[1].error.startswith("KeyError")


def test_save_keeps_history_and_restores(lab):
    original = lab.read("meeting", "daily")["system.md"]
    assert lab.save("meeting", "daily", "system.md", original) is None          # без изменений - без копии
    assert lab.save("meeting", "daily", "system.md", "Новая версия") is not None
    assert lab.read("meeting", "daily")["system.md"] == "Новая версия"
    (version,) = lab.history("meeting", "daily", "system.md")
    lab.restore("meeting", "daily", "system.md", version)
    assert lab.read("meeting", "daily")["system.md"] == original
    assert len(lab.history("meeting", "daily", "system.md")) == 2               # откат сам сохранил «Новую версию»
    with pytest.raises(ValueError):
        lab.save("meeting", "daily", "../../evil.md", "x")


def test_delete_goes_to_trash_and_returns(lab):
    lab.new("image", "tmp_prompt")
    plan = lab.delete("image", "tmp_prompt")                                    # пробный прогон
    assert not plan["выполнено"] and lab.folder("image", "tmp_prompt").exists()
    done = lab.delete("image", "tmp_prompt", confirm=True)
    assert done["выполнено"] and not lab.folder("image", "tmp_prompt").exists()
    lab.undelete(done["корзина"])
    assert (lab.folder("image", "tmp_prompt") / "system.md").exists()


def test_delete_meeting_needs_unregister(lab):
    lab.new("meeting", "retro", title="Ретро")
    with pytest.raises(ValueError):
        lab.delete("meeting", "retro", confirm=True)
    lab.delete("meeting", "retro", confirm=True, unregister_kind=True)
    assert "retro" not in lab._registered_kinds() and "daily" in lab._registered_kinds()


def test_protected_cannot_be_deleted(lab):
    lab.protected.add(("audience", "team"))
    with pytest.raises(ValueError):
        lab.delete("audience", "team", confirm=True)


def test_ui_create_edit_delete_flow(lab):
    pytest.importorskip("ipywidgets")
    from audio_assistant.client.prompt_lab_ui import PromptStudioUI
    ui = PromptStudioUI(lab)
    ui.kind.value = "meeting"
    ui.new_name.value, ui.new_title.value, ui.new_from.value = "retro", "Ретро", "daily"
    ui.on_create()
    assert ui.name.value == "retro" and "system.md" in ui._editors

    ui._editors["system.md"].value = "Правила v2"
    ui.on_save()
    assert lab.read("meeting", "retro")["system.md"] == "Правила v2"
    assert len(ui.versions.options) == 1
    ui.on_restore()
    assert lab.read("meeting", "retro")["system.md"] != "Правила v2"

    ui.on_delete()                                   # без подтверждения - только план
    assert lab.folder("meeting", "retro").exists()
    ui.del_confirm.value = True
    ui.on_delete()                                   # тип привязан, нужен флажок
    assert lab.folder("meeting", "retro").exists()
    ui.del_kind.value = True
    ui.on_delete()
    assert not lab.folder("meeting", "retro").exists()
    ui.trash.value = lab.trash()[0]
    ui.on_undelete()
    assert lab.folder("meeting", "retro").exists() and ui.name.value == "retro"
