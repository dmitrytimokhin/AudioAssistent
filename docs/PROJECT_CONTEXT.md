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
- `audio_assistant/endpoint/` — `assistant_{settings,entities,lifespan,router}.py` (`/assistant/*`): справочные, стадии (`transcribe`, `runs/{id}/{attribute,summarize,report,process}`), `GET runs[/{id}]`, сквозные (`process-file`, `process-audio`, `summarize`), удаление (`DELETE runs/{id}`, `DELETE runs?file_name&meeting_date`, `DELETE runs?all=true`, везде `dry_run`). Ошибки: LookupError/FileNotFound→404, ValueError→422, NotImplementedError (нет Postgres)→501.
- `audio_assistant/cli.py` — локальный запуск (Ollama + файловая история, без FastAPI/PG); `build_local_pipeline()` используется и ноутбуком.
- `prompts/<management|project_managers|team>/` — `system.md`, `person.md`, `examples.md`, `audience.yaml`. `config/team.yaml` — участники, алиасы.

## Принятые решения
1. Диаризации по голосу нет; метод — именные метки (имя в начале фразы = смена говорящего). Имена участников идут в Whisper `initial_prompt`.
2. Структуру итогового текста собирает код, LLM пишет только строку на сотрудника.
3. Алиасы уникальны (два Сергея — по фамилии), иначе ошибка при загрузке. Окончания имён только падежные (иначе «Катим» ≈ «Катя»).
4. Контекст для LLM = прошлый статус сотрудника из `person_statuses` (приоритет той же аудитории, только `has_data=true`).
5. Молчавший участник → «нет обновлений» без вызова LLM; ошибка LLM → `layout.error_text`, `has_data=false` (в историю как контекст не попадёт).
6. Запуск = встреча, не аудитория: `meeting_runs.status` проходит created→transcribed→attributed→summarized→reported|error. Результаты стадий: `transcripts` (raw/attributed), `speaker_utterances`, `person_statuses` (+audience), `reports` (+audience), `llm_calls`. Повтор стадии заменяет её результат (delete+insert по run_id[, audience]); успешная стадия снимает `error`. Сбой записи журнала LLM не роняет генерацию.
7. `save=false`: не пишется ни история, ни запуск (вызовы LLM всё же логируются без run_id).
8. Удаление (`RunStore.delete`): дочерние строки удаляются ЯВНО, не каскадом: у `person_statuses`/`llm_calls` FK = SET NULL, иначе статусы удалённой встречи остались бы «прошлым статусом». `all=true` чистит и строки без run_id (вызовы LLM при save=false); с фильтрами не сочетается; без параметров 422.
9. Имя записи `YYYYMMDDHHMMSS.WAV` → `recorded_at` и `meeting_date` (если дата не задана явно).
10. Разметка говорящих моделью (по умолчанию, `ASSISTANT_ATTRIBUTION_METHOD=llm`): модель НЕ переписывает текст и НЕ ставит границы сама. Код режет на пронумерованные фразы (в т.ч. после каждого имени) и находит имена; модель по каждой фразе с именем выбирает `next_speaker` ∈ {без изменений, ведущий, названные в ЭТОЙ фразе} (JSON-схема с enum, Ollama `format`); фразы без имён наследуют говорящего (правило «нет имени — предыдущий»). Решение по последней фразе чанка переносится в следующий чанк. Негодный ответ/сбой -> этот чанк размечается правилами (`fallback_chunks` в meta и в ответе ручки). Если в чанке нет имён, модель не вызывается. Что НЕ сработало (и почему так): 1) просить модель выписать границы смен («turns») — 4B выдумала участника, которого не называли, а потом зациклилась, выписав одного говорящего на каждую строку; 2) разрешать в схеме весь состав — то же. Промпт: `prompts/_tasks/speaker_attribution/`. Правило благодарности («спасибо, Рома» = говорит ведущий) — дополнение к правилам пользователя, оно в промпте и правится там.
11. Именные метки (правила) (по реальной записи пользователя): начало реплики — имя в начале фразы или в обороте передачи слова («начнем с Ромы», «переходим к Денису», «следующий Паша», «теперь Катя»); конец — «спасибо, <имя>» в именительном (дальше говорит ведущий/`speaker=NULL`). Whisper часто теряет пунктуацию или ставит запятую вместо точки, поэтому «спасибо, имя» и однозначные обороты («начнем с», «переходим к», «следующий», «слово») ищутся внутри фразы, а недописанная фраза переносится в следующий сегмент (время склеенной фразы приблизительно). Голое имя («Катя.») — только в начале фразы. Проверено на выводе mlx- и faster-whisper одной записи; они пунктуируют по-разному.

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
- [ ] НЕ проверено: `/assistant/process-audio` (upload), сборка Docker-образа; multi-host failover Postgres; Docker/Helm; Grafana-дашборд под наши метрики.
- Тестовая запись содержит заглушки («сделал то-то-то»), поэтому качество суммаризации на ней оценивать нельзя; 4B местами домысливает обрывки (Whisper услышал «в п» → модель написала «ПН»/«пенсионную систему»).

## Запуск и Docker
`Dockerfile` (python:3.12-slim + ffmpeg, зависимости из pyproject extra `faster`), `docker-compose.yml` (postgres:16 + app; Ollama на хосте через `host.docker.internal`; тома: `./data`, `./config`, `./prompts`, `hf_cache`, `pgdata`), `.env.example`, `scripts/process_audio.py` (stdlib-клиент ручек: транскрибация один раз, затем `/runs/{id}/process` на аудиторию, пишет txt в `data/output`). Подробно: `docs/RUNNING.md`. В контейнере Whisper = faster-whisper (CPU), на хосте Mac = mlx-whisper (Metal). **Образ НЕ собирался** (демон Docker не был запущен); проверены compose-синтаксис и тот же стек без контейнера.

## Статус разметки моделью
Проверено на qwen3.5:4b: обе реальные записи (mlx- и faster-whisper) и пример из 9 участников, в т.ч. при искусственно маленьком чанке (3 вызова = результат одного вызова, расхождения с правилами 1-2 слова). Одиночный вызов ~6-11 с. НЕ проверено: длинная запись на десятки минут, несколько реальных чанков на живой записи.

## Известные ограничения / идеи
- Качество 4B: «заполнители» и подмешивание фактов из few-shot (поэтому примеры нейтральные и выдуманные). Лечение — модель покрупнее (`ollama_model_name`).
- Реплики ведущего между говорящими («Теперь проект …») прилипают к предыдущему.
- Загрузка аудио в API без лимита размера; очередь/фоновые задачи для длинных записей не сделаны (запросы синхронные, часовая запись = минуты).
- Статус «нет данных…», сгенерированный моделью, считается `has_data=true` и попадёт в «прошлый статус»; можно фильтровать.
- Локальный Postgres для проверок: `LC_ALL=en_US.UTF-8 initdb` + `-c unix_socket_directories=''` (длинный путь сокета и локаль ломают запуск).
- Qwen Omni как «всё в одном» — обсуждалась, не внедрялась (см. историю чата): держать за портом `Transcriber`, как эксперимент.
