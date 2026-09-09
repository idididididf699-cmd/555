# Moon-Userbot - telegram userbot
# Mafia module for @TrueMafiaBlackBot (личные сообщения + своя игра)
# Режим преследования + авто-вход + реакция на edit + мгновенный отклик + логи в чат


import asyncio
import base64
import html
import logging
import random
import re
import time
import traceback


from pyrogram import Client, filters
from pyrogram.raw import functions


from utils import modules_help, prefix
from utils.config import owner_id
from utils.db import db


MAFIA_BOT = "TrueMafiaBlackBot"
DEFAULT_LOG_CHAT = -5276889918


log = logging.getLogger("mafia_ls")


# ============================================
# РЕГУЛЯРКИ
# ============================================


ROLE_RE = re.compile(r"ваша\s+роль\s*[:\-—]\s*([^\n<]{2,40})", re.IGNORECASE)
MANIAC_ROLE_RE = re.compile(r"(маньяк|maniac|ман\b|убийц)", re.IGNORECASE)
MAFIA_ROLE_RE = re.compile(r"(мафи|дон|don|godfather|крестн|глава)", re.IGNORECASE)


DANGER_RE = re.compile(
    r"(выход|выйти|покинуть|меню|профиль|правила|отмена|назад|статистика|ошибка|настройки|купить|магазин|донат)",
    re.IGNORECASE,
)
GAME_END_RE = re.compile(r"(игра\s+завершена|победил|победа|игра\s+окончена)", re.IGNORECASE)
ROSTER_HEADER_RE = re.compile(r"(список\s+игроков|живые\s+игроки|список\s+живых|в\s+игре\s*[:\n])", re.IGNORECASE)


TG_USER_ID_RE = re.compile(r"(?:tg://user\?id=|id\=|%3Fid%3D)(\d{4,})")
LINK_RE = re.compile(r"t\.me/TrueMafiaBlackBot\?start=([A-Za-z0-9_\-=]+)", re.IGNORECASE)
JOIN_RE = re.compile(
    r"(участв|участие|в игру|будете играть|хочешь сыграть|присоединиться|вступаешь|"
    r"вступить|войти|начать|соглас|в бой|поиграть|играть)",
    re.IGNORECASE,
)
RECRUIT_RE = re.compile(
    r"(вед[её]тся\s+набор|набор\s+в\s+игру|ид[её]т\s+набор|открыт\s+набор|"
    r"начинаем\s+(набор|игру)|начинается\s+игра|поехали)",
    re.IGNORECASE,
)


# ============================================
# ДБ-ФУНКЦИИ И ЛОГГЕР
# ============================================




def _dbg(msg):
    log.info("[mafia_ls] %s", msg)




def _db_enabled():
    return db.get("custom.mafia_ls", "debug", False)




def _dbg_on(msg):
    if _db_enabled():
        _dbg(msg)




def get_log_chat():
    return db.get("custom.mafia_ls", "log_chat", DEFAULT_LOG_CHAT)




def set_log_chat(chat_id):
    db.set("custom.mafia_ls", "log_chat", chat_id)




async def _log_to_chat(client: Client, title: str, details: str = "", exc: Exception = None, is_error: bool = False):
    chat_id = get_log_chat()
    if not chat_id:
        return


    icon = "🚨" if is_error else "📝"
    text = f"{icon} <b>[Mafia Log] {html.escape(title)}</b>\n"
    if details:
        text += f"{details}\n"
    if exc:
        err_msg = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        if len(err_msg) > 3000:
            err_msg = err_msg[-3000:]
        text += f"\n⚠️ <b>Трейсбэк:</b>\n<pre>{html.escape(err_msg)}</pre>"


    try:
        await client.send_message(chat_id, text)
    except Exception as e:
        _dbg(f"Ошибка отправки лога в чат {chat_id}: {e}")




def get_hunt_mode():
    return db.get("custom.mafia_ls", "hunt_mode", False)




def set_hunt_mode(state):
    db.set("custom.mafia_ls", "hunt_mode", state)




def get_hunt_target():
    return db.get("custom.mafia_ls", "hunt_target", None)




def set_hunt_target(target):
    db.set("custom.mafia_ls", "hunt_target", target)




def get_my_role():
    return db.get("custom.mafia_ls", "my_role", None)




def set_my_role(role):
    db.set("custom.mafia_ls", "my_role", role)




def get_last_start():
    return db.get("custom.mafia_ls", "last_start", None)




def set_last_start(param):
    db.set("custom.mafia_ls", "last_start", param)




def get_last_game_group():
    return db.get("custom.mafia_ls", "last_game_group", None)




def set_last_game_group(gid):
    db.set("custom.mafia_ls", "last_game_group", gid)




def get_roster_map():
    data = db.get("custom.mafia_ls", "roster_map", {})
    return data if isinstance(data, dict) else {}




def set_roster_for_group(gid, roster_dict):
    data = get_roster_map()
    data[str(gid)] = roster_dict
    db.set("custom.mafia_ls", "roster_map", data)




def get_roster_for_group(gid):
    return get_roster_map().get(str(gid), {})




def _self_info_cache():
    return db.get("custom.mafia_ls", "self_info", None)




def game_groups():
    saved = db.get("custom.mafia_ls", "groups", [])
    return set(saved) if saved else set()




def add_game_group(gid):
    groups = game_groups()
    if gid not in groups:
        groups.add(gid)
        db.set("custom.mafia_ls", "groups", list(groups))
        _dbg(f"добавлена игровая группа: {gid}")




# Мафия-бот постоянно РЕДАКТИРУЕТ свои сообщения (счётчики, таймеры, ростер),
# а обработчики висят и на новых, и на отредактированных сообщениях.
# Без защиты это даёт повторные /start и клики — поэтому дедуп по кулдауну.
_JOIN_COOLDOWN = 600  # сек: не слать один и тот же /start чаще
_CLICK_TTL = 300  # сек: не жать одну и ту же кнопку на том же сообщении


def _join_history():
    data = db.get("custom.mafia_ls", "join_history", {})
    return data if isinstance(data, dict) else {}


def _click_history():
    data = db.get("custom.mafia_ls", "click_history", {})
    return data if isinstance(data, dict) else {}


async def _join_game(client, param=None, force=False):
    target = param or get_last_start()
    if not target:
        return
    now = time.time()
    history = {
        k: v for k, v in _join_history().items()
        if now - float(v) < _JOIN_COOLDOWN
    }
    if not force and target in history:
        _dbg(f"скип повторного /start {target} (уже входили, cooldown {_JOIN_COOLDOWN}с)")
        return
    try:
        await client.send_message(MAFIA_BOT, f"/start {target}")
        history[target] = now
        db.set("custom.mafia_ls", "join_history", history)
        _dbg(f"отправлен /start {target} боту {MAFIA_BOT}")
        await _log_to_chat(client, "Вход в игру", f"Отправлен <code>/start {html.escape(target)}</code> боту @{MAFIA_BOT}")
    except Exception as e:
        _dbg(f"ошибка при отправке /start: {e}")
        await _log_to_chat(client, "Ошибка отправки /start", f"Параметр: <code>{html.escape(str(target))}</code>", exc=e, is_error=True)




# ============================================
# ВСПОМОГАТЕЛЬНЫЕ
# ============================================




def _is_from_mafia_bot(message) -> bool:
    fu = message.from_user
    return bool(fu and (fu.username or "").lower() == MAFIA_BOT.lower())




def _buttons(message):
    buttons = []
    if message.reply_markup and message.reply_markup.inline_keyboard:
        for row in message.reply_markup.inline_keyboard:
            for b in row:
                text = b.text or ""
                data = b.callback_data
                if data is not None:
                    buttons.append((text, data))
    return buttons




def _btn_parse(data):
    if isinstance(data, bytes):
        hay = data.decode("utf-8", "ignore")
    else:
        hay = str(data or "")


    parts = hay.split()
    if len(parts) >= 4 and parts[1].startswith("-100"):
        gid, action, slot = parts[1], parts[-2], parts[-1]
        return gid, action, (int(slot) if slot.isdigit() else None)
    if len(parts) >= 3:
        gid, action, slot = parts[0], parts[-2], parts[-1]
        return gid, action, (int(slot) if slot.isdigit() else None)
    return None, None, None




def _btn_action(data):
    return _btn_parse(data)[1]




def _btn_slot(data, text=""):
    slot = _btn_parse(data)[2]
    if slot is not None:
        return slot
    if text:
        m = re.search(r"(?:^|[\[\(#])(\d{1,2})(?:[\]\)\.\s:]|$)", text.strip())
        if m:
            try:
                return int(m.group(1))
            except ValueError:
                pass
    return None




def _btn_group(data):
    gid = _btn_parse(data)[0]
    if gid and gid.startswith("-100"):
        try:
            return int(gid)
        except ValueError:
            return None
    return None




def _find_link(message):
    m = LINK_RE.search((message.text or message.caption or ""))
    if m:
        return m
    if message.reply_markup and message.reply_markup.inline_keyboard:
        for row in message.reply_markup.inline_keyboard:
            for b in row:
                if b.url:
                    m = LINK_RE.search(b.url)
                    if m:
                        return m
    return None




def _has_join_button(btns):
    return any(JOIN_RE.search(bt) for bt, _ in btns)




def _extract_roster_sync(text, message):
    """Быстрый синхронный парсер ростера без сетевых блокировок."""
    if not text:
        return {}


    lines = text.split("\n")
    roster_dict = {}
    current_slot = 1
    entities = message.entities or [] if message else []


    for line in lines:
        line_clean = line.strip()
        if not line_clean:
            continue


        slot_match = re.match(r"^(?:#|\[|\()?(\d{1,2})(?:\]|\)|\.|\:|\s)\s*(.*)", line_clean)
        slot_num = int(slot_match.group(1)) if slot_match else current_slot


        matched_id = None
        for ent in entities:
            ent_text = text[ent.offset : ent.offset + ent.length]
            if ent_text in line_clean:
                if ent.type.name == "TEXT_LINK" and ent.url:
                    m = TG_USER_ID_RE.search(ent.url)
                    if m:
                        matched_id = int(m.group(1))
                        break
                elif ent.type.name == "TEXT_MENTION" and ent.user:
                    matched_id = ent.user.id
                    break


        if not matched_id:
            m = TG_USER_ID_RE.search(line_clean)
            if m:
                matched_id = int(m.group(1))


        if matched_id:
            roster_dict[str(matched_id)] = slot_num
            current_slot = slot_num + 1


    if len(roster_dict) < 2:
        simple_order = []
        for ent in entities:
            if ent.type.name == "TEXT_LINK" and ent.url:
                m = TG_USER_ID_RE.search(ent.url)
                if m:
                    simple_order.append((ent.offset, int(m.group(1))))
            elif ent.type.name == "TEXT_MENTION" and ent.user:
                simple_order.append((ent.offset, ent.user.id))


        if len(simple_order) >= 3:
            simple_order.sort(key=lambda x: x[0])
            roster_dict = {str(uid): i + 1 for i, (_, uid) in enumerate(simple_order)}


    return roster_dict




async def _get_self_id(client):
    info = _self_info_cache()
    if info:
        return info
    try:
        me = await client.get_me()
        info = me.id
        db.set("custom.mafia_ls", "self_info", info)
        return info
    except Exception as e:
        _dbg(f"self info fail: {e}")
        return None




def _find_slot_for_target(target_id, roster):
    if isinstance(target_id, int) and target_id < 100:
        return target_id


    if isinstance(roster, dict):
        if str(target_id) in roster:
            return roster[str(target_id)]
        if int(target_id) in roster:
            return roster[int(target_id)]
    elif isinstance(roster, list):
        target_int = int(target_id) if str(target_id).isdigit() else target_id
        if target_int in roster:
            return roster.index(target_int) + 1


    return None




async def _force_click(client, message, button_text, callback_data, reason="Клик"):
    raw_cb_str = callback_data.decode("utf-8", "ignore") if isinstance(callback_data, bytes) else str(callback_data)

    # Дедуп: то же сообщение + та же кнопка. Мафия-бот любит редактировать
    # сообщения (таймеры, счётчики) — без этого клик повторялся на каждый edit.
    # Охота не страдает: каждый раунд — новое сообщение с другим msg_id.
    now = time.time()
    clicks = {
        k: v for k, v in _click_history().items()
        if now - float(v) < _CLICK_TTL
    }
    click_key = f"{message.chat.id}:{message.id}:{raw_cb_str}"
    if click_key in clicks:
        _dbg(f"скип повторного клика {button_text!r} (msg {message.id})")
        return True

    def _remember_click():
        clicks[click_key] = now
        db.set("custom.mafia_ls", "click_history", clicks)

    details = (
        f"🔘 <b>Кнопка:</b> <code>{html.escape(button_text)}</code>\n"
        f"📦 <b>Callback:</b> <code>{html.escape(raw_cb_str)}</code>\n"
        f"🎯 <b>Причина:</b> {html.escape(reason)}\n"
        f"💬 <b>Чат:</b> <code>{message.chat.id}</code> (msg_id: <code>{message.id}</code>)"
    )


    try:
        await message.click(button_text)
        _remember_click()
        _dbg(f"клик через message.click: {button_text!r}")
        await _log_to_chat(client, "Нажата кнопка (message.click)", details)
        return True
    except Exception as e1:
        _dbg_on(f"message.click не удался ({e1})")


    try:
        c_data = callback_data.encode("utf-8") if isinstance(callback_data, str) else callback_data
        peer = await client.resolve_peer(message.chat.id)
        await client.invoke(
            functions.messages.GetBotCallbackAnswer(peer=peer, msg_id=message.id, data=c_data)
        )
        _remember_click()
        _dbg(f"клик через raw API: {button_text!r}")
        await _log_to_chat(client, "Нажата кнопка (Raw API)", details)
        return True
    except Exception as e2:
        _dbg(f"raw API клик не удался: {e2}")
        await _log_to_chat(
            client,
            "Ошибка нажатия кнопки",
            details,
            exc=e2,
            is_error=True,
        )
        return False




async def _store_last_msg(chat_id, text, btns):
    db.set(
        "custom.mafia_ls",
        "last_msg",
        {
            "chat": chat_id,
            "text": (text or "")[:400],
            "buttons": [
                [bt, base64.b64encode(d).decode("ascii") if isinstance(d, bytes) else str(d)]
                for bt, d in btns
            ],
        },
    )




# ============================================
# ЛОГИКА ОХОТЫ
# ============================================




def _find_hunt_button(buttons, target_id, roster, self_id):
    slot = _find_slot_for_target(target_id, roster)
    if slot is None:
        return None, f"Цель {target_id} не найдена в росторе"


    candidates = []
    for bt, data in buttons:
        if DANGER_RE.search(bt):
            continue
        if _btn_slot(data, bt) != slot:
            continue
        candidates.append((bt, data))


    if not candidates:
        return None, f"Кнопка слота #{slot} отсутствует"


    role = get_my_role() or ""
    if role and (MANIAC_ROLE_RE.search(role) or MAFIA_ROLE_RE.search(role)):
        for bt, data in candidates:
            act = (_btn_action(data) or "").lower()
            if MANIAC_ROLE_RE.search(role) and ("kill" in act or "убит" in act):
                return (bt, data), f"Охота (Маньяк kill -> слот #{slot})"
            if MAFIA_ROLE_RE.search(role) and ("vote" in act or "lynch" in act or "убит" in act):
                return (bt, data), f"Охота (Мафия vote -> слот #{slot})"


    chosen = random.choice(candidates)
    return chosen, f"Охота (Слот #{slot})"




# ============================================
# КОМАНДЫ
# ============================================




@Client.on_message(filters.command("mafialogchat", prefix) & filters.me)
async def mafia_set_log_chat(client, message):
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        current = get_log_chat()
        await message.reply_text(
            f"📡 <b>Текущий чат для логов:</b> <code>{current or 'Отключен'}</code>\n\n"
            f"Для изменения: <code>{prefix}mafialogchat -100123456789</code>\n"
            f"Для отключения: <code>{prefix}mafialogchat off</code>"
        )
        return


    val = args[1].strip()
    if val.lower() in ("off", "none", "0", "выкл", "откл"):
        set_log_chat(None)
        await message.reply_text("🔕 Отправка логов отключена")
        return


    try:
        chat_id = int(val)
        set_log_chat(chat_id)
        await message.reply_text(f"✅ Лог-чат успешно установлен: <code>{chat_id}</code>")
        await _log_to_chat(client, "Подключение лог-чата", "Логирование активировано!")
    except ValueError:
        await message.reply_text("❌ ID чата должен быть числом (например: <code>-5276889918</code>)")




@Client.on_message(filters.command("mafiahunt", prefix) & filters.me)
async def mafia_hunt(client, message):
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        cur = get_hunt_target()
        if cur:
            role = get_my_role() or "не определена"
            await message.reply_text(
                f"🎯 Режим преследования активен\n"
                f"Цель: <code>{cur}</code>\n"
                f"Роль: <b>{role}</b>\n\n"
                f"<i>{prefix}mafiahunt off</i> — отключить"
            )
        else:
            await message.reply_text(
                f"❌ Укажи цель: <code>{prefix}mafiahunt @username</code>, <code>{prefix}mafiahunt 7</code> или <code>{prefix}mafiahunt 123456789</code>\n"
                f"Поддерживаются: ник (@username), номер в списке или Telegram ID"
            )
        return


    raw = args[1].strip()
    low = raw.lower()


    if low in ("off", "none", "0", "выкл", "убр", "-"):
        set_hunt_mode(False)
        set_hunt_target(None)
        await message.reply_text("🔕 Режим преследования отключён")
        await _log_to_chat(client, "Режим преследования", "Охота отключена пользователем")
        return


    target = None


    if raw.isdigit():
        target = int(raw)
    else:
        username = raw.lstrip("@")
        try:
            user = await client.get_users(username)
            if user:
                target = user.id
        except Exception as e:
            _dbg(f"ошибка резолва ника {raw}: {e}")
            await _log_to_chat(client, "Ошибка резолва цели", f"Юзернейм: @{html.escape(username)}", exc=e, is_error=True)
            await message.reply_text(f"❌ Не удалось найти пользователя <code>{raw}</code>")
            return


    if not target:
        await message.reply_text("❌ Некорректная цель. Укажи @username, номер или ID.")
        return


    set_hunt_target(target)
    set_hunt_mode(True)
    role = get_my_role() or "автоопределение"
    action = (
        "kill"
        if MANIAC_ROLE_RE.search(role)
        else "mafia_vote"
        if MAFIA_ROLE_RE.search(role)
        else "авто"
    )
    await message.reply_text(
        f"🎯 Режим преследования активирован\n"
        f"Цель: <code>{raw}</code> (ID/Слот: <code>{target}</code>)\n"
        f"Роль: <b>{role}</b>\n"
        f"Действие: <code>{action}</code>\n\n"
        f"⚡ Будет преследовать эту цель до конца игры\n"
        f"<i>{prefix}mafiahunt off</i> — отключить"
    )
    await _log_to_chat(
        client,
        "Режим преследования включен",
        f"🎯 <b>Цель:</b> <code>{html.escape(raw)}</code> (ID/Слот: <code>{target}</code>)\n"
        f"🎭 <b>Роль:</b> <code>{html.escape(role)}</code>\n"
        f"⚡ <b>Действие:</b> <code>{action}</code>"
    )




@Client.on_message(filters.command("mafiarole", prefix) & filters.me)
async def mafia_role_cmd(client, message):
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        role = get_my_role()
        if role:
            await message.reply_text(f"🎭 Твоя роль: <b>{role}</b>")
        else:
            await message.reply_text(
                f"❌ Роль не определена. Укажи вручную:\n"
                f"<code>{prefix}mafiarole maniac</code> — маньяк (kill)\n"
                f"<code>{prefix}mafiarole mafia</code> — мафия (mafia_vote)"
            )
        return
    role_input = args[1].strip().lower()
    if role_input in ("maniac", "маньяк", "kill", "м"):
        set_my_role("маньяк")
        await message.reply_text("🔪 Роль: <b>МАНЬЯК</b> (будет нажимать kill)")
        await _log_to_chat(client, "Смена роли", "Установлена роль: <b>МАНЬЯК</b>")
    elif role_input in ("mafia", "мафия", "дон", "don", "мф"):
        set_my_role("мафия")
        await message.reply_text("🕵️ Роль: <b>МАФИЯ</b> (будет нажимать mafia_vote)")
        await _log_to_chat(client, "Смена роли", "Установлена роль: <b>МАФИЯ</b>")
    else:
        await message.reply_text("❌ Доступные роли: maniac, mafia")




@Client.on_message(filters.command("mafiaroster", prefix) & filters.me)
async def mafia_roster_cmd(client, message):
    groups = get_roster_map()
    if not groups:
        await message.reply_text("📭 Ростер пуст. Дождись сообщения со списком игроков.")
        return
    text = "📋 Списки игроков по группам:\n\n"
    for gid, roster_data in groups.items():
        text += f"<code>{gid}</code>:\n"
        if isinstance(roster_data, dict):
            for uid, slot in roster_data.items():
                text += f"• Слот #{slot} -> <code>{uid}</code>\n"
        elif isinstance(roster_data, list):
            for i, uid in enumerate(roster_data):
                text += f"• Слот #{i+1} -> <code>{uid}</code>\n"
        text += "\n"


    target = get_hunt_target()
    if target:
        for gid, roster_data in groups.items():
            slot = _find_slot_for_target(target, roster_data)
            if slot:
                text += f"🎯 Цель <code>{target}</code> -> слот #{slot} в группе <code>{gid}</code>\n"
                break
    await message.reply_text(text)




@Client.on_message(filters.command("mafiajoin", prefix) & filters.me)
async def mafia_join(client, message):
    lt = get_last_start()
    if lt:
        await message.edit("<b>⏳ Пробуем войти в последнюю игру...</b>")
        try:
            await client.send_message(MAFIA_BOT, f"/start {lt}")
            await _log_to_chat(client, "Ручной вход (.mafiajoin)", f"Отправлен /start {html.escape(lt)}")
        except Exception as e:
            _dbg(f"join fail: {e}")
            await _log_to_chat(client, "Ошибка входа через /start", f"Param: {lt}", exc=e, is_error=True)
    else:
        await message.edit("<b>❌ Нет сохранённой ссылки на игру.</b>")
    await asyncio.sleep(2)
    await message.delete()




@Client.on_message(filters.command("mafialink", prefix) & filters.me)
async def mafia_link(client, message):
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.edit(f"<b>Использование:</b> <code>{prefix}mafialink &lt;start&gt;</code>")
        return
    param = args[1].strip()
    set_last_start(param)
    try:
        await client.send_message(MAFIA_BOT, f"/start {param}")
        await message.edit("<b>Активно.</b>")
        await _log_to_chat(client, "Вход по ссылке (.mafialink)", f"Param: <code>{html.escape(param)}</code>")
    except Exception as e:
        await message.edit(f"<b>Ошибка:</b> {e}")
        await _log_to_chat(client, "Ошибка .mafialink", f"Param: {param}", exc=e, is_error=True)
    await asyncio.sleep(2)
    await message.delete()




@Client.on_message(filters.command("mafiadebug", prefix) & filters.me)
async def mafia_debug(client, message):
    cur = _db_enabled()
    db.set("custom.mafia_ls", "debug", not cur)
    await message.reply_text(f"<b>Mafia debug: {'on' if not cur else 'off'}</b>")




@Client.on_message(filters.command("mafiabtns", prefix) & filters.me)
async def mafia_btns(client, message):
    data = db.get("custom.mafia_ls", "last_msg", None)
    if not data:
        await message.reply_text("Нет сохранённого сообщения бота. Дождись хода.")
        return
    lines = [f"chat: {data['chat']}", f"text: {data['text']}", "buttons:"]
    for bt, b64d in data["buttons"]:
        lines.append(f"- {bt!r}  cb: {b64d}")
    await message.reply_text("\n".join(lines))




_OWNER_FILTER = filters.user(int(owner_id)) if owner_id else filters.user(0)




@Client.on_message(filters.command("mafia", prefix) & filters.me)
async def mafia_last_join(client, message):
    lt = get_last_start()
    if lt:
        await message.edit("<b>⏳ Пробуем войти в последнюю игру...</b>")
        await _join_game(client, lt, force=True)
    else:
        await message.edit("<b>❌ Ссылка на игру не сохранена. Дождись набора в чате!</b>")
    await asyncio.sleep(2)
    await message.delete()




@Client.on_message(filters.command(["mafiagroup", "mafiachat"], prefix) & filters.me)
async def mafia_set_group(client, message):
    if message.chat.type.name in ("GROUP", "SUPERGROUP"):
        add_game_group(message.chat.id)
        set_last_game_group(message.chat.id)
        await message.reply_text(f"<b>Чат добавлен в игровые:</b> <code>{message.chat.id}</code>")
        await _log_to_chat(client, "Игровой чат добавлен", f"Chat ID: <code>{message.chat.id}</code> ({html.escape(message.chat.title or '')})")
    else:
        await message.reply_text(f"<b>Активные группы:</b> <code>{list(game_groups())}</code>")




@Client.on_message(_OWNER_FILTER & filters.text)
async def mafia_autolink(client, message):
    try:
        m = _find_link(message)
        if not m:
            return
        set_last_start(m.group(1))
        await _join_game(client, m.group(1))
    except Exception as e:
        await _log_to_chat(client, "Ошибка mafia_autolink", exc=e, is_error=True)




# ============================================
# ОБРАБОТЧИК ЛС (НОВЫЕ + РЕДАКТИРОВАННЫЕ СООБЩЕНИЯ)
# ============================================




@Client.on_message(filters.private & ~filters.me)
@Client.on_edited_message(filters.private & ~filters.me)
async def mafia_ls_handler(client, message):
    if not _is_from_mafia_bot(message):
        return


    try:
        text = message.text or message.caption or ""
        btns = _buttons(message)
        await _store_last_msg(message.chat.id, text, btns)


        if GAME_END_RE.search(text):
            return


        # Набор в игру в ЛС
        if (text and RECRUIT_RE.search(text)) or _has_join_button(btns):
            for bt, data in btns:
                if JOIN_RE.search(bt):
                    await asyncio.sleep(random.uniform(0.3, 0.8))
                    await _force_click(client, message, bt, data, reason="Авто-вход (кнопка)")
                    return
            m = _find_link(message)
            if m:
                set_last_start(m.group(1))
                await _join_game(client, m.group(1))
                return


        # Парсинг ростера (списка игроков) в личке
        is_roster_msg = bool(ROSTER_HEADER_RE.search(text))
        extracted_roster = _extract_roster_sync(text, message)
        if extracted_roster and (is_roster_msg or len(extracted_roster) >= 4):
            set_roster_for_group(message.chat.id, extracted_roster)
            set_last_game_group(message.chat.id)
            _dbg(f"roster ЛС {message.chat.id}: {len(extracted_roster)} игроков")
            roster_lines = [f"#{slot}: <code>{uid}</code>" for uid, slot in extracted_roster.items()]
            await _log_to_chat(
                client,
                "Обновлен ростер игроков (ЛС)",
                f"👥 <b>Чат:</b> <code>{message.chat.id}</code>\n"
                f"🔢 <b>Игроков ({len(extracted_roster)}):</b>\n" + ", ".join(roster_lines)
            )


        if not btns:
            return


        # Определение роли
        role_match = ROLE_RE.search(text) if text else None
        if role_match:
            detected_role = role_match.group(1).strip()
            set_my_role(detected_role)
            _dbg(f"роль: {detected_role}")
            await _log_to_chat(client, "Определена роль", f"🎭 <b>Роль:</b> <code>{html.escape(detected_role)}</code>")


        # Охота на цель
        hunt_mode = get_hunt_mode()
        hunt_target = get_hunt_target()
        safe_btns = [(bt, data) for bt, data in btns if not DANGER_RE.search(bt)]


        # AFK: если цель не задана или преследование выключено — ничего не нажимаем
        if not (hunt_mode and hunt_target):
            _dbg_on("🦥 AFK: цель не задана, клики пропущены")
            return


        if hunt_mode and hunt_target:
            candidates_keys = []
            if btns:
                gid = _btn_group(btns[0][1])
                if gid:
                    candidates_keys.append(gid)
            lgg = get_last_game_group()
            if lgg:
                candidates_keys.append(lgg)
            candidates_keys.append(message.chat.id)


            roster = {}
            for key in candidates_keys:
                if key is None:
                    continue
                stored = get_roster_for_group(key)
                if stored and _find_slot_for_target(hunt_target, stored) is not None:
                    roster = stored
                    break
            if not roster:
                # Поиск по всем сохранённым чатам
                for _, r_map in get_roster_map().items():
                    if _find_slot_for_target(hunt_target, r_map):
                        roster = r_map
                        break


            self_id = await _get_self_id(client)
            btn, reason = _find_hunt_button(btns, hunt_target, roster, self_id)
            if btn:
                bt, data = btn
                _dbg(f"🎯 клик по цели {hunt_target}: {bt!r}")
                await asyncio.sleep(random.uniform(0.5, 1.2))
                await _force_click(client, message, bt, data, reason=reason)
                return


            # Цель не найдена — не голосуем за случайного человека (AFK)
            _dbg_on(f"🦥 AFK: кнопка цели {hunt_target} не найдена, ход пропущен")
            return


        # Обычный режим — недоступен без цели (AFK)
        _dbg_on("🦥 AFK: нет цели, случайные клики отменены")
        return


    except Exception as e:
        _dbg(f"Ошибка в mafia_ls_handler: {e}")
        await _log_to_chat(client, "Ошибка в обработчике ЛС", exc=e, is_error=True)




# ============================================
# СБОР РОСТЕРА ИЗ ГРУППЫ
# ============================================




@Client.on_message(filters.group & ~filters.me)
@Client.on_edited_message(filters.group & ~filters.me)
async def mafia_roster_collector(client, message):
    if not _is_from_mafia_bot(message):
        return


    try:
        text = message.text or message.caption or ""
        btns = _buttons(message)


        m = _find_link(message)
        is_recruit = bool(text and (RECRUIT_RE.search(text) or _has_join_button(btns)))


        if m or is_recruit:
            add_game_group(message.chat.id)
            set_last_game_group(message.chat.id)
            if m:
                set_last_start(m.group(1))
                await _join_game(client, m.group(1))
                return
            for bt, data in btns:
                if JOIN_RE.search(bt):
                    await asyncio.sleep(random.uniform(0.3, 0.8))
                    await _force_click(client, message, bt, data, reason="Авто-вход из группы (кнопка)")
                    return


        # Парсинг ростера (списка игроков)
        is_roster_msg = bool(ROSTER_HEADER_RE.search(text))
        extracted_roster = _extract_roster_sync(text, message)


        if extracted_roster and (is_roster_msg or len(extracted_roster) >= 4):
            set_roster_for_group(message.chat.id, extracted_roster)
            set_last_game_group(message.chat.id)
            _dbg(f"roster группы {message.chat.id}: {len(extracted_roster)} игроков")


            roster_lines = [f"#{slot}: <code>{uid}</code>" for uid, slot in extracted_roster.items()]
            await _log_to_chat(
                client,
                "Обновлен ростер игроков",
                f"👥 <b>Чат:</b> <code>{message.chat.id}</code> ({html.escape(message.chat.title or '')})\n"
                f"🔢 <b>Игроков ({len(extracted_roster)}):</b>\n" + ", ".join(roster_lines)
            )


    except Exception as e:
        _dbg(f"Ошибка в mafia_roster_collector: {e}")
        await _log_to_chat(client, "Ошибка в сборщике ростера", exc=e, is_error=True)




modules_help["mafia_ls"] = {
    "mafiahunt <@ник|номер|id>": "Режим преследования — бот преследует указанного игрока",
    "mafiahunt off": "Отключить режим преследования",
    "mafiarole [maniac|mafia]": "Установить роль вручную",
    "mafiaroster": "Показать списки игроков по группам",
    "mafialogchat <chat_id>": "Настроить чат для логов (по умолч. -5276889918)",
    "mafia": "Зайти в последнюю игру",
    "mafiajoin": "Зайти в последнюю игру (синоним)",
    "mafialink <start>": "Зайти по ссылке-параметру",
    "mafiagroup": "Добавить текущий чат в игровые / показать список",
    "mafiabtns": "Показать последние кнопки бота",
    "mafiadebug": "Включить/выключить логи",
}
