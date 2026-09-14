#  Moon-Userbot - telegram userbot
#  Copyright (C) 2020-present Moon Userbot Organization
#
#  This program is free software: you can redistribute it and/or modify
#  it under the terms of the GNU General Public License as published by
#  the Free Software Foundation, either version 3 of the License, or
#  (at your option) any later version.
#
#  This module is a Pyrogram/Pyrofork adaptation of the chat module from
#  kastaid/getter (Copyright (C) 2022-present kastaid, AGPL-3.0).

import asyncio
import html
import random
from contextlib import suppress
from typing import AsyncGenerator, Optional, Tuple, Union

from pyrogram import Client, filters, raw
from pyrogram.enums import ChatType, ParseMode
from pyrogram.raw import functions, types
from pyrogram.types import Message
from pyrogram.utils import get_peer_id

from utils import modules_help, prefix
from utils.config import owner_id
from utils.db import db

REACTIONS = (
    "👍",
    "👎",
    "❤",
    "🔥",
    "🥰",
    "👏",
    "😁",
    "🤔",
    "🤯",
    "😱",
    "🤬",
    "😢",
    "🎉",
    "🤩",
    "🤮",
    "💩",
    "🙏",
)

PRIVATE_CHAT_TYPES = {ChatType.PRIVATE, ChatType.BOT}
GROUP_CHAT_TYPES = {ChatType.GROUP, ChatType.SUPERGROUP, ChatType.FORUM}
CHANNEL_CHAT_TYPES = {ChatType.CHANNEL, ChatType.MONOFORUM}


def _chat_target(value: str) -> Union[int, str]:
    """Convert a numeric chat id to int while keeping usernames intact."""
    value = value.strip()
    if value.lstrip("-").isdigit():
        return int(value)
    return value


def _display_name(entity) -> str:
    name = getattr(entity, "full_name", None)
    if name:
        return name
    return (
        getattr(entity, "title", None)
        or getattr(entity, "first_name", None)
        or str(getattr(entity, "id", "Unknown"))
    )


def _error_text(error: Exception) -> str:
    return (
        "<b>Telegram error:</b> "
        f"<code>{html.escape(error.__class__.__name__)}: "
        f"{html.escape(str(error))}</code>"
    )


async def _edit_safely(message: Message, text: str) -> Optional[Message]:
    with suppress(Exception):
        return await message.edit(text)
    return None


async def _finish_bulk(client: Client, message: Message, text: str) -> None:
    """Show a result even if the command's private dialog was deleted."""
    if await _edit_safely(message, text):
        return
    with suppress(Exception):
        await client.send_message("me", text)


async def _temporary_message(
    client: Client, chat_id: int, text: str, seconds: float = 3
) -> None:
    status = await client.send_message(chat_id, text)
    await asyncio.sleep(seconds)
    with suppress(Exception):
        await status.delete()


async def _delete_ids(client: Client, chat_id: int, ids: list[int]) -> int:
    """Delete ids in Telegram-safe chunks and return the reported count."""
    total = 0
    for offset in range(0, len(ids), 100):
        chunk = ids[offset : offset + 100]
        deleted = await client.delete_messages(chat_id, chunk)
        total += int(deleted or 0)
        if offset + 100 < len(ids):
            await asyncio.sleep(0.15)
    return total


async def _delete_private_dialog(client: Client, chat_id: int) -> None:
    peer = await client.resolve_peer(chat_id)
    await client.invoke(
        functions.messages.DeleteHistory(
            peer=peer,
            max_id=0,
            revoke=True,
        )
    )


async def _mute_forever(client: Client, chat_id: int) -> None:
    peer = await client.resolve_peer(chat_id)
    await client.invoke(
        functions.account.UpdateNotifySettings(
            peer=types.InputNotifyPeer(peer=peer),
            settings=types.InputPeerNotifySettings(mute_until=2_147_483_647),
        )
    )


async def _iter_folder_dialogs(
    client: Client, folder_id: int = 0
) -> AsyncGenerator[Tuple[raw.types.Dialog, object, int], None]:
    """Yield raw dialogs from one folder without losing entity metadata."""
    offset_date = 0
    offset_id = 0
    offset_peer = types.InputPeerEmpty()
    previous_offset = None

    while True:
        result = await client.invoke(
            functions.messages.GetDialogs(
                offset_date=offset_date,
                offset_id=offset_id,
                offset_peer=offset_peer,
                limit=100,
                hash=0,
                folder_id=folder_id,
            ),
            sleep_threshold=60,
        )
        dialogs = [
            dialog
            for dialog in result.dialogs
            if isinstance(dialog, raw.types.Dialog)
        ]
        if not dialogs:
            return

        users = {user.id: user for user in result.users}
        chats = {chat.id: chat for chat in result.chats}

        for dialog in dialogs:
            if isinstance(dialog.peer, types.PeerUser):
                entity = users.get(dialog.peer.user_id)
            elif isinstance(dialog.peer, types.PeerChat):
                entity = chats.get(dialog.peer.chat_id)
            else:
                entity = chats.get(dialog.peer.channel_id)

            if entity is not None:
                yield dialog, entity, get_peer_id(dialog.peer)

        last = dialogs[-1]
        last_id = get_peer_id(last.peer)
        last_message = next(
            (
                item
                for item in result.messages
                if not isinstance(item, types.MessageEmpty)
                and get_peer_id(item.peer_id) == last_id
                and item.id == last.top_message
            ),
            None,
        )
        if last_message is None:
            return

        new_offset = (last_id, last.top_message, last_message.date)
        if new_offset == previous_offset:
            return
        previous_offset = new_offset
        offset_id = last.top_message
        offset_date = last_message.date
        offset_peer = await client.resolve_peer(last_id)


async def _resolve_action_chat(client: Client, message: Message):
    if len(message.command) > 1:
        target = _chat_target(message.command[1])
    elif message.reply_to_message:
        replied = message.reply_to_message
        sender = replied.sender_chat or replied.from_user
        target = sender.id if sender else message.chat.id
    else:
        return message.chat
    return await client.get_chat(target)


def _get_botlog_chat() -> Optional[Union[int, str]]:
    target = db.get("core.chat", "botlog_chat", None)
    if target is None:
        return None
    if isinstance(target, str):
        return _chat_target(target)
    return target


# ---------------------------------------------------------------------------
# Message deletion and read state
# ---------------------------------------------------------------------------


@Client.on_message(filters.command(["read", "r"], prefix) & filters.me)
@Client.on_edited_message(filters.command(["read", "r"], prefix) & filters.me)
async def read_chat(client: Client, message: Message):
    with suppress(Exception):
        await message.delete()

    with suppress(Exception):
        await client.read_chat_history(message.chat.id)

    peer = await client.resolve_peer(message.chat.id)
    with suppress(Exception):
        await client.invoke(functions.messages.ReadMentions(peer=peer))
    with suppress(Exception):
        await client.invoke(functions.messages.ReadReactions(peer=peer))


@Client.on_message(filters.command(["del", "d"], prefix) & filters.me)
@Client.on_edited_message(filters.command(["del", "d"], prefix) & filters.me)
async def delete_replied_message(_, message: Message):
    with suppress(Exception):
        await message.delete()
    if message.reply_to_message:
        with suppress(Exception):
            await message.reply_to_message.delete()


@Client.on_message(filters.command(["purge", "pg"], prefix) & filters.me)
async def purge(client: Client, message: Message):
    if not message.reply_to_message:
        return await message.edit("<b>Reply to the first message to purge.</b>")

    first_id = message.reply_to_message.id
    ids = []
    try:
        async for item in client.get_chat_history(
            message.chat.id,
            offset_id=message.id + 1,
            min_id=max(first_id - 1, 0),
            max_id=message.id + 1,
        ):
            if first_id <= item.id <= message.id:
                ids.append(item.id)

        deleted = await _delete_ids(client, message.chat.id, ids)
        await _temporary_message(
            client, message.chat.id, f"<code>Purged {deleted} messages.</code>"
        )
    except Exception as error:
        await _edit_safely(message, _error_text(error))


@Client.on_message(filters.command(["purgeme", "pgm"], prefix) & filters.me)
async def purge_me(client: Client, message: Message):
    ids = []
    try:
        if message.reply_to_message:
            first_id = message.reply_to_message.id
            async for item in client.get_chat_history(
                message.chat.id,
                offset_id=message.id + 1,
                min_id=max(first_id - 1, 0),
                max_id=message.id + 1,
            ):
                if (
                    first_id <= item.id <= message.id
                    and item.from_user
                    and item.from_user.is_self
                ):
                    ids.append(item.id)
        elif len(message.command) > 1 and message.command[1].isdigit():
            wanted = int(message.command[1])
            if wanted < 1:
                return await message.edit("<b>The number must be greater than zero.</b>")

            async for item in client.get_chat_history(
                message.chat.id,
                offset_id=message.id + 1,
                max_id=message.id + 1,
            ):
                if item.from_user and item.from_user.is_self:
                    ids.append(item.id)
                    if len(ids) >= wanted:
                        break
        else:
            return await message.edit(
                "<b>Reply to one of your messages or use "
                f"<code>{prefix}purgeme [number]</code>.</b>"
            )

        deleted = await _delete_ids(client, message.chat.id, ids)
        await _temporary_message(
            client, message.chat.id, f"<code>Purged {deleted} of your messages.</code>"
        )
    except Exception as error:
        await _edit_safely(message, _error_text(error))


@Client.on_message(filters.command("purgeall", prefix) & filters.me)
async def purge_all(client: Client, message: Message):
    replied = message.reply_to_message
    if not replied:
        return await message.edit("<b>Reply to a user's message.</b>")

    sender = replied.from_user or replied.sender_chat
    if sender is None:
        return await message.edit("<b>The message sender could not be determined.</b>")

    await message.edit(
        f"<b>Deleting all messages from {html.escape(_display_name(sender))}...</b>"
    )
    deleted = 0
    try:
        # Search uses a positional offset. Always request the first page again
        # after deleting it, otherwise deleting page one would make page two
        # shift and Telegram would skip messages.
        while True:
            ids = [
                item.id
                async for item in client.search_messages(
                    message.chat.id,
                    from_user=sender.id,
                    limit=100,
                )
            ]
            if not ids:
                break
            page_deleted = await _delete_ids(client, message.chat.id, ids)
            if page_deleted < 1:
                raise RuntimeError(
                    "Telegram did not delete the selected messages; check admin rights"
                )
            deleted += page_deleted

        await _finish_bulk(
            client,
            message,
            f"<code>Purged {deleted} messages from "
            f"{html.escape(_display_name(sender))}.</code>",
        )
    except Exception as error:
        await _finish_bulk(client, message, _error_text(error))


@Client.on_message(filters.command("copy", prefix) & filters.me)
async def copy_message(_, message: Message):
    if not message.reply_to_message:
        return await message.edit("<b>Reply to a message to copy it.</b>")
    try:
        await message.reply_to_message.copy(
            message.chat.id,
            reply_to_message_id=message.reply_to_message.id,
        )
        await message.delete()
    except Exception as error:
        await message.edit(_error_text(error))


# ---------------------------------------------------------------------------
# Dialog cleanup
# ---------------------------------------------------------------------------


@Client.on_message(filters.command(["nodraft", "nodrafts"], prefix) & filters.me)
async def clear_drafts(client: Client, message: Message):
    await message.edit("<code>Processing drafts...</code>")
    count = 0
    try:
        result = await client.invoke(functions.messages.GetAllDrafts())
        for update in getattr(result, "updates", []):
            if not isinstance(update, types.UpdateDraftMessage) or isinstance(
                update.draft, types.DraftMessageEmpty
            ):
                continue

            chat_id = get_peer_id(update.peer)
            reply_to = None
            if getattr(update, "top_msg_id", None):
                reply_to = types.InputReplyToMessage(
                    reply_to_msg_id=update.top_msg_id,
                    top_msg_id=update.top_msg_id,
                )
            await client.invoke(
                functions.messages.SaveDraft(
                    peer=await client.resolve_peer(chat_id),
                    message="",
                    reply_to=reply_to,
                )
            )
            count += 1
            await asyncio.sleep(0.1)
    except Exception as error:
        return await message.edit(_error_text(error))

    if count:
        await message.edit(f"<code>Cleared {count} drafts.</code>")
    else:
        await message.edit("<code>No drafts found.</code>")


@Client.on_message(filters.command(["noghost", "noghosts"], prefix) & filters.me)
async def clear_ghosts(client: Client, message: Message):
    await message.edit("<code>Processing deleted accounts...</code>")
    count = 0
    ghost_ids = []
    try:
        async for dialog in client.get_dialogs():
            if dialog.chat.type not in PRIVATE_CHAT_TYPES:
                continue
            with suppress(Exception):
                user = await client.get_users(dialog.chat.id)
                if user.is_deleted:
                    ghost_ids.append(dialog.chat.id)

        for chat_id in ghost_ids:
            with suppress(Exception):
                await _delete_private_dialog(client, chat_id)
                count += 1
                await asyncio.sleep(0.15)
    except Exception as error:
        return await _finish_bulk(client, message, _error_text(error))

    text = (
        f"<code>Deleted {count} ghost chats.</code>"
        if count
        else "<code>No ghost chats found.</code>"
    )
    await _finish_bulk(client, message, text)


@Client.on_message(filters.command(["cleanuser", "cleanusers"], prefix) & filters.me)
async def clean_users(client: Client, message: Message):
    await message.edit("<code>Processing user chats...</code>")
    count = 0
    me = await client.get_me()
    protected_ids = {me.id, int(owner_id)}

    user_ids = []
    try:
        async for dialog in client.get_dialogs():
            if dialog.chat.type != ChatType.PRIVATE:
                continue
            with suppress(Exception):
                user = await client.get_users(dialog.chat.id)
                if user.id in protected_ids or user.is_self or user.is_bot:
                    continue
                user_ids.append(user.id)

        for user_id in user_ids:
            with suppress(Exception):
                await _delete_private_dialog(client, user_id)
                count += 1
                await asyncio.sleep(0.15)
    except Exception as error:
        return await _finish_bulk(client, message, _error_text(error))

    text = (
        f"<code>Deleted {count} user chats.</code>"
        if count
        else "<code>No user chats found.</code>"
    )
    await _finish_bulk(client, message, text)


@Client.on_message(
    filters.command(
        [
            "nouser",
            "nousers",
            "nobot",
            "nobots",
            "nochannel",
            "nochannels",
            "nogroup",
            "nogroups",
        ],
        prefix,
    )
    & filters.me
)
async def archive_dialog_type(client: Client, message: Message):
    command = message.command[0].lower()
    mode = next(name for name in ("user", "bot", "channel", "group") if name in command)
    await message.edit(f"<code>Processing {mode} dialogs...</code>")

    count = 0
    chat_ids = []
    try:
        async for _, entity, chat_id in _iter_folder_dialogs(client, folder_id=0):
            matches = (
                mode == "user"
                and isinstance(entity, types.User)
                and not entity.bot
                and not entity.deleted
                or mode == "bot"
                and isinstance(entity, types.User)
                and bool(entity.bot)
                and not entity.deleted
                or mode == "channel"
                and isinstance(entity, types.Channel)
                and bool(entity.broadcast)
                or mode == "group"
                and (
                    isinstance(entity, types.Chat)
                    or isinstance(entity, types.Channel)
                    and bool(entity.megagroup)
                )
            )
            if (
                matches
                and chat_id != int(owner_id)
                and not bool(getattr(entity, "is_self", False))
            ):
                chat_ids.append(chat_id)

        # Mutating the folder while paginating through it can skip dialogs,
        # so archive only after all matching ids have been collected.
        for chat_id in chat_ids:
            with suppress(Exception):
                await _mute_forever(client, chat_id)
            try:
                await client.archive_chats(chat_id)
            except Exception:
                continue
            count += 1
            await asyncio.sleep(0.2)
    except Exception as error:
        return await _finish_bulk(client, message, _error_text(error))

    text = (
        f"<code>Archived and muted {count} {mode} dialogs.</code>"
        if count
        else f"<code>No {mode} dialogs found.</code>"
    )
    await _finish_bulk(client, message, text)


# ---------------------------------------------------------------------------
# Sending, saving and reactions
# ---------------------------------------------------------------------------


@Client.on_message(filters.command(["sd", "sdm"], prefix) & filters.me)
async def self_destruct_message(_, message: Message):
    raw_text = message.text or message.caption or ""
    tail = raw_text.split(maxsplit=1)[1] if len(raw_text.split(maxsplit=1)) > 1 else ""
    ttl = 1
    text = tail

    if tail:
        first, *rest = tail.split(maxsplit=1)
        if first.isdigit():
            ttl = int(first)
            text = rest[0] if rest else ""

    if not text and message.reply_to_message:
        text = message.reply_to_message.text or message.reply_to_message.caption or ""
    if not text:
        return await message.delete()

    output = html.escape(text)
    if message.command[0].lower() == "sdm":
        output += f"\n\n<code>Self-destructing in {ttl} seconds.</code>"

    await message.edit(output, parse_mode=ParseMode.HTML)
    await asyncio.sleep(ttl)
    with suppress(Exception):
        await message.delete()


@Client.on_message(filters.command(["send", "dm"], prefix) & filters.me)
async def send_to_chat(client: Client, message: Message):
    if len(message.command) < 2:
        return await message.edit("<b>Give a chat username or id.</b>")

    target = _chat_target(message.command[1])
    raw_text = message.text or message.caption or ""
    parts = raw_text.split(maxsplit=2)
    payload = parts[2] if len(parts) > 2 else ""

    if not payload and not message.reply_to_message:
        return await message.edit("<b>Give text or reply to a message.</b>")

    try:
        if payload:
            sent = await client.send_message(target, html.escape(payload))
        else:
            sent = await message.reply_to_message.copy(target)

        delivered = "Message delivered!"
        sent_link = None
        if sent.chat.type not in PRIVATE_CHAT_TYPES:
            with suppress(Exception):
                sent_link = sent.link
        if sent_link:
            delivered = f'<a href="{html.escape(sent_link)}">{delivered}</a>'
        await message.edit(delivered)
    except Exception as error:
        await message.edit(_error_text(error))


@Client.on_message(
    filters.command(["saved", "savedl", "fsaved", "fsavedl"], prefix)
    & filters.me
)
async def save_message(_, message: Message):
    if not message.reply_to_message:
        return await message.edit("<b>Reply to a message to save it.</b>")

    command = message.command[0].lower()
    target = (_get_botlog_chat() if command.endswith("l") else None) or "me"

    try:
        if command.startswith("f"):
            await message.reply_to_message.forward(target)
        else:
            await message.reply_to_message.copy(target)
        await message.edit("<code>Saved.</code>")
    except Exception as error:
        await message.edit(_error_text(error))


@Client.on_message(filters.command("chatlog", prefix) & filters.me)
async def set_chat_log(_, message: Message):
    if len(message.command) == 1:
        target = _get_botlog_chat()
        status = html.escape(str(target)) if target is not None else "not configured"
        return await message.edit(f"<b>BOTLOG chat:</b> <code>{status}</code>")

    argument = message.command[1].strip()
    if argument.lower() in {"off", "disable", "none"}:
        db.remove("core.chat", "botlog_chat")
        return await message.edit("<b>BOTLOG chat disabled.</b>")

    target: Union[int, str]
    if argument.lower() == "here":
        target = message.chat.id
    elif argument.lower() in {"me", "self"}:
        target = "me"
    else:
        target = _chat_target(argument)

    db.set("core.chat", "botlog_chat", target)
    await message.edit(
        f"<b>BOTLOG chat set to:</b> <code>{html.escape(str(target))}</code>"
    )


@Client.on_message(filters.command("react", prefix) & filters.me)
async def random_reaction(client: Client, message: Message):
    if not message.reply_to_message:
        return await message.edit("<b>Reply to a message to react.</b>")

    reaction = random.choice(REACTIONS)
    try:
        await client.send_reaction(
            message.chat.id,
            message.reply_to_message.id,
            emoji=reaction,
            big=True,
        )
        await message.edit(f"<code>Reacted {reaction}</code>")
    except Exception as error:
        await message.edit(_error_text(error))


# ---------------------------------------------------------------------------
# Reports, invitations and chat management
# ---------------------------------------------------------------------------


@Client.on_message(filters.command(["report_spam", "rs"], prefix) & filters.me)
async def report_spam(client: Client, message: Message):
    await message.edit("<code>Reporting...</code>")
    try:
        if message.reply_to_message and message.reply_to_message.from_user:
            user = message.reply_to_message.from_user
        elif len(message.command) > 1:
            user = await client.get_users(_chat_target(message.command[1]))
        elif message.chat.type in PRIVATE_CHAT_TYPES:
            user = await client.get_users(message.chat.id)
        else:
            return await message.edit(
                "<b>Reply to a user's message or give a username/id.</b>"
            )

        me = await client.get_me()
        if user.id == me.id:
            return await message.edit("<b>You cannot report yourself.</b>")
        if user.id == int(owner_id):
            return await message.edit("<b>This user is protected from reports.</b>")

        user_peer = await client.resolve_peer(user.id)
        replied = message.reply_to_message
        chat_peer = await client.resolve_peer(message.chat.id)

        if (
            replied
            and message.chat.type in GROUP_CHAT_TYPES | CHANNEL_CHAT_TYPES
            and isinstance(chat_peer, types.InputPeerChannel)
        ):
            await client.invoke(
                functions.channels.ReportSpam(
                    channel=chat_peer,
                    participant=user_peer,
                    id=[replied.id],
                )
            )
        else:
            await client.invoke(
                functions.account.ReportPeer(
                    peer=user_peer,
                    reason=types.InputReportReasonSpam(),
                    message="This account sends unsolicited spam messages.",
                )
            )

        mention = (
            f'<a href="tg://user?id={user.id}">'
            f"{html.escape(_display_name(user))}</a>"
        )
        await message.edit(f"<b>{mention} was reported for spam.</b>")
    except Exception as error:
        await message.edit(_error_text(error))


@Client.on_message(filters.command("invite", prefix) & filters.me & filters.group)
async def invite_users(client: Client, message: Message):
    if len(message.command) < 2:
        return await message.edit("<b>Give one or more usernames/ids.</b>")

    invited = []
    failed = []
    for value in message.command[1:]:
        try:
            user = await client.get_users(_chat_target(value))
            await client.add_chat_members(message.chat.id, user.id, forward_limit=100)
            invited.append(_display_name(user))
        except Exception as error:
            failed.append(f"{value}: {error.__class__.__name__}")
        await asyncio.sleep(0.2)

    lines = [f"<b>Invited:</b> <code>{len(invited)}</code>"]
    if invited:
        lines.append(html.escape(", ".join(invited)))
    if failed:
        lines.append("<b>Failed:</b> " + html.escape("; ".join(failed)))
    await message.edit("\n".join(lines))


@Client.on_message(filters.command("kickme", prefix) & filters.me)
async def leave_chat(client: Client, message: Message):
    try:
        chat = await _resolve_action_chat(client, message)
        is_current = chat.id == message.chat.id
        if is_current and chat.type in PRIVATE_CHAT_TYPES:
            return await message.edit("<b>Use this for a group or channel.</b>")
        if chat.type not in GROUP_CHAT_TYPES | CHANNEL_CHAT_TYPES:
            return await message.edit("<b>The target is not a group or channel.</b>")

        if is_current:
            with suppress(Exception):
                await message.delete()
        await client.leave_chat(chat.id)
        if not is_current:
            await message.edit(f"<code>Left chat {chat.id}.</code>")
    except Exception as error:
        await _edit_safely(message, _error_text(error))


@Client.on_message(filters.command(["archive", "unarchive"], prefix) & filters.me)
async def move_archive(client: Client, message: Message):
    try:
        chat = await _resolve_action_chat(client, message)
        is_current = chat.id == message.chat.id
        if is_current and chat.type in PRIVATE_CHAT_TYPES:
            return await message.edit("<b>Use this for a group or channel.</b>")

        if is_current:
            with suppress(Exception):
                await message.delete()

        if message.command[0].lower() == "unarchive":
            await client.unarchive_chats(chat.id)
            result = "Unarchived"
        else:
            await client.archive_chats(chat.id)
            result = "Archived"

        if not is_current:
            await message.edit(f"<code>{chat.id}: {result}.</code>")
    except Exception as error:
        await _edit_safely(message, _error_text(error))


@Client.on_message(filters.command("delchannel", prefix) & filters.me)
async def delete_channel(client: Client, message: Message):
    try:
        chat = await _resolve_action_chat(client, message)
        is_current = chat.id == message.chat.id
        if chat.type not in CHANNEL_CHAT_TYPES | {ChatType.SUPERGROUP, ChatType.FORUM}:
            return await message.edit("<b>The target is not a channel or supergroup.</b>")

        if is_current:
            with suppress(Exception):
                await message.delete()
        await client.delete_channel(chat.id)
        if not is_current:
            await message.edit(f"<code>Deleted channel {chat.id}.</code>")
    except Exception as error:
        await _edit_safely(message, _error_text(error))


modules_help["chat"] = {
    "read | r": "Mark the current chat as read and clear mentions/reactions",
    "del | d [reply]*": "Delete the replied message",
    "purge | pg [reply]*": "Delete all messages from the replied message through the command",
    "purgeme | pgm [number]/[reply]*": "Delete only your recent messages",
    "purgeall [reply]*": "Delete every message from the replied sender in this chat",
    "copy [reply]*": "Copy the replied message into the current chat",
    "nodraft": "Clear all Telegram drafts",
    "noghost": "Delete private dialogs with deleted accounts",
    "cleanuser": "Delete all non-bot private dialogs",
    "nouser | nobot | nochannel | nogroup": "Archive and mute dialogs of the selected type",
    "sd [seconds] [text]/[reply]": "Send a self-destructing message",
    "sdm [seconds] [text]/[reply]": "Self-destructing message with a timer note",
    "send | dm [chat] [text]/[reply]": "Send text or a copy of the replied message to another chat",
    "saved | fsaved [reply]*": "Copy/forward a message to Saved Messages",
    "savedl | fsavedl [reply]*": "Copy/forward a message to the configured BOTLOG chat",
    "chatlog [me|here|chat_id|@username|off]": "Configure the BOTLOG destination used by savedl/fsavedl",
    "react [reply]*": "Add a random reaction to the replied message",
    "report_spam | rs [reply]/[username/id]": "Report a user for spam",
    "invite [username/id] ...": "Invite users to the current group",
    "kickme [chat_id/username]": "Leave the current or selected group/channel",
    "archive [chat_id/username]": "Move the current or selected chat to Archive",
    "unarchive [chat_id/username]": "Remove the current or selected chat from Archive",
    "delchannel [chat_id/username]": "Delete a channel or supergroup you own",
}
