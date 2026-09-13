from dataclasses import dataclass

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError

from app.config import Config
from app.db.functions import Chat, Integration, User
from app.utils.forgejo import HookError, check_repo, create_webhook


@dataclass
class IntegrationResult:
    success: bool
    message: str


async def integrate_repo(
    bot: Bot,
    chat_id: int,
    telegram_user_id: int,
    repo_name: str,
    config: Config,
    *,
    skip_admin_check: bool = False,
) -> IntegrationResult:
    if not skip_admin_check:
        try:
            admins = await bot.get_chat_administrators(chat_id)
        except TelegramAPIError:
            return IntegrationResult(
                False,
                "I don't have access to that chat anymore. Make sure I'm "
                "still a member there.",
            )
        if telegram_user_id not in [a.user.id for a in admins]:
            return IntegrationResult(
                False, "You're no longer an administrator in that chat."
            )

    user = await User.get_or_none(telegram_id=telegram_user_id)
    if user is None:
        return IntegrationResult(
            False,
            "You're not registered. Send /start to me in private chat first.",
        )

    if not user.token:
        return IntegrationResult(
            False,
            "You haven't set up your Forgejo / Gitea token yet. "
            "Tap <b>🔌 Connect</b> in DM to connect your account.",
        )

    server_url = user.server_url or "https://codeberg.org"

    existing = await Integration.get_by_chat_and_repo(
        chat_id=chat_id, repo_name=repo_name
    )
    if existing:
        return IntegrationResult(
            False,
            f"<code>{repo_name}</code> is already integrated in this chat.",
        )

    ok, repo_or_error = await check_repo(server_url, user.token, repo_name)
    if not ok and isinstance(repo_or_error, HookError):
        return IntegrationResult(False, repo_or_error.message)

    await Chat.ensure_registered(chat_id)

    integration, reused_existing = await Chat.add_integration(
        chat_id=chat_id,
        user_id=user.id,
        repository_name=repo_name,
        server_url=server_url,
    )

    if not reused_existing:
        hook_err = await create_webhook(
            server_url=server_url,
            token=user.token,
            repo_name=repo_name,
            host=config.api.host,
            endpoint=integration.integration_token,
        )
        if isinstance(hook_err, HookError):
            await Integration.delete_by_id(integration.id)
            return IntegrationResult(
                False,
                f"Couldn't create the webhook for <code>{repo_name}</code> on {server_url}.\n\n"
                f"{hook_err.message}",
            )

    return IntegrationResult(
        True,
        f"✅ Repository <code>{repo_name}</code> ({server_url}) integrated. "
        "Notifications will arrive in the chat.",
    )
