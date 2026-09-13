from aiogram import Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import Message

from app.db.functions import User
from app.keyboards.main_menu import main_menu_keyboard

router = Router()


WELCOME = (
    "👋 <b>Hi! I'm a Forgejo / Gitea Notifier bot.</b>\n\n"
    "I deliver real-time notifications from your Forgejo and Gitea repositories "
    "(Codeberg, self-hosted instances, etc.) — commits, pull requests, issues, releases, "
    "and more — into Telegram chats via webhooks.\n\n"
    "📌 <b>First step:</b> tap <b>🔌 Connect</b> below to choose your server and enter your "
    "Access Token. Then use <b>➕ Add to chat</b> to invite me to a group, and "
    "<b>🏢 Repos</b> to pick what to integrate."
)


HELP = (
    "<b>Buttons in DM:</b>\n"
    "• <b>🔌 Connect</b> — select Forgejo server and set Access Token\n"
    "• <b>➕ Add to chat</b> — invite me to a group via Telegram's chat picker\n"
    "• <b>🏢 Repos</b> — browse repositories and integrate them into chats\n"
    "• <b>💬 My chats</b> — overview of chats with your integrations\n"
    "• <b>❓ Help</b> — show this message\n\n"
    "<b>Commands in groups (admins only):</b>\n"
    "• <code>/integrate owner/repo</code> — add a repo to this chat\n"
    "• <code>/integrations</code> — list and manage repos integrated here\n"
    "• <code>/events</code> — toggle which event types are delivered\n"
    "• <code>/set_topic</code> — deliver to current forum topic\n"
    "• <code>/reinstall</code> — re-sync webhook subscriptions\n"
    "• <code>/delete owner/repo</code> — remove an integration"
)


@router.message(CommandStart())
async def cmd_start(message: Message, command: CommandObject):
    if message.from_user is None:
        return
    if message.chat.id != message.from_user.id:
        return await message.answer(
            "Please send /start to me in <b>private chat</b> to register."
        )

    user_id = message.from_user.id
    if not await User.is_registered(user_id):
        await User.register(user_id)

    await message.answer(WELCOME, reply_markup=main_menu_keyboard())


@router.message(Command(commands=["help"]))
async def cmd_help(message: Message):
    is_dm = (
        message.from_user is not None
        and message.chat.id == message.from_user.id
    )
    if is_dm:
        await message.answer(HELP, reply_markup=main_menu_keyboard())
    else:
        await message.answer(HELP)
