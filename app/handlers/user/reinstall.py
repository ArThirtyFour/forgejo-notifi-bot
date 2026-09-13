from aiogram import Bot, Router
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command
from aiogram.types import Message

from app.config import Config
from app.db.functions import Chat, User
from app.handlers.user.event_settings import invalidate_subscription_cache
from app.utils.forgejo import HookError, update_webhook

router = Router()


async def _is_admin(bot: Bot, chat_id: int, user_id: int) -> bool | None:
    try:
        admins = await bot.get_chat_administrators(chat_id)
    except (TelegramBadRequest, TelegramForbiddenError):
        return None
    return user_id in [a.user.id for a in admins]


@router.message(Command(commands=["reinstall"]))
async def reinstall_handler(message: Message, bot: Bot, config: Config):
    if message.from_user is None:
        return
    if message.chat.id == message.from_user.id:
        return await message.answer(
            "This command works only in a group or channel."
        )

    is_admin = await _is_admin(bot, message.chat.id, message.from_user.id)
    if is_admin is None:
        return await message.answer(
            "I can't read the admin list in this chat. "
            "Please grant me <b>administrator</b> rights and try again."
        )
    if not is_admin:
        return await message.answer(
            "Only chat <b>administrators</b> can run /reinstall."
        )

    integrations = await Chat.get_integrations(message.chat.id)
    if not integrations:
        return await message.answer("No integrations in this chat to reinstall.")

    progress = await message.answer(
        f"🔄 Reinstalling {len(integrations)} webhook(s)…"
    )

    successes: list[str] = []
    failures: list[tuple[str, str]] = []

    for integration in integrations:
        user = await User.get_or_none(id=integration.user_id)
        if user is None or not user.token:
            failures.append(
                (
                    integration.repository_name,
                    "Owner of this integration has no token saved.",
                )
            )
            continue

        server_url = integration.server_url or user.server_url or "https://codeberg.org"
        result = await update_webhook(
            server_url=server_url,
            token=user.token,
            repo_name=integration.repository_name,
            host=config.api.host,
            endpoint=integration.integration_token,
        )

        if isinstance(result, HookError):
            failures.append((integration.repository_name, result.message))
        else:
            successes.append(integration.repository_name)

    if successes:
        invalidate_subscription_cache(message.chat.id)

    parts = []
    if successes:
        parts.append(
            "✅ <b>Updated:</b>\n"
            + "\n".join(f"• <code>{name}</code>" for name in successes)
        )
    if failures:
        failed_block = "❌ <b>Failed:</b>\n" + "\n".join(
            f"• <code>{name}</code> — {msg}" for name, msg in failures
        )
        parts.append(failed_block)

    text = "\n\n".join(parts) if parts else "Nothing happened."
    try:
        await progress.edit_text(text)
    except TelegramBadRequest:
        await message.answer(text)
