# ScanOps

Веб‑приложение для мониторинга GitLab CI/CD security‑пайплайнов, сгруппированное по продуктам и репозиториям. Backend — FastAPI, база — PostgreSQL, интерфейс — тёмная адаптивная верстка без внешних зависимостей.

Правила разработки, проверки и pull requests: [CONTRIBUTING.md](CONTRIBUTING.md).

## 🎯 Возможности

- **Онлайн-мониторинг пайплайнов** — статус security‑джоб сразу после выполнения или опроса GitLab.
- **Интерактивный дашборд** — графики и метрики (успешность, тренды за 3/7/30 дней и 12 месяцев).
- **Каталог продуктов и репозиториев** — агрегированные показатели + переход к журналу.
- **Логи неуспешных джоб** — отдельный раздел с фильтрами (репо, job, статус, период).
- **REST API с Bearer‑токеном** для интеграции из GitLab CI.
- **Опциональный poller** — регистрируем пайплайн один раз, дальше воркер сам дергает GitLab по PAT.
- **Docker/Compose** — приложение упаковано в образ, конфигурируется через `.env`.

## 🏗️ Архитектура

- **Backend**: FastAPI (Python 3.12)
- **DB**: PostgreSQL 16 + SQLAlchemy 2.x
- **Frontend**: React/Vite для всех канонических разделов Signal Control;
  Jinja2 + HTML/CSS/JS остаются для detail/operator workbench в том же
  product shell; `/classic...` — только compatibility redirects
- **Инфраструктура**: Docker + Docker Compose
- **Auth**: HTTP Basic (общая командная УЗ, `UI_AUTH_USERNAME`/`UI_AUTH_PASSWORD`)
  для UI и всех GET-эндпоинтов; Bearer‑токен (`PIPE_BEARER_TOKEN`) для CI.
  `/api/health` открыт для docker healthcheck. Если `UI_AUTH_*` не заданы —
  обычная auth-проверка выключена (warning в логах при старте), но ручная
  синхронизация ASOC (кнопка Synchronize ASOC — единственный путь, который может
  подтвердить массовое удаление карточек) остаётся закрыта без настроенной
  UI-учётной записи и принимает только same-origin Basic-auth оператора; CI
  Bearer её запускать не может. Плановая синхронизация в `pipeline_worker`
  работает независимо от UI-auth, пока ASOC подключён: она применяет новые и
  изменённые карточки, но никогда не удаляет карточки — если снимок ASOC
  что-то удаляет, он целиком ждёт ручной синхронизации. Отключается через
  `ASOC_PRODUCT_SYNC_INTERVAL_MINUTES=0`.

### Сервисы (один образ, разные команды)

| Сервис | Команда | Что делает |
|---|---|---|
| `app` | uvicorn (CMD по умолчанию) | UI + API, миграции при старте, config scan poller |
| `pipeline_worker` | `python -m app.workers.pipeline` | GitLab pipeline poller, алерты long-running джоб, плановая синхронизация ASOC-карточек, retention cleanup |
| `iac_worker` | `python -m app.iac_worker` | IaC сканирование (checkov) + загрузка в ASOC |

Каждый воркер пишет heartbeat в таблицу `worker_heartbeats` (виден в
`/api/health`) и в файл `/tmp/secpipe-heartbeat` (docker healthcheck).

### Миграции БД

Схема создаётся `Base.metadata.create_all` (новые таблицы), изменения
существующих таблиц — упорядоченные SQL-файлы в `migrations/`
(`YYYYMMDD_slug.sql`), применяются на старте `app` и трекаются в
`schema_migrations`. Файлы старше baseline (см. `app/migrations_runner.py`)
помечаются применёнными без выполнения.

### Audit digest

`AUDIT_DIGEST_ENABLED=true` (дефолт) — pipeline_worker раз в
`AUDIT_DIGEST_INTERVAL_DAYS` (7) шлёт в Mattermost сводку проблемных реп из
аудита (нет security-джоб / пайпы не работают / репа недоступна), сгруппировано
по статусу, со ссылками и ссылкой на полный список `/coverage`. Маркер последней
отправки — в таблице `app_state`, рестарты не спамят.

### ASOC enrichment (KEV/EPSS)

`ASOC_ENRICH_ENABLED=true` — раз в сутки pipeline_worker тянет каталоги
CISA KEV и FIRST EPSS, через `/scan_issues/filter/` (фильтр `cveIDs`,
`findingState=DETECTED`) находит в Vampy открытые issues по этим CVE и вешает
комментарий («CVE в KEV, эксплуатируется in the wild» / «EPSS 0.93»).
Повторных комментариев нет — пары (issue, kind) трекаются в
`asoc_enriched_issues`. Опция `ASOC_ENRICH_RAISE_SEVERITY=true` дополнительно
поднимает severity KEV-issues до CRITICAL (по умолчанию выключено — только
комментарии).

Если из сети недоступны cisa.gov / epss.cyentia.com — положи зеркала фидов
на диск и укажи `KEV_FEED_URL=/path/kev.json`,
`EPSS_FEED_URL=/path/epss.csv.gz` (file:// тоже работает).

### Retention

`SECPIPE_RETENTION_DAYS` (в compose по умолчанию 365, `0` = выключено) —
ежедневная очистка старых `job_reports`, завершённых `active_pipelines`,
старых config scan / IaC / audit ранов. Последний ран на product/scope
сохраняется всегда.

Перед публикацией изменений см. [SECURITY.md](SECURITY.md). Все примеры используют
демонстрационные адреса и требуют ваших собственных credentials.

## 🚀 Быстрый старт

### Требования

- Docker + Docker Compose
- Git

### Установка

1. Клонируем репозиторий:
   ```bash
   git clone https://github.com/drenderyga-del/secops.git
   cd secops
   ```
2. Экспортируем ключ:
   ```bash
   export PIPE_BEARER_TOKEN="my-secure-token"
   ```
3. Поднимаем сервис:
   ```bash
   docker compose up -d
   ```
4. Проверяем:
   - UI: http://localhost:8000
   - Swagger: http://localhost:8000/docs
   - Health: http://localhost:8000/api/health

### Локальный demo-стенд

Для UI-разработки и ручной проверки не нужны CI-образы, корпоративный registry,
GitLab, ASOC или production-данные. Отдельный compose собирает `Dockerfile.local`,
создаёт изолированную БД `scanops_demo` и автоматически наполняет её synthetic data:

```bash
docker compose -f docker-compose.demo.yml up -d --build
```

После завершения `seed` открой `http://127.0.0.1:18081` с учётными данными
`demo` / `demo`. Проверка готовности: `curl -fsS http://127.0.0.1:18081/api/health`.
Signal Control доступен на `http://127.0.0.1:18081`; его разделы, data
contracts, навигация и fallback-переходы описаны в
[docs/signal-control.md](docs/signal-control.md).
Все порты ограничены loopback; параметры можно переопределить переменными
`DEMO_APP_PORT`, `DEMO_UI_USERNAME`, `DEMO_UI_PASSWORD` и
`DEMO_POSTGRES_PASSWORD`. Остановить и удалить demo-данные:

```bash
docker compose -f docker-compose.demo.yml down -v
```

## 🔧 Настройки

### Переменные окружения

| Переменная | Описание | Значение по умолчанию |
|-----------|----------|------------------------|
| `DATABASE_URL` | Строка подключения к PostgreSQL | `postgresql://postgres:password@localhost:5432/security_dashboard` |
| `PIPE_BEARER_TOKEN` | Токен для API | **обязателен** |
| `APP_PORT` | Порт FastAPI | `8000` |
| `GITLAB_API_URL` | REST API GitLab по умолчанию | `https://gitlab.com/api/v4` |
| `GITLAB_PAT` | PAT/Project Token для poller; `Test connection` проверяет аутентификацию и доступ хотя бы к одному проекту | пусто = опрос отключён |
| `GITLAB_INSTANCES` | JSON‑массив `{host, api_url, token}` для нескольких GitLab | опционально |
| `PIPELINE_POLL_INTERVAL_SECONDS` | Интервал опроса | `300` |
| `PIPELINE_POLL_BATCH_SIZE` | Сколько пайпов за один цикл | `10` |
| `PIPELINE_MAX_AGE_SECONDS` | Когда активный пайп считается «протухшим» | `86400` |
| `INTEGRATION_TEST_TIMEOUT_SECONDS` | Timeout ручной проверки коннектора из UI | `10` |
| `MATTERMOST_WEBHOOK_URL` | Incoming webhook для операционных алертов; может быть сохранён через Integrations UI | пусто |
| `ALERT_SMTP_HOST` / `ALERT_EMAIL_TO` | Альтернативный SMTP-канал; параметры задаются только полной парой | пусто |
| `SECPIPE_JOB_LONG_ALERT_*` | Порог, периодичность и фильтр long-running job alerts | см. `.env.example` |
| `PTAI_SCAN_STATUS_*` | Опрос и алерты terminal/hung PT AI scans | см. `.env.example` |
| `PTAI_QUEUE_ALERT_*` | Порог и периодичность PT AI queue alerts | см. `.env.example` |
| `HEALTH_EXPECTED_WORKERS` | Worker heartbeat names, обязательные для healthy operational status | `pipeline_worker,iac_worker` |

`scripts/preflight_internal.py` проверяет production-секреты и конфигурацию
каналов без сетевых запросов. Mattermost может быть настроен через Integrations
UI, поэтому отсутствие env-backed webhook/SMTP даёт явное предупреждение, а не
ложный hard failure. После запуска оператор обязан получить
`operational_status=healthy` и выполнить `Test connection` для включённого
Mattermost-коннектора.

`PRODUCT_NESTED_NAMESPACES` задаёт top-level namespaces, в которых продуктом
считается вторая группа пути. По умолчанию список пуст: продукт определяется
первым сегментом. Включайте это правило явно для своей структуры GitLab.
При обновлении существующего deployment с двухуровневой атрибуцией настройте
список до запуска новой версии, чтобы сохранить идентичность продуктов.

### Универсальная доставка отчётов и внешние ИИ-агенты

Дополнительный versioned API позволяет настроить несколько независимых report
destinations: **Hexway ASOC/Vampy**, **DefectDojo** (SARIF) и
**Dependency-Track** (CycloneDX SBOM). Настройки и возможности каждого адаптера,
проверка связи и примеры CI-доставки — в [docs/asoc-adapters.md](docs/asoc-adapters.md).
Существующая интеграция ASOC/Vampy продолжает обслуживать product registry,
PT AI и IaC; новый API не переключает эти потоки автоматически.

Внешний ИИ-агент может брать явно разрешённые контексты failed jobs, возвращать
триаж и предлагаемый patch, а оператор — принять или отклонить предложение.
Для агента нужен отдельный `SCANOPS_AGENT_API_TOKEN`; CI-токен не подходит.
Данные не отправляются в LLM автоматически. Контракт и полный сценарий работы:
[docs/agent-api.md](docs/agent-api.md).

### Настройка интеграций из UI

GitLab, ASOC/Vampy, PT AI Engine и Mattermost можно подключать на странице
`/integrations`. URL и токены после сохранения берутся из PostgreSQL, поэтому
integration-specific секреты не требуется передавать каждому контейнеру через
`.env`. Старые переменные окружения остаются fallback до первого сохранения или
явного отключения коннектора в UI.

Несколько GitLab добавляются в UI отдельными записями через `Add GitLab`: для
каждой указываются понятное имя, REST API URL (`.../api/v4`) и собственный
токен. `GITLAB_INSTANCES` с JSON остаётся только обратносовместимым env-форматом;
в интерфейсе редактировать JSON не требуется. Сохранение, включение и
`Test connection` выполняются для конкретной записи GitLab: проверка одной
записи не меняет health-state другой. UI хранит отдельно latest check и last
successful check; token и encrypted payload в эти результаты не попадают.

`Connection enabled` управляет доступностью конкретного credential, а
`Pipeline polling` — его использованием worker-процессами. Для одного GitLab
host может быть несколько independently testable credentials, но polling
разрешён только у одного: без product-to-instance binding выбор двух токенов по
одному host был бы неоднозначным. Отключённые записи не попадают в runtime
poller configuration.

Env-backed GitLab остаётся read-only, пока оператор явно не подтвердит import
в UI-managed encrypted storage. Простое открытие или тестирование Integrations
не копирует env credentials в БД.

`Test connection` проверяет только аутентификацию и discovery доступного
проекта. Он не запускает GitLab CI Lint на случайно выбранном проекте и поэтому
не требует прав, которые не относятся к базовой read-connectivity. Config
Guard использует CI Lint отдельно, в контексте конкретного проекта; его
доступность зависит от роли токена и политики этого проекта. Ошибка такого
project-scoped вызова не отменяет уже подтверждённое чтение execution-сигналов.

Секретные поля шифруются перед записью в БД и никогда не возвращаются в HTML.
В production обязателен отдельный `INTEGRATION_CREDENTIALS_KEY`: compose
передаёт один и тот же ключ web-app и обоим worker-процессам. Это позволяет
ротировать `PIPE_BEARER_TOKEN` для CI без потери доступа к сохранённым
интеграциям. Для обратной совместимости локальный запуск без этого ключа может
использовать `PIPE_BEARER_TOKEN`. Смена ключа без предварительной миграции
сделает сохранённые токены нечитаемыми. Ключ не обязан иметь Fernet-формат:
создайте один случайный секрет командой `openssl rand -hex 32`, сохраните его
как protected CI/deployment variable и больше не генерируйте заново при
перезапуске. Если прежний ключ потерян, введите все credential-поля интеграции
заново; ScanOps заменит нечитаемый encrypted payload новым.

Для локальной проверки success/failure/recovery без внешних запросов
используйте контракт из [`qa/README.md`](qa/README.md). Он поднимает
детерминированные GitLab, ASOC, PT AI и Mattermost doubles и не заменяет
browser release gate.

Перед первой установкой отдельного ключа сохраните прежний release/config,
оставьте прежний `PIPE_BEARER_TOKEN` доступным только на время миграции и
сделайте проверяемый backup БД. В production используйте одобренный snapshot
с проверенным restore. Для локального Compose создайте mode-600 custom archive
и убедитесь, что PostgreSQL может прочитать его каталог:

```bash
docker compose stop app pipeline_worker iac_worker

umask 077
backup_dir=$(mktemp -d)
backup_file="$backup_dir/security_dashboard-before-credential-migration.dump"
docker compose exec -T postgres \
  pg_dump --username postgres --dbname security_dashboard \
  --format=custom --no-owner --no-privileges > "$backup_file"
chmod 600 "$backup_file"
test -s "$backup_file"
docker compose exec -T postgres \
  pg_restore --list < "$backup_file" >/dev/null
```

Не продолжайте, если archive пуст, имеет иные права или `pg_restore --list`
завершается с ошибкой. Сверьте snapshot/backup с той же БД
`postgres:5432/security_dashboard`, которую покажет migration dry-run.

Все процессы, способные сохранять настройки интеграций, должны оставаться
остановленными до завершения post-apply dry-run. Это не позволяет старому
процессу снова записать ciphertext под legacy key. Одноразовый контейнер
получает явный `DATABASE_URL` из Compose. Dry-run выводит redacted target,
счётчики и имена коннекторов, но не значения секретов:

```bash
docker compose run --rm --no-deps app \
  python scripts/migrate_integration_credentials.py || {
    echo "Dry-run blocked; do not apply or start services." >&2
    exit 1
  }
```

Если dry-run вернул `blocked`/код 2, не запускайте `--apply`: восстановите
правильный legacy key либо выберите документированный rollback/reset. После
успешного dry-run проверьте `database_target` и выполните оставшиеся команды
одним блоком. `set -e` намеренно запрещает запуск сервисов после любой ошибки:

```bash
set -e
# Скопируйте database_target из dry-run без изменений.
docker compose run --rm --no-deps app \
  python scripts/migrate_integration_credentials.py \
  --apply --confirm-target postgres:5432/security_dashboard

# Не запускайте сервисы, пока этот dry-run не покажет:
# status=ready_to_apply, remaining_irrecoverable=0 и ожидаемый database_target.
docker compose run --rm --no-deps app \
  python scripts/migrate_integration_credentials.py

docker compose up -d
```

После запуска откройте `/integrations` и выполните `Test connection` для
каждого включённого коннектора. Проверка не должна раскрывать сохранённый
секрет в HTML, логах или выводе команды.

После успешного `--apply` сохранённые credentials читаются только через
`INTEGRATION_CREDENTIALS_KEY`, а `PIPE_BEARER_TOKEN` можно ротировать. Если
apply или post-apply dry-run возвращает `blocked`/ненулевой код, сервисы не
запускайте: recoverable строки уже могли быть закоммичены, хотя CLI сообщил об
оставшихся irrecoverable строках.

Для rollback остановите сервисы, сохраните неисправную БД для разбора и
восстановите проверенный pre-migration backup. Затем разверните сохранённый
предыдущий release/config, который ещё поддерживает legacy storage через
`PIPE_BEARER_TOKEN`. Текущий Compose нельзя запускать без отдельного
`INTEGRATION_CREDENTIALS_KEY`; пустой или равный legacy token ключ также
отклоняется migration preflight. Для повторной попытки снова остановите старый
release, задайте новый отдельный ключ, отличный от прежнего bearer token, и
повторите dry-run.

В локальном Compose восстановление выполняйте только после явного решения об
откате и проверки пути/цели:

```bash
test -s "$backup_file"
docker compose stop app pipeline_worker iac_worker
docker compose exec -T postgres \
  pg_restore --clean --if-exists --single-transaction \
  --username postgres --dbname security_dashboard < "$backup_file"
```

Не удаляйте backup и не ротируйте прежний bearer token до успешного
post-apply dry-run и проверки всех коннекторов.

Намеренный reset — не rollback: он удаляет только одобренные нечитаемые
credentials, после чего оператор должен заново ввести их через UI.
Reset требует mode-600 manifest из отдельного dry-run. Manifest привязан к
redacted database target, именам коннекторов и SHA-256 текущего ciphertext;
изменение строки или цели БД блокирует весь reset:

```bash
recovery_dir=$(mktemp -d)
chmod 700 "$recovery_dir"

docker compose run --rm --no-deps \
  --user "$(id -u):$(id -g)" \
  -v "$recovery_dir:/recovery" app \
  python scripts/migrate_integration_credentials.py \
  --write-reset-manifest /recovery/reset.json

# Проверьте список irrecoverable connectors перед необратимым сбросом.
docker compose run --rm --no-deps \
  --user "$(id -u):$(id -g)" \
  -v "$recovery_dir:/recovery" app \
  python scripts/migrate_integration_credentials.py \
  --apply --confirm-target postgres:5432/security_dashboard \
  --reset-irrecoverable --reset-manifest /recovery/reset.json
```

Reset очищает только строки, точно совпавшие с manifest, сбрасывает прежний
health-check и отключает соответствующий коннектор.

Изменение настроек разрешено только при включённой UI-аутентификации
(`UI_AUTH_USERNAME` / `UI_AUTH_PASSWORD`). Сохранения, отключения и проверки
соединения фиксируются в `integration_config_events` без значений секретов.

### Единый lifecycle исполнений

GitLab pipelines/jobs, Config Scan, IaC и PT AI/ASOC записывают канонические
состояния в общие таблицы `executions` и `execution_steps`. Исходные таблицы
сканеров остаются операционными источниками, а общий lifecycle даёт один
репозиторный timeline независимо от типа сканера. Записи идемпотентны по
`(source, source_scope, correlation_key)`, шаги — по `(execution_id, source, external_id)`;
terminal-состояние не может откатиться обратно в running/queued.

Timeline доступен через `GET /api/repositories/{repository_id}/executions`.
Ответ содержит канонический status, timestamps, безопасный diagnostic context
и упорядоченные steps, artifacts, producer provenance и delivery attempts.
Успешный exit сканера сам по себе не означает успешную end-to-end доставку:
для IaC требуется непустой SARIF с SHA-256 и подтверждённая ASOC delivery.
`no_iac` остаётся успешным not-applicable outcome без artifact. Skipped/failed
upload завершает исходный execution ошибкой; ручной re-upload записывается как
новая delivery attempt и не переписывает terminal-историю исходного запуска.

Внешние jobs и adapters пишут scanner-neutral telemetry через versioned endpoint
`POST /api/v1/execution-events`. Envelope имеет source-scoped idempotency key
`(source, source_scope, event_id)`, correlation key и два времени: source
`observed_at` и серверное `received_at`. Точный replay возвращает существующее
событие, а повтор того же ключа с другим payload — `409 Conflict`. Историю можно
читать через `GET /api/v1/execution-events`; подробный контракт и примеры — в
[`docs/execution-event-ingestion-v1.md`](docs/execution-event-ingestion-v1.md).

### PT AI сканирование

PT AI сканы выполняются через Vampy/ASOC orchestration, secpipe-monitor в этом
flow не участвует. GitLab CI-джоба `ptai_scan` (см. `security_pipeline/controls/ptai.yml`)
делает один `POST /api/ext/v1/scan/` в Vampy с указанием профиля (по умолчанию
`security-pipeline-ptai`), repository slug и ветки. Vampy сам клонирует репу,
запускает PT AI scanner image, дожидается отчёта и кладёт findings в свой продукт.

Образ для PT AI scanner собирается из репы `ptai` и пушится в
`registry.example.com/scanners/ptai:<TAG>` — Vampy
ожидает образ по этому пути в Profile Configuration.

Secpipe-monitor отслеживает состояние async PT AI сканов отдельно от GitLab job:
pipeline poller читает trace завершившейся `ptai_scan`, сохраняет ASOC
`pipeline_id`/`task_id` в `ptai_scans`, после чего `pipeline_worker` опрашивает
Vampy/ASOC (`/ext/v1/scan/pipelines/{id}/status/`, fallback на background task).
Операционный экран — `/ptai-scans`; `/pipelines` показывает только состояние
GitLab jobs, а не фактический прогресс PT AI внутри ASOC.
ASOC repository ID, pipeline ID и task ID отображаются независимо: если
repository уже известен, ссылка на него остаётся доступной, а отсутствующий
pipeline/task ID показывается как явное состояние `No pipeline/task ID`.

Для ASOC polling нужны `HEXWAY_URL` и `HEXWAY_TOKEN`. Для дополнительного
engine-side статуса PT AI (`stage`, `progress`, errors) можно задать
`PTAI_URL` и `PTAI_TOKEN`; без них ASOC-статус всё равно обновляется.
Когда сохранены `project_id`, `branch_id` и `scan_result_id`, список, карточка и
алерт дают прямую ссылку на результат PT AI в web-интерфейсе:
`/ui/projects/{project_id}/branches/{branch_id}/scans/{scan_result_id}`.
Нестандартный web deep-link можно задать через `PTAI_RESULT_URL_TEMPLATE`
без отдельного public URL. PT AI алерты также содержат ссылки на GitLab job,
карточку скана в ScanOps и ASOC repository. Внешний адрес ScanOps задаётся через
`SECPIPE_PUBLIC_URL`; адрес ASOC UI выводится из `HEXWAY_URL` удалением
завершающего `/api`. Идентификатор заявки, возвращённый `/ext/v1/scan/`, не
используется как ASOC repository scan ID.

Для ручного IaC можно задать `IAC_HEXWAY_PRODUCT_ID`: если ASOC repository не
найдена ни по slug, ни по GitLab URL, ScanOps создаст её в этом выделенном ASOC
продукте и загрузит SARIF. Существующие ASOC repositories не перемещаются.

Операторский drill-down начинается с `/iac/repositories`: клик по репозиторию
открывает единый workspace с историей его IaC-запусков. В timeline можно выбрать
конкретный run и на том же экране увидеть этапы discover → Checkov → ASOC,
severity-сводку, execution metadata, ошибку и findings. Findings фильтруются по
severity и тексту без перезагрузки; выбранный активный run опрашивается до
завершения. Rescan относится к репозиторию, а re-upload и SARIF — к выбранному
запуску.

`pipeline_worker` также умеет мониторить очередь непосредственно в PT AI:
раз в `PTAI_QUEUE_ALERT_INTERVAL_SECONDS` он смотрит проекты/ветки в PT AI,
считает проекты со stage из `PTAI_QUEUE_ALERT_STAGES` (по умолчанию
`Enqueued,Queued`) и отправляет алерт, если их не меньше
`PTAI_QUEUE_ALERT_THRESHOLD` (по умолчанию `10`). Повторный алерт ограничен
`PTAI_QUEUE_ALERT_RESEND_MINUTES`; если очередь опустилась ниже порога, состояние
сбрасывается и новый всплеск алертит сразу.

### Интеграция в GitLab CI

Проще всего постить результат каждой security‑джобы:

```bash
curl -X POST "http://secpipe.example.com/api/ingest/job" \
  -H "Authorization: Bearer $PIPE_BEARER_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "gitlab": {
      "url": "$CI_PROJECT_URL",
      "project": {"path": "$CI_PROJECT_PATH"},
      "pipeline_url": "$CI_PIPELINE_URL",
      "ref": "$CI_COMMIT_REF_NAME"
    },
    "job": {
      "name": "$CI_JOB_NAME",
      "status": "$CI_JOB_STATUS",
      "finished_at": "$CI_JOB_FINISHED_AT"
    }
  }'
```

### Регистрация пайплайна + poller (PAT)

Если хотим дергать GitLab из сервиса самостоятельно, добавляем в первую джобу пайпа `before_script`:

```bash
curl -X POST "http://secpipe.example.com/api/pipelines/register" \
  -H "Authorization: Bearer $PIPE_BEARER_TOKEN" \
  -H "Content-Type: application/json" \
  -d "{
    \"project_id\": $CI_PROJECT_ID,
    \"pipeline_id\": $CI_PIPELINE_ID,
    \"project_path\": \"$CI_PROJECT_PATH\",
    \"project_url\": \"$CI_PROJECT_URL\",
    \"ref\": \"$CI_COMMIT_REF_NAME\",
    \"commit_sha\": \"$CI_COMMIT_SHA\",
    \"tracked_jobs\": [\"sast_scan\",\"sca_scan\",\"gitleaks_scan\"]
  }"
```

Далее воркер `app_poller` (см. docker-compose) раз в N секунд опрашивает GitLab по `GITLAB_PAT`, пишет статусы в `job_reports` и закрывает пайплайны как `completed/stale`. Включаем его вместе с основным сервисом:

```bash
docker compose up -d app app_poller
```

#### Скрипт для GitLab CI

Файл `scripts/register_pipeline.sh` уже делает все проверки:

```yaml
stages:
  - prepare
  - security

security:register:
  stage: prepare
  before_script:
    - bash scripts/register_pipeline.sh
  script:
    - echo "bootstrap security pipeline"
```

Он валидирует `SECPIPE_URL`, `PIPE_BEARER_TOKEN`, `CI_PROJECT_ID`, выбирает ветку (`CI_COMMIT_BRANCH` или `CI_COMMIT_REF_NAME`) и падает с понятным сообщением, если чего-то нет.

#### Два и более GitLab

Укажи `GITLAB_INSTANCES`:

```env
GITLAB_INSTANCES=[
  {"host":"gitlab.primary.local","api_url":"https://gitlab.primary.local/api/v4","token":"xxx"},
  {"host":"gitlab.partner.local","api_url":"https://gitlab.partner.local/api/v4","token":"yyy"}
]
```

Во время регистрации хост берётся из `project_url`. Poller подставляет соответствующий API URL + PAT; если хоста нет в списке — используется пара `GITLAB_API_URL/GITLAB_PAT`.

## 🛳️ Продовый деплой

`Dockerfile` собирает production-образ из публичных Node/Python/Alpine
образов и пакетных индексов. Частные registry, trust bundles и deploy keys
настраиваются в вашей инфраструктуре и не хранятся в этом репозитории.
`NODE_BASE_IMAGE`, `PYTHON_BASE_IMAGE`, `NPM_REGISTRY_URL` и `PIP_INDEX_URL`
можно переопределить build args. `Dockerfile.local` предназначен для demo и тестов.

1. **Собираем и пушим образ**
   ```bash
   export SECPIPE_IMAGE="registry.example.com/scanops/scanops:${CI_COMMIT_TAG}"
   docker build \
     --platform linux/amd64 \
     -t "${SECPIPE_IMAGE}" .
   docker push "${SECPIPE_IMAGE}"
   ```
2. **Создаём `.env`**
   ```env
   POSTGRES_PASSWORD=change-me
   PIPE_BEARER_TOKEN=super-secret-token
   INTEGRATION_CREDENTIALS_KEY=separate-stable-encryption-key
   UI_AUTH_USERNAME=scanops
   UI_AUTH_PASSWORD=change-me-too
   GITLAB_PAT=gitlab-pat-or-empty
   SECPIPE_IMAGE=registry.example.com/scanops/scanops:v1.3.8
   PIPELINE_POLL_INTERVAL_SECONDS=120
   PIPELINE_POLL_BATCH_SIZE=20
   PIPELINE_MAX_AGE_SECONDS=10800
   GITLAB_INSTANCES=[{"host":"gitlab.internal","api_url":"https://gitlab.internal/api/v4","token":"pat"}]
   ```
3. **Проверяем release-конфигурацию**
   ```bash
   python3 scripts/preflight_internal.py --env-file .env
   ```
   Preflight не выводит секреты и fail-closed проверяет обязательную auth,
   отдельный ключ шифрования, уникальность/минимальную длину секретов,
   immutable image tag и включённую TLS-проверку. Исключение для временного
   internal TLS допускается только явным флагом `--allow-insecure-tls`.
4. **Поднимаем стек**
   ```bash
   docker compose --env-file .env up -d
   ```

5. **Проверяем operational readiness**
   ```bash
   curl -fsS http://127.0.0.1:8081/api/health
   ```
   HTTP 200 подтверждает доступность web-app и БД. Поле
   `operational_status` должно быть `healthy`; `degraded_reasons` перечисляет
   отсутствующие/stale worker heartbeats, просроченный pipeline polling,
   выключенную UI-auth или отсутствующий credential-store key.

Поднимаются:
- `postgres` + volume `postgres_data` (порт доступен только на `127.0.0.1`)
- `app` (host-порт `8081`) — UI, API, миграции и Config Scan scheduler
- `pipeline_worker` — GitLab poller и операционные алерты
- `iac_worker` — Checkov/IaC scan и SARIF delivery

Config Scan exports лежат в именованном volume `config_scan_reports`, поэтому
ссылки на JSON/HTML отчёты переживают пересоздание `app`. В production
TLS-проверка ASOC/PT AI включена по умолчанию; отключать её можно только как
временное исключение для доверенного internal contour.

На странице Integrations кнопка `Test` у каждой записи GitLab проверяет
`/version` и project discovery. CI Lint остаётся отдельной project-scoped
операцией Config Guard и не выполняется как часть connection test.

### Connector и ScannerInstance registry

`GET /api/registry` и страница **Integrations** показывают read-only
operational snapshot: logical connector, его endpoint, credential reference,
live scanner/worker instances, версии/capabilities, health reason и last-seen
time. **Scanner fleet** использует только безопасное состояние подключения и
отдельно показывает latest run и workload за семь дней; endpoint и credentials
остаются только в Integrations. `IntegrationConfig` остаётся единственным
хранилищем конфигурации и зашифрованных credentials; registry сохраняет только
redacted `credential_ref` и никогда не отдаёт token либо encrypted payload.

Health states: `healthy`, `degraded`, `stale`, `unknown`, `disabled`. Disabled
connectors исключаются из success rate, а stale worker определяется по
heartbeat старше 30 минут. Registry предназначен для наблюдаемости: запуск,
остановка и retry сканов через него не выполняются.

### Перенос в внутренний репозиторий

Публичный GitHub содержит исходники, тесты и документацию. Для
внутреннего контура не нужно вручную выбирать файлы. Для обычного копирования
из текущего checkout можно запустить:

```bash
bash scripts/sync_internal_release.sh --remote origin \
  /absolute/path/to/internal/secpipe-monitor v1.2.0
```

Для релиза используй более безопасный вариант: он забирает точный Git tag,
создаёт временный detached worktree на этом revision и экспортирует только его
в чистый внутренний репозиторий. Локальные изменения и состояние `develop` в
копирование не попадут:

```bash
bash scripts/sync_internal_release.sh \
  /absolute/path/to/internal/secpipe-monitor \
  v1.2.0
```

По умолчанию source remote называется `personal-gitlab`; если на другом Mac
он назван иначе, передай его явно:

```bash
bash scripts/sync_internal_release.sh --remote origin \
  /absolute/path/to/internal/secpipe-monitor v1.2.0
```

Скрипт намеренно **не** делает commit/push внутреннего репозитория. После
проверки показанного diff выполни там явно:

```bash
git add -A
git commit -m "release: ScanOps v1.2.0"
git push
```

Это создаст commit в вашем deployment-репозитории. Публичный репозиторий
проверяет исходники и не публикует production images.

Скрипт копирует deployable исходники, `Dockerfile`, `docker-compose.yml`,
и миграции, но не трогает `.git` и `.gitlab-ci.yml` в destination.
Для legacy destination без export manifest сначала проверь чистый tracked
checkout и выполни:

```bash
bash scripts/sync_internal_release.sh --initialize-manifest --remote origin \
  /absolute/path/to/internal/secpipe-monitor v1.2.0
```

Эта явная инициализация принимает только пересекающиеся tracked paths;
untracked файлы source и destination отклоняются.
Production CI и секреты хранятся только в вашем deployment-репозитории. Тесты, docs, design previews, локальные `.env` и Git
metadata в export не попадают. Скрипт не использует глобальный `--delete`:
в destination ведётся `.scanops-export-manifest` с файлами, которыми владеет
source export. При следующем экспорте удаляются только прежние source-owned
файлы, которых больше нет в новом snapshot; `.env`, `.gitlab-ci.yml`,
`.scanops-release` и internal-only файлы сохраняются.

## 📊 Схема данных

### `products`
- `id`, `name`, `path`, `gitlab_base_url`

### `repositories`
- `id`, `product_id`, `path`, `last_report_at`, `overall_status`

### `job_reports`
- `id`, `repository_id`, `pipeline_url`, `job_name`, `job_status`, `finished_at`, `ref`

Дополнительно `job_report_details` (JSONB) хранит сырой payload для отладки.

## 🌐 Интерфейс

Основная навигация разделена по операторским целям: **Operations** содержит
Overview и Executions; **Security posture** — Coverage и Targets;
**Infrastructure** — Scanner fleet и Integrations.
Внутри Executions единый адаптивный переключатель открывает Jobs, Pipelines,
Config Guard, IaC и PT AI по каноническим URL.
Короткие страницы не дублируют структуру декоративным `Jump to`. На длинных
страницах используется единый sticky-блок **On this page**: активный раздел
обновляется при прокрутке, ссылки доступны с клавиатуры, а hash URL сохраняет
точную секцию после reload. Переключатели экранов и фильтры остаются отдельными
визуальными паттернами.

- Глобальная команда **Go to…** открывается кликом или `Ctrl/Cmd+K`, фильтрует
  канонические операторские экраны и поддерживает `↑`/`↓`, `Enter` и `Esc`.
- **`/` Overview** — live system pulse на реальных execution-данных: success
  rate terminal security jobs за выбранный период с явными numerator /
  denominator, отдельная метрика terminal pipeline failures, наблюдаемые шаги,
  нормализованные артефакты,
  компактный execution flow, отдельная all-time очередь внимания, throughput и
  median latency. Состояния `Live`, `Stale` и no-data не маскируются
  декоративной анимацией.
- **`/logs` Jobs** — общий execution journal; failures, требующие операторского
  действия, доступны как URL-фильтр `status=failed`. Старый `GET /failed`
  перенаправляется на этот фильтр с сохранением query string.
- **`/coverage` Coverage** — ожидаемые security controls и gaps; старый `GET /audit` сохраняется как compatibility redirect.
- **`/targets` Targets** — единый browser с уровнями Product target и Repository target; `/targets/products/{id}` открывает product aggregate, а `/targets/repositories/{id}` — Target 360 с покрытием контролями, свежестью данных, наблюдаемыми версиями, единым execution timeline, артефактами, подтверждением доставки и evidence входных событий. Старые `GET /products`, `/repositories` и `/repos/{id}` перенаправляются на canonical routes с сохранением query string; `/repos/{id}/jobs` остаётся Operations-маршрутом подробного журнала jobs.
- В списках repository targets действие **Open Target 360** открывает canonical
  target detail, а **View job logs** — repository-scoped evidence в
  `/repos/{id}/jobs`. ASOC monitoring links открывают только реально
  сопоставленный product target.
- **`/scanners` Scanner fleet** показывает registry live scanner/worker
  runtimes, а явное действие **Adapter runtime details** открывает
  `/scanners/operator`, где по каждому scanner adapter отдельно показаны
  Connection, Latest run и семидневное Workload window.
  Для GitLab connector поле `Last seen` означает последнее сохранённое job
  observation из успешного pipeline poll; неуспешная попытка poll его не
  обновляет. Время connection test показывается отдельно и не подменяет
  operational signal.
  Только источник с успешным latest connection test считается `Connected`;
  настроенный, но ещё не проверенный источник остаётся `Configured · untested`
  и не входит в `Adapters connected`. Причина setup/degraded/no-data называет
  источник, а явные ссылки ведут в Integrations без дублирования endpoint или
  credential settings.
- **`/targets?level=product` Products** — квартальный реестр продуктов из ASOC. Продукт считается подключённым, когда его карточка появилась в ASOC: дата подключения — дата создания карточки (`created`) в часовом поясе `UI_TIMEZONE`. ScanOps затем синхронизирует карточку с GitLab-группами из её Description и показывает статус синхронизации (`Synced`, `Partially synced`, `Conflict`, `Group not in ScanOps`, `No GitLab link`, `Waiting for sync`, `Removed from ASOC`), покрытие репозиториев группы сканированием (по GitLab-аудиту: scanned / not scanned / без pipelines за 30 дней) и успешность pipelines за 30 дней. Карточка, которую ScanOps ещё ни разу не связал (нет даты связи и нет связанной группы) и которая ждёт синхронизации дольше `PRODUCT_SYNC_GRACE_DAYS` (по умолчанию 3) с момента создания в ASOC, помечается `overdue`; связанная карточка, позже деградировавшая, остаётся в статусе внимания, но не считается ожидающей. Карточки, связанные до появления отслеживания дат, показываются как «Unknown · linked before date tracking». Фильтр `period=YYYY-QN` с режимами `mode=new` (подключены в квартале) и `mode=total` (подключены к концу квартала, включая удалённые позже) сохраняется в URL; тот же срез выгружается в XLSX (`/api/ui/products/export.xlsx`), поэтому выгрузка совпадает с экраном. Аудитория задаётся маркером `ScanOps-Scope: internal|external` в ASOC Description. Строка открывает `/targets/products/asoc/{asoc_id}` с GitLab-ссылками карточки и репозиториями по статусу; GitLab-группы без подключённой ASOC-карточки (включая группы в конфликте, с пометкой «ASOC card conflict») перечислены отдельно и открываются через `/targets/products/{id}`. Воркер синхронизирует ASOC каждые `ASOC_PRODUCT_SYNC_INTERVAL_MINUTES` минут (по умолчанию 60, `0` отключает) и никогда не удаляет карточки сам: снимок с удалениями ждёт ручной синхронизации. Старые `/reports`, `/reports/operator` и `/reports/export.xlsx` перенаправляют сюда, переводя `year` + `quarter` в `period`.
- **`/integrations` Integrations** — responsive connector registry with endpoint copy access and settings shown immediately after the selected connector.
- Широкие data tables в Target 360, Pipelines и PT AI сохраняют
  читаемую ширину колонок на поддерживаемом desktop (от 1440 px) и прокручиваются внутри
  подписанной focusable-области: сфокусируйте таблицу и используйте клавиши
  `←`/`→`. Длинные diagnostics/evidence по умолчанию показаны двухстрочным
  excerpt; `Expand full …` раскрывает полное значение, а `Copy …` копирует его
  без усечения.

## 🔌 API

### POST `/api/ingest/job`
Принимает результат джобы.

Заголовки:
- `Authorization: Bearer <token>`
- `Content-Type: application/json`

Пример тела:
```json
{
  "gitlab": {
    "url": "https://gitlab.example.com/project/repo",
    "project": {"path": "project/repo"},
    "pipeline_url": "https://gitlab.example.com/project/repo/-/pipelines/123",
    "ref": "main"
  },
  "job": {
    "name": "sast-security-scan",
    "status": "success",
    "finished_at": "2024-01-15T10:30:00Z"
  }
}
```

### GET `/api/health`
Простой health‑check для мониторинга.

### Expected security-control coverage

`Product` и `Repository` — внутренние targets. Каталог `SecurityControl`
нейтрален к scanner-ам: repository binding фиксирует ожидаемый control и его
наблюдаемую конфигурацию (`configured`, `missing`, `unknown` или `excluded`).
Coverage считается как **configured / expected**; `excluded` остаётся в audit
context, но не включается в expected. Неактивный control исключается из обоих
слагаемых coverage: его можно безопасно вывести из политики без искусственного
снижения показателя.

```text
POST /api/security-controls
PUT  /api/repositories/{repository_id}/controls/{control_key}
GET  /api/repositories/{repository_id}/controls
```

Mutating calls защищены `Authorization: Bearer $PIPE_BEARER_TOKEN`. GET отдаёт
coverage, scope, source, evidence и reason, чтобы оператор мог перейти от
агрегата в Products к диагностическому контексту без scanner-specific state.

### Versioned execution telemetry

```text
POST /api/v1/execution-events
GET  /api/v1/execution-events?source=gitlab_adapter&correlation_key=pipeline:42:100
```

POST поддерживает `execution.transition`, `step.transition`,
`artifact.created` и `delivery.transition` и проецирует их в общие
Execution/Step/Artifact/Delivery contracts. Для POST обязателен Bearer token.
Тело не должно содержать credential keys (`authorization`, `token`, `password`,
`secret`, `api_key`, `access_token`, `private_token`).

## 🗂️ Хранение и ретеншн

- `job_reports` содержит только метаданные (репозиторий, имя job, статус, ref, pipeline, таймстемпы).
- `job_report_details` хранит исходный JSON (можно чистить кроном):
  ```python
  from app.database import DatabaseService
  deleted = DatabaseService(db).purge_job_details_older_than(days=30)
  print(f"Removed {deleted} outdated payloads")
  ```
- Поддерживаются партиции по `finished_at`, пример:
  ```sql
  CREATE TABLE job_reports_2024_11
    PARTITION OF job_reports
    FOR VALUES FROM ('2024-11-01') TO ('2024-12-01');
  ```
- Скрипт миграции — `migrations/20241101_split_job_details.sql`.

## 🎨 UI

- Тёмная тема с пастельными акцентами
- Адаптивная сетка
- Гладкие графики без сторонних библиотек
- Цветовые бейджи статусов
- Автобоезаполнение фильтров + подсказки
- Tooltips и микроанимации

## 🛠️ Разработка

1. Ставим зависимости:
   ```bash
   pip install -r requirements.txt
   ```
2. Поднимаем Postgres:
   ```bash
   docker run -d --name postgres \
     -e POSTGRES_DB=security_dashboard \
     -e POSTGRES_USER=postgres \
     -e POSTGRES_PASSWORD=password \
     -p 5432:5432 postgres:16
   ```
3. Стартуем API:
   ```bash
   uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
   ```

Таблицы создаются автоматически; ordered SQL migrations из `migrations/`
применяются на старте и учитываются в `schema_migrations`.

### Локальный browser release gate

Полный seeded UI acceptance gate запускается одной командой:

```bash
npm run qa:release-gate
```

Команда поднимает одноразовый локальный stand, последовательно проверяет профили `empty`, `kitchen-sink` и `volume` на поддерживаемых desktop-ширинах 1920 и 1440 px и завершается ошибкой при любом P0/P1. Артефакты по умолчанию сохраняются вне репозитория в `${TMPDIR:-/tmp}/scanops-browser-gate/<UTC timestamp>/`. Контракт, зависимости и диагностические параметры описаны в [`qa/README.md`](qa/README.md).

## 🔒 Безопасность

- Сильные Bearer‑токены и хранение в секрет‑хранилищах
- HTTPS/Reverse‑proxy в бою
- Ограничение доступа к БД + сложные пароли
- `.env` и секреты не коммитим
- Файрволлы/SG для БД и API

## 📈 Мониторинг и эксплуатация

- Health‑endpoint для probe
- Логи через `docker compose logs`
- Регулярный vacuum/анализ БД
- Бэкапы тома `postgres_data`

## 🤝 Контрибьюции

1. Форк
2. Ответвление
3. Изменения + тесты
4. PR

## 📄 Лицензия

MIT (см. `LICENSE`).

## 🆘 Поддержка

1. Документация в README
2. Поиск по issues
3. Новый issue с подробностями

## 🔄 Обновления

1. `git pull`
2. Соберите образ из выбранного release tag в своей deployment-среде
3. `docker compose up -d --build`

---

**Security Dashboard** — мониторинг GitLab CI/CD security‑пайплайнов 🛡️


`ASOC_GITLAB_HOST_PREFIXES` при необходимости задаёт JSON mapping GitLab host →
существующий ASOC slug prefix. По умолчанию используется полный hostname.
Существующие deployments должны задать совместимые prefixes до обновления;
частные host mappings остаются за пределами публичного репозитория.
