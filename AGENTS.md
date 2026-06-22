# Instructions for AI agents using this repository

This repository contains a Hermes Agent platform plugin that adds Max messenger as a gateway channel.

When a user gives you this repository and asks to connect Hermes to Max:

1. Do not invent Max bot setup steps. Use the fact-checked sources in `README.md`; if available, re-open the official pages under `https://dev.max.ru/` and prefer current official docs.
2. Install Hermes first if it is missing: https://hermes-agent.nousresearch.com/docs
3. Install this plugin with:

   ```bash
   hermes plugins install vladimiraldushin/hermes-max-platform --enable
   ```

4. Ensure `aiohttp` and `httpx` are installed in the same Python environment that runs Hermes.
5. Help the user obtain `MAX_BOT_TOKEN` from Max for Partners. Current checked path: `Chat-bots → Go → Advanced settings → Configure → Token` after bot moderation.
6. Treat `MAX_BOT_TOKEN` and `MAX_WEBHOOK_SECRET` as secrets. Do not print them back to the chat after the user provides them.
7. Configure a public HTTPS webhook URL that points to the local Hermes gateway server, default local URL `http://localhost:8646/max/webhook`.
8. Register the webhook through Max Bot API `POST https://platform-api.max.ru/subscriptions` with `Authorization: <token>`, JSON body containing `url`, `update_types`, and optional `secret`.
9. Restart Hermes gateway and verify with `hermes plugins list`, `hermes gateway status`, local `/health`, and a real message to the Max bot.

Important current Max API facts checked on 2026-06-22:

- Bot API requests use `Authorization: <token>` header; token in query parameters is no longer supported.
- Webhook requires public HTTPS; HTTP and self-signed certificates are not supported for webhooks.
- Webhook `secret` is sent back by Max as the raw `X-Max-Bot-Api-Secret` header value, not as an HMAC signature.
- For production, Max recommends Webhook, not Long Polling; both cannot be used simultaneously.
- `POST /messages` accepts `user_id` or `chat_id`; message `text` is up to 4000 characters and `format` can be `markdown` or `html`.
