#  Moon-Userbot - telegram userbot
#  Copyright (C) 2020-present Moon Userbot Organization
#
#  This program is free software: you can redistribute it and/or modify
#  it under the terms of the GNU General Public License as published by
#  the Free Software Foundation, either version 3 of the License, or
#  (at your option) any later version.

#  This program is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#  GNU General Public License for more details.

#  You should have received a copy of the GNU General Public License
#  along with this program.  If not, see <https://www.gnu.org/licenses/>.

import asyncio

from pyrogram import Client, filters
from pyrogram.errors import FloodWait
from pyrogram.types import Message

from utils import modules_help, prefix
from utils.scripts import format_exc

commands = ["spam", "statspam", "slowspam", "fastspam"]


@Client.on_message(filters.command(commands, prefix) & filters.me)
async def spam(client: Client, message: Message):
    if len(message.command) < 3:
        return await message.edit(
            f"<b>Usage:</b> <code>{prefix}{message.command[0]} [amount 1-100] [text]</code>"
        )

    try:
        amount = int(message.command[1])
    except ValueError:
        return await message.edit("<b>Amount must be a number!</b>")

    amount = max(1, min(amount, 100))
    text = " ".join(message.command[2:])

    if not text.strip():
        return await message.edit("<b>Text to spam is empty!</b>")

    cooldown = {"spam": 0.15, "statspam": 0.1, "slowspam": 0.9, "fastspam": 0}

    await message.delete()

    for _msg in range(amount):
        try:
            if message.reply_to_message:
                sent = await message.reply_to_message.reply(text)
            else:
                sent = await client.send_message(message.chat.id, text)
        except FloodWait as e:
            await asyncio.sleep(e.value + 1)
            continue
        except Exception as e:
            await client.send_message(
                message.chat.id, f"<b>Spam stopped:</b>\n{format_exc(e)}"
            )
            break

        if message.command[0] == "statspam":
            await asyncio.sleep(0.1)
            try:
                await sent.delete()
            except Exception:
                pass

        await asyncio.sleep(cooldown[message.command[0]])


modules_help["spam"] = {
    "spam [amount] [text]": "Start spam",
    "statspam [amount] [text]": "Send and delete",
    "fastspam [amount] [text]": "Start fast spam",
    "slowspam [amount] [text]": "Start slow spam",
}
