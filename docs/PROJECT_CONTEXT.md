# AudioAssistent: контекст проекта (для передачи другому агенту)

## Цель
Тимлид записывает встречи на диктофон. Локальный (macOS, Apple Silicon) сервис: аудио → транскрипт → атрибуция говорящих → статус дейли для руководства / РП / команды в заданном стиле.
Окружение: Ollama (`qwen3.5:4b`, также `qwen3:4b`, `phi3`), Whisper — mlx-whisper (в .venv НЕ установлен), PostgreSQL 16 (brew) для интеграционных проверок.

## Эталон выходного формата (management)
```
Дейли 14.11.2026
Проект 3924:
1. Гончар Роман - катим так как есть сейчас, ... + также в ПН поставили техническое ПСИ на индексер.
Проект 3641:
1. Агафонов Алексей - ...
```

## Структура репозитория (стиль промышленного сервиса пользователя: пакеты X_client/{client,endpoint})
- `app/main.py` — точка входа. `init_observability()` ПЕРВЫМ; порядок роутеров значим (postgres → ollama → assistant → metrics), lifespan роутера читает `app.state` предыдущего.
- `observability/` — взят из прод-сервиса. **Удалены каналы TSLG и DB** (папка `logging/channels`, настройки `tslg_*`/`db_*`, метрики `log_channel_*`, `LoggerNameExclusionFilter`, `start/stop_channel_senders`). Остались stdout-логирование (structlog JSON), Prometheus, OTel, middleware. Остались неиспользуемые `stream_*` метрики и дашборд `rag-service` (Redis-специфика исходного проекта), имена по умолчанию `rag-service` (переопределяются `SERVICE_NAME`).
- `ollama_client/` — `OllamaClient`: `connect`, `list_models`, `has_model`, `query(system,user)`, `chat(messages)`; `think`/`num_ctx` из настроек; метрика `track_external_service("ollama","chat")`. Эндпоинты `/ollama/{health,models,chat}`.
- `postgres_client/` — универсальный `PostgresClient` (Engine, `init_schema(Base)`, `session()`), multi-host failover. Из роутера удалены доменные `/load`,`/export`,`/reset` чужого проекта (`batch_model_client`).
- `audio_assistant/client/` — доменная логика (SOLID):
  `domain.py` модели, `ports.py` Protocol-интерфейсы (Transcriber, SpeakerAttributor, LLMClient, HistoryRepository, RunStore, CallLog + Null-реализации), `recordings.py` (время записи из имени файла YYYYMMDDHHMMSS, список/поиск аудио в `data/audio`), `transcription/` (mlx, faster), `speakers/name_cue.py` (правила), `speakers/units.py` (нарезка на фразы после имён, `NameIndex`), `speakers/llm_attributor.py` (разметка моделью чанками), `prompts.py` (профиль аудитории из `prompts/<имя>/`), `summarizer.py` (запрос LLM на каждого сотрудника; сбой одного не теряет остальных), `rendering.py`, `pipeline.py` (три уровня: чистые шаги; стадии `stage_*` по run_id с сохранением в RunStore; сквозные `run_audio/run_transcript`), `llm_adapter.py` (OllamaLLMAdapter, LoggedLLMClient — пишет каждый вызов в CallLog), `run_context.py` (ContextVar run_id/person/audience), `storage/` (модели и `PostgresHistoryRepository`/`PostgresRunStore`/`PostgresCallLog`), `bootstrap.py` (composition root), `history_file.py` (локальная история).
- `audio_assistant/endpoint/` — `assistant_{settings,entities,lifespan,router}.py` (`/assistant/*`): справочные (`prompts`, `models`), типы встреч (`PATCH runs/{id}`, фильтры `GET runs`), разбор встречи (`POST runs/{id}/analyze`, `client/analysis.py`), разбор картинок (`/images/*`, `client/images.py`, `image_prep.py`, `images_router.py`), сводки в Excel (`/digests`, `client/digest.py`, `xlsx_export.py`, `meetings_router.py`), загрузка (`POST audio`), свой промпт (`POST llm`, `client/prompt_runner.py`, `Pipeline.run_prompt`), стадии (`transcribe`, `runs/{id}/{attribute,summarize,report,process}`), `GET runs[/{id}]`, сквозные (`process-file`, `process-audio`, `summarize`), удаление (`DELETE runs/{id}`, `DELETE runs?file_name&meeting_date`, `DELETE runs?all=true`, везде `dry_run`). Ошибки: LookupError/FileNotFound→404, ValueError→422, NotImplementedError (нет Postgres)→501.
- `audio_assistant/cli.py` — локальный запуск (Ollama + файловая история, без FastAPI/PG); `build_local_pipeline()` используется и ноутбуком.
- `prompts/<management|project_managers|team>/` — `system.md`, `person.md`, `examples.md`, `audience.yaml`. `config/team.yaml` — участники, алиасы.

## Принятые решения
1. Диаризации по голосу нет; основной метод — контекстная разметка каждой фразы моделью; правила используются как fallback. Имена участников идут в Whisper `initial_prompt`.
2. Структуру итогового текста собирает код, LLM пишет только строку на сотрудника.
3. Алиасы уникальны (два Сергея — по фамилии), иначе ошибка при загрузке. Окончания имён только падежные (иначе «Катим» ≈ «Катя»).
4. Контекст для LLM = прошлый статус сотрудника из `person_statuses` (приоритет той же аудитории, только `has_data=true`).
5. Молчавший участник → «нет обновлений» без вызова LLM; ошибка LLM → `layout.error_text`, `has_data=false` (в историю как контекст не попадёт).
6. Запуск = встреча, не аудитория: `meeting_runs.status` проходит created→transcribed→attributed→summarized→reported|error. Результаты стадий: `transcripts` (raw/attributed), `speaker_utterances`, `person_statuses` (+audience), `reports` (+audience), `llm_calls`. Повтор стадии заменяет её результат (delete+insert по run_id[, audience]); успешная стадия снимает `error`. Сбой записи журнала LLM не роняет генерацию.
7. `save=false`: не пишется ни история, ни запуск (вызовы LLM всё же логируются без run_id).
8. Удаление (`RunStore.delete`): дочерние строки удаляются ЯВНО, не каскадом: у `person_statuses`/`llm_calls` FK = SET NULL, иначе статусы удалённой встречи остались бы «прошлым статусом». `all=true` чистит и строки без run_id (вызовы LLM при save=false); с фильтрами не сочетается; без параметров 422.
9. Имя записи `YYYYMMDDHHMMSS.WAV` → `recorded_at` и `meeting_date` (если дата не задана явно).
10. Обновлено 10.11.2026: LLM размечает автора САМОЙ каждой фразы (`decisions: [{unit, speaker}]`), включая фразы без имени. Доступны все участники из team.yaml и «ведущий» (также неизвестный автор, в Transcript `None`). Упоминание имени не ограничивает выбор автора и не вызывает автоматическую смену. Код сохраняет текст, проверяет диапазоны, повторы, полноту и допустимые имена. Некорректный ответ/сбой → только текущий чанк на rules (`fallback_chunks`). Старый протокол `next_speaker` заменён: он не позволял размечать перебивания и возвращение без имени.
11. Контекст: текущий чанк до `ASSISTANT_ATTRIBUTION_CHUNK_CHARS` (6000 символов), плюс до `ASSISTANT_ATTRIBUTION_CONTEXT_UNITS` (12) предыдущих размеченных и следующих неразмеченных фраз. Контекстные фразы не входят в ответ. Вызов выполняется и для чанков без имён. Промпты: `prompts/_tasks/speaker_attribution/`. Лимит чанка не включает промпт, roster и контекст — нужен запас num_ctx.
12. Rules: отдельная метка («Роман.»), явные обороты передачи слова и «спасибо, имя». Голое обращение с продолжением («Паша, проверь») и сообщение о коллеге («Катя закончила») не переключают автора. Благодарность «спасибо Рома за помощь» не закрывает реплику. Rules не понимают перебивания; голосовой диаризации по-прежнему нет.

Изменения 10.11.2026 проверены автоматическими тестами (100 passed), включая контракт ответа, контекст между чанками, fallback и сохранение текста. Новая схема НЕ проверена на живой Ollama и реальной длинной записи; прежние результаты интеграции ниже относятся к старой схеме.

## Исправления в образцах пользователя (проверить, что устраивают)
- `OllamaClient`: `if e.response` → `is not None` (bool(Response) False для 4xx/5xx, статус терялся); `OllamaSettings.model_name` alias был `phi` → `ollama_model_name`, дефолт `qwen3.5:4b`; таймаут 300 с; ответ чистится от `<think>`.
- `postgres_connection`: драйвер зафиксирован `postgresql+psycopg2://` (SQLAlchemy 2.1 для `postgresql://` берёт psycopg3, которого нет).
- Синхронный `requests` в async-эндпоинтах Ollama вынесен в `run_in_threadpool`.

## Статус проверок
- [x] `pytest` — 49 тестов (name-cue вкл. дословный вывод Whisper, пайплайн, ошибки, хранилище на SQLite, API через TestClient вкл. все стадии).
- [x] Интеграция: uvicorn + Postgres 16 + реальный `qwen3.5:4b`, 2 дня подряд: 2 запуска, 18 вызовов LLM без ошибок, прошлый статус попадает в промпт 2-го дня, метрики Prometheus (`http_requests_total`, `external_service_availability`) обновляются.
- [x] Ноутбук `pipeline_manual_test.ipynb` (часть A локально, часть B — ручки) выполнен целиком против живого сервиса; CLI выполняется. Удаление проверено на реальном Postgres (по id, дате, имени файла, all, 404/422).
- [x] Реальная запись `data/audio/20261101180957.WAV` (65 с, 16 кГц моно): mlx-whisper (`whisper-large-v3-turbo`, скачивается с HF при первом запуске) → 4 сегмента → 6 реплик с правильными говорящими → статусы → отчёт, всё в Postgres; CLI `run` на этом же файле.
- [x] `faster_whisper` (large-v3-turbo, CPU) проверен на реальной записи; для него нужен `av<17` (PyAV 17+ убрал `metadata_errors`, который передаёт faster-whisper 1.2.1) — закреплено в pyproject.
- [ ] НЕ проверено на реальном сервере: `/assistant/process-audio` и `POST /assistant/audio` (upload; в тестах проверены на фейковом Whisper), сборка Docker-образа; multi-host failover Postgres; Docker/Helm; Grafana-дашборд под наши метрики.
- Тестовая запись содержит заглушки («сделал то-то-то»), поэтому качество суммаризации на ней оценивать нельзя; 4B местами домысливает обрывки (Whisper услышал «в п» → модель написала «ПН»/«пенсионную систему»).

## Запуск и Docker
`Dockerfile` (python:3.12-slim + ffmpeg, зависимости из pyproject extra `faster`), `docker-compose.yml` (postgres:16 + app; Ollama на хосте через `host.docker.internal`; тома: `./data`, `./config`, `./prompts`, `hf_cache`, `pgdata`), `.env.example`, `scripts/process_audio.py` (stdlib-клиент ручек: транскрибация один раз, затем `/runs/{id}/process` на аудиторию, пишет txt в `data/output`). Подробно: `docs/RUNNING.md`. В контейнере Whisper = faster-whisper (CPU), на хосте Mac = mlx-whisper (Metal). **Образ НЕ собирался** (демон Docker не был запущен); проверены compose-синтаксис и тот же стек без контейнера.

## Статус разметки моделью
Проверено на qwen3.5:4b: обе реальные записи (mlx- и faster-whisper) и пример из 9 участников, в т.ч. при искусственно маленьком чанке (3 вызова = результат одного вызова, расхождения с правилами 1-2 слова). Одиночный вызов ~6-11 с. НЕ проверено: длинная запись на десятки минут, несколько реальных чанков на живой записи.

## Известные ограничения / идеи
- Качество 4B: «заполнители» и подмешивание фактов из few-shot (поэтому примеры нейтральные и выдуманные). Лечение — модель покрупнее (`ollama_model_name`).
- Текстовая атрибуция неоднозначна без голосовых признаков; LLM может ошибаться в авторе перебивания.
- Загрузка аудио в API без лимита размера; очередь/фоновые задачи для длинных записей не сделаны (запросы синхронные, часовая запись = минуты).
- Статус «нет данных…», сгенерированный моделью, считается `has_data=true` и попадёт в «прошлый статус»; можно фильтровать.
- Локальный Postgres для проверок: `LC_ALL=en_US.UTF-8 initdb` + `-c unix_socket_directories=''` (длинный путь сокета и локаль ломают запуск).
- Qwen Omni как «всё в одном» — обсуждалась, не внедрялась (см. историю чата): держать за портом `Transcriber`, как эксперимент.

## Типы встреч, разбор, сводки, проверка моделей (добавлено позже)
- `meeting_runs.kind` (тип из `config/meeting_kinds.yaml`, `None` = не указан; опечатка -> 422) и `tags` (JSON, свободные). Старые БД
  получают колонки при старте (`_add_missing_columns`, ALTER TABLE ... ADD COLUMN).
- Таблицы `analyses` (разбор встречи, unique run_id+prompt_name) и `digests` (сводка: статус, строки, порядок колонок `headers`, имя xlsx).
- Промпты: `prompts/meetings/<тип>/{system,user}.md` (разбор), `prompts/digests/<имя>/{digest.yaml,system,user}.md` (сводка).
  Метки в `user.md` подставляются `str.replace` (`meeting_text.fill`), не `format`; нет `{transcript}` -> текст дописывается.
- Сводка = map: запрос к LLM на каждую встречу с `json_schema` {"rows":[{колонка: str}]}, склейка кодом, xlsx (`openpyxl`).
  Сбой одной встречи не роняет сводку (статус встречи «ошибка»/«частично»/«пропущена»). Длинный транскрипт режется `split_text`
  (`ASSISTANT_ANALYSIS_CHUNK_CHARS`), иначе Ollama молча обрезает контекст.
- `ReadinessChecker` (`client/readiness.py`): Ollama+модель, Whisper (файлы в кэше HF, `preload()` докачивает в фоне без загрузки в память),
  ffmpeg, Postgres, папки, промпты для типов. При старте лог `[CHECK]`; `ASSISTANT_STARTUP_CHECK=off|warn|strict`; `GET /assistant/models`.
- Проверено вживую (Mac, Metal + Ollama qwen3.5:4b + Postgres 16): старт-проверки, миграция колонок на БД со старыми данными,
  запись -> тип -> разметка -> разбор -> сводка с xlsx. Не проверено: сводка на неделю реальных встреч разных людей, Docker-образ с openpyxl.

## Разбор картинок (vision), независимый от встреч
- Порт `LLMClient.generate(system, user, json_schema=None, images=None)`; `OllamaClient.query(images=[base64])` кладёт `images` в user-сообщение.
  `LoggedLLMClient` пишет в журнал число картинок (`[изображений: N]`), байты не хранит. Узкие LLM-заглушки без `images` работают: параметр передаётся, только если задан.
- Файлы в `data/images` (`ASSISTANT_IMAGES_DIR`), перед отправкой `prepare_image`: EXIF-поворот, уменьшение до `ASSISTANT_IMAGE_MAX_SIDE`, прозрачность -> белый фон.
  Лимиты: `ASSISTANT_IMAGE_MAX_PER_REQUEST` (8), `ASSISTANT_IMAGE_MAX_UPLOAD_MB` (25). HEIC не читается -> понятная ошибка.
- Промпты `prompts/images/<имя>/{system,user}.md` либо свой `prompt` в запросе (ровно одно из двух). `mode=together|each`.
- Таблица `image_analyses` (картинки с размерами, итоговый промпт, модель, теги, текст); без привязки к `meeting_runs`.
- Проверка при старте `vision` (Ollama `/api/show` -> capabilities). Проверено вживую на `qwen3.5:4b`: печатный русский текст читается, одна ошибка в имени («Ката»).


## Промпт-лаборатория (prompt studio)
- `audio_assistant/client/prompt_lab.py`: `PromptLab` (каталог, `new` из шаблонов `prompts/_templates/<вид>/`, `check`, `preview`;
  для meeting регистрирует тип в `config/meeting_kinds.yaml`), `PromptTrial` (прогон версий промпта на реальной встрече/фото без записи в БД).
- CLI: `scripts/prompt_studio.py list|new|check|preview`; ноутбук: часть C. Руководство: `prompts/README.md`. Тесты: `tests/test_prompt_lab.py`.
- Правка/удаление: `PromptLab.read/save/history/restore/delete/undelete` (история в `prompts/.history/`, корзина `prompts/.trash/`); форма в ноутбуке: `prompt_lab_ui.PromptStudioUI` (ipywidgets, dev-зависимость).
