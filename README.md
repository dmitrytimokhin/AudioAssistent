# AudioAssistent

FastAPI-сервис: аудио дейли → транскрипт (Whisper) → атрибуция говорящих по именам → статус по шаблону аудитории (Ollama). История статусов, журнал запусков и вызовов LLM — в PostgreSQL.

```
app/               точка входа FastAPI (app.main:app)
audio_assistant/   предметная логика: client/ (пайплайн, хранилище) + endpoint/ (роутер, настройки)
ollama_client/     клиент и эндпоинты Ollama (/ollama/chat, /models, /health)
postgres_client/   обёртка над Engine, multi-host failover, служебные эндпоинты (/postgres/*)
observability/     structlog (stdout, JSON), метрики Prometheus (/metrics), трассировка OTel
prompts/           промпты (руководство: prompts/README.md; создание и проверка: scripts/prompt_studio.py)
```

## Установка
```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[mlx,dev]"          # mlx-whisper для Apple Silicon
ollama pull qwen3.5:4b
```

## Запуск
Подробно (Docker, режимы, скрипт, Jupyter): **[docs/RUNNING.md](docs/RUNNING.md)**. Коротко: запись в `data/audio/`, затем `python3 scripts/process_audio.py`, результат в `data/output/*.txt`.

## Сервис и ручки
```bash
# PostgreSQL (локально): POSTGRES_HOST/PORT/USER/PASSWORD/DB, либо DB_CONNECTION_URL + DB_PASSWORD
uvicorn app.main:app --port 8005
```
Запись кладётся в `data/audio/` (имя `YYYYMMDDHHMMSS.WAV` задаёт дату и время встречи). Каждую стадию можно вызвать и перепроверить отдельно; результат каждой хранится в PostgreSQL под `run_id`:

| Стадия / эндпоинт | Что делает | Где лежит результат |
|---|---|---|
| `GET /assistant/audio` | список записей, время из имени файла | |
| `POST /assistant/audio` (multipart `file`, `overwrite`, `transcribe`) | загрузка записи в `data/audio`; `transcribe=true` сразу запускает стадию 1 | файл в `data/audio` |
| `POST /assistant/transcribe` `{file_name, wait}` | 1. Whisper, создаёт `run_id`; `wait=false` — сразу ответ, работа в фоне (статус и прогресс в `GET /runs/{id}`) | `transcripts` (raw) |
| `POST /assistant/runs/{id}/attribute` `{method: llm\|rules}` | 2. разбивка по говорящим (модель чанками или правила) | `transcripts` (attributed), `speaker_utterances` |
| `POST /assistant/runs/{id}/summarize` `{audience}` | 3. статус каждого сотрудника (LLM) | `person_statuses` |
| `POST /assistant/runs/{id}/report` `{audience}` | 4. итоговый текст (без LLM) | `reports` |
| `POST /assistant/runs/{id}/process` `{audience}` | стадии 2–4 подряд | |
| `POST /assistant/llm` `{prompt, run_id, source: raw\|dialogue, system?, transcript_text?, json_schema?}` | свой промпт поверх транскрипта (`{transcript}` в промпте — место подстановки); статусы и отчёты не меняет | `llm_calls` (audience=`custom`) |
| `GET /assistant/runs/{id}` | всё по запуску одним ответом | |
| `GET /assistant/tables`, `GET /assistant/tables/{имя}?run_id=&limit=` | сколько строк в таблицах / строки таблицы (только наши таблицы) для `pd.DataFrame` | |
| `PATCH /assistant/runs/{id}` `{kind, tags}` | тип встречи (`config/meeting_kinds.yaml`) и теги; меняются только переданные поля | `meeting_runs.kind/tags` |
| `GET /assistant/runs?kind=&tag=&date_from=&date_to=` | список запусков с фильтрами | |
| `POST /assistant/runs/{id}/analyze` `{prompt?, source}` | разбор встречи промптом её типа (`prompts/meetings/<тип>/`) | `analyses` |
| `POST /assistant/digests` `{name, date_from, date_to, kinds, tags, wait}` | сводка по встречам за период в таблицу: строки в Postgres и xlsx в `data/output/digests/` | `digests` |
| `GET /assistant/digests[/{id}[/xlsx]]`, `DELETE /assistant/digests[/{id}]` | список, строки и ход, скачать Excel, удалить (Postgres + файл) | |
| `POST /assistant/images` (multipart `files`), `GET /assistant/images` | загрузка фото в `data/images` / список | файлы в `data/images` |
| `POST /assistant/images/analyze` `{files, prompt_name \| prompt, mode, tags}` | независимый разбор фото промптом (vision): `together` — одним запросом, `each` — по записи на фото | `image_analyses` |
| `POST /assistant/images/analyze-upload` | то же с загрузкой файлов в одном вызове | `image_analyses` |
| `GET /assistant/images/analyses[/{id}]`, `DELETE /assistant/images/analyses[/{id}]` | история разборов / удаление записи (файл остаётся) | |
| `GET /assistant/prompts`, `GET /assistant/models` | типы встреч и промпты / проверка моделей и зависимостей | |
| `GET /assistant/runs` | последние запуски и их стадия | `meeting_runs` |
| `DELETE /assistant/runs/{id}` | удалить запуск по id | все таблицы запуска |
| `DELETE /assistant/runs?file_name=…&meeting_date=…` | по имени файла и/или дате (AND) | |
| `DELETE /assistant/runs?all=true` | очистить все таблицы целиком | |
| `POST /assistant/process-file` `{file_name, audience}` | все стадии одним вызовом | |
| `POST /assistant/process-audio`, `/summarize` | то же для загруженного файла / текста | |
| `/ollama/*`, `/postgres/*`, `/metrics`, `/healthz/*` | служебные | |

У всех `DELETE` есть `dry_run=true` (только посчитать строки по таблицам); без фильтров и `all=true` ручка вернёт 422. Удаляются запуск и вся связанная история, включая статусы (они не попадут в «прошлый статус» следующего дня).

Повторный вызов стадии заменяет её результат (например, `attribute` после правки алиасов в `config/team.yaml` или `summarize` после правки промпта) и не требует повторной транскрибации. Статусы и отчёты хранятся отдельно по аудиториям: одну встречу можно разобрать для руководства и для команды.

Без PostgreSQL: `ASSISTANT_HISTORY_BACKEND=file` (журналов запусков и LLM не будет).

## Локально без сервиса
```bash
python -m audio_assistant summarize examples/sample_transcript.txt --audience management --no-save
python -m audio_assistant run meeting.m4a --audience management --date 2026-11-14
```
Ноутбук `notebooks/pipeline_manual_test.ipynb`: часть A — локально без сервера, часть B — вызов ручек (стадии, просмотр запуска, удаление) против запущенного сервиса.

## Таблицы PostgreSQL (создаются при старте)
`meeting_runs` (стадия и ошибка запуска, время записи), `transcripts` (raw и attributed, JSONB), `speaker_utterances` (диалог по репликам: кто что сказал), `person_statuses` (статус сотрудника по аудиториям; источник «прошлого статуса» для LLM), `reports` (итоговый текст), `llm_calls` (промпты, ответ, ошибка, длительность).

## Как размечаются говорящие
`ASSISTANT_ATTRIBUTION_METHOD=llm` (по умолчанию): модель определяет автора каждой пронумерованной фразы по смыслу разговора. Ей доступны весь состав встречи, текущий фрагмент, предыдущие размеченные и следующие фразы (по `ASSISTANT_ATTRIBUTION_CONTEXT_UNITS`, по умолчанию 12 с каждой стороны). Смена автора возможна без имени: вопрос, ответ, перебивание и возврат к рассказу рассматриваются в контексте. Упоминание имени само по себе не означает смену говорящего. Если личность неизвестна, используется общая метка ведущего (`speaker=null`).

Текст собирается кодом без изменения слов. Модель выбирает только участников из состава встречи или ведущего. Размер текущего фрагмента задаёт `ASSISTANT_ATTRIBUTION_CHUNK_CHARS` (6000); контекст и список участников добавляются к нему, поэтому оставляйте запас в контекстном окне модели. Ответ проверяется: нужна ровно одна метка на каждую фразу. При сбое текущий чанк обрабатывается rules; номера таких чанков доступны в `fallback_chunks`. Промпты: `prompts/_tasks/speaker_attribution/`.

`rules` — осторожный запасной вариант: явная передача слова («Начнем с Ромы», «Теперь Катя»), отдельная именная метка («Роман.»), закрытие «Спасибо, Рома». Обращения («Паша, проверь») и сообщения («Катя закончила анализ») сохраняют текущего автора. Правила не умеют устанавливать автора перебивания по смыслу.

## Как говорить на встрече
Явная передача слова помогает разметке: «Начнем с Ромы», «Теперь переходим к Денису». LLM также анализирует обычный диалог и не требует имени перед каждой репликой. Голосовой диаризации нет: по одному тексту нельзя надёжно установить личность каждого перебивающего или разделить одновременную речь. Качество зависит от транскрипта и модели; неоднозначные фразы требуют проверки.

## Настройки (env)
`ASSISTANT_*` (аудитория по умолчанию, backend истории, `AUDIO_DIR`, Whisper), `OLLAMA_*` (`ollama_url`, `ollama_model_name`, `ollama_think`, ...), `POSTGRES_*` / `DB_*`, `LOG_LEVEL`, `LOG_FORMAT`, `SERVICE_NAME`.

Тесты: `pytest`. Контекст для другого агента: `docs/PROJECT_CONTEXT.md`.

## Типы встреч, разбор и сводки

Каждая запись помечается **типом** (`daily`, `one_to_one`, `business_customer`, `management`, `other`; список в
`config/meeting_kinds.yaml`) и произвольными **тегами**: `POST /assistant/transcribe {"file_name": ..., "kind": "daily",
"tags": ["проект 3924"]}`, позже `PATCH /assistant/runs/{id}`. Тип и теги лежат в `meeting_runs` и фильтруют список запусков и сводки.

**Куда писать свои промпты:**

| Что нужно | Где | Файлы |
|---|---|---|
| Разбор одной встречи своего типа | `prompts/meetings/<тип>/` | `system.md`, `user.md` |
| Сводка по многим встречам в Excel | `prompts/digests/<имя>/` | `digest.yaml` (колонки, типы встреч), `system.md`, `user.md` |
| Статусы дейли для руководства / РП / команды | `prompts/<аудитория>/` | как раньше |

Метки в `user.md` (`{transcript}`, `{date}`, `{participants}` ...) и формат `digest.yaml` описаны в
`prompts/meetings/README.md` и `prompts/digests/README.md`; там же стартовые шаблоны, которые вы перепишете под себя.
Файлы читаются на каждый запрос, перезапуск не нужен.

**Сводка за неделю в Excel:** `POST /assistant/digests {"name": "weekly_dailies"}` (период по умолчанию: 7 дней). Для каждой
встречи периода идёт запрос к модели, который возвращает строки таблицы; код склеивает их и пишет xlsx. В ноутбуке это раздел B10.

**Проверка при старте:** сервис проверяет Ollama и её модель, файлы модели Whisper (докачивает в фоне), ffmpeg, Postgres, папки и
промпты. Результат в логе (`[CHECK]`) и в `GET /assistant/models`; `ASSISTANT_STARTUP_CHECK=strict` запрещает старт при ошибке.

## Разбор картинок

Независимо от встреч: фото + ваш промпт -> текст (модель с vision, `qwen3.5:4b` подходит). Фото кладутся в `data/images/`
(`POST /assistant/images`), промпты пишутся в `prompts/images/<имя>/{system,user}.md` (метки `{file}`, `{count}`, `{date}`, `{tags}`)
или передаются в запросе текстом. Фото поворачивается по EXIF и уменьшается до `ASSISTANT_IMAGE_MAX_SIDE` (1568 px) перед отправкой.
Форматы: jpg, png, webp, bmp, gif (HEIC сначала конвертируйте). Ноутбук: раздел B11.
