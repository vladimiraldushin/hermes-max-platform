---
name: max-gateway
description: Install and configure Hermes Agent access through Max messenger using the hermes-max-platform plugin.
version: 0.1.0
author: Vladimir Aldushin / Hermes Agent community
license: MIT
metadata:
  hermes:
    tags: [hermes, gateway, messaging, max, chatbot]
---

# Max Gateway for Hermes

Use this skill when a user wants to control Hermes Agent through Max messenger.

## Official facts to trust first

Checked on 2026-06-22:

- Max partner platform connection: https://dev.max.ru/docs/maxbusiness/connection
- Chatbot creation and token location: https://dev.max.ru/docs/chatbots/bots-create
- Developer setup and token warning: https://dev.max.ru/docs/chatbots/bots-coding/prepare
- API overview and webhook recommendations: https://dev.max.ru/docs-api
- Webhook subscriptions: https://dev.max.ru/docs-api/methods/POST/subscriptions
- Sending messages: https://dev.max.ru/docs-api/methods/POST/messages

If these docs changed, follow the current official docs instead of this skill.

## Procedure

1. Verify Hermes is installed and `hermes --version` works.
2. Install plugin dependencies in the Hermes Python environment:

   ```bash
   pip install aiohttp httpx
   ```

3. Install and enable the plugin:

   ```bash
   hermes plugins install vladimiraldushin/hermes-max-platform --enable
   ```

4. Help the user get a Max bot token. Current official path after moderation:
   `Chat-bots → Go → Advanced settings → Configure → Token`.
5. Save token as `MAX_BOT_TOKEN` in Hermes `.env`. Do not echo the token back.
6. Configure webhook bind settings, default:

   ```text
   MAX_WEBHOOK_HOST=0.0.0.0
   MAX_WEBHOOK_PORT=8646
   MAX_WEBHOOK_PATH=/max/webhook
   ```

7. Create a public HTTPS tunnel to `http://localhost:8646` or use the user's HTTPS domain.
8. Register subscription:

   ```bash
   curl -X POST "https://platform-api.max.ru/subscriptions"      -H "Authorization: $MAX_BOT_TOKEN"      -H "Content-Type: application/json"      -d '{"url":"https://YOUR-DOMAIN/max/webhook","update_types":["message_created","message_callback","bot_started"],"secret":"CHANGE_ME_5_256_CHARS"}'
   ```

9. Restart and verify:

   ```bash
   hermes gateway restart
   hermes gateway status
   curl http://localhost:8646/health
   ```

10. Ask the user to send a real message to the Max bot and verify Hermes answers.

## Pitfalls

- Use `Authorization: <token>`, not query params and not `Bearer`.
- Webhook must be HTTPS with a trusted certificate.
- If `secret` is configured, Max sends it raw in `X-Max-Bot-Api-Secret`; compare it directly.
- Webhook and Long Polling are mutually exclusive.
- Keep tunnel/gateway running while using Max.
