# Max platform plugin installed

Next steps:

1. Install runtime dependencies if they are missing:

   ```bash
   pip install aiohttp httpx
   ```

2. Configure the platform:

   ```bash
   hermes gateway setup
   ```

   Choose **Max**, paste `MAX_BOT_TOKEN`, set webhook host/port/path and optional secret.

3. Expose the local webhook server as public HTTPS:

   ```bash
   cloudflared tunnel --url http://localhost:8646
   # or: ngrok http 8646
   ```

4. Register the public URL with Max:

   ```bash
   curl -X POST "https://platform-api.max.ru/subscriptions"      -H "Authorization: $MAX_BOT_TOKEN"      -H "Content-Type: application/json"      -d '{"url":"https://YOUR-DOMAIN/max/webhook","update_types":["message_created","message_callback","bot_started"],"secret":"CHANGE_ME_5_256_CHARS"}'
   ```

5. Restart Hermes gateway:

   ```bash
   hermes gateway restart
   ```

Official Max docs checked on 2026-06-22:

- https://dev.max.ru/docs/chatbots/bots-create
- https://dev.max.ru/docs/chatbots/bots-coding/prepare
- https://dev.max.ru/docs-api/methods/POST/subscriptions
