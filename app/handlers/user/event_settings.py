import asyncio
import time

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from app.config import Config
from app.db.functions import Chat, EventSetting, User
from app.db.models import EventType
from app.utils.aiogram_helpers import accessible_message, safe_edit_markup
from app.utils.group_admin import is_user_admin
from app.utils.forgejo import HookError, get_subscribed_events_for

router = Router()


EVENT_LABELS: dict[str, str] = {
    "ping": "Ping",
    "push": "Push",
    "issues": "Issues",
    "issue_comment": "Issue comments",
    "pull_request": "Pull requests",
    "pull_request_review": "PR reviews",
    "pull_request_review_comment": "PR review comments",
    "commit_comment": "Commit comments",
    "star": "Stars",
    "fork": "Forks",
    "create": "Branch/tag created",
    "delete": "Branch/tag deleted",
    "release": "Releases",
    "workflow_run": "CI runs",
    "discussion": "Discussions",
    "discussion_comment": "Discussion comments",
    "deployment_status": "Deployments",
    "member": "Members",
    "public": "Repo made public",
}

_SUBSCRIPTION_CACHE: dict[int, tuple[set[str] | None, float]] = {}
_CACHE_TTL = 120.0


def invalidate_subscription_cache(chat_id: int) -> None:
    _SUBSCRIPTION_CACHE.pop(chat_id, None)


def _label(event_type: str) -> str:
    return EVENT_LABELS.get(event_type, event_type)


def build_keyboard(
    settings: list[EventSetting], available: set[str] | None
) -> InlineKeyboardMarkup:
    settings = sorted(
        settings,
        key=lambda s: list(EVENT_LABELS).index(s.event_type)
        if s.event_type in EVENT_LABELS
        else len(EVENT_LABELS),
    )

    def render(s: EventSetting) -> str:
        state = "✅" if s.enabled else "❌"
        if (
            available is not None
            and s.event_type != "ping"
            and s.event_type not in available
        ):
            return f"⚠️{state} {_label(s.event_type)}"
        return f"{state} {_label(s.event_type)}"

    buttons = [
        InlineKeyboardButton(
            text=render(s),
            callback_data=f"toggle_event:{s.event_type}",
        )
        for s in settings
    ]
    return InlineKeyboardMarkup(
        inline_keyboard=[buttons[i : i + 2] for i in range(0, len(buttons), 2)]
    )


async def compute_available_events(
    chat_id: int, host: str
) -> set[str] | None:
    cached = _SUBSCRIPTION_CACHE.get(chat_id)
    now = time.monotonic()
    if cached and cached[1] > now:
        return cached[0]

    integrations = await Chat.get_integrations(chat_id)
    if not integrations:
        _SUBSCRIPTION_CACHE[chat_id] = (None, now + _CACHE_TTL)
        return None

    results: list[set[str] | None] = []
    for integration in integrations:
        user = await User.get_or_none(id=integration.user_id)
        if user is None or not user.token:
            _SUBSCRIPTION_CACHE[chat_id] = (None, now + _CACHE_TTL)
            return None
        server_url = integration.server_url or user.server_url or "https://codeberg.org"
        res = await get_subscribed_events_for(
            server_url,
            user.token,
            integration.repository_name,
            host,
            integration.integration_token,
        )
        results.append(res if isinstance(res, set) else None)

    sets: list[set[str]] = [r for r in results if r is not None]
    if len(sets) != len(results) or not sets:
        _SUBSCRIPTION_CACHE[chat_id] = (None, now + _CACHE_TTL)
        return None

    intersection = set.intersection(*sets)
    _SUBSCRIPTION_CACHE[chat_id] = (intersection, now + _CACHE_TTL)
    return intersection


def _stale_events_text(available: set[str] | None) -> str:
    if available is None:
        return ""
    all_events = {e.value for e in EventType} - {"ping"}
    stale = all_events - available
    if not stale:
        return ""
    return (
        "\n\n⚠️ Some events are not subscribed on the server side "
        "(integration was created before they were supported). "
        "Run /reinstall to update webhook subscriptions."
    )


async def render_events_message(
    chat_id: int, config: Config
) -> tuple[str, InlineKeyboardMarkup]:
    await Chat.ensure_registered(chat_id)
    available = await compute_available_events(chat_id, config.api.host)
    settings = await EventSetting.for_chat(chat_id)
    text = "✨ Events settings" + _stale_events_text(available)
    return text, build_keyboard(settings, available)


@router.message(Command("events"))
async def show_event_settings(message: Message, bot: Bot, config: Config):
    if message.from_user is None:
        return
    if message.chat.id == message.from_user.id:
        return await message.answer(
            "You can manage event settings only in a group or channel."
        )

    if not await is_user_admin(bot, message.chat.id, message.from_user.id):
        return await message.answer(
            "Only administrators can change event settings."
        )

    text, kb = await render_events_message(message.chat.id, config)
    await message.answer(text, reply_markup=kb)


@router.callback_query(F.data.startswith("toggle_event:"))
async def toggle_event_setting(
    callback: CallbackQuery, bot: Bot, config: Config
):
    msg = accessible_message(callback)
    if msg is None:
        return await callback.answer(
            "Original message is no longer accessible.", show_alert=True
        )
    if not await is_user_admin(bot, msg.chat.id, callback.from_user.id):
        return await callback.answer(
            "Only administrators can change event settings.", show_alert=True
        )
    if callback.data is None:
        return await callback.answer()

    try:
        event_type = EventType(callback.data.split(":")[1])
    except ValueError:
        return await callback.answer("Unknown event type.", show_alert=True)

    available = await compute_available_events(msg.chat.id, config.api.host)
    setting = await EventSetting.get_or_none(
        chat_id=msg.chat.id, event_type=event_type.value
    )
    if setting is None:
        return await callback.answer("Setting not found.", show_alert=True)

    is_stale = (
        available is not None
        and event_type.value != "ping"
        and event_type.value not in available
    )
    if is_stale and not setting.enabled:
        return await callback.answer(
            "This event isn't subscribed on the server side. "
            "Run /reinstall to update webhook subscriptions before enabling it.",
            show_alert=True,
        )

    setting.enabled = not setting.enabled
    await setting.save()

    updated = await EventSetting.for_chat(msg.chat.id)
    await safe_edit_markup(msg, build_keyboard(updated, available))
    await callback.answer(
        f"{event_type.value} {'enabled' if setting.enabled else 'disabled'}"
    )
