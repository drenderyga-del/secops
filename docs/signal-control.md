# Signal Control workspace

Signal Control — production workspace ScanOps поверх существующей доменной модели.
Это не отдельный mock и не замена backend-функций: интерфейс читает сохранённые
executions, jobs, pipelines, products, repositories, control bindings, scanner
runs, deliveries, worker heartbeats и integration health через read-only view
models. Детальные доменные операции открывают канонический operator workbench,
а конфигурация интеграций выполняется непосредственно в Signal Control через
существующие защищённые backend endpoints.

## Навигация

Все основные разделы имеют канонические product URL:

```text
/
/dashboard/duration
/executions
/logs
/logs?status=failed
/pipelines
/pipelines/{active_pipeline_id}
/config-scan
/iac
/ptai-scans
/coverage
/targets?level=product
/targets?level=repository
/targets/operator?level=product
/targets/operator?level=repository
/scanners
/scanners/operator
/integrations
/integrations?integration=asoc
/integrations?integration=gitlab
```

`/executions` показывает durable execution lifecycle. `/logs` — полный job
journal с category, repository, job, status и timeframe filters.
Executions, Job Journal/Failed Jobs, Active Pipelines, Config Guard findings,
PT AI scans и Repositories используют один нижний pagination control: диапазон,
`Page N of M`, переходы назад/вперёд и выбор `25`/`50`/`100` строк. `page` и
`page_size` сохраняются в URL, сбрасываются на первую страницу при изменении
фильтра или размера страницы и восстанавливаются через browser Back.
`/logs?status=failed` использует тот же контракт как recovery queue. Старые
`/next?view=...` остаются только permanent redirect
aliases и не являются product URLs. Старые `/classic...` адреса также являются
только permanent redirect aliases: второго пользовательского интерфейса в
продукте больше нет.

Для операций, которым нужен расширенный server-rendered workbench (запуск
сканов, ручная синхронизация и расширенная диагностика), основной экран
открывает тот же текстовый product shell по явным маршрутам
`/config-scan/operator`, `/iac/operator`, `/coverage/operator`,
`/scanners/operator` и `/targets/operator`.
`/overview/operator` остаётся только `308` compatibility alias на `/` и
не является отдельным workbench. На scanner
operator workspace сохранены существующие adapter connection, latest-run и
seven-day workload drill-downs; канонический `/scanners` остаётся runtime
registry. Target operator workspace сохраняет административные действия
Normalize paths, Cleanup duplicates и Delete product; канонические Products и
Repositories остаются рабочими read-only inventory. Pipeline и PT AI filters,
pagination, products, repositories и integration management работают
непосредственно на канонических React-экранах.
Сохранённые server-rendered snapshots этих коллекций доступны по явным
`/pipelines/operator`, `/ptai-scans/operator` и `/integrations/operator`; они не
владеют каноническими product URL.
Из `/pipelines` основной переход открывает внутренний detail route по
`ActivePipeline.id`; отдельное действие `GitLab ↗` сохраняет прямую внешнюю
ссылку. Detail показывает только сохранённые jobs, группирует их в фактические
stages и явно помечает `tracked_only` scope, если регистрация ограничивала
список job names.
GitLab's retained jobs snapshot does not include the declared CI stage list.
The detail view therefore groups stage names alphabetically for a stable
diagnostic view and does not present that grouping as execution order.
Detail и mutation routes продолжают использовать существующие backend-контракты.
`Products` является единственной основной вкладкой product inventory и ASOC
mapping. Синхронизация получает ASOC product cards и сопоставляет их с
monitored Product по нормализованным точным GitLab URL из описания ASOC
продукта (`.git`, регистр и завершающий `/` не меняют идентичность). Состав
ASOC-репозитория не используется как источник связи, а slug хранится только
для внешнего контекста. Число repositories в этой проекции — число GitLab
ссылок, найденных в описании, а не размер ASOC membership.
Registry сохраняет ASOC-only и ScanOps-only строки и различает `mapped`,
`partial`, `conflict`, `missing_target` и `no_repositories`. Карточки, удалённые
из ASOC, остаются в квартальной истории с исходной датой подключения, но не
считаются активными в каталоге. Конфликтные карточки не публикуют target-связи
как однозначный mapping. Ручная синхронизация (единственная, что может
подтвердить массовое удаление) требует same-origin UI Basic-auth; CI Bearer её
запускать не может. Плановая синхронизация в pipeline worker от UI-auth не
зависит и карточки никогда не удаляет: снимок с удалениями ждёт ручной
синхронизации. Последний успешный
pipeline — максимум terminal timestamp только среди retained
`ActivePipeline.status=completed`; более новый failed pipeline его не заменяет.
Переход открывает неизменённый `/pipelines?product=<local id>`.
Квартальная отчётность живёт в самом реестре. Дата подключения продукта —
дата создания ASOC-карточки (`created`) в `UI_TIMEZONE`, а не дата первой
синхронизации: карточка может появиться в ASOC раньше, чем ScanOps увидит её
GitLab-группу, и такое ожидание показывается отдельно (`waiting N days`,
`overdue` после `PRODUCT_SYNC_GRACE_DAYS`) — только пока ScanOps ни разу не
связал карточку (нет даты связи и нет связанной группы); связанная карточка,
позже деградировавшая, ожидающей не считается, а связанная до появления
отслеживания дат показывается как «Unknown · linked before date tracking». Срез `period=YYYY-QN&mode=new`
показывает продукты, подключённые в квартале, `mode=total` — все, подключённые
к концу квартала, включая удалённые из ASOC позже. Сводка статусов не зависит
от фильтра статуса, а XLSX (`/api/ui/products/export.xlsx`) строится из той же
проекции, что и таблица. Покрытие репозиториев берётся из GitLab-аудита всех
проектов группы, поэтому видны и репозитории, ни разу не приславшие security
job. Карточка открывает `/targets/products/asoc/{asoc_id}`, GitLab-группа без
карточки — `/targets/products/{id}`. Pipeline worker синхронизирует ASOC каждые
`ASOC_PRODUCT_SYNC_INTERVAL_MINUTES` минут и записывает исход последней
попытки; снимок, который удалил бы хоть одну карточку, он не применяет —
такой снимок ждёт ручной синхронизации.
`/reports`, `/reports/operator` и `/reports/export.xlsx` отвечают `308` на
реестр и его выгрузку, переводя `year` + `quarter` в `period`.
Canonical Overview обрабатывает timeframe на `/`; старый
`/overview/operator?timeframe=today` только сохраняет query при `308` redirect на
`/?timeframe=today`. Внутри Signal Control переход к Overview сохраняет
прежние timeframe/tool controls, а Back возвращает в Signal Control. Attention
queue Overview включает существующий
`IacService.dashboard()` backlog `critical + high`, без отдельной формулы.
Overview success rate считается только по terminal GitLab pipelines за
текущий UTC-день: `completed / (completed + failed)`. Числитель, знаменатель и
окно `Today` показаны рядом с процентом. Attention queue — отдельный текущий
backlog, который может включать нерешённые сигналы из более ранних периодов и
поэтому не является знаменателем этого процента.

Unfiltered legacy `/audit` по-прежнему перенаправляется на канонический
`/coverage`. Если legacy URL содержит `only_problems`,
`selected_product_ids`, `status`, `status_filter` или `run_id`, он
перенаправляется на `/coverage/operator` с теми же query parameters, потому
что только operator workspace реализует этот полный фильтрационный контракт.
Canonical IaC содержит прямой переход к полному списку
`/iac/repositories`.

Переходы внутри workspace обновляют browser history. Быстрый переход по
разделам открывается кнопкой `Quick action` или сочетанием `⌘K` / `Ctrl+K`.

На экранах до 780 px primary navigation закрепляется снизу как горизонтальная
лента с подписями; активный раздел автоматически попадает в видимую область.
Все видимые кнопки, ссылки и поля ввода получают touch-target не меньше 44 px.
В master-detail разделах `Executions` и
`Failed jobs` выбор строки раскрывает и прокручивает соответствующую detail
панель, а длинный список остаётся в собственном ограниченном scroll-контейнере.

`Products` и `Repositories` намеренно разделены:

- Карточка продукта (`/targets/products/asoc/{asoc_id}`) и страница группы
  (`/targets/products/{id}`) показывают ссылки ASOC-карточки и их состояние
  синхронизации, репозитории групп со статусом сканирования, pipelines и
  ASOC deliveries; operator workbench (`/targets/products/{id}/operator`)
  сохраняет обслуживание продукта и control bindings.
- `Browse repositories` сохраняет product filter в URL.
- Repository 360 показывает ownership, coverage, execution timeline,
  normalized artifacts, delivery evidence и producer versions.

В `Integrations` каталог и настройки остаются в одном визуальном и
навигационном контексте:

- выбор connector открывает правую settings-панель, не покидая `/integrations`;
- ASOC, PT AI и Mattermost показывают реальные поля конфигурации;
- GitLab показывает список connections и только один editor за раз;
- secret-поля являются write-only и никогда не возвращаются в UI API;
- `Save changes` оставляет панель открытой, после чего можно выполнить
  `Test connection`;
- при несохранённых изменениях закрытие и переключение требуют подтверждения;
- параметры `integration` и `instance` делают выбранное состояние доступным по
  прямой ссылке и сохраняют browser history.
- тест Mattermost явно предупреждает, что отправит одно видимое тестовое
  сообщение в настроенный канал.

## Live data contracts

Workspace использует следующие authenticated GET endpoints:

| Endpoint | Назначение |
|---|---|
| `/api/ui/overview` | operational signal, topology, attention queue, throughput |
| `/api/ui/executions` | durable execution lifecycle и artifacts с `25`/`50`/`100` pagination |
| `/api/ui/failed-jobs` | unresolved failed job evidence |
| `/api/ui/job-journal` | filtered job journal, `25`/`50`/`100` pagination, autocomplete options и summary |
| `/api/ui/pipelines` | active/observed GitLab pipeline lifecycle с `25`/`50`/`100` pagination |
| `/api/ui/pipelines/{active_pipeline_id}` | persisted pipeline header, completeness, ordered stages, jobs and external links |
| `/api/ui/config-guard` | configuration scans и server-paginated findings (`25`/`50`/`100`) |
| `/api/ui/iac` | IaC runs, severities и persisted scanner details |
| `/api/ui/ptai` | PT AI runs и фактический scanner state с `25`/`50`/`100` pagination |
| `/api/ui/coverage` | product audit coverage и control bindings |
| `/api/ui/products` | квартальный реестр ASOC-карточек, сводка статусов, GitLab-группы без карточки |
| `/api/ui/products/widgets/{asoc_id}`, `/api/ui/products/targets/{id}` | drill-down карточки или группы: ссылки, репозитории, pipelines, ASOC deliveries |
| `/api/ui/products/export.xlsx` | XLSX того же среза, что и таблица |
| `/api/ui/repositories` | repository inventory с batch-calculated health, server-side `q`/`product` filters и `25`/`50`/`100` pagination; bounded legacy `limit<=1000` остаётся совместимым |
| `/api/ui/repositories/{id}` | Repository 360 |
| `/api/ui/scanner-fleet` | worker heartbeats и capacity |
| `/api/ui/integrations` | безопасная конфигурационная метаинформация, connection counts и агрегированное connector health |

Коллекции обновляются автоматически. Loading, empty и error состояния являются
явными; отсутствие scanner evidence не превращается в выдуманный root cause.
Read-only карточки не маскируются под кнопки. Все действия, которых нет в
Signal Control API, ведут в существующий канонический или operator workbench,
включая `/targets/operator` для product maintenance.

Integration settings используют существующие POST-контракты
`/integrations/{key}/save`, `/integrations/{key}/test`,
`/integrations/{key}/disable` и
`/integrations/gitlab/instances/...`. Ответ обрабатывается внутри workspace:
перехода в legacy-интерфейс после действия нет. `/api/ui/integrations` не
возвращает PAT, tokens или сохранённые secret values.

## Проверка

Focused interaction suite:

```bash
QA_BASE_URL=http://127.0.0.1:18081 \
QA_WORKERS=2 \
npx playwright test qa/e2e/signal-control.spec.ts
```

Полный visual/accessibility audit разделов и integration settings запускается
на двух поддерживаемых desktop-ширинах: 1920 и 1440 px. Более узкие tablet и
mobile layouts не входят в продуктовый контракт:

```bash
QA_BASE_URL=http://127.0.0.1:18081 \
QA_ROUTE_TAGS=signal-control \
QA_WORKERS=2 \
QA_ARTIFACTS_DIR=qa/artifacts/signal-control \
npx playwright test qa/e2e/exploratory.spec.ts
```

Проверять нужно не только exit code, но и `findings.json`: release candidate не
должен содержать P0/P1 findings, runtime/network errors, horizontal overflow
или текст меньше 12 px.
