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

import datetime
import random

import aiohttp
from dulwich.refs import Ref
from pyrogram import Client, filters
from pyrogram.types import Message

from utils import gitrepo, modules_help, prefix, python_version, userbot_version
from utils.config import (
    db_type,
    owner_id,
    quotes_api,
    rmbg_key,
    vt_key,
)
from utils.module import ModuleManager


@Client.on_message(filters.command(["support", "repo"], prefix) & filters.me)
async def support(_, message: Message):
    devs = ["@Qbtaumai", "@H4T3H46K3R"]
    random.shuffle(devs)

    commands_count = sum(len(commands) for commands in modules_help.values())

    await message.edit(
        f"<b>Moon-Userbot\n\n"
        "GitHub: <a href=https://github.com/The-MoonTg-project/Moon-Userbot>Moon-Userbot</a>\n"
        "Custom modules repository: <a href=https://github.com/The-MoonTg-project/custom_modules>"
        "custom_modules</a>\n"
        "License: <a href=https://github.com/The-MoonTg-project/Moon-Userbot/blob/master/LICENSE>GNU GPL v3</a>\n\n"
        "Channel: @moonuserbot\n"
        "Custom modules: @moonub_modules\n"
        "Chat [EN]: @moonub_chat\n"
        f"Main developers: {', '.join(devs)}\n\n"
        f"Python version: {python_version}\n"
        f"Modules count: {len(modules_help) / 1}\n"
        f"Commands count: {commands_count}</b>",
        disable_web_page_preview=True,
    )


@Client.on_message(filters.command(["version", "ver"], prefix) & filters.me)
async def version(client: Client, message: Message):
    changelog = ""
    ub_version = ".".join(userbot_version.split(".")[:2])
    async for m in client.search_messages("moonuserbot", query=f"{userbot_version}."):
        if ub_version in m.text:
            changelog = m.message_id

    await message.delete()

    if gitrepo is None:
        await message.reply(
            f"<b>Moon Userbot version: {userbot_version}\n"
            f"Changelog </b><i><a href=https://t.me/moonuserbot/{changelog}>in channel</a></i>.<b>\n"
            f"Git info unavailable (deployed without .git)</b>",
        )
        return

    config = gitrepo.get_config()
    try:
        remote_url = config.get((b"remote", b"origin"), b"url").decode("utf-8")
        if remote_url.endswith(".git"):
            remote_url = remote_url[:-4]
    except KeyError:
        remote_url = "https://github.com/The-MoonTg-project/Moon-Userbot"

    head_sha = gitrepo.head()
    hexsha = head_sha.decode("utf-8")
    commit_obj = gitrepo.get_object(head_sha)

    commit_time = (
        datetime.datetime.fromtimestamp(commit_obj.commit_time)
        .astimezone(datetime.timezone.utc)
        .strftime("%Y-%m-%d %H:%M:%S %Z")
    )

    _, ref_path = gitrepo.refs.follow(Ref(b"HEAD"))
    if ref_path:
        active_branch = ref_path.split(b"/")[-1].decode("utf-8")
    else:
        active_branch = "detached"

    author_name = commit_obj.author.decode("utf-8").split("<")[0].strip()

    await message.reply(
        f"<b>Moon Userbot version: {userbot_version}\n"
        f"Changelog </b><i><a href=https://t.me/moonuserbot/{changelog}>in channel</a></i>.<b>\n"
        f"Changelog written by </b><i>"
        f"<a href=https://t.me/Qbtaumai>Abhi</a></i>\n\n"
        + (
            f"<b>Branch: <a href={remote_url}/tree/{active_branch}>{active_branch}</a>\n"
            if active_branch not in ["master", "main"]
            else ""
        )
        + f"Commit: <a href={remote_url}/commit/{hexsha}>"
        f"{hexsha[:7]}</a> by {author_name}\n"
        f"Commit time: {commit_time}</b>",
    )


@Client.on_message(filters.command(["doctor", "diag"], prefix) & filters.me)
async def doctor(client: Client, message: Message):
    """Самодиагностика: почему что-то может не работать."""
    await message.edit("<b>🩺 Diagnosing... (it'll take a few seconds)</b>")

    manager = ModuleManager.get_instance()
    me = await client.get_me()

    # --- AI key (chatbot) ---
    try:
        from modules.chatbot import get_ai_key  # lazy: порядок загрузки модулей

        ai_key_ok = bool(get_ai_key())
    except Exception:
        ai_key_ok = False

    # --- Quotes API ---
    quotes_status = "❌ not set"
    if quotes_api:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    "https://quotes-o042.onrender.com/",
                    timeout=aiohttp.ClientTimeout(total=8),
                ) as resp:
                    quotes_status = (
                        "✅ reachable" if resp.status < 500 else f"⚠️ HTTP {resp.status}"
                    )
        except Exception as e:
            quotes_status = f"❌ unreachable ({type(e).__name__})"

    owner_ok = (me.id == int(owner_id)) if owner_id else False

    lines = [
        "<b>🩺 Doctor report</b>",
        "",
        f"• Prefix: <code>{prefix}</code>",
        f"• Me: <code>{me.id}</code> (@{me.username or 'no_username'})",
        f"• OWNER_ID: <code>{owner_id}</code> "
        + ("✅ match" if owner_ok else "⚠️ MISMATCH — mafia autolink won't work!"),
        f"• Database: <code>{db_type}</code>",
        f"• Modules loaded: <b>{len(modules_help)}</b> "
        f"(failed: <b>{manager.failed_modules}</b>)",
    ]
    if manager.failed_list:
        failed = ", ".join(f"<code>{m}</code>" for m in manager.failed_list[:10])
        lines.append(f"  ↳ failed: {failed}")

    lines += [
        "",
        "<b>API keys / services:</b>",
        f"• AI_KEY (chatbot): {'✅ set' if ai_key_ok else '❌ NOT SET — set via <code>.aikey</code>'}",
        f"• RMBG_KEY (removebg): {'✅ set' if rmbg_key else '❌ not set'}",
        f"• VT_KEY (virustotal): {'✅ set' if vt_key else '❌ not set'}",
        f"• QUOTES_API (q): {quotes_status}",
        "",
        "<i>Tip: failing modules log tracebacks to moonlogs.txt "
        f"(view with <code>{prefix}moonlogs</code>)</i>",
    ]
    await message.edit("\n".join(lines))


modules_help["support"] = {
    "support": "Information about userbot",
    "version": "Check userbot version",
    "doctor": "Self-diagnostics: keys, services, failed modules",
}
