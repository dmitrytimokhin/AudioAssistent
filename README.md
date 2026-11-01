# AudioAssistent

FastAPI-сервис: аудио дейли → транскрипт (Whisper) → атрибуция говорящих по именам → статус по шаблону аудитории (Ollama). История статусов, журнал запусков и вызовов LLM — в PostgreSQL.

```
app/               точка входа FastAPI (app.main:app)
audio_assistant/   предметная логика: client/ (пайплайн, хранилище) + endpoint/ (роутер, настройки)
ollama_client/     клиент и эндпоинты Ollama (/ollama/chat, /models, /health)
postgres_client/   обёртка над Engine, multi-host failover, служебные эндпоинты (/postgres/*)
observability/     structlog (stdout, JSON), метрики Prometheus (/metrics), трассировка OTel
prompts/           папки аудиторий; config/team.yaml — состав дейли
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
uvicorn app.main:app --port 8000
```
Запись кладётся в `data/audio/` (имя `YYYYMMDDHHMMSS.WAV` задаёт дату и время встречи). Каждую стадию можно вызвать и перепроверить отдельно; результат каждой хранится в PostgreSQL под `run_id`:

| Стадия / эндпоинт | Что делает | Где лежит результат |
|---|---|---|
| `GET /assistant/audio` | список записей, время из имени файла | |
| `POST /assistant/transcribe` `{file_name}` | 1. Whisper, создаёт `run_id` | `transcripts` (raw) |
| `POST /assistant/runs/{id}/attribute` `{method: llm\|rules}` | 2. разбивка по говорящим (модель чанками или правила) | `transcripts` (attributed), `speaker_utterances` |
| `POST /assistant/runs/{id}/summarize` `{audience}` | 3. статус каждого сотрудника (LLM) | `person_statuses` |
| `POST /assistant/runs/{id}/report` `{audience}` | 4. итоговый текст (без LLM) | `reports` |
| `POST /assistant/runs/{id}/process` `{audience}` | стадии 2–4 подряд | |
| `GET /assistant/runs/{id}` | всё по запуску одним ответом | |
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
`ASSISTANT_ATTRIBUTION_METHOD=llm` (по умолчанию): код режет транскрипт на пронумерованные фразы (после каждого имени тоже) и сам находит имена в любых падежах; модель по каждой фразе с именем решает, кто говорит дальше: названный, ведущий или без изменений. Нет имени — говорит предыдущий. Текст собирается кодом дословно, поэтому слова не теряются, а участника, которого не называли, модель назначить не может. Короткий транскрипт (до `ASSISTANT_ATTRIBUTION_CHUNK_CHARS`) — один вызов, длинный — чанки с переносом текущего говорящего. Если ответ по чанку негодный, этот чанк размечается правилами. Промпт: `prompts/_tasks/speaker_attribution/`. Метод можно переключать в ручке `attribute` и сравнивать.

## Как говорить на встрече
Передавайте слово по имени: «Начнем с Ромы», «Теперь переходим к Денису», «Следующий Паша», «Теперь Катя». Закрывайте реплику словами «Спасибо, Рома»: после этого говорит ведущий, пока не прозвучит новое имя (реплика без названного имени не припишется предыдущему человеку). Имя в обычной фразе («обсудил с Пашей», «спасибо Паше за помощь») смену не вызывает.

## Настройки (env)
`ASSISTANT_*` (аудитория по умолчанию, backend истории, `AUDIO_DIR`, Whisper), `OLLAMA_*` (`ollama_url`, `ollama_model_name`, `ollama_think`, ...), `POSTGRES_*` / `DB_*`, `LOG_LEVEL`, `LOG_FORMAT`, `SERVICE_NAME`.

Тесты: `pytest` (33 шт.). Контекст для другого агента: `docs/PROJECT_CONTEXT.md`.
