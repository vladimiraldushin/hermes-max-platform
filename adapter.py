"""
Max (max.ru) Platform Adapter for Hermes Agent.

A plugin-based gateway adapter that runs an HTTP server to receive
webhook events from Max Bot API and sends responses back via REST API.

Architecture:
  - Inbound: Max POSTs webhook events → aiohttp HTTP server → message queue → Hermes agent
  - Outbound: Hermes agent response → httpx POST /messages to Max API

Configuration in config.yaml::

    gateway:
      platforms:
        max:
          enabled: true
          token: "your_bot_token"
          extra:
            host: "0.0.0.0"
            port: 8646
            path: "/max/webhook"
            webhook_secret: "your_webhook_secret"    # optional, for X-Max-Bot-Api-Secret check
            max_message_length: 4000
            allowed_users: []                         # empty = allow all

Or via environment variables:
    MAX_BOT_TOKEN, MAX_WEBHOOK_SECRET, MAX_WEBHOOK_HOST,
    MAX_WEBHOOK_PORT, MAX_WEBHOOK_PATH
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import mimetypes
import os
import socket as _socket
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

try:
    from aiohttp import web

    AIOHTTP_AVAILABLE = True
except ImportError:
    web = None  # type: ignore[assignment]
    AIOHTTP_AVAILABLE = False

try:
    import httpx

    HTTPX_AVAILABLE = True
except ImportError:
    httpx = None  # type: ignore[assignment]
    HTTPX_AVAILABLE = False

from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    SendResult,
    SUPPORTED_DOCUMENT_TYPES,
    cache_audio_from_bytes,
    cache_document_from_bytes,
    cache_image_from_bytes,
)
from gateway.session import SessionSource
from gateway.config import PlatformConfig, Platform

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8646
DEFAULT_PATH = "/max/webhook"
DEFAULT_MAX_MESSAGE_LENGTH = 4000
MAX_API_BASE = "https://platform-api.max.ru"


def check_max_requirements() -> bool:
    """Check if aiohttp and httpx are available."""
    ok = AIOHTTP_AVAILABLE and HTTPX_AVAILABLE
    if not ok:
        missing = []
        if not AIOHTTP_AVAILABLE:
            missing.append("aiohttp")
        if not HTTPX_AVAILABLE:
            missing.append("httpx")
        logger.warning("Max: missing dependencies: %s", ", ".join(missing))
    return ok


# ---------------------------------------------------------------------------
# Webhook secret verification
# ---------------------------------------------------------------------------

def _verify_secret(body: bytes, secret: str, secret_header: Optional[str]) -> bool:
    """Verify Max webhook secret header.

    Official Max Bot API docs for ``POST /subscriptions`` state that when a
    subscription ``secret`` is configured, Max sends that exact value in the
    ``X-Max-Bot-Api-Secret`` header on every webhook request. It is not an
    HMAC signature of the request body. Keep the ``body`` parameter for
    backwards-compatible call sites, but compare the configured secret directly
    using constant-time comparison.
    """
    del body
    if not secret:
        return True
    if not secret_header:
        return False
    return hmac.compare_digest(str(secret), str(secret_header))


# ---------------------------------------------------------------------------
# Max Adapter
# ---------------------------------------------------------------------------

class MaxAdapter(BasePlatformAdapter):
    """Async Max adapter implementing the BasePlatformAdapter interface.

    This class is instantiated by the adapter_factory passed to
    register_platform() in the plugin loader.
    """

    def __init__(self, config: PlatformConfig, **kwargs):
        platform = Platform("max")
        super().__init__(config=config, platform=platform)

        extra = getattr(config, "extra", {}) or {}

        # Token
        self._token: str = os.getenv("MAX_BOT_TOKEN") or getattr(config, "token", "") or extra.get("token", "")

        # HTTP server settings
        self._host: str = (
            os.getenv("MAX_WEBHOOK_HOST")
            or str(extra.get("host") or DEFAULT_HOST)
        )
        self._port: int = int(
            os.getenv("MAX_WEBHOOK_PORT") or extra.get("port") or DEFAULT_PORT
        )
        self._path: str = (
            os.getenv("MAX_WEBHOOK_PATH")
            or str(extra.get("path") or DEFAULT_PATH)
        )
        self._webhook_secret: str = (
            os.getenv("MAX_WEBHOOK_SECRET")
            or str(extra.get("webhook_secret") or "")
        )

        # Message limits
        self.max_message_length: int = int(
            extra.get("max_message_length") or DEFAULT_MAX_MESSAGE_LENGTH
        )

        # Auth
        self.allowed_users: list = extra.get("allowed_users", [])
        self._allowed_users_set: set = set()
        for u in self.allowed_users:
            if isinstance(u, (int, str)):
                self._allowed_users_set.add(str(u))

        # Runtime state
        self._runner: Optional[web.AppRunner] = None
        self._site: Optional[web.TCPSite] = None
        self._app: Optional[web.Application] = None
        self._http_client: Optional[httpx.AsyncClient] = None
        self._message_queue: asyncio.Queue[MessageEvent] = asyncio.Queue()
        self._poll_task: Optional[asyncio.Task] = None
        self._running: bool = False

    # ── Connection lifecycle ──────────────────────────────────────────────

    async def connect(self) -> bool:
        """Start the HTTP server and begin processing messages."""
        if not self._token:
            logger.error("[Max] MAX_BOT_TOKEN not configured")
            return False
        if not check_max_requirements():
            return False

        # Quick port-in-use check
        try:
            with _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM) as sock:
                sock.settimeout(1)
                sock.connect(("127.0.0.1", self._port))
            logger.error("[Max] Port %d already in use", self._port)
            return False
        except (ConnectionRefusedError, OSError):
            pass

        try:
            self._http_client = httpx.AsyncClient(timeout=30.0)
            self._app = web.Application()
            self._app.router.add_get("/health", self._handle_health)
            self._app.router.add_post(self._path, self._handle_webhook)
            self._runner = web.AppRunner(self._app)
            await self._runner.setup()
            self._site = web.TCPSite(self._runner, self._host, self._port)
            await self._site.start()

            self._running = True
            self._poll_task = asyncio.create_task(self._poll_loop())
            self._mark_connected()

            logger.info(
                "[Max] HTTP server listening on %s:%s%s (token=%s...)",
                self._host,
                self._port,
                self._path,
                self._token[:8] if self._token else "NONE",
            )
            return True
        except Exception:
            await self._cleanup()
            logger.exception("[Max] Failed to start")
            return False

    async def disconnect(self) -> None:
        """Shut down the HTTP server and cleanup."""
        self._running = False
        if self._poll_task:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
            self._poll_task = None
        await self._cleanup()
        self._mark_disconnected()
        logger.info("[Max] Disconnected")

    async def _cleanup(self) -> None:
        """Release HTTP server and HTTP client resources."""
        self._site = None
        if self._runner:
            await self._runner.cleanup()
            self._runner = None
        self._app = None
        if self._http_client:
            await self._http_client.aclose()
            self._http_client = None

    # ── Outbound: send via Max REST API ───────────────────────────────────

    def _split_outbound_text(self, content: str) -> List[str]:
        """Split long outbound text into Max-sized chunks.

        Max rejects or truncates very long messages. Keep a safety margin below
        the configured limit so Markdown/Unicode payloads survive JSON/API
        processing, prefer paragraph boundaries, and hard-split only as a last
        resort.
        """
        limit = max(500, min(int(self.max_message_length or DEFAULT_MAX_MESSAGE_LENGTH), DEFAULT_MAX_MESSAGE_LENGTH) - 100)
        if len(content) <= limit:
            return [content]

        chunks: List[str] = []
        current = ""

        def flush() -> None:
            nonlocal current
            if current:
                chunks.append(current.strip())
                current = ""

        # Preserve paragraph/list readability where possible.
        for block in content.split("\n\n"):
            block = block.strip()
            if not block:
                continue
            candidate = f"{current}\n\n{block}" if current else block
            if len(candidate) <= limit:
                current = candidate
                continue

            flush()
            if len(block) <= limit:
                current = block
                continue

            # Very long paragraph: split on lines/spaces, then hard-split words.
            line_current = ""
            for line in block.splitlines() or [block]:
                for word in line.split(" "):
                    if not word:
                        continue
                    if len(word) > limit:
                        if line_current:
                            chunks.append(line_current.strip())
                            line_current = ""
                        for i in range(0, len(word), limit):
                            chunks.append(word[i : i + limit])
                        continue
                    candidate_word = f"{line_current} {word}" if line_current else word
                    if len(candidate_word) <= limit:
                        line_current = candidate_word
                    else:
                        chunks.append(line_current.strip())
                        line_current = word
                if line_current and len(line_current) + 1 <= limit:
                    line_current += "\n"
            if line_current:
                chunks.append(line_current.strip())

        flush()
        return chunks or [content[:limit]]

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Send a text message to a Max user or chat."""
        if not self._http_client:
            return SendResult(success=False, error="Not connected")

        # chat_id format: "user:12345" or "chat:67890"
        parts = chat_id.split(":", 1)
        target_type = parts[0] if len(parts) > 1 else "user"
        target_id = parts[1] if len(parts) > 1 else chat_id

        # Build URL with query params
        params = {}
        if target_type == "chat":
            params["chat_id"] = target_id
        else:
            params["user_id"] = target_id

        chunks = self._split_outbound_text(content)
        last_result: Optional[SendResult] = None
        for idx, text in enumerate(chunks, start=1):
            # Prefix continuation chunks so users can see delivery order.
            if len(chunks) > 1:
                prefix = f"({idx}/{len(chunks)})\n"
                text = prefix + text[: max(0, self.max_message_length - len(prefix) - 100)]

            # Build body
            body: Dict[str, Any] = {
                "text": text,
                "format": "markdown",
                "notify": True,
            }

            # Reply linking disabled — Max API requires full mid format (mid.xxx),
            # not just numeric message_id from webhook.
            # if reply_to:
            #     body["link"] = {"type": "reply", "mid": reply_to}

            headers = {
                "Authorization": self._token,
                "Content-Type": "application/json",
            }

            try:
                resp = await self._http_client.post(
                    f"{MAX_API_BASE}/messages",
                    params=params,
                    json=body,
                    headers=headers,
                )
                resp.raise_for_status()
                data = resp.json()
                msg = data.get("message", {})
                last_result = SendResult(
                    success=True,
                    message_id=str(msg.get("message_id", "")),
                    raw_response=data,
                )
            except Exception as exc:
                logger.error("[Max] Failed to send message chunk %s/%s: %s", idx, len(chunks), exc)
                return SendResult(success=False, error=str(exc))

        if len(chunks) > 1:
            logger.info("[Max] Split outbound message into %s chunks for %s", len(chunks), chat_id)
        return last_result or SendResult(success=False, error="No content to send")

    async def send_typing(self, chat_id: str, metadata=None) -> None:
        """Send Max typing indicator where the Bot API supports it.

        Max exposes typing/actions only for group chats via
        POST /chats/{chatId}/actions. Direct user DMs currently have no
        documented typing endpoint, so they remain a no-op.
        """
        if not self._http_client:
            return

        # chat_id format: "user:12345" or "chat:67890"
        parts = chat_id.split(":", 1)
        target_type = parts[0] if len(parts) > 1 else "user"
        target_id = parts[1] if len(parts) > 1 else chat_id

        if target_type != "chat":
            return

        headers = {
            "Authorization": self._token,
            "Content-Type": "application/json",
        }

        try:
            resp = await self._http_client.post(
                f"{MAX_API_BASE}/chats/{target_id}/actions",
                json={"action": "typing_on"},
                headers=headers,
            )
            resp.raise_for_status()
        except Exception as exc:
            logger.debug("[Max] Failed to send typing action: %s", exc)

    async def send_image(self, chat_id: str, image_url: str, caption: str = "") -> SendResult:
        """Send an image to a Max user or chat.

        Max API supports sending images as attachments.
        """
        if not self._http_client:
            return SendResult(success=False, error="Not connected")

        parts = chat_id.split(":", 1)
        target_type = parts[0] if len(parts) > 1 else "user"
        target_id = parts[1] if len(parts) > 1 else chat_id

        # First upload the image to get a file_id
        try:
            # Max might use inline image URLs or require upload
            # For now, send as text with image URL (Max may auto-preview)
            text = f"![image]({image_url})"
            if caption:
                text = f"{caption}\n![image]({image_url})"

            params = {}
            if target_type == "chat":
                params["chat_id"] = target_id
            else:
                params["user_id"] = target_id

            body = {
                "text": text[:self.max_message_length],
                "format": "markdown",
            }
            headers = {
                "Authorization": self._token,
                "Content-Type": "application/json",
            }
            resp = await self._http_client.post(
                f"{MAX_API_BASE}/messages",
                params=params,
                json=body,
                headers=headers,
            )
            resp.raise_for_status()
            data = resp.json()
            return SendResult(
                success=True,
                message_id=str(data.get("message", {}).get("message_id", "")),
            )
        except Exception as exc:
            logger.error("[Max] Failed to send image: %s", exc)
            return SendResult(success=False, error=str(exc))

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        """Return basic chat info."""
        parts = chat_id.split(":", 1)
        target_type = parts[0] if len(parts) > 1 else "user"
        return {
            "name": chat_id,
            "type": "group" if target_type == "chat" else "dm",
        }

    # ── Inbound: HTTP webhook handlers ────────────────────────────────────

    async def _handle_health(self, request: web.Request) -> web.Response:
        """Health check endpoint."""
        return web.json_response({"status": "ok", "platform": "max"})

    async def _handle_webhook(self, request: web.Request) -> web.Response:
        """POST endpoint — receive update events from Max."""
        # Verify webhook secret if configured
        if self._webhook_secret:
            body = await request.read()
            signature = request.headers.get("X-Max-Bot-Api-Secret", "")
            if not _verify_secret(body, self._webhook_secret, signature):
                logger.warning("[Max] Webhook secret verification failed")
                return web.Response(status=403, text="webhook secret verification failed")
            try:
                payload = json.loads(body)
            except json.JSONDecodeError:
                logger.warning("[Max] Invalid JSON in webhook body")
                return web.Response(status=400, text="invalid json")
        else:
            try:
                payload = await request.json()
            except Exception:
                logger.warning("[Max] Failed to parse webhook JSON")
                return web.Response(status=400, text="invalid json")

        # Process the update
        event = await self._build_event(payload)
        if event is not None:
            await self._message_queue.put(event)

        # Always return 200 immediately — agent response comes later
        return web.Response(text="ok", content_type="text/plain")

    async def _extract_inbound_media(self, payload: Dict[str, Any], message: Dict[str, Any], body: Dict[str, Any]) -> Tuple[List[str], List[str]]:
        """Download supported Max inbound media attachments to local caches.

        Max uses different shapes for direct vs forwarded messages. Direct voice
        notes/images/documents usually live under message.body.attachments/message.attachments,
        but forwarded messages can be nested under keys such as forwarded_messages,
        forwards, link, message, or body. Walk the webhook object recursively and
        treat any audio/voice/photo/image/file/document-looking object as a candidate attachment.
        """
        attachments: List[Dict[str, Any]] = []
        seen: set[int] = set()

        def add_attachment(item: Any) -> None:
            if not isinstance(item, dict):
                return
            ident = id(item)
            if ident in seen:
                return
            seen.add(ident)
            attachments.append(item)

        def walk(obj: Any) -> None:
            if isinstance(obj, dict):
                raw = obj.get("attachments")
                if isinstance(raw, list):
                    for item in raw:
                        add_attachment(item)
                elif isinstance(raw, dict):
                    add_attachment(raw)

                # Direct media wrappers, including nested/forwarded messages.
                for key in ("audio", "voice", "file", "document", "doc", "attachment", "media", "image", "photo", "picture"):
                    value = obj.get(key)
                    if isinstance(value, dict):
                        pseudo = {"type": key, "payload": value}
                        add_attachment(pseudo)

                # If the object itself looks like an attachment, include it.
                if self._attachment_kind(obj) in {"audio", "voice", "image", "document"}:
                    add_attachment(obj)

                for value in obj.values():
                    walk(value)
            elif isinstance(obj, list):
                for value in obj:
                    walk(value)

        walk(payload)

        media_paths: List[str] = []
        media_types: List[str] = []
        seen_media_refs: set[str] = set()
        for attachment in attachments:
            kind = self._attachment_kind(attachment)
            media_ref = self._find_first_url(attachment) or f"object:{id(attachment)}"
            if media_ref in seen_media_refs:
                continue
            seen_media_refs.add(media_ref)
            if kind in {"audio", "voice"}:
                cached = await self._cache_audio_attachment(attachment, kind)
                if not cached:
                    logger.info(
                        "[Max] Voice/audio attachment present but no downloadable URL found; attachment_keys=%s payload_keys=%s",
                        sorted(attachment.keys()),
                        sorted((attachment.get("payload") or {}).keys()) if isinstance(attachment.get("payload"), dict) else [],
                    )
                    continue
            elif kind == "image":
                cached = await self._cache_image_attachment(attachment)
                if not cached:
                    logger.info(
                        "[Max] Image/photo attachment present but no downloadable URL found; attachment_keys=%s payload_keys=%s",
                        sorted(attachment.keys()),
                        sorted((attachment.get("payload") or {}).keys()) if isinstance(attachment.get("payload"), dict) else [],
                    )
                    continue
            elif kind == "document":
                cached = await self._cache_document_attachment(attachment)
                if not cached:
                    logger.info(
                        "[Max] File/document attachment present but no downloadable URL found; attachment_keys=%s payload_keys=%s",
                        sorted(attachment.keys()),
                        sorted((attachment.get("payload") or {}).keys()) if isinstance(attachment.get("payload"), dict) else [],
                    )
                    continue
            else:
                continue
            path, media_type = cached
            media_paths.append(path)
            media_types.append(media_type)

        return media_paths, media_types

    @staticmethod
    def _attachment_kind(attachment: Dict[str, Any]) -> str:
        values: List[str] = []
        for key in ("type", "attachment_type", "kind", "media_type"):
            value = attachment.get(key)
            if value:
                values.append(str(value).lower())
        payload = attachment.get("payload")
        if isinstance(payload, dict):
            for key in ("type", "attachment_type", "kind", "media_type", "mime_type", "content_type"):
                value = payload.get(key)
                if value:
                    values.append(str(value).lower())
            # Max attachment payloads sometimes identify media by nested object keys.
            for key in ("audio", "voice", "image", "photo", "picture", "file", "document", "doc"):
                if key in payload:
                    values.append(key)
        filename = MaxAdapter._find_first_filename(attachment) or ""
        if filename:
            values.append(filename.lower())
        joined = " ".join(values)
        if "voice" in joined:
            return "voice"
        if "audio" in joined or joined.startswith("ptt"):
            return "audio"
        if any(marker in joined for marker in ("image", "photo", "picture")):
            return "image"
        if any(marker in joined for marker in ("file", "document", "doc", "attachment")):
            return "document"
        ext = Path(filename).suffix.lower() if filename else ""
        if ext in SUPPORTED_DOCUMENT_TYPES:
            return "document"
        return ""

    @staticmethod
    def _find_first_filename(data: Any) -> Optional[str]:
        """Find a plausible original filename inside an attachment payload."""
        if isinstance(data, dict):
            for key in (
                "filename",
                "file_name",
                "fileName",
                "name",
                "title",
                "display_name",
                "displayName",
                "original_filename",
                "originalFilename",
            ):
                value = data.get(key)
                if isinstance(value, str) and value.strip():
                    return Path(value.strip()).name
            for value in data.values():
                found = MaxAdapter._find_first_filename(value)
                if found:
                    return found
        elif isinstance(data, list):
            for value in data:
                found = MaxAdapter._find_first_filename(value)
                if found:
                    return found
        return None

    @staticmethod
    def _find_first_url(data: Any) -> Optional[str]:
        """Find a plausible public/API download URL inside an attachment payload."""
        if isinstance(data, dict):
            for key in (
                "url",
                "download_url",
                "downloadUrl",
                "file_url",
                "fileUrl",
                "media_url",
                "mediaUrl",
                "href",
                "link",
            ):
                value = data.get(key)
                if isinstance(value, str) and value.startswith(("http://", "https://")):
                    return value
            for value in data.values():
                found = MaxAdapter._find_first_url(value)
                if found:
                    return found
        elif isinstance(data, list):
            for value in data:
                found = MaxAdapter._find_first_url(value)
                if found:
                    return found
        return None

    async def _cache_audio_attachment(self, attachment: Dict[str, Any], kind: str) -> Optional[Tuple[str, str]]:
        url = self._find_first_url(attachment)
        if not url:
            return None

        if not self._http_client:
            return None

        headers = {
            "Authorization": self._token,
            "User-Agent": "HermesAgent/1.0 MaxBot",
            "Accept": "audio/*,*/*;q=0.8",
        }
        try:
            resp = await self._http_client.get(url, headers=headers, follow_redirects=True)
            resp.raise_for_status()
        except Exception as exc:
            logger.warning("[Max] Failed to download %s attachment from %s: %s", kind, self._safe_url_for_log(url), exc)
            return None

        content_type = str(resp.headers.get("content-type") or "").split(";", 1)[0].strip().lower()
        if not content_type or content_type == "application/octet-stream":
            guessed, _ = mimetypes.guess_type(urlparse(url).path)
            content_type = guessed or "audio/ogg"
        if not content_type.startswith("audio/"):
            # Max/CDN sometimes returns octet-stream for voice notes; accept only if URL extension looks audio-like.
            ext_from_url = Path(urlparse(url).path).suffix.lower()
            if ext_from_url not in {".ogg", ".oga", ".opus", ".mp3", ".m4a", ".aac", ".wav", ".amr", ".webm"}:
                logger.info("[Max] Downloaded %s attachment but content-type is not audio: %s", kind, content_type)
                return None

        ext = mimetypes.guess_extension(content_type) if content_type else None
        if not ext:
            ext = Path(urlparse(url).path).suffix.lower() or ".ogg"
        if ext == ".oga":
            ext = ".ogg"
        return cache_audio_from_bytes(resp.content, ext), content_type or "audio/ogg"

    async def _cache_image_attachment(self, attachment: Dict[str, Any]) -> Optional[Tuple[str, str]]:
        url = self._find_first_url(attachment)
        if not url:
            return None

        if not self._http_client:
            return None

        headers = {
            "Authorization": self._token,
            "User-Agent": "HermesAgent/1.0 MaxBot",
            "Accept": "image/*,*/*;q=0.8",
        }
        try:
            resp = await self._http_client.get(url, headers=headers, follow_redirects=True)
            resp.raise_for_status()
        except Exception as exc:
            logger.warning("[Max] Failed to download image attachment from %s: %s", self._safe_url_for_log(url), exc)
            return None

        content_type = str(resp.headers.get("content-type") or "").split(";", 1)[0].strip().lower()
        if not content_type or content_type == "application/octet-stream":
            guessed, _ = mimetypes.guess_type(urlparse(url).path)
            content_type = guessed or "image/jpeg"

        ext = mimetypes.guess_extension(content_type) if content_type else None
        if not ext:
            ext = Path(urlparse(url).path).suffix.lower() or ".jpg"
        if ext in {".jpe", ".jpeg"}:
            ext = ".jpg"

        try:
            return cache_image_from_bytes(resp.content, ext), content_type or "image/jpeg"
        except ValueError as exc:
            logger.warning("[Max] Rejected non-image bytes from %s: %s", self._safe_url_for_log(url), exc)
            return None

    async def _cache_document_attachment(self, attachment: Dict[str, Any]) -> Optional[Tuple[str, str]]:
        url = self._find_first_url(attachment)
        if not url:
            return None

        if not self._http_client:
            return None

        headers = {
            "Authorization": self._token,
            "User-Agent": "HermesAgent/1.0 MaxBot",
            "Accept": "application/*,text/*,*/*;q=0.8",
        }
        try:
            resp = await self._http_client.get(url, headers=headers, follow_redirects=True)
            resp.raise_for_status()
        except Exception as exc:
            logger.warning("[Max] Failed to download document attachment from %s: %s", self._safe_url_for_log(url), exc)
            return None

        content_type = str(resp.headers.get("content-type") or "").split(";", 1)[0].strip().lower()
        filename = self._find_first_filename(attachment)
        if not filename:
            filename = Path(urlparse(url).path).name or "document"

        ext = Path(filename).suffix.lower()
        if not content_type or content_type == "application/octet-stream":
            guessed, _ = mimetypes.guess_type(filename)
            content_type = guessed or "application/octet-stream"
        if not ext:
            guessed_ext = mimetypes.guess_extension(content_type) if content_type else None
            ext = guessed_ext or ".bin"
            filename = f"{filename}{ext}"

        # Preserve common office/text/pdf types for downstream document handling.
        if ext in SUPPORTED_DOCUMENT_TYPES:
            content_type = SUPPORTED_DOCUMENT_TYPES[ext]
        elif content_type.startswith("text/"):
            pass
        elif content_type.startswith("application/"):
            pass
        else:
            content_type = "application/octet-stream"

        try:
            return cache_document_from_bytes(resp.content, filename), content_type
        except Exception as exc:
            logger.warning("[Max] Failed to cache document from %s: %s", self._safe_url_for_log(url), exc)
            return None

    @staticmethod
    def _safe_url_for_log(url: str) -> str:
        parsed = urlparse(url)
        if not parsed.scheme or not parsed.netloc:
            return "[invalid-url]"
        path = parsed.path or "/"
        return f"{parsed.scheme}://{parsed.netloc}{path}"

    @staticmethod
    def _derive_message_type(text: str, media_types: List[str]) -> MessageType:
        if any(mtype.startswith(("application/", "text/")) or mtype == "application/octet-stream" for mtype in media_types):
            return MessageType.DOCUMENT
        if any(mtype.startswith("image/") for mtype in media_types):
            return MessageType.TEXT if text else MessageType.PHOTO
        if any(mtype.startswith("audio/") for mtype in media_types):
            return MessageType.TEXT if text else MessageType.VOICE
        return MessageType.TEXT

    async def _build_event(self, payload: Dict[str, Any]) -> Optional[MessageEvent]:
        """Parse a Max Update object into a MessageEvent.

        Expected Max Update format (based on API docs):
        {
            "update_type": "message_created",
            "message": {
                "message_id": 123,
                "user_id": 456,
                "chat_id": 789,     # optional, for group chats
                "text": "hello",
                ...
            },
            "user": {...},
            "chat": {...}
        }
        """
        update_type = payload.get("update_type", "")

        if update_type == "bot_started":
            # User started the bot — treat as /start
            user = payload.get("user", {})
            user_id = str(user.get("user_id", ""))
            if not user_id:
                return None
            source = self.build_source(
                chat_id=f"user:{user_id}",
                chat_name=user.get("name", user_id),
                chat_type="dm",
                user_id=user_id,
                user_name=user.get("name", user_id),
            )
            return MessageEvent(
                text="/start",
                message_type=MessageType.TEXT,
                source=source,
                raw_message=payload,
                message_id=f"start_{user_id}",
            )

        if update_type in ("message_created", "message_edited", "message_updated"):
            message = payload.get("message", {}) or {}
            body = message.get("body") or {}
            sender = message.get("sender") or payload.get("user") or {}
            recipient = message.get("recipient") or {}

            # Official Max Message schema stores text in message.body.text,
            # not message.text. Keep the old location as a fallback for tests/manual probes.
            text = (body.get("text") or message.get("text") or "").strip()
            media_urls, media_types = await self._extract_inbound_media(payload, message, body)
            if not text and not media_urls:
                logger.info(
                    "[Max] Ignoring %s without text/media; payload_keys=%s message_keys=%s body_keys=%s",
                    update_type,
                    sorted(payload.keys()),
                    sorted(message.keys()) if isinstance(message, dict) else [],
                    sorted(body.keys()) if isinstance(body, dict) else [],
                )
                return None

            user_id = str(
                sender.get("user_id")
                or payload.get("user_id")
                or message.get("user_id")
                or ""
            )
            user_name = (
                sender.get("name")
                or sender.get("first_name")
                or sender.get("username")
                or user_id
            )

            chat = payload.get("chat", {}) or {}
            chat_id_str = str(
                recipient.get("chat_id")
                or chat.get("chat_id")
                or message.get("chat_id")
                or ""
            )

            if chat_id_str:
                # Group chat/channel message
                chat_type = "group"
                scoped_chat_id = f"chat:{chat_id_str}"
            else:
                # Direct message: send replies back to the sender, not to the bot recipient.
                chat_type = "dm"
                scoped_chat_id = f"user:{user_id}"

            if not user_id:
                logger.warning("[Max] Ignoring %s: missing sender user_id; payload_keys=%s", update_type, sorted(payload.keys()))
                return None

            # Auth check
            if self._allowed_users_set and user_id not in self._allowed_users_set:
                logger.debug("[Max] Ignoring message from unauthorized user %s", user_id)
                return None

            source = self.build_source(
                chat_id=scoped_chat_id,
                chat_name=user_name if chat_type == "dm" else (chat.get("title") or chat_id_str),
                chat_type=chat_type,
                user_id=user_id,
                user_name=user_name,
            )

            msg_id = str(body.get("mid") or message.get("mid") or message.get("message_id") or "")
            return MessageEvent(
                text=text,
                message_type=self._derive_message_type(text, media_types),
                source=source,
                raw_message=payload,
                message_id=msg_id,
                media_urls=media_urls,
                media_types=media_types,
            )

        if update_type in ("message_callback", "callback_query"):
            # Inline button callback
            callback = payload.get("callback_query", {})
            data = callback.get("data", "")
            user = callback.get("user", {})
            user_id = str(user.get("user_id", ""))

            if not user_id or not data:
                return None

            source = self.build_source(
                chat_id=f"user:{user_id}",
                chat_name=user.get("name", user_id),
                chat_type="dm",
                user_id=user_id,
                user_name=user.get("name", user_id),
            )
            return MessageEvent(
                text=data,
                message_type=MessageType.TEXT,
                source=source,
                raw_message=payload,
                message_id=f"cb_{user_id}_{int(time.time())}",
            )

        return None

    # ── Message polling ───────────────────────────────────────────────────

    async def _poll_loop(self) -> None:
        """Drain the message queue and dispatch to the gateway runner."""
        while self._running:
            try:
                # Wait for next message with a timeout to allow clean shutdown
                event = await asyncio.wait_for(self._message_queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break

            if not self._running:
                break

            try:
                task = asyncio.create_task(self.handle_message(event))
                self._background_tasks.add(task)
                task.add_done_callback(self._background_tasks.discard)
            except Exception:
                logger.exception("[Max] Failed to enqueue event")


# ---------------------------------------------------------------------------
# Plugin registration
# ---------------------------------------------------------------------------

def check_requirements() -> bool:
    """Check if Max is configured (token present)."""
    return bool(os.getenv("MAX_BOT_TOKEN", ""))


def _coerce_bool(value: Any, default: bool = False) -> bool:
    """Coerce user-facing config/env strings into booleans."""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def validate_config(config) -> bool:
    """Validate that the platform config has enough info to connect."""
    extra = getattr(config, "extra", {}) or {}
    token = os.getenv("MAX_BOT_TOKEN") or getattr(config, "token", "") or extra.get("token", "")
    return bool(str(token).strip())


def is_connected(config) -> bool:
    """Check whether Max is configured (env or config.yaml)."""
    return validate_config(config)


def _env_enablement() -> Optional[dict]:
    """Seed PlatformConfig.extra from env-only setups.

    This lets ``hermes gateway status``, cron delivery, and platform discovery
    see Max as configured when users only filled ``~/.hermes/.env`` via
    ``hermes plugins install`` / ``hermes gateway setup``.
    """
    token = os.getenv("MAX_BOT_TOKEN", "").strip()
    if not token:
        return None

    extra: dict[str, Any] = {"token": token}
    str_vars = {
        "MAX_WEBHOOK_HOST": "host",
        "MAX_WEBHOOK_PATH": "path",
        "MAX_WEBHOOK_SECRET": "webhook_secret",
    }
    for env_name, key in str_vars.items():
        value = os.getenv(env_name, "").strip()
        if value:
            extra[key] = value

    port = os.getenv("MAX_WEBHOOK_PORT", "").strip()
    if port:
        try:
            extra["port"] = int(port)
        except ValueError:
            extra["port"] = port

    allowed = os.getenv("MAX_ALLOWED_USERS", "").strip()
    if allowed:
        extra["allowed_users"] = [part.strip() for part in allowed.split(",") if part.strip()]
    allow_all = os.getenv("MAX_ALLOW_ALL_USERS", "").strip()
    if allow_all:
        extra["allow_all_users"] = _coerce_bool(allow_all, True)

    home = os.getenv("MAX_HOME_CHANNEL", "").strip()
    if home:
        extra["home_channel"] = {
            "chat_id": home,
            "name": os.getenv("MAX_HOME_CHANNEL_NAME", "Max Home") or "Max Home",
        }
    return extra


def _apply_yaml_config(yaml_cfg: dict, platform_cfg: dict) -> Optional[dict]:
    """Translate optional top-level ``max:`` config into env/extras.

    Hermes' native ``gateway.platforms.max`` block is handled by core config
    parsing. This bridge is a convenience for users who prefer a short
    top-level ``max:`` section in config.yaml. Environment variables still win
    over YAML for secrets and runtime overrides.
    """
    del yaml_cfg
    if not isinstance(platform_cfg, dict):
        return None

    extra: dict[str, Any] = {}
    mapping = {
        "token": "MAX_BOT_TOKEN",
        "webhook_secret": "MAX_WEBHOOK_SECRET",
        "host": "MAX_WEBHOOK_HOST",
        "port": "MAX_WEBHOOK_PORT",
        "path": "MAX_WEBHOOK_PATH",
        "allowed_users": "MAX_ALLOWED_USERS",
        "allow_all_users": "MAX_ALLOW_ALL_USERS",
        "home_channel": "MAX_HOME_CHANNEL",
    }
    for key, env_name in mapping.items():
        if key not in platform_cfg:
            continue
        value = platform_cfg.get(key)
        if value is None:
            continue

        if key == "allowed_users" and isinstance(value, list):
            env_value = ",".join(str(v) for v in value)
            extra[key] = [str(v) for v in value]
        elif key == "home_channel" and isinstance(value, dict):
            chat_id = str(value.get("chat_id") or "").strip()
            if not chat_id:
                continue
            env_value = chat_id
            extra[key] = value
        else:
            env_value = str(value)
            extra[key] = value

        if env_value and not os.getenv(env_name):
            os.environ[env_name] = env_value

    return extra or None


def interactive_setup() -> None:
    """Interactive `hermes gateway setup` flow for the Max platform."""
    from hermes_cli.setup import (
        prompt,
        prompt_yes_no,
        save_env_value,
        get_env_value,
        print_header,
        print_info,
        print_warning,
        print_success,
    )

    print_header("Max (max.ru)")
    existing_token = get_env_value("MAX_BOT_TOKEN")
    if existing_token:
        print_info(f"Max: already configured (token: {existing_token[:8]}...)")
        if not prompt_yes_no("Reconfigure Max?", False):
            return

    print_info("Connect Hermes to Max messenger (max.ru). Requires aiohttp + httpx.")
    print_info("   pip install aiohttp httpx")
    print()

    token = prompt("Bot token (from Max Platform → Chat-bots → Integration)", default="", password=True)
    if not token:
        print_warning("Token is required — skipping Max setup")
        return
    save_env_value("MAX_BOT_TOKEN", token.strip())

    print()
    print_info("🔒 Webhook security")
    use_secret = prompt_yes_no("Set a webhook secret for HMAC verification?", True)
    if use_secret:
        secret = prompt("Webhook secret (5-256 chars: A-Z, a-z, 0-9, _ or -)", password=True)
        if secret:
            save_env_value("MAX_WEBHOOK_SECRET", secret)
    else:
        save_env_value("MAX_WEBHOOK_SECRET", "")

    print()
    host = prompt("HTTP server host", default=get_env_value("MAX_WEBHOOK_HOST") or "0.0.0.0")
    save_env_value("MAX_WEBHOOK_HOST", host.strip() or "0.0.0.0")

    port = prompt("HTTP server port", default=get_env_value("MAX_WEBHOOK_PORT") or "8646")
    save_env_value("MAX_WEBHOOK_PORT", port.strip() or "8646")

    path = prompt("Webhook path", default=get_env_value("MAX_WEBHOOK_PATH") or "/max/webhook")
    save_env_value("MAX_WEBHOOK_PATH", path.strip() or "/max/webhook")

    print()
    print_info("🔒 Access control: restrict who can message the bot")
    allow_all = prompt_yes_no("Allow all Max users to talk to the bot?", True)
    if allow_all:
        save_env_value("MAX_ALLOW_ALL_USERS", "true")
        save_env_value("MAX_ALLOWED_USERS", "")
    else:
        save_env_value("MAX_ALLOW_ALL_USERS", "false")
        allowed = prompt(
            "Allowed user IDs (comma-separated, leave empty to deny everyone)",
            default=get_env_value("MAX_ALLOWED_USERS") or "",
        )
        if allowed:
            save_env_value("MAX_ALLOWED_USERS", allowed.replace(" ", ""))

    print()
    print_success("Max configuration saved to ~/.hermes/.env")
    print_info("Important: You must manually register the webhook URL with Max API:")
    print_info(f"  curl -X POST https://platform-api.max.ru/subscriptions \\")
    print_info(f"    -H 'Authorization: <your_token>' \\")
    print_info(f"    -H 'Content-Type: application/json' \\")
    print_info(f"    -d '{{\"url\": \"https://your-domain.com{path}\", \"update_types\": [\"message_created\", \"bot_started\"], \"secret\": \"<your_secret_if_configured>\"}}'")
    print_info("Restart the gateway for changes to take effect: hermes gateway restart")


def register(ctx):
    """Plugin entry point — called by the Hermes plugin system."""
    ctx.register_platform(
        name="max",
        label="Max",
        adapter_factory=lambda cfg: MaxAdapter(cfg),
        check_fn=check_max_requirements,
        validate_config=validate_config,
        is_connected=is_connected,
        required_env=["MAX_BOT_TOKEN"],
        install_hint="pip install aiohttp httpx",
        setup_fn=interactive_setup,
        env_enablement_fn=_env_enablement,
        apply_yaml_config_fn=_apply_yaml_config,
        cron_deliver_env_var="MAX_HOME_CHANNEL",
        standalone_sender_fn=_standalone_send,
        # Auth env vars for _is_user_authorized() integration
        allowed_users_env="MAX_ALLOWED_USERS",
        allow_all_env="MAX_ALLOW_ALL_USERS",
        # Message limit
        max_message_length=4000,
        # Display
        emoji="🟣",
        # No phone numbers to redact
        pii_safe=True,
        allow_update_command=True,
        # LLM guidance
        platform_hint=(
            "You are chatting via Max (max.ru) messenger. "
            "Max supports markdown formatting (**bold**, *italic*, `code`, ```blocks```). "
            "Messages are limited to 4000 characters. "
            "You can send images using markdown ![alt](url) syntax. "
            "Keep responses clear and well-structured."
        ),
    )

    skill_path = Path(__file__).parent / "skills" / "max-gateway" / "SKILL.md"
    if skill_path.exists():
        ctx.register_skill(
            "max-gateway",
            skill_path,
            description="Install and configure Hermes Agent gateway access through Max messenger.",
        )


# ---------------------------------------------------------------------------
# Standalone send function (for cron jobs and send_message tool)
# ---------------------------------------------------------------------------

async def _send_max_message(pconfig: PlatformConfig, chat_id: str, message: str) -> SendResult:
    """Send a message via Max API without requiring the full adapter.

    Used by the send_message tool and cron delivery outside the gateway process.
    """
    extra = getattr(pconfig, "extra", {}) or {}
    token = os.getenv("MAX_BOT_TOKEN") or getattr(pconfig, "token", "") or extra.get("token", "")

    if not token:
        return SendResult(success=False, error="MAX_BOT_TOKEN not configured")

    parts = chat_id.split(":", 1)
    target_type = parts[0] if len(parts) > 1 else "user"
    target_id = parts[1] if len(parts) > 1 else chat_id

    params = {}
    if target_type == "chat":
        params["chat_id"] = target_id
    else:
        params["user_id"] = target_id

    body = {
        "text": message[:4000],
        "format": "markdown",
    }
    headers = {
        "Authorization": token,
        "Content-Type": "application/json",
    }

    try:
        import httpx as _httpx
        async with _httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"{MAX_API_BASE}/messages",
                params=params,
                json=body,
                headers=headers,
            )
            resp.raise_for_status()
            data = resp.json()
            return SendResult(
                success=True,
                message_id=str(data.get("message", {}).get("message_id", "")),
            )
    except Exception as exc:
        logger.error("[Max] send_message failed: %s", exc)
        return SendResult(success=False, error=str(exc))


async def _standalone_send(
    pconfig: PlatformConfig,
    chat_id: str,
    message: str,
    *,
    thread_id: Optional[str] = None,
    media_files: Optional[List[str]] = None,
    force_document: bool = False,
) -> dict:
    """Standalone sender contract for send_message/cron delivery.

    Hermes calls this when a separate process (for example cron) needs to
    deliver to Max without having the live gateway adapter in memory. Max media
    delivery is intentionally left to the live adapter for now; text delivery is
    enough for cron/status notifications and cross-platform ``send_message``.
    """
    del thread_id, force_document
    if media_files:
        logger.warning("[Max] standalone send currently ignores media_files=%s", media_files)

    result = await _send_max_message(pconfig, chat_id, message)
    if result.success:
        return {"success": True, "message_id": result.message_id}
    return {"error": result.error or "Max send failed"}
