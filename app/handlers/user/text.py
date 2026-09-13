from aiogram import F, Router
from aiogram.types import Message

from app.db.functions import User
from app.utils.filters import IS_DM
from app.utils.forgejo import HookError, check_repo

router = Router()


@router.message(IS_DM, F.text)
async def dm_text_handler(message: Message):
    text = (message.text or "").strip()
    if not text or text.startswith("/"):
        return
    if message.from_user is None:
        return

    user = await User.get_or_none(telegram_id=message.from_user.id)
    if user is None:
        await message.answer(
            "You are not registered. Please use /start to register."
        )
        return

    if user.token:
        await _show_repo_summary(message, user, text)
    else:
        await message.answer(
            "Tap <b>🔌 Connect</b> on the keyboard or send /connect "
            "to connect your Forgejo/Gitea account."
        )


async def _show_repo_summary(
    message: Message, user: User, repo_name: str
) -> None:
    server_url = user.server_url or "https://codeberg.org"
    ok, repo_or_err = await check_repo(server_url, user.token or "", repo_name)
    if not ok and isinstance(repo_or_err, HookError):
        await message.answer(f"❌ {repo_or_err.message}")
        return

    if isinstance(repo_or_err, dict):
        full_name = repo_or_err.get("full_name") or repo_name
        is_private = bool(repo_or_err.get("private", False))
        stars = repo_or_err.get("stars_count", 0)
        desc = repo_or_err.get("description") or "No description"
        html_url = repo_or_err.get("html_url") or f"{server_url}/{full_name}"

        summary = (
            f"📐 <b>{'🔒' if is_private else ''} "
            f"<a href='{html_url}'>{full_name}</a></b> "
            f"{stars} ⭐️\n"
            f"<i>{desc}</i>\n\n"
            f"Server: <code>{server_url}</code>\n"
            f"Run in a group: <code>/integrate {full_name}</code> "
            "to start receiving notifications."
        )
        await message.answer(summary)
