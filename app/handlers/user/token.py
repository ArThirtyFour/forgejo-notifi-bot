from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message
from aiogram_dialog import DialogManager, StartMode

from app.dialogs.token import TokenSG

router = Router()


@router.message(Command(commands=["token", "connect"]))
async def cmd_token(message: Message, dialog_manager: DialogManager):
    if message.from_user is None:
        return
    if message.chat.id != message.from_user.id:
        return await message.answer(
            "Please use /connect in private chat."
        )

    await dialog_manager.start(TokenSG.main, mode=StartMode.RESET_STACK)
