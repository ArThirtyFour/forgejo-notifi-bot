import html
import logging
import time
from typing import Optional

import aiohttp
from fastapi import APIRouter, Header, Request

from app.config import Config, parse_config
from app.db.functions import Chat, EventSetting, Integration
from app.events import EventCtx, build_message
from app.utils.text_splitter import split_html_message

router = APIRouter()
_config: Optional[Config] = None


def set_config(cfg: Config) -> None:
    global _config
    _config = cfg


def get_config() -> Config:
    global _config
    if _config is None:
        _config = parse_config()
    return _config


floodwait_cache: dict[int, float] = {}
_delivery_failure_notified: dict[tuple[int, int], float] = {}
_NOTIFY_INTERVAL = 1800.0

EVENT_NAME_MAP = {
    "issue": "issues",
    "pull_request_comment": "pull_request_review_comment",
    "pull_request_approved": "pull_request_review",
}


def check_floodwait(chat_id: int, floodwait: int = 3) -> bool:
    now = time.time()
    if (last := floodwait_cache.get(chat_id)) and now - last < floodwait:
        return True
    floodwait_cache[chat_id] = now
    return False


async def _post_send(
    session: aiohttp.ClientSession, data: dict
) -> tuple[int, str]:
    cfg = get_config()
    async with session.post(
        f"https://api.telegram.org/bot{cfg.bot.token}/sendMessage",
        json=data,
        timeout=aiohttp.ClientTimeout(total=5),
    ) as response:
        return response.status, await response.text()


async def _notify_owner_of_delivery_failure(
    session: aiohttp.ClientSession,
    integration: Integration,
    failure_summary: str,
) -> None:
    user = integration.user
    chat = integration.chat
    if user is None or chat is None or not user.telegram_id:
        return

    key = (user.telegram_id, chat.chat_id)
    now = time.time()
    last = _delivery_failure_notified.get(key)
    if last is not None and now - last < _NOTIFY_INTERVAL:
        return
    _delivery_failure_notified[key] = now

    text = (
        "⚠️ <b>I couldn't deliver a notification</b>\n\n"
        f"Repository: <code>{html.escape(integration.repository_name or '?')}</code>\n"
        f"Target chat id: <code>{chat.chat_id}</code>\n"
        f"Error: <code>{html.escape(failure_summary[:300])}</code>\n\n"
        "Possible causes:\n"
        "• I was removed from the chat\n"
        "• The chat was deleted or migrated to a different id\n"
        "• I lost permissions to write there\n"
        "• The forum topic the integration uses was closed\n\n"
        "Run <code>/integrations</code> in that chat to manage, or "
        "<code>/delete owner/repo</code> there to remove the integration."
    )
    data = {
        "chat_id": user.telegram_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    try:
        status, body = await _post_send(session, data)
        if status < 400:
            logging.info(
                "Sent delivery-failure DM to %s about chat %s",
                user.telegram_id,
                chat.chat_id,
            )
        else:
            logging.warning(
                "Couldn't DM owner %s about delivery failure: %s — %s",
                user.telegram_id,
                status,
                body,
            )
    except aiohttp.ClientError as e:
        logging.warning(
            "Network error DM-ing owner %s about delivery failure: %s",
            user.telegram_id,
            e,
        )


async def send_message(
    session: aiohttp.ClientSession,
    integration: Integration,
    text: str,
) -> None:
    chunks = split_html_message(text)
    for chunk in chunks:
        await _send_one_chunk(session, integration, chunk)


async def _send_one_chunk(
    session: aiohttp.ClientSession,
    integration: Integration,
    text: str,
) -> None:
    chat = integration.chat
    if chat is None:
        return
    chat_id = chat.chat_id
    topic_id = chat.topic_id

    data: dict = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if topic_id:
        data["message_thread_id"] = topic_id

    try:
        status, body = await _post_send(session, data)
        if status < 400:
            return

        if topic_id and status == 400 and (
            "thread not found" in body.lower() or "topic_closed" in body.lower()
        ):
            thread_gone = "thread not found" in body.lower()
            logging.warning(
                "Topic %s in chat %s is unavailable, retrying without thread.",
                topic_id,
                chat_id,
            )
            data.pop("message_thread_id", None)
            status, body = await _post_send(session, data)
            if status < 400:
                if thread_gone:
                    try:
                        await Chat.remove_topic(chat_id)
                    except Exception:
                        pass
                return

        if status == 403 or (400 <= status < 500):
            await _notify_owner_of_delivery_failure(session, integration, body)
    except aiohttp.ClientError as e:
        logging.error("Error sending to chat %s: %s", chat_id, e)


@router.post("/{token}")
async def webhook(
    req: Request,
    token: str,
    x_forgejo_event: Optional[str] = Header(None, alias="X-Forgejo-Event"),
    x_gitea_event: Optional[str] = Header(None, alias="X-Gitea-Event"),
    x_github_event: Optional[str] = Header(None, alias="X-GitHub-Event"),
):
    raw_event = x_forgejo_event or x_gitea_event or x_github_event or ""
    event_type = EVENT_NAME_MAP.get(raw_event, raw_event)

    payload = await req.json()
    integrations = await Integration.get_by_token(token)

    if not integrations:
        logging.info(
            "Webhook %s for token %s…: no matching integrations",
            event_type,
            token[:6],
        )
        return {"status": "ok", "matched": 0, "sent": 0}

    sent = 0
    skipped_event = 0
    skipped_floodwait = 0
    skipped_no_message = 0

    async with aiohttp.ClientSession() as session:
        for integration in integrations:
            chat = integration.chat
            user = integration.user

            if not await EventSetting.is_enabled(chat.chat_id, event_type):
                skipped_event += 1
                continue

            if event_type == "star" and check_floodwait(
                chat.chat_id, chat.floodwait
            ):
                skipped_floodwait += 1
                continue

            server_url = integration.server_url or (user.server_url if user else None) or "https://codeberg.org"
            ctx = EventCtx(
                auth_token=user.token if user else None,
                server_url=server_url,
                config=get_config(),
            )
            message = build_message(event_type, payload, ctx)
            if not message:
                skipped_no_message += 1
                continue
            await send_message(session, integration, message)
            sent += 1

    logging.info(
        "Webhook %s for token %s…: %d integrations, sent=%d, "
        "skipped(event=%d floodwait=%d no_msg=%d)",
        event_type,
        token[:6],
        len(integrations),
        sent,
        skipped_event,
        skipped_floodwait,
        skipped_no_message,
    )
    return {
        "status": "ok",
        "matched": len(integrations),
        "sent": sent,
        "skipped": {
            "event_disabled": skipped_event,
            "floodwait": skipped_floodwait,
            "no_message": skipped_no_message,
        },
    }
