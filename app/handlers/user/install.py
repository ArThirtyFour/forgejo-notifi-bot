from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import Message
from aiogram_dialog import DialogManager, StartMode

from app.dialogs.token import TokenSG

router = Router()
router.message.filter(F.chat.type == "private")


@router.message(Command(commands=["install"]))
async def cmd_install(message: Message, dialog_manager: DialogManager):
    await dialog_manager.start(TokenSG.main, mode=StartMode.RESET_STACK)
