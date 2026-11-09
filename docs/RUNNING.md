# Запуск

Сценарий: кладёте запись в `data/audio/`, из терминала запускаете скрипт, в `data/output/` появляются txt:

```
data/audio/20261101180957.WAV   ->   data/output/20261101180957.dialogue.txt          диалог по говорящим
                                     data/output/20261101180957_management.txt         статус для руководства
                                     data/output/20261101180957_<аудитория>.txt        ...
```

## Выбор режима

Docker на Mac не видит GPU (Metal), а `mlx-whisper` без него не работает. Поэтому:

| | **A. PostgreSQL в Docker, сервис на хосте** | **B. Всё в Docker** |
|---|---|---|
| Whisper | `mlx-whisper` на GPU, быстро | `faster-whisper` на CPU, медленнее |
| Ollama | на хосте | на хосте (контейнер ходит на `host.docker.internal:11434`) |
| Когда | основной режим на вашем Mac | один `docker compose up`, переносимо на сервер/Linux |

Ollama в обоих режимах работает на хосте: в контейнере на Mac у неё не было бы GPU.

## Подготовка (один раз)
```bash
cp .env.example .env            # пароль БД, порт, модель
ollama pull qwen3.5:4b          # Ollama должна быть запущена на хосте
```

## Режим B: всё в Docker
```bash
docker compose up -d --build    # PostgreSQL + сервис
curl localhost:8005/assistant/health
docker compose logs -f app
```
Первая транскрибация скачает модель Whisper (`large-v3-turbo`, около 1,5 ГБ) в том `hf_cache`, дальше она берётся из кэша.

## Режим A: сервис на хосте
```bash
docker compose up -d postgres
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[mlx,dev]"
uvicorn app.main:app --port 8005      # настройки БД берутся из .env
```

## Ежедневная работа (оба режима)
```bash
python3 scripts/process_audio.py                                   # самая свежая запись, аудитория по умолчанию
python3 scripts/process_audio.py 20261101180957.WAV --audience management --audience team
python3 scripts/process_audio.py --audience all                    # все аудитории из prompts/
python3 scripts/process_audio.py --all-new                         # все записи, для которых ещё нет результата
python3 scripts/process_audio.py --replace                         # перед обработкой удалить прошлые запуски файла
python3 scripts/process_audio.py --attribution rules               # говорящих правилами вместо модели
```
Скрипту нужен только Python 3 (без venv). Whisper выполняется один раз на запись, остальные аудитории строятся по сохранённому транскрипту. Адрес сервиса: `--url` или `ASSISTANT_URL`.

То же без скрипта:
```bash
curl -X POST localhost:8005/assistant/process-file -H 'Content-Type: application/json' \
     -d '{"file_name":"20261101180957.WAV","audience":"management"}'
```

## Jupyter
`notebooks/pipeline_manual_test.ipynb` запускается на хосте (`.venv` с `jupyter`):
- **часть B** ходит по ручкам на `http://localhost:8005`, то есть работает с сервисом и в Docker, и на хосте; все стадии, просмотр запуска и удаление;
- **часть A** собирает пайплайн локально (нужны Ollama и `mlx-whisper` в `.venv`), не использует Postgres и не зависит от сервиса.

## Что менять и когда перезапускать
| Правка | Что делать |
|---|---|
| `prompts/*` | ничего, читаются на каждый запрос |
| `config/team.yaml` (алиасы, участники) | `docker compose restart app` (режим A: перезапустить uvicorn); затем стадия `attribute` по нужному `run_id` |
| код | `docker compose up -d --build` |

## Остановка и данные
```bash
docker compose down        # данные Postgres сохраняются в томе pgdata
docker compose down -v     # ... и стираются вместе с кэшем Whisper
```
Удалять отдельные запуски лучше ручками `DELETE /assistant/runs…` (README).

## Что проверено
- Тот же стек без контейнера: Postgres 16, сервис с `faster-whisper` (`large-v3-turbo`, CPU), скрипт `process_audio.py` на реальной записи, включая `--all-new`, `--replace`, `--audience all` и ошибки (нет файла, сервис недоступен).
- `docker compose config` (синтаксис compose).
- **Не проверено:** сборка образа и запуск контейнеров: Docker-демон на машине не был запущен. Первую сборку стоит выполнить и посмотреть `docker compose logs app`.
- CPU-Whisper по длинной записи не замерялся: на 65 с аудио весь прогон со статусами занял около 30 с, оценку для часовой записи нужно снять самому.

## Скорость транскрибации

Замер на записи 6 мин 15 с, `large-v3-turbo`, Apple M1 Pro / Docker Desktop с 8 vCPU:

| Где считается | Время | Комментарий |
|---|---|---|
| mlx-whisper на хосте (Metal) | **17 с** | `scripts/run_host.sh` |
| faster-whisper CPU на хосте, 4 потока | 53 с | 8 потоков медленнее (69 с), `beam_size=1` почти не ускоряет (54 с) и меняет разбивку |
| faster-whisper CPU в Docker | ~150 с | плюс разовая загрузка модели ~1,5 ГБ при первом запуске |

Для ежедневной работы на Mac: `scripts/run_host.sh` (Postgres в Docker, сервис на хосте). Контейнер `app` нужен для сервера без Metal.

В ноутбуке и скриптах транскрибация идёт в фоне (`POST /assistant/transcribe {"wait": false}`): ручка отвечает сразу,
прогресс и статус в `GET /assistant/runs/{id}` (`status: transcribing`, `progress` 0..1 для faster-whisper).
Один файл не запускается дважды (вернётся тот же `run_id`), разные файлы идут по очереди. Если сервис перезапущен посреди
транскрибации, запуск получает статус `error` и транскрибацию надо запустить заново.
