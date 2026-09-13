from typing import Any, Optional

from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from aiogram_dialog import Dialog, DialogManager, Window
from aiogram_dialog.widgets.input import MessageInput
from aiogram_dialog.widgets.kbd import Button, Cancel, Group, Row, SwitchTo
from aiogram_dialog.widgets.text import Const, Format

from app.db.functions import User
from app.utils.dialog_helpers import current_user_for_manager as _current_user
from app.utils.dialog_state import DialogState
from app.utils.forgejo import (
    invalidate_for_user,
    normalize_server_url,
    validate_server,
    validate_token,
)


class TokenSG(StatesGroup):
    main = State()
    select_server = State()
    awaiting_custom_server = State()
    awaiting_token = State()
    confirm_remove = State()


class TokenState(DialogState):
    selected_server: Optional[str] = None
    error: Optional[str] = None


def _mask(token: str) -> str:
    if len(token) <= 10:
        return "•" * len(token)
    return f"{token[:4]}…{token[-4:]}"


async def main_getter(dialog_manager: DialogManager, **_: Any) -> dict[str, Any]:
    user = await _current_user(dialog_manager)
    server_url = (user.server_url if user and user.server_url else "https://codeberg.org")
    has_token = bool(user and user.token)

    if has_token:
        token_line = f"✅ Saved (<code>{_mask(user.token or '')}</code>)"
    else:
        token_line = "❌ No token saved"

    return {
        "server_url": server_url,
        "token_line": token_line,
        "has_token": has_token,
    }


async def awaiting_server_getter(
    dialog_manager: DialogManager, **_: Any
) -> dict[str, Any]:
    state = TokenState.load(dialog_manager)
    return {
        "error": state.error or "",
        "has_error": bool(state.error),
    }


async def awaiting_token_getter(
    dialog_manager: DialogManager, **_: Any
) -> dict[str, Any]:
    user = await _current_user(dialog_manager)
    state = TokenState.load(dialog_manager)
    server_url = state.selected_server or (
        user.server_url if user and user.server_url else "https://codeberg.org"
    )
    return {
        "server_url": server_url,
        "settings_url": f"{server_url}/user/settings/applications",
        "error": state.error or "",
        "has_error": bool(state.error),
    }


async def on_change_server_clicked(
    callback: CallbackQuery, button: Button, manager: DialogManager
) -> None:
    state = TokenState.load(manager)
    state.error = None
    state.save(manager)
    await manager.switch_to(TokenSG.select_server)


async def on_preset_server(
    callback: CallbackQuery, button: Button, manager: DialogManager
) -> None:
    presets = {
        "srv_codeberg": "https://codeberg.org",
        "srv_forgejo": "https://next.forgejo.org",
        "srv_disroot": "https://git.disroot.org",
    }
    server_url = presets.get(button.widget_id, "https://codeberg.org")
    state = TokenState.load(manager)
    state.selected_server = server_url
    state.error = None
    state.save(manager)
    await manager.switch_to(TokenSG.awaiting_token)


async def on_custom_server_clicked(
    callback: CallbackQuery, button: Button, manager: DialogManager
) -> None:
    state = TokenState.load(manager)
    state.error = None
    state.save(manager)
    await manager.switch_to(TokenSG.awaiting_custom_server)


async def on_custom_server_message(
    message: Message, _input: MessageInput, manager: DialogManager
) -> None:
    raw_url = (message.text or "").strip()
    state = TokenState.load(manager)

    normalized = normalize_server_url(raw_url)
    ok, ver_or_err = await validate_server(normalized)
    if not ok:
        state.error = f"Cannot reach Forgejo/Gitea at {normalized}: {ver_or_err}"
        state.save(manager)
        return

    state.selected_server = normalized
    state.error = None
    state.save(manager)
    await manager.switch_to(TokenSG.awaiting_token)


async def on_update_token_clicked(
    callback: CallbackQuery, button: Button, manager: DialogManager
) -> None:
    user = await _current_user(manager)
    state = TokenState.load(manager)
    state.selected_server = (
        user.server_url if user and user.server_url else "https://codeberg.org"
    )
    state.error = None
    state.save(manager)
    await manager.switch_to(TokenSG.awaiting_token)


async def on_test_clicked(
    callback: CallbackQuery, button: Button, manager: DialogManager
) -> None:
    user = await _current_user(manager)
    if user is None or not user.token:
        await callback.answer("No token saved to test.", show_alert=True)
        return

    server_url = user.server_url or "https://codeberg.org"
    ok, user_or_err = await validate_token(server_url, user.token)
    if ok and isinstance(user_or_err, dict):
        username = user_or_err.get("username") or user_or_err.get("login") or "user"
        await callback.answer(
            f"✅ Valid! Authenticated as @{username} on {server_url}",
            show_alert=True,
        )
    else:
        await callback.answer(
            f"❌ Connection failed: {user_or_err}",
            show_alert=True,
        )


async def on_remove_clicked(
    callback: CallbackQuery, button: Button, manager: DialogManager
) -> None:
    await manager.switch_to(TokenSG.confirm_remove)


async def on_remove_confirmed(
    callback: CallbackQuery, button: Button, manager: DialogManager
) -> None:
    user = await _current_user(manager)
    if user is not None:
        await User.filter(id=user.id).update(token=None)
        invalidate_for_user(user)
    await manager.switch_to(TokenSG.main)


async def on_back_to_main(
    callback: CallbackQuery, button: Button, manager: DialogManager
) -> None:
    state = TokenState.load(manager)
    state.error = None
    state.save(manager)
    await manager.switch_to(TokenSG.main)


async def on_token_message(
    message: Message, _input: MessageInput, manager: DialogManager
) -> None:
    token = (message.text or "").strip()
    try:
        await message.delete()
    except Exception:
        pass

    state = TokenState.load(manager)
    user = await _current_user(manager)
    server_url = state.selected_server or (
        user.server_url if user and user.server_url else "https://codeberg.org"
    )

    ok, user_or_err = await validate_token(server_url, token)
    if not ok:
        state.error = str(user_or_err)
        state.save(manager)
        return

    if message.from_user is None:
        state.error = "Could not identify your Telegram account."
        state.save(manager)
        return

    user_id = message.from_user.id
    if await User.get_or_none(telegram_id=user_id) is None:
        await User.register(user_id, server_url=server_url)
    await User.set_server_and_token(user_id, server_url, token)

    db_user = await User.get_or_none(telegram_id=user_id)
    if db_user:
        invalidate_for_user(db_user)

    state.error = None
    state.selected_server = None
    state.save(manager)
    await manager.switch_to(TokenSG.main)


main_window = Window(
    Const("🌐 <b>Forgejo / Gitea Connection</b>\n"),
    Format("🌐 <b>Server:</b> <code>{server_url}</code>"),
    Format("🔑 <b>Access Token:</b> {token_line}\n"),
    Row(
        Button(
            Const("🌐 Change Server"),
            id="change_server",
            on_click=on_change_server_clicked,
        ),
        Button(
            Const("🔑 Update Token"),
            id="update_token",
            on_click=on_update_token_clicked,
        ),
    ),
    Row(
        Button(
            Const("🧪 Test Connection"),
            id="test_conn",
            on_click=on_test_clicked,
            when="has_token",
        ),
        Button(
            Const("🗑 Remove"),
            id="rm_conn",
            on_click=on_remove_clicked,
            when="has_token",
        ),
    ),
    Cancel(Const("❎ Close")),
    state=TokenSG.main,
    getter=main_getter,
)

select_server_window = Window(
    Const(
        "🌐 <b>Select your Forgejo / Gitea Server</b>\n\n"
        "Choose a popular instance below or enter your self-hosted URL:"
    ),
    Group(
        Button(
            Const("🏔 Codeberg.org"),
            id="srv_codeberg",
            on_click=on_preset_server,
        ),
        Button(
            Const("🦊 Forgejo Next (next.forgejo.org)"),
            id="srv_forgejo",
            on_click=on_preset_server,
        ),
        Button(
            Const("🌱 Disroot Git (git.disroot.org)"),
            id="srv_disroot",
            on_click=on_preset_server,
        ),
        Button(
            Const("✏️ Enter custom server URL…"),
            id="srv_custom",
            on_click=on_custom_server_clicked,
        ),
        width=1,
    ),
    Button(Const("◀️ Back"), id="back_main", on_click=on_back_to_main),
    state=TokenSG.select_server,
)

awaiting_custom_server_window = Window(
    Const(
        "🌐 <b>Enter your Forgejo / Gitea server URL</b>\n\n"
        "Example: <code>https://git.example.com</code> or <code>codeberg.org</code>\n\n"
        "Send the URL as a text message."
    ),
    Format("\n❌ {error}", when="has_error"),
    MessageInput(on_custom_server_message),
    SwitchTo(
        Const("◀️ Back"),
        id="back_select",
        state=TokenSG.select_server,
    ),
    state=TokenSG.awaiting_custom_server,
    getter=awaiting_server_getter,
)

awaiting_token_window = Window(
    Format(
        "🔑 <b>Send your Personal Access Token for {server_url}</b>\n\n"
        "1. Open: {settings_url}\n"
        "2. Click <b>Generate New Token</b>\n"
        "3. Required permissions:\n"
        "• <code>read:organization</code>\n"
        "• <code>read:repository</code>\n"
        "• <code>write:repository</code> (or <code>repo:hook</code> to create webhooks)\n\n"
        "🔒 <i>The message containing your token will be auto-deleted immediately.</i>"
    ),
    Format("\n❌ {error}", when="has_error"),
    MessageInput(on_token_message),
    Button(Const("◀️ Cancel"), id="cancel", on_click=on_back_to_main),
    state=TokenSG.awaiting_token,
    getter=awaiting_token_getter,
)

confirm_remove_window = Window(
    Const(
        "⚠️ <b>Are you sure you want to disconnect?</b>\n\n"
        "Your saved token will be removed from the bot. "
        "Existing webhooks will continue firing, but you won't be able "
        "to manage or add new repos until you reconnect."
    ),
    Row(
        Button(Const("✅ Yes, remove"), id="yes", on_click=on_remove_confirmed),
        Button(Const("◀️ Cancel"), id="no", on_click=on_back_to_main),
    ),
    state=TokenSG.confirm_remove,
)

token_dialog = Dialog(
    main_window,
    select_server_window,
    awaiting_custom_server_window,
    awaiting_token_window,
    confirm_remove_window,
)
