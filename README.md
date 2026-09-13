# Remnawave Routing Updater

Безопасно синхронизирует Happ deeplink из GitHub с Remnawave. Основной режим
обновляет заголовок `routing` только внутри выбранного Response Rule и не
перезаписывает остальные правила или заголовки.

## Защитные проверки

Перед каждым PATCH сервис:

1. загружает deeplink и декодирует Base64/JSON;
2. проверяет обязательные поля и HTTPS-хосты геобаз;
3. проверяет доступность `geoip.dat` и `geosite.dat`;
4. повторно читает актуальные настройки Remnawave;
5. требует увеличения `LastUpdated` и разрешает только явно заданные пары
   переименования профиля;
6. сохраняет полную резервную копию текущих настроек;
7. меняет только заголовок `routing`;
8. повторно читает настройки и проверяет результат.

По умолчанию включён `DRY_RUN=true`: сервис только показывает планируемое
изменение.

## Установка

```bash
git clone https://github.com/indie-master/Remnawave-Routing-update.git
cd Remnawave-Routing-update
cp .env.example .env
nano .env
mkdir -p backups
docker compose up -d --build
docker compose logs -f routing-updater
```

Пример для локальной панели Remnawave:

```env
REMNA_BASE_URL=http://remnawave:3000/api
REMNA_TOKEN=replace_with_api_token

UPDATE_TARGET=response-rule
RESPONSE_RULE_NAME=Happ
GITHUB_RAW_URL=https://raw.githubusercontent.com/indie-master/happ-routing/main/HAPP/DEFAULT.DEEPLINK

DRY_RUN=true
VALIDATE_GEO_URLS=true
ALLOW_PROFILE_RENAME=false
ALLOWED_PROFILE_RENAMES=RoscomVPN:swiftless-routing
CRON_SCHEDULE=30 4 * * *
TZ=UTC
```

Контейнер подключается к существующей сети `remnawave-network`. Если панель
имеет другое имя контейнера или порт, измените `REMNA_BASE_URL`.

## Canary-порядок

1. Создать отдельный Response Rule `Happ-canary` и назначить его только своей
   тестовой подписке.
2. В `.env` выбрать canary-правило и источник, оставив `DRY_RUN=true`:

   ```env
   RESPONSE_RULE_NAME=Happ-canary
   GITHUB_RAW_URL=https://raw.githubusercontent.com/indie-master/happ-routing/main/HAPP/CANARY.DEEPLINK
   DRY_RUN=true
   ```

3. Убедиться в логах, что профиль и обе базы проходят проверку.
4. Если тестовое правило скопировано с production, разрешить только конкретную
   смену имени и включить запись:

   ```env
   ALLOWED_PROFILE_RENAMES=RoscomVPN:swiftless-routing-canary
   DRY_RUN=false
   ```

5. После клиентских тестов вернуть production-правило и источник:

   ```env
   RESPONSE_RULE_NAME=Happ
   GITHUB_RAW_URL=https://raw.githubusercontent.com/indie-master/happ-routing/main/HAPP/DEFAULT.DEEPLINK
   ALLOWED_PROFILE_RENAMES=RoscomVPN:swiftless-routing
   ALLOW_PROFILE_RENAME=false
   DRY_RUN=false
   ```

6. Перезапустить только updater:

   ```bash
   docker compose up -d --build routing-updater
   docker compose logs --tail=100 routing-updater
   ```

Production deeplink использует `happ://routing/onadd/`, чтобы профиль с новым
именем `swiftless-routing` стал активным после успешной загрузки геобаз. Общий
переключатель `ALLOW_PROFILE_RENAME=true` для этой миграции не нужен.

## Переменные окружения

| Переменная | По умолчанию | Назначение |
|---|---|---|
| `REMNA_BASE_URL` | — | URL API, например `http://remnawave:3000/api` |
| `REMNA_TOKEN` | — | Bearer-токен Remnawave |
| `GITHUB_RAW_URL` | production deeplink | Источник профиля |
| `UPDATE_TARGET` | `response-rule` | `response-rule` либо совместимый режим `global` |
| `RESPONSE_RULE_NAME` | `Happ` | Точное имя изменяемого Response Rule |
| `DRY_RUN` | `true` | Запретить фактический PATCH |
| `VALIDATE_GEO_URLS` | `true` | Проверять обе базы перед обновлением |
| `ALLOW_PROFILE_RENAME` | `false` | Разрешить изменение поля `Name` |
| `ALLOWED_PROFILE_RENAMES` | пусто | Разрешённые точные пары `старое:новое`, через запятую |
| `ALLOWED_GEO_HOSTS` | jsDelivr, GitHub Raw, GitHub | Разрешённые хосты баз |
| `CRON_SCHEDULE` | пусто | Cron вместо интервального опроса |
| `CHECK_INTERVAL` | `21600` | Интервал без cron, минимум 60 секунд |
| `REMNA_SSL_VERIFY` | `true` | Проверка TLS внешнего API Remnawave |
| `BACKUP_DIR` | `/data/backups` | Каталог резервных копий |
| `REQUEST_TIMEOUT` | `30` | HTTP timeout в секундах |
| `SQUAD_N_UUID`, `SQUAD_N_URL` | пусто | Необязательные внешние сквады |

## Глобальный режим

Для старой схемы с `customResponseHeaders`:

```env
UPDATE_TARGET=global
```

Даже в этом режиме сервис сохраняет все остальные глобальные заголовки.

## Тесты

```bash
pip install -r requirements.txt
python -m unittest discover -s tests -v
python -m py_compile app.py
```

При каждом push GitHub Actions запускает тесты и публикует контейнер:

```text
ghcr.io/indie-master/remnawave-routing-update:latest
```

## Лицензия

MIT
