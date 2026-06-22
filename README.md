# Hermes Max Platform Plugin

Подключает [Hermes Agent](https://hermes-agent.nousresearch.com/docs) к мессенджеру **Max** как gateway-канал: вы пишете своему Max-боту, а отвечает Hermes.

Репозиторий сделан для мастер-класса: участник устанавливает Hermes, даёт агенту ссылку на этот репозиторий и говорит: «подключи мне Max». Внутри есть код плагина, инструкция для человека и `AGENTS.md` для AI-агентов.

## Самый простой сценарий для участника

После установки Hermes скажите своему Hermes-агенту:

```text
Установи и настрой Max-канал для Hermes из репозитория:
https://github.com/vladimiraldushin/hermes-max-platform

Прочитай README и AGENTS.md. Не выдумывай инструкции по Max: используй официальные ссылки dev.max.ru из репозитория. Сначала проверь, что установлен Hermes и есть доступ к терминалу. Потом установи плагин, помоги мне получить или вставить MAX_BOT_TOKEN, настроить HTTPS webhook и перезапустить gateway.
```

Если вы делаете всё руками, используйте команды ниже.

## Установка плагина

```bash
# 1. Установить зависимости плагина в окружение Hermes
pip install aiohttp httpx

# 2. Установить и включить плагин
hermes plugins install vladimiraldushin/hermes-max-platform --enable

# 3. Запустить мастер настройки gateway
hermes gateway setup

# 4. Перезапустить gateway
hermes gateway restart
```

Если Hermes установлен в отдельном venv/desktop-бандле, попросите самого Hermes выполнить установку зависимостей в своё окружение или используйте тот `pip`, которым установлен `hermes`.

## Что нужно получить в Max

Для работы нужен **токен чат-бота Max** (`MAX_BOT_TOKEN`). По официальной документации Max, на 2026-06-22:

1. Подключение к платформе Max для партнёров доступно юрлицам, ИП и самозанятым — резидентам РФ. См. [`dev.max.ru/docs/maxbusiness/connection`](https://dev.max.ru/docs/maxbusiness/connection).
2. Чтобы создать бота, нужно подключиться к платформе, создать и верифицировать профиль организации/ИП/самозанятого. См. [`Создание чат-бота`](https://dev.max.ru/docs/chatbots/bots-create).
3. Бот проходит модерацию; в документации указано: до 48 часов по рабочим дням.
4. После модерации токен доступен на платформе: **Чат-боты → Перейти → Расширенные настройки → Настроить** — поле «Токен». См. [`Что дальше`](https://dev.max.ru/docs/chatbots/bots-create#%D0%A7%D1%82%D0%BE%20%D0%B4%D0%B0%D0%BB%D1%8C%D1%88%D0%B5) и [`Подготовка бота с разработкой`](https://dev.max.ru/docs/chatbots/bots-coding/prepare).
5. Токен — прямой доступ к управлению ботом. Не публикуйте его в репозиториях, скриншотах и чатах.

## Настройка webhook

Плагин поднимает локальный HTTP-сервер:

```text
http://0.0.0.0:8646/max/webhook
```

Max должен видеть его как **публичный HTTPS URL**. Для мастер-класса проще всего использовать tunnel:

```bash
# вариант 1: Cloudflare Tunnel
cloudflared tunnel --url http://localhost:8646

# вариант 2: ngrok
ngrok http 8646
```

Скопируйте выданный HTTPS URL и добавьте путь `/max/webhook`, например:

```text
https://example-tunnel.trycloudflare.com/max/webhook
```

Официальная документация Max требует для Webhook HTTPS URL; получение webhook по HTTP и самоподписанные сертификаты не поддерживаются с 25 мая 2026. См. [`API Max → Рекомендации`](https://dev.max.ru/docs-api) и [`POST /subscriptions`](https://dev.max.ru/docs-api/methods/POST/subscriptions).

Зарегистрируйте webhook-подписку:

```bash
curl -X POST "https://platform-api.max.ru/subscriptions"   -H "Authorization: $MAX_BOT_TOKEN"   -H "Content-Type: application/json"   -d '{
    "url": "https://YOUR-PUBLIC-HTTPS-DOMAIN/max/webhook",
    "update_types": ["message_created", "message_callback", "bot_started"],
    "secret": "CHANGE_ME_5_256_CHARS"
  }'
```

Важно:

- Токен передаётся в HTTP-заголовке `Authorization: <token>`. Передача токена в query-параметрах больше не поддерживается.
- `secret` необязателен, но Max рекомендует его использовать. Если `secret` указан, Max отправляет его в заголовке `X-Max-Bot-Api-Secret`; плагин проверяет это значение.
- Нельзя одновременно использовать Webhook и Long Polling: выберите один способ. Для production Max рекомендует Webhook.

## Переменные окружения

Минимум:

```bash
MAX_BOT_TOKEN="..."
```

Рекомендуемо:

```bash
MAX_WEBHOOK_SECRET="CHANGE_ME_5_256_CHARS"
MAX_WEBHOOK_HOST="0.0.0.0"
MAX_WEBHOOK_PORT="8646"
MAX_WEBHOOK_PATH="/max/webhook"
MAX_ALLOW_ALL_USERS="false"
MAX_ALLOWED_USERS="123456789"   # ID пользователей через запятую
```

Для доставки cron/send_message в Max можно указать:

```bash
MAX_HOME_CHANNEL="user:123456789"   # или chat:987654321
```

## Проверка

```bash
# Проверить, что плагин установлен и включён
hermes plugins list

# Проверить gateway
hermes gateway status

# Запустить gateway в foreground для отладки
hermes gateway run

# Health check локального webhook-сервера
curl http://localhost:8646/health
```

После запуска gateway напишите своему боту в Max. Если всё настроено верно, Hermes ответит в том же диалоге.

## Как это работает

- Входящие события Max приходят на `POST /max/webhook`.
- Плагин превращает Max `Update` в Hermes `MessageEvent`.
- Ответ Hermes отправляется в Max через `POST https://platform-api.max.ru/messages`.
- Для Markdown плагин отправляет `format: "markdown"`; Max поддерживает базовое форматирование Markdown/HTML. См. [`POST /messages`](https://dev.max.ru/docs-api/methods/POST/messages).

## Частые проблемы

### Gateway запущен, но Max не достучался

Проверьте:

- tunnel живой и ведёт на `http://localhost:8646`;
- webhook URL оканчивается на `/max/webhook`;
- в Max подписке указан HTTPS URL, не HTTP;
- `curl http://localhost:8646/health` возвращает `{"status":"ok","platform":"max"}`;
- gateway перезапущен после установки плагина.

### 401 от platform-api.max.ru

Проверьте, что токен передаётся как:

```http
Authorization: <token>
```

не как query-параметр и не как `Bearer <token>`.

### 403 на webhook

Если настроен `MAX_WEBHOOK_SECRET`, значение `secret` в `POST /subscriptions` должно совпадать с тем, что лежит в Hermes `.env`.

### Бот не отвечает в группе

Проверьте настройки приватности бота в Max и добавление бота в групповые чаты. В текущей документации Max privacy-настройка находится в расширенных настройках бота.

## Официальные источники, использованные для факт-чека

Проверено: **2026-06-22**.

- [Подключение к платформе Max для партнёров](https://dev.max.ru/docs/maxbusiness/connection)
- [Создание чат-бота](https://dev.max.ru/docs/chatbots/bots-create)
- [Подготовка и настройка бота с разработкой](https://dev.max.ru/docs/chatbots/bots-coding/prepare)
- [API Max overview](https://dev.max.ru/docs-api)
- [POST /subscriptions](https://dev.max.ru/docs-api/methods/POST/subscriptions)
- [POST /messages](https://dev.max.ru/docs-api/methods/POST/messages)

Если официальная документация Max изменилась — следуйте ей, а в этот репозиторий лучше отправить issue/PR.
