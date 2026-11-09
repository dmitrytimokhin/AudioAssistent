"""Независимый разбор картинок: загрузка, подготовка фото, промпты, история разборов."""
import io

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from audio_assistant.client.bootstrap import build_pipeline
from audio_assistant.client.image_prep import prepare_image
from audio_assistant.client.readiness import ReadinessChecker
from audio_assistant.client.storage.image_store import PostgresImageStore
from audio_assistant.client.storage.repositories import PostgresHistoryRepository, PostgresRunStore, init_storage
from audio_assistant.endpoint import images_router as images_module
from audio_assistant.endpoint.assistant_router import assistant_router
from audio_assistant.endpoint.assistant_settings import AssistantSettings
from postgres_client.client.postgres_client import PostgresClient


class VisionLLM:
    def __init__(self):
        self.calls = []
        self.fail_on_images = 0

    def generate(self, system, user, json_schema=None, images=None):
        self.calls.append({"system": system, "user": user, "schema": json_schema, "images": images})
        if self.fail_on_images and images and len(images[0]) == self.fail_on_images:
            raise RuntimeError("модель упала")
        return f"на фото: {len(images or [])} шт."


def picture(size=(400, 300), fmt="JPEG", color="white", exif_orientation=None):
    image = Image.new("RGB", size, color)
    buffer = io.BytesIO()
    kwargs = {}
    if exif_orientation:
        exif = Image.Exif()
        exif[0x0112] = exif_orientation
        kwargs["exif"] = exif
    image.save(buffer, fmt, **kwargs)
    return buffer.getvalue()


@pytest.fixture
def env(tmp_path):
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    pg = PostgresClient(engine)
    init_storage(pg)
    settings = AssistantSettings(history_dir=tmp_path, images_dir=tmp_path / "images", image_max_side=512,
                                 image_max_per_request=3, image_max_upload_mb=1, attribution_method="rules")
    llm = VisionLLM()
    pipeline = build_pipeline(settings, llm, PostgresHistoryRepository(pg), PostgresRunStore(pg), None,
                              PostgresImageStore(pg))
    app = FastAPI()
    app.include_router(assistant_router)
    app.state.assistant_pipeline = pipeline
    original = images_module.get_assistant_settings
    images_module.get_assistant_settings = lambda: settings
    yield type("Env", (), {"client": TestClient(app), "llm": llm, "settings": settings, "pipeline": pipeline})
    images_module.get_assistant_settings = original


def upload(env, *names, **data):
    files = [("files", (n, picture(), "image/jpeg")) for n in names]
    return env.client.post("/assistant/images", files=files, data=data)


# ── подготовка фото ─────────────────────────────────────────────────────
def test_prepare_resizes_rotates_and_flattens():
    big = prepare_image(picture((2000, 1000)), 512)
    assert max(big.width, big.height) == 512 and big.height == 256 and big.original_bytes > 0
    # EXIF «повернуто на 90°» (6): фото с телефона ложится набок -> после поворота стороны меняются местами
    rotated = prepare_image(picture((400, 200), exif_orientation=6), 512)
    assert (rotated.width, rotated.height) == (200, 400)
    small = prepare_image(picture((100, 50)), 512)
    assert (small.width, small.height) == (100, 50)                 # маленькие не растягиваются

    rgba = io.BytesIO()
    Image.new("RGBA", (50, 50), (0, 0, 0, 0)).save(rgba, "PNG")      # прозрачный PNG -> белый фон, а не чёрный
    flat = Image.open(io.BytesIO(prepare_image(rgba.getvalue(), 512).data))
    assert flat.format == "PNG" and flat.convert("RGB").getpixel((10, 10)) == (255, 255, 255)

    with pytest.raises(ValueError, match="не читается"):
        prepare_image(b"not an image", 512, "x.jpg")
    with pytest.raises(ValueError, match="HEIC"):
        prepare_image(b"\x00\x00\x00\x18ftypheic", 512, "IMG_1.HEIC")


# ── файлы ───────────────────────────────────────────────────────────────
def test_upload_list_and_limits(env):
    assert upload(env, "доска.jpg", "слайд.JPG").json() == {"saved": ["доска.jpg", "слайд.JPG"]}
    assert {f["file_name"] for f in env.client.get("/assistant/images").json()} == {"доска.jpg", "слайд.JPG"}
    assert upload(env, "доска.jpg").status_code == 409
    assert upload(env, "доска.jpg", overwrite="true").status_code == 200

    assert env.client.post("/assistant/images", files=[("files", ("a.txt", b"x", "text/plain"))]).status_code == 422
    assert env.client.post("/assistant/images", files=[("files", ("bad.jpg", b"not image", "image/jpeg"))]
                           ).status_code == 422
    assert not (env.settings.images_dir / "bad.jpg").exists()                 # битый файл не сохраняется
    huge = io.BytesIO()
    Image.effect_noise((1200, 1200), 100).save(huge, "PNG")
    assert len(huge.getvalue()) > 1024 * 1024
    assert env.client.post("/assistant/images", files=[("files", ("big.png", huge.getvalue(), "image/png"))]
                           ).status_code == 413


# ── разбор ──────────────────────────────────────────────────────────────
def test_analyze_with_named_prompt_and_history(env):
    upload(env, "доска.jpg")
    r = env.client.post("/assistant/images/analyze", json={
        "files": ["доска.jpg"], "prompt_name": "tasks_from_photo", "tags": ["проект 3924", " ", "проект 3924"]})
    assert r.status_code == 200, r.text
    [item] = r.json()
    assert item["text"] == "на фото: 1 шт." and item["analysis_id"] and item["error"] is None

    call = env.llm.calls[-1]
    assert "Файл: доска.jpg. Теги: проект 3924." in call["user"] and "{file}" not in call["user"]
    assert "Ничего не домысливай" in call["system"]
    sent = Image.open(io.BytesIO(call["images"][0]))
    assert sent.size == (400, 300)                                         # меньше лимита 512 -> без изменений

    saved = env.client.get(f"/assistant/images/analyses/{item['analysis_id']}").json()
    assert saved["images"] == [{"file": "доска.jpg", "width": 400, "height": 300}]
    assert saved["tags"] == ["проект 3924"] and saved["prompt_name"] == "tasks_from_photo"
    assert saved["text"] == "на фото: 1 шт."


def test_analyze_with_own_prompt_schema_and_no_save(env):
    upload(env, "a.jpg")
    r = env.client.post("/assistant/images/analyze", json={
        "files": ["a.jpg"], "prompt": "Что на фото? {file}", "system": "Коротко.", "save": False,
        "json_schema": {"type": "object"}}).json()
    assert r[0]["analysis_id"] is None
    call = env.llm.calls[-1]
    assert call["user"] == "Что на фото? a.jpg" and call["system"] == "Коротко." and call["schema"] == {"type": "object"}
    assert env.client.get("/assistant/images/analyses").json() == []


def test_together_vs_each_modes(env):
    upload(env, "a.jpg", "b.jpg", "c.jpg")
    together = env.client.post("/assistant/images/analyze", json={"files": ["a.jpg", "b.jpg"], "prompt": "Опиши"}).json()
    assert len(together) == 1 and together[0]["files"] == ["a.jpg", "b.jpg"] and len(env.llm.calls[-1]["images"]) == 2

    env.llm.calls.clear()
    each = env.client.post("/assistant/images/analyze", json={
        "files": ["a.jpg", "b.jpg", "c.jpg"], "prompt": "Опиши", "mode": "each"}).json()
    assert [x["files"] for x in each] == [["a.jpg"], ["b.jpg"], ["c.jpg"]] and len(env.llm.calls) == 3
    assert len({x["analysis_id"] for x in each}) == 3


def test_each_mode_isolates_failures(env):
    env.client.post("/assistant/images", files=[("files", ("ok.jpg", picture((300, 300), color="red"), "image/jpeg")),
                                                 ("files", ("broken.jpg", picture((200, 200)), "image/jpeg"))])
    (env.settings.images_dir / "broken.jpg").write_bytes(b"corrupted")     # испортился уже на диске
    result = env.client.post("/assistant/images/analyze", json={
        "files": ["ok.jpg", "broken.jpg"], "prompt": "Опиши", "mode": "each"}).json()
    assert result[0]["error"] is None and result[0]["analysis_id"]
    assert "не читается" in result[1]["error"] and result[1]["analysis_id"] is None


def test_analyze_errors(env):
    upload(env, "a.jpg", "b.jpg", "c.jpg", "d.jpg")
    post = lambda **body: env.client.post("/assistant/images/analyze", json=body)  # noqa: E731
    assert post(files=[], prompt="x").status_code == 422
    assert post(files=["a.jpg"]).status_code == 422                                   # нет промпта
    assert post(files=["a.jpg"], prompt="x", prompt_name="describe").status_code == 422   # два промпта
    assert post(files=["a.jpg"], prompt="  ").status_code == 422
    assert post(files=["a.jpg"], prompt_name="нет_такого").status_code == 404
    assert post(files=["нет.jpg"], prompt="x").status_code == 404
    assert post(files=["../x.jpg"], prompt="x").status_code == 422
    too_many = post(files=["a.jpg", "b.jpg", "c.jpg", "d.jpg"], prompt="x")
    assert too_many.status_code == 422 and "mode=each" in too_many.json()["detail"]
    assert post(files=["a.jpg"], prompt="x", mode="bad").status_code == 422
    assert env.llm.calls == []                                                          # до модели не дошло


def test_analyze_upload_one_call(env):
    files = [("files", ("фото.jpg", picture(), "image/jpeg"))]
    first = env.client.post("/assistant/images/analyze-upload", files=files,
                            data={"prompt_name": "describe", "tags": "a, b"}).json()
    second = env.client.post("/assistant/images/analyze-upload", files=files, data={"prompt": "Что это?"}).json()
    assert first[0]["analysis_id"] and second[0]["files"] == ["фото_1.jpg"]            # то же имя -> суффикс
    assert {f["file_name"] for f in env.client.get("/assistant/images").json()} == {"фото.jpg", "фото_1.jpg"}
    assert env.client.get("/assistant/images/analyses", params={"tag": "b"}).json()[0]["id"] == first[0]["analysis_id"]


def test_history_filters_and_delete(env):
    upload(env, "a.jpg", "b.jpg")
    ids = {}
    for name, tag in (("a.jpg", "x"), ("b.jpg", "y")):
        ids[name] = env.client.post("/assistant/images/analyze", json={
            "files": [name], "prompt": "p", "tags": [tag]}).json()[0]["analysis_id"]
    listing = lambda **q: [a["id"] for a in env.client.get("/assistant/images/analyses", params=q).json()]  # noqa: E731
    assert listing() == [ids["b.jpg"], ids["a.jpg"]]
    assert listing(tag="x") == [ids["a.jpg"]] and listing(file_name="b.jpg") == [ids["b.jpg"]]

    assert env.client.delete(f"/assistant/images/analyses/{ids['a.jpg']}", params={"dry_run": True}).json()["dry_run"]
    assert len(listing()) == 2
    assert env.client.delete(f"/assistant/images/analyses/{ids['a.jpg']}").status_code == 200
    assert env.client.delete(f"/assistant/images/analyses/{ids['a.jpg']}").status_code == 404
    assert (env.settings.images_dir / "a.jpg").exists()                                  # файл остаётся
    assert env.client.delete("/assistant/images/analyses").status_code == 422
    assert env.client.delete("/assistant/images/analyses", params={"all": True}).json()["analysis_ids"] == [ids["b.jpg"]]
    assert env.client.get("/assistant/images/analyses/999").status_code == 404


def test_llm_journal_marks_images():
    from audio_assistant.client.llm_adapter import LoggedLLMClient

    class Log:
        records = []

        def record(self, call):
            self.records.append(call)

    llm = LoggedLLMClient(VisionLLM(), Log(), "m")
    llm.generate("s", "u", images=[b"1", b"2"])
    llm.generate("s", "u")
    assert Log.records[0].user_prompt.endswith("[изображений: 2]") and Log.records[1].user_prompt == "u"


def test_prompts_listing_and_vision_check(env):
    assert set(env.client.get("/assistant/prompts").json()["image_prompts"]) == {"describe", "extract_text", "tasks_from_photo"}

    def checker(capabilities):
        ollama = type("O", (), {"settings": type("S", (), {"model_name": "m", "ollama_url": "u"})(),
                                "connect": lambda s: True, "has_model": lambda s, n=None: True,
                                "capabilities": lambda s, n=None: capabilities})()
        return {r.name: r for r in ReadinessChecker(ollama=ollama, transcriber_backend="faster_whisper").run()}["vision"]
    assert checker(["completion", "vision"]).status == "ok"
    assert checker(["completion"]).status == "warn" and "vision" in checker(["completion"]).detail
    assert checker([]).status == "warn"
