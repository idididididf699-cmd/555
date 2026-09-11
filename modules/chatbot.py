# Chatbot module: AI — управление через Telegram
# Ключ, модели, URL, характер, чеченский словарь и рабочее логирование.

import asyncio
import html
import logging
import os
import re
import time
from typing import Optional, Union

import aiohttp
from pyrogram import Client, enums, filters
from pyrogram.types import Message

from utils import modules_help, prefix
from utils.config import (
    ai_base_url as cfg_base_url,
    ai_key as cfg_key,
    ai_model as cfg_model,
    owner_id as cfg_owner_id,
    owner_name as cfg_owner_name,
)
from utils.db import db

log = logging.getLogger(__name__)


# ============================================================
# НАСТРОЙКИ ПО УМОЛЧАНИЮ
# ============================================================

_DEFAULT_MODELS = [
    "dots-studio/dots-3-note-preview:free",
    "deepseek/deepseek-chat",
    "google/gemini-2.0-flash-001",
    "meta-llama/llama-3.3-70b-instruct:free",
    "mistralai/mistral-7b-instruct:free",
    "openai/gpt-4o-mini",
]

_FALLBACK_OWNER_USERNAME = "ai_borz"
_DEFAULT_IGNORE_PATTERNS = [r"t\.me/(TrueMafiaBlackBot|mafiabot)"]
_DEFAULT_MAX_TOKENS = 2048

_session: Optional[aiohttp.ClientSession] = None

# Регулярка для фильтрации ссылок с сохранением текста разметки Markdown
_LINK_RE = re.compile(r"\[([^\]]+)\]\(https?://\S+\)|https?://\S+|t\.me/\S+", re.IGNORECASE)


# ============================================================
# ПРЕСЕТЫ ХАРАКТЕРА
# ============================================================

_PRESETS = {
    "default": (
        "Ты — личный ИИ-ассистент, работающий в Telegram. "
        "Твой владелец и создатель: {owner} "
        "(юзербот @{owner_username}). "
        "Никогда не называй собеседника именем владельца. "
        "Обращайся к собеседнику нейтрально. "
        "Отвечай на том же языке, на котором написан запрос, "
        "по делу, грамотно и кратко."
    ),
    "toxic": (
        "Ты — дерзкий, высокомерный, саркастичный и язвительный ассистент. "
        "Твой владелец: {owner} (юзербот @{owner_username}). "
        "Отвечай с насмешкой, лёгким презрением и подколами, "
        "но СТРОГО БЕЗ МАТА и прямых грубых оскорблений. "
        "Показывай своё интеллектуальное превосходство, "
        "высмеивай банальные вопросы, но факты давай максимально точные. "
        "Отвечай кратко и колко."
    ),
    "friendly": (
        "Ты — дружелюбный, тёплый и очень вежливый помощник. "
        "Твой владелец: {owner} (юзербот @{owner_username}). "
        "Общайся позитивно, проявляй эмпатию, "
        "поддерживай собеседника и давай ясные ответы."
    ),
    "bro": (
        "Ты — чёткий пацан, надёжный бро и кореш. "
        "Твой владелец: {owner} (юзербот @{owner_username}). "
        "Общайся по-простому, на 'ты', "
        "используй уверенный сленг, но СТРОГО БЕЗ МАТА. "
        "Отвечай коротко, чётко и по фактам."
    ),
    "chechen": (
        "Ты — чеченский ИИ-ассистент. "
        "Отвечай ТОЛЬКО на чеченском языке (нохчийн мотт). "
        "Используй правильное чеченское написание.\n\n"
        "Словарь (слово → перевод):\n"
        "{glossary}\n\n"
        "Отвечай кратко и по делу. "
        "Не используй русский язык, кроме случаев, "
        "когда пользователь прямо просит перевести."
    ),
}


# ============================================================
# ФИЛЬТР ВХОДЯЩИХ СООБЩЕНИЙ
# ============================================================

_PREFIXES = tuple(prefix) if isinstance(prefix, (list, tuple, str)) else (".",)


def _looks_like_command(text: str) -> bool:
    """Определяет, является ли сообщение командой юзербота."""
    if not text:
        return False
    stripped = text.lstrip()
    if not stripped:
        return False
    if stripped[0] not in _PREFIXES:
        return False
    if len(stripped) < 2 or not (stripped[1].isalnum() or stripped[1] == "_"):
        return False
    return True


_TRIGGER = (
    (filters.text | filters.caption)
    & ~filters.me
    & ~filters.bot
)


# ============================================================
# УТИЛИТА: БЕЗОПАСНОЕ ЧТЕНИЕ КОНФИГА
# ============================================================

def _read_cfg(value, default: str = "") -> str:
    if value is None:
        return default
    if isinstance(value, (str, int, float)):
        return str(value).strip()
    inner = getattr(value, "value", None)
    if isinstance(inner, (str, int, float)):
        return str(inner).strip()
    return default


# ============================================================
# УПРАВЛЕНИЕ АКТИВНОСТЬЮ (GLOBAL / PER-CHAT)
# ============================================================

def is_chatbot_enabled() -> bool:
    return bool(db.get("custom.chatbot", "enabled", True))


def set_chatbot_enabled(value: bool):
    db.set("custom.chatbot", "enabled", bool(value))


def get_disabled_chats() -> list[int]:
    disabled = db.get("custom.chatbot", "disabled_chats", [])
    if not isinstance(disabled, list):
        db.set("custom.chatbot", "disabled_chats", [])
        return []

    clean_list = []
    dirty = False
    for item in disabled:
        try:
            clean_list.append(int(item))
        except (ValueError, TypeError):
            dirty = True
            continue

    if dirty or len(clean_list) != len(disabled):
        db.set("custom.chatbot", "disabled_chats", clean_list)

    return clean_list


def toggle_chat_enabled(chat_id: int) -> bool:
    disabled_chats = get_disabled_chats()
    chat_id = int(chat_id)

    if chat_id in disabled_chats:
        disabled_chats.remove(chat_id)
        db.set("custom.chatbot", "disabled_chats", disabled_chats)
        return True
    else:
        disabled_chats.append(chat_id)
        db.set("custom.chatbot", "disabled_chats", disabled_chats)
        return False


def is_chat_enabled(chat_id: int) -> bool:
    try:
        return int(chat_id) not in get_disabled_chats()
    except (ValueError, TypeError):
        return True


# ============================================================
# КОНФИГУРАЦИЯ API И МОДЕЛЕЙ
# ============================================================

def get_ai_key() -> str:
    saved = db.get("custom.chatbot", "ai_key", None)
    if saved:
        return str(saved).strip()
    return (
        os.getenv("AI_KEY")
        or os.getenv("ai_key")
        or _read_cfg(cfg_key, "")
    )


def set_ai_key(key: str):
    db.set("custom.chatbot", "ai_key", key.strip())


def get_ai_base_url() -> str:
    saved = db.get("custom.chatbot", "base_url", None)
    if saved:
        return str(saved).strip().rstrip("/")
    value = (
        os.getenv("AI_BASE_URL")
        or os.getenv("ai_base_url")
        or _read_cfg(cfg_base_url, "https://openrouter.ai/api/v1")
    )
    return value.rstrip("/")


def set_ai_base_url(url: str):
    db.set("custom.chatbot", "base_url", url.strip().rstrip("/"))


def get_models() -> list[str]:
    saved = db.get("custom.chatbot", "models", None)
    if not isinstance(saved, list):
        db.set("custom.chatbot", "models", _DEFAULT_MODELS.copy())
        return _DEFAULT_MODELS.copy()

    models = [str(m).strip() for m in saved if str(m).strip()]
    if not models:
        models = _DEFAULT_MODELS.copy()
        db.set("custom.chatbot", "models", models)
    return models


def save_models(models: list[str]):
    db.set("custom.chatbot", "models", models)


def get_current_model() -> str:
    model = db.get("custom.chatbot", "current_model", None)
    if model:
        return str(model).strip()
    model = (
        os.getenv("AI_MODEL")
        or os.getenv("ai_model")
        or _read_cfg(cfg_model, "dots-studio/dots-3-note-preview:free")
    )
    db.set("custom.chatbot", "current_model", model)
    return model


def set_current_model(model: str) -> bool:
    model = model.strip()
    if not model:
        return False
    models = get_models()
    if model not in models:
        models.append(model)
        save_models(models)
    db.set("custom.chatbot", "current_model", model)
    return True


def get_max_tokens() -> int:
    try:
        val = int(db.get("custom.chatbot", "max_tokens", _DEFAULT_MAX_TOKENS))
        return max(64, min(val, 32768))
    except (ValueError, TypeError):
        return _DEFAULT_MAX_TOKENS


def set_max_tokens(value: int):
    db.set("custom.chatbot", "max_tokens", int(value))


def get_strip_links() -> bool:
    return bool(db.get("custom.chatbot", "strip_links", False))


def set_strip_links(value: bool):
    db.set("custom.chatbot", "strip_links", bool(value))


def get_ignore_patterns() -> list[str]:
    saved = db.get("custom.chatbot", "ignore_patterns", None)
    if not isinstance(saved, list):
        db.set("custom.chatbot", "ignore_patterns", _DEFAULT_IGNORE_PATTERNS.copy())
        return _DEFAULT_IGNORE_PATTERNS.copy()
    return [str(x) for x in saved if str(x).strip()]


def add_ignore_pattern(pattern: str) -> bool:
    patterns = get_ignore_patterns()
    if pattern in patterns:
        return False
    patterns.append(pattern)
    db.set("custom.chatbot", "ignore_patterns", patterns)
    return True


def del_ignore_pattern(pattern: str) -> bool:
    patterns = get_ignore_patterns()
    if pattern not in patterns:
        return False
    patterns.remove(pattern)
    db.set("custom.chatbot", "ignore_patterns", patterns)
    return True


# ============================================================
# ХАРАКТЕР И ПРОМПТЫ
# ============================================================

def get_current_preset() -> str:
    preset = db.get("custom.chatbot", "preset", "default")
    if preset not in _PRESETS and preset != "custom":
        return "default"
    return preset


def set_preset(preset_name: str):
    db.set("custom.chatbot", "preset", preset_name)


def get_custom_prompt() -> str:
    value = db.get("custom.chatbot", "custom_prompt", "")
    return str(value) if value else ""


def set_custom_prompt(prompt: str):
    db.set("custom.chatbot", "custom_prompt", prompt)


# ============================================================
# ЛОГИРОВАНИЕ
# ============================================================

def get_log_chat() -> Optional[Union[int, str]]:
    target = db.get("custom.chatbot", "log_chat", None)
    if not target:
        return None
    if isinstance(target, str):
        target_clean = target.strip()
        if target_clean.lower() == "me":
            return "me"
        if (target_clean.startswith("-") and target_clean[1:].isdigit()) or target_clean.isdigit():
            return int(target_clean)
        return target_clean
    return target


def set_log_chat(chat_id: Optional[Union[int, str]]):
    db.set("custom.chatbot", "log_chat", chat_id)


async def _send_log(client: Client, text: str):
    target = get_log_chat()
    if not target:
        return
    try:
        await client.send_message(
            chat_id=target,
            text=text,
            parse_mode=enums.ParseMode.HTML,
            disable_web_page_preview=True,
        )
    except Exception as e:
        log.error("Failed to forward AI log to %s: %s", target, e)


# ============================================================
# ЧЕЧЕНСКИЙ СЛОВАРЬ
# ============================================================

_DEFAULT_GLOSSARY = [
    "салам → здравствуй",
    "хьо → ты",
    "со → я",
    "вай → мы",
    "баркалла → спасибо",
    "гӏуллакх → дело/помощь",
    "маьрша → свободный",
    "нана → мама",
    "да → отец",
    "хи → вода",
    "латта → земля",
    "цӏа → дом",
    "некъ → дорога",
    "дика → хорошо",
    "вон → плохой",
    "доккха → большой",
    "жима → маленький",
    "адам → человек",
    "нохчийн → чеченский",
    "хаза → красиво/прекрасно",
]


def _che_enabled() -> bool:
    return bool(db.get("custom.chatbot", "chechen", False))


def set_che_enabled(value: bool):
    db.set("custom.chatbot", "chechen", bool(value))


def _get_glossary() -> list[str]:
    saved = db.get("custom.chatbot", "glossary", None)
    if not isinstance(saved, list):
        db.set("custom.chatbot", "glossary", _DEFAULT_GLOSSARY.copy())
        return _DEFAULT_GLOSSARY.copy()
    return [str(x).strip() for x in saved if str(x).strip()]


def add_glossary_word(entry: str) -> bool:
    entry = entry.strip()
    if not entry:
        return False

    separators = ["→", "->", "="]
    split_pos = -1
    used_sep = None
    for sep in separators:
        pos = entry.find(sep)
        if pos != -1 and (split_pos == -1 or pos < split_pos):
            split_pos = pos
            used_sep = sep

    if split_pos == -1 or not used_sep:
        return False

    left = entry[:split_pos].strip()
    right = entry[split_pos + len(used_sep):].strip()

    if not left or not right:
        return False

    formatted = f"{left} → {right}"
    glossary = _get_glossary()

    if formatted not in glossary:
        glossary.append(formatted)
        db.set("custom.chatbot", "glossary", glossary)
        return True
    return False


def del_glossary_word(entry: str) -> bool:
    entry = entry.strip().lower()
    if not entry:
        return False

    glossary = _get_glossary()
    before = len(glossary)
    result = []

    for item in glossary:
        parts = item.split("→", 1)
        left = parts[0].strip().lower()
        right = parts[1].strip().lower() if len(parts) > 1 else ""
        if entry in (item.lower(), left, right):
            continue
        result.append(item)

    if len(result) != before:
        db.set("custom.chatbot", "glossary", result)
        return True
    return False


def _extract_words_from_text(text: str) -> list[str]:
    entries = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        cleaned = (
            line.replace("→", " → ")
            .replace("->", " → ")
            .replace("—", " → ")
            .replace("–", " → ")
            .replace("\t", " ")
        )
        cleaned = re.sub(r"\s+", " ", cleaned).strip()

        left = right = None
        if " → " in cleaned:
            left, right = cleaned.split(" → ", 1)
        elif " = " in cleaned:
            left, right = cleaned.split(" = ", 1)
        elif ": " in cleaned:
            left, right = cleaned.split(": ", 1)
        else:
            match = re.match(r"^\s*(.+?)\s+-\s+(.+?)\s*$", cleaned)
            if match:
                left, right = match.group(1), match.group(2)
            else:
                match = re.match(r"^([^\W\d_]+)-([^\W\d_].+)$", cleaned, re.UNICODE)
                if match:
                    left, right = match.group(1), match.group(2)

        if left and right:
            left = left.strip()
            right = re.sub(r"^\s*[—-]\s*", "", right.strip())
            if left and right:
                entries.append(f"{left} → {right}")

    return entries


def load_glossary_from_pdf(pdf_path: str) -> tuple[int, str]:
    try:
        import fitz as pymupdf
    except ImportError:
        try:
            import pymupdf
        except ImportError:
            return (
                0,
                "Библиотека PyMuPDF не установлена.\n"
                "Установите: <code>pip install pymupdf</code>",
            )

    try:
        with pymupdf.open(pdf_path) as doc:
            full_text = "\n".join(page.get_text() for page in doc)
    except Exception as e:
        return 0, f"Не удалось прочитать PDF: {e}"

    entries = _extract_words_from_text(full_text)
    added = 0
    for entry in entries:
        if add_glossary_word(entry):
            added += 1
    return added, ""


# ============================================================
# HTTP СЕССИЯ И СИСТЕМНЫЙ ПРОМПТ
# ============================================================

async def _get_session() -> aiohttp.ClientSession:
    """Пересоздаёт сессию при смене event loop (перезапуск бота)."""
    global _session
    try:
        current_loop = asyncio.get_running_loop()
    except RuntimeError:
        current_loop = None

    needs_new = (
        _session is None
        or _session.closed
        or (current_loop is not None and getattr(_session, "_loop", None) is not current_loop)
    )
    if needs_new:
        await close_session()
        _session = aiohttp.ClientSession()
    return _session


async def close_session():
    """Позволяет избежать утечки ресурсов при остановке или перезапуске бота."""
    global _session
    if _session and not _session.closed:
        try:
            await _session.close()
        except Exception:
            pass
        _session = None


def _get_owner_details(client: Optional[Client] = None) -> tuple[str, Optional[str], int]:
    username: Optional[str] = None
    owner_id = 0

    if client and getattr(client, "me", None):
        username = client.me.username or None
        owner_id = client.me.id
    else:
        try:
            owner_id = int(_read_cfg(cfg_owner_id, "0") or 0)
        except (TypeError, ValueError):
            owner_id = 0

    owner_name = _read_cfg(cfg_owner_name, "владелец") or "владелец"
    if "@" in owner_name:
        owner_name = owner_name.split("@", 1)[0].strip()
    return owner_name, username, owner_id


def _build_system_prompt(client: Optional[Client] = None) -> str:
    owner_name, username, owner_id = _get_owner_details(client)
    username_for_prompt = username or _FALLBACK_OWNER_USERNAME
    owner_info = f"{owner_name} (@{username_for_prompt}, ID: {owner_id})"

    if _che_enabled():
        glossary_list = _get_glossary()
        glossary = (
            "\n".join(f"- {x}" for x in glossary_list)
            if glossary_list
            else "- (словарь пуст)"
        )
        return _PRESETS["chechen"].format(glossary=glossary)

    preset = get_current_preset()
    if preset == "custom":
        custom = get_custom_prompt()
        if custom:
            return custom.replace("{owner}", owner_info).replace("{owner_username}", username_for_prompt)
        preset = "default"

    template = _PRESETS.get(preset, _PRESETS["default"])
    return template.format(owner=owner_info, owner_username=username_for_prompt)


# ============================================================
# ВЫЗОВ OPENAI-СОВМЕСТИМОГО API
# ============================================================

async def _chat(prompt: str, system: str) -> str:
    key = get_ai_key()
    if not key:
        raise RuntimeError("AI_KEY не задан! Установите его: .aikey <ключ>")

    model = get_current_model()
    base_url = get_ai_base_url()
    if not base_url:
        raise RuntimeError("AI_BASE_URL не задан!")

    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://github.com/Moon-Userbot",
        "X-Title": "Moon-Userbot",
    }

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ],
        "max_tokens": get_max_tokens(),
    }

    session = await _get_session()
    url = f"{base_url.rstrip('/')}/chat/completions"

    try:
        async with session.post(
            url,
            headers=headers,
            json=payload,
            timeout=aiohttp.ClientTimeout(total=90),
        ) as resp:
            try:
                data = await resp.json(content_type=None)
            except Exception:
                raw_err = await resp.text()
                raise RuntimeError(f"HTTP {resp.status}: {raw_err[:300]}")

            if not 200 <= resp.status < 300:
                err_msg = f"HTTP {resp.status}"
                if isinstance(data, dict):
                    err = data.get("error")
                    if isinstance(err, dict):
                        err_msg = str(err.get("message", err_msg))
                    elif err:
                        err_msg = str(err)
                    elif data.get("message"):
                        err_msg = str(data["message"])
                raise RuntimeError(err_msg)

            if not isinstance(data, dict):
                raise RuntimeError("API вернул некорректный JSON.")

            choices = data.get("choices")
            if not isinstance(choices, list) or not choices:
                raise RuntimeError("Ответ API не содержит choices.")

            message_data = choices[0].get("message")
            if not isinstance(message_data, dict):
                raise RuntimeError("Ответ API не содержит message.")

            choice = message_data.get("content")
            if choice is None:
                raise RuntimeError("Модель прислала пустой content.")

            clean_choice = str(choice).strip()
            if not clean_choice:
                raise RuntimeError("Модель вернула пустую строку.")
            return clean_choice
    except aiohttp.ClientError as e:
        raise RuntimeError(f"Сетевая ошибка: {e}") from e


# ============================================================
# ОСНОВНОЙ ОБРАБОТЧИК СООБЩЕНИЙ
# ============================================================

@Client.on_message(_TRIGGER)
async def chatbot(client: Client, message: Message):
    if not is_chatbot_enabled():
        return
    if not is_chat_enabled(message.chat.id):
        return
    if not get_ai_key():
        return

    if message.from_user and message.from_user.is_bot:
        return

    text = message.text or message.caption or ""
    if not text.strip():
        return

    if _looks_like_command(text):
        return

    for pattern in get_ignore_patterns():
        try:
            if re.search(pattern, text, re.IGNORECASE):
                return
        except re.error:
            continue

    _, self_username, _ = _get_owner_details(client)

    if message.chat.type not in (enums.ChatType.PRIVATE,):
        is_reply_to_me = bool(
            message.reply_to_message
            and message.reply_to_message.from_user
            and message.reply_to_message.from_user.is_self
        )
        is_mentioned = False
        if self_username:
            username_pattern = rf"@{re.escape(self_username)}\b"
            is_mentioned = bool(re.search(username_pattern, text, re.IGNORECASE))

        if not (is_reply_to_me or is_mentioned):
            return

    if self_username:
        username_pattern = rf"@{re.escape(self_username)}\b"
        prompt = re.sub(username_pattern, "", text, flags=re.IGNORECASE).strip()
    else:
        prompt = text.strip()

    if not prompt:
        prompt = text.strip()

    if message.reply_to_message:
        reply_context = message.reply_to_message.text or message.reply_to_message.caption or ""
        if reply_context.strip():
            prompt = (
                f"Контекст предыдущего сообщения:\n"
                f"{reply_context.strip()}\n\n"
                f"Ответ/вопрос пользователя:\n"
                f"{prompt}"
            )

    if len(prompt) > 4000:
        prompt = prompt[:4000]

    system = _build_system_prompt(client)
    start_time = time.perf_counter()

    try:
        try:
            await message.reply_chat_action(enums.ChatAction.TYPING)
        except Exception:
            pass

        answer = await _chat(prompt, system)
        elapsed = round(time.perf_counter() - start_time, 2)

        if get_strip_links():
            answer = _LINK_RE.sub(
                lambda m: f"[{m.group(1)}]([ссылка скрыта])" if m.group(1) else "[ссылка скрыта]",
                answer
            )

        if len(answer) > 4090:
            answer = answer[:4090] + "…"

        try:
            await message.reply_text(answer, parse_mode=enums.ParseMode.MARKDOWN)
        except Exception:
            await message.reply_text(answer, parse_mode=None)

        # Логирование
        user_name = message.from_user.first_name if message.from_user else "Unknown"
        user_tag = f"@{message.from_user.username}" if (message.from_user and message.from_user.username) else "нет"
        user_id = message.from_user.id if message.from_user else 0

        if message.chat.type == enums.ChatType.PRIVATE:
            chat_title = message.chat.first_name or "Личные сообщения"
        else:
            chat_title = message.chat.title or "Группа"

        chat_id = message.chat.id
        current_model = get_current_model()
        preset_info = "chechen" if _che_enabled() else get_current_preset()

        log_msg = (
            f"🤖 <b>AI Request Log</b>\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"👤 <b>Пользователь:</b> {html.escape(user_name)} ({user_tag}) [<code>{user_id}</code>]\n"
            f"💬 <b>Чат:</b> {html.escape(chat_title)} [<code>{chat_id}</code>]\n"
            f"⚙️ <b>Модель:</b> <code>{html.escape(current_model)}</code>\n"
            f"🎭 <b>Пресет:</b> <code>{html.escape(str(preset_info))}</code>\n"
            f"⏱ <b>Время ответа:</b> <code>{elapsed}s</code>\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"❓ <b>Запрос:</b>\n"
            f"<blockquote>{html.escape(prompt[:400])}</blockquote>\n\n"
            f"💡 <b>Ответ ИИ:</b>\n"
            f"<blockquote>{html.escape(answer[:400])}</blockquote>"
        )
        await _send_log(client, log_msg)

    except Exception as e:
        log.exception("AI request failed")
        error_msg = (
            f"⚠️ <b>AI Error Log</b>\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"💬 <b>Чат:</b> <code>{message.chat.id}</code>\n"
            f"👤 <b>От:</b> <code>{message.from_user.id if message.from_user else 0}</code>\n"
            f"❌ <b>Ошибка:</b>\n"
            f"<code>{html.escape(str(e))}</code>"
        )
        await _send_log(client, error_msg)


# ============================================================
# .AITOGGLE
# ============================================================

@Client.on_message(filters.command("aitoggle", prefix) & filters.me)
async def aitoggle_cmd(_, message: Message):
    args = message.text.split(maxsplit=1)

    if len(args) < 2:
        new_state = not is_chatbot_enabled()
        set_chatbot_enabled(new_state)
        status_emoji = "✅" if new_state else "🔴"
        status_text = "включен" if new_state else "выключен"
        await message.reply_text(
            f"{status_emoji} <b>Чатбот глобально {status_text}</b>\n\n"
            "<b>Параметры:</b>\n"
            "• <code>.aitoggle on / off</code>\n"
            "• <code>.aitoggle here</code> — вкл/выкл в этом чате\n"
            "• <code>.aitoggle status</code>"
        )
        return

    param = args[1].strip().lower()

    if param in ("on", "1", "yes", "true", "enable"):
        set_chatbot_enabled(True)
        await message.reply_text("✅ <b>Чатбот глобально включен.</b>")
        return
    if param in ("off", "0", "no", "false", "disable"):
        set_chatbot_enabled(False)
        await message.reply_text("🔴 <b>Чатбот глобально выключен.</b>")
        return
    if param in ("here", "chat", "toggle"):
        chat_id = message.chat.id
        new_state = toggle_chat_enabled(chat_id)
        chat_title = message.chat.title or "Личные сообщения"
        status_emoji = "✅" if new_state else "🔴"
        status_text = "включен" if new_state else "выключен"
        await message.reply_text(
            f"{status_emoji} <b>Чатбот {status_text} для чата:</b>\n"
            f"<code>{html.escape(chat_title)}</code> (<code>{chat_id}</code>)"
        )
        return
    if param in ("status", "info", "state"):
        global_enabled = is_chatbot_enabled()
        disabled_chats = get_disabled_chats()
        current_chat_enabled = is_chat_enabled(message.chat.id) and global_enabled
        global_emoji = "✅" if global_enabled else "🔴"
        chat_emoji = "✅" if current_chat_enabled else "🔴"
        text = (
            f"<b>📊 Статус AI Chatbot</b>\n"
            f"━━━━━━━━━━━━━━━━━━\n\n"
            f"{global_emoji} <b>Глобально:</b> {'включен' if global_enabled else 'выключен'}\n"
            f"{chat_emoji} <b>В этом чате:</b> {'работает' if current_chat_enabled else 'выключен'}\n"
            f"🚫 <b>Отключено чатов:</b> <code>{len(disabled_chats)}</code>\n\n"
        )
        if disabled_chats:
            text += "<b>Отключенные ID:</b>\n"
            for c_id in disabled_chats[:6]:
                text += f"• <code>{c_id}</code>\n"
            if len(disabled_chats) > 6:
                text += f"<i>...и еще {len(disabled_chats) - 6}</i>\n"
        await message.reply_text(text)
        return

    await message.reply_text("❌ Используйте: <code>.aitoggle [on|off|here|status]</code>")


# ============================================================
# .AILOG
# ============================================================

@Client.on_message(filters.command("ailog", prefix) & filters.me)
async def ailog_cmd(client: Client, message: Message):
    args = message.text.split(maxsplit=1)
    current = get_log_chat()

    if len(args) < 2:
        status = f"<code>{html.escape(str(current))}</code>" if current else "<b>выключены</b>"
        await message.reply_text(
            f"📋 <b>Текущий чат логов:</b> {status}\n\n"
            "<b>Команды:</b>\n"
            "• <code>.ailog here</code>\n"
            "• <code>.ailog me</code>\n"
            "• <code>.ailog &lt;chat_id / @channel&gt;</code>\n"
            "• <code>.ailog test</code>\n"
            "• <code>.ailog off</code>"
        )
        return

    param = args[1].strip()

    if param.lower() == "off":
        set_log_chat(None)
        await message.reply_text("✅ <b>Логирование выключено.</b>")
        return

    if param.lower() == "test":
        if not current:
            await message.reply_text("❌ Логи не настроены!")
            return
        try:
            await client.send_message(
                chat_id=current,
                text="🔔 <b>Тест логирования AI Chatbot</b>\nЛогирование успешно подключено!",
                parse_mode=enums.ParseMode.HTML,
            )
            await message.reply_text(f"✅ Тест успешно отправлен в: <code>{html.escape(str(current))}</code>")
        except Exception as e:
            await message.reply_text(f"❌ <b>Ошибка:</b>\n<code>{html.escape(str(e))}</code>")
        return

    if param.lower() == "here":
        set_log_chat(message.chat.id)
        await message.reply_text(f"✅ Логи → текущий чат: <code>{message.chat.id}</code>")
        return
    if param.lower() == "me":
        set_log_chat("me")
        await message.reply_text("✅ Логи → в <b>Избранное</b>.")
        return

    try:
        chat_id = int(param)
        set_log_chat(chat_id)
        await message.reply_text(f"✅ Логи → ID: <code>{chat_id}</code>")
    except ValueError:
        set_log_chat(param)
        await message.reply_text(f"✅ Логи → <code>{html.escape(param)}</code>")


# ============================================================
# .AIPRESET / .AICHAR
# ============================================================

@Client.on_message(filters.command(["aipreset", "aichar"], prefix) & filters.me)
async def aipreset_cmd(_, message: Message):
    args = message.text.split(maxsplit=1)
    current = get_current_preset()

    if len(args) < 2:
        await message.reply_text(
            "<b>🎭 Управление характером ИИ:</b>\n\n"
            f"• <b>Текущий режим:</b> <code>{html.escape(current)}</code>\n\n"
            "<b>Пресеты:</b> <code>toxic</code>, <code>default</code>, "
            "<code>friendly</code>, <code>bro</code>, <code>custom</code>\n\n"
            "<code>.aipreset toxic</code>\n"
            "<code>.aipreset custom Ты полезный ассистент...</code>"
        )
        return

    choice = args[1].strip()

    if choice.lower().startswith("custom"):
        parts = choice.split(maxsplit=1)
        if len(parts) > 1:
            set_custom_prompt(parts[1].strip())
            set_preset("custom")
            await message.reply_text("✅ <b>Кастомный промпт сохранён!</b>")
        else:
            current_custom = get_custom_prompt() or "не задан"
            await message.reply_text(
                f"<b>Текущий кастомный промпт:</b>\n<code>{html.escape(current_custom)}</code>"
            )
        return

    choice_clean = choice.lower()
    if choice_clean in _PRESETS:
        set_preset(choice_clean)
        await message.reply_text(f"✅ <b>Характер:</b> <code>{html.escape(choice_clean)}</code>")
        return

    await message.reply_text(f"❌ Неизвестный пресет: <code>{html.escape(choice)}</code>")


# ============================================================
# .AIKEY
# ============================================================

@Client.on_message(filters.command("aikey", prefix) & filters.me)
async def aikey_cmd(_, message: Message):
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        current = get_ai_key()
        if current:
            masked = current[:6] + "..." + current[-4:] if len(current) > 10 else "***"
            await message.reply_text(f"🔑 <b>AI_KEY:</b> <code>{html.escape(masked)}</code>")
        else:
            await message.reply_text("❌ <b>AI_KEY не задан!</b>")
        return
    set_ai_key(args[1].strip())
    await message.reply_text("✅ <b>AI_KEY сохранён!</b>")


# ============================================================
# .AIBASE
# ============================================================

@Client.on_message(filters.command("aibase", prefix) & filters.me)
async def aibase_cmd(_, message: Message):
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.reply_text(f"🌐 <b>URL:</b>\n<code>{html.escape(get_ai_base_url())}</code>")
        return
    new_url = args[1].strip()
    if not re.match(r"^https?://", new_url, re.IGNORECASE):
        await message.reply_text("❌ URL должен начинаться с http:// или https://")
        return
    set_ai_base_url(new_url)
    await message.reply_text(f"✅ <b>URL изменён:</b>\n<code>{html.escape(new_url)}</code>")


# ============================================================
# .AISTATUS
# ============================================================

@Client.on_message(filters.command("aistatus", prefix) & filters.me)
async def aistatus_cmd(_, message: Message):
    args = message.text.split(maxsplit=1)
    do_test = len(args) > 1 and args[1].strip().lower() == "test"

    key = get_ai_key()
    current_model = get_current_model()
    base_url = get_ai_base_url()
    models = get_models()
    preset = get_current_preset()
    log_target = get_log_chat()
    global_enabled = is_chatbot_enabled()
    chat_enabled = is_chat_enabled(message.chat.id) and global_enabled
    disabled_count = len(get_disabled_chats())

    lines = [
        "<b>🤖 Статус AI ChatBot</b>",
        "━━━━━━━━━━━━━━━━━━",
        f"• Глобально: <b>{'✅ включен' if global_enabled else '🔴 выключен'}</b>",
        f"• В этом чате: <b>{'✅ работает' if chat_enabled else '🔴 выключен'}</b>",
        f"• Отключено чатов: <code>{disabled_count}</code>",
        f"• Чеченский режим: <b>{'✅ ВКЛ' if _che_enabled() else 'выкл'}</b>",
        "• AI_KEY: " + ("<code>задан</code>" if key else "<b>❌ НЕ ЗАДАН</b>"),
        f"• URL: <code>{html.escape(base_url)}</code>",
        f"• Модель: <code>{html.escape(current_model)}</code>",
        f"• Пресет: <code>{html.escape(preset)}</code>",
        f"• Max tokens: <code>{get_max_tokens()}</code>",
        f"• Strip links: <code>{get_strip_links()}</code>",
        f"• Логи: <code>{html.escape(str(log_target or 'выключены'))}</code>",
        f"• Моделей: <b>{len(models)}</b>",
    ]

    if do_test:
        if not key:
            lines.append("\n⚠️ Ключ не задан — тест невозможен.")
        else:
            try:
                answer = await _chat("ping", "Отвечай одним словом: pong")
                lines.append(f"\n✅ <b>Test OK:</b> <code>{html.escape(answer[:60])}</code>")
            except Exception as e:
                lines.append(f"\n❌ <b>Test FAIL:</b>\n<code>{html.escape(str(e))}</code>")
    else:
        lines.append("\n<i>Для проверки API: <code>.aistatus test</code></i>")

    await message.reply_text("\n".join(lines))


# ============================================================
# .AIMODEL
# ============================================================

@Client.on_message(filters.command("aimodel", prefix) & filters.me)
async def aimodel_cmd(_, message: Message):
    args = message.text.split()
    models = get_models()
    current = get_current_model()

    if len(args) < 2 or args[1].lower() == "list":
        text = f"<b>📋 Модели ({len(models)}):</b>\n\n"
        for i, model in enumerate(models, 1):
            marker = "✅ " if model == current else "▫️ "
            text += f"{marker}<b>{i}.</b> <code>{html.escape(model)}</code>\n"
        text += f"\n<i>Текущая: <code>{html.escape(current)}</code></i>\n\n"
        text += (
            "<b>Команды:</b>\n"
            "• <code>.aimodel &lt;номер|имя&gt;</code>\n"
            "• <code>.aimodel add &lt;id&gt;</code>\n"
            "• <code>.aimodel del &lt;номер&gt;</code>"
        )
        await message.reply_text(text)
        return

    sub = args[1].lower()

    if sub == "add":
        if len(args) < 3:
            await message.reply_text(
                "❌ Укажите идентификатор модели:\n<code>.aimodel add openai/gpt-4o</code>"
            )
            return
        new_model = message.text.split(maxsplit=2)[2].strip()
        if not new_model:
            await message.reply_text("❌ Пустое имя модели.")
            return
        if new_model in models:
            await message.reply_text("ℹ️ Эта модель уже есть.")
            return
        models.append(new_model)
        save_models(models)
        await message.reply_text(f"✅ <b>Добавлена:</b> <code>{html.escape(new_model)}</code>")
        return

    if sub == "del":
        if len(args) < 3:
            await message.reply_text("❌ Укажите номер модели: <code>.aimodel del 2</code>")
            return
        try:
            idx = int(args[2]) - 1
        except ValueError:
            await message.reply_text("❌ Номер должен быть числом.")
            return
        if not (0 <= idx < len(models)):
            await message.reply_text("❌ Неверный номер.")
            return
        if len(models) <= 1:
            await message.reply_text("❌ Нельзя удалить единственную модель.")
            return
        removed = models.pop(idx)
        if removed == current:
            db.set("custom.chatbot", "current_model", models[0])
        save_models(models)
        await message.reply_text(f"✅ <b>Удалена:</b> <code>{html.escape(removed)}</code>")
        return

    arg = args[1]
    try:
        idx = int(arg) - 1
        if 0 <= idx < len(models):
            selected = models[idx]
            set_current_model(selected)
            await message.reply_text(f"✅ <b>Выбрана:</b> <code>{html.escape(selected)}</code>")
            return
        await message.reply_text(f"❌ Неверный номер (всего: {len(models)})")
        return
    except ValueError:
        pass

    if set_current_model(arg):
        await message.reply_text(f"✅ <b>Установлена:</b> <code>{html.escape(arg)}</code>")


# ============================================================
# .AITOKENS / .AILINKS / .AIIGNORE
# ============================================================

@Client.on_message(filters.command("aitokens", prefix) & filters.me)
async def aitokens_cmd(_, message: Message):
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.reply_text(
            f"📊 <b>Max tokens:</b> <code>{get_max_tokens()}</code>\n\n"
            "Изменить: <code>.aitokens 4096</code>"
        )
        return
    try:
        val = int(args[1].strip())
    except ValueError:
        await message.reply_text("❌ Введите число.")
        return
    set_max_tokens(val)
    await message.reply_text(f"✅ <b>Max tokens:</b> <code>{get_max_tokens()}</code>")


@Client.on_message(filters.command("ailinks", prefix) & filters.me)
async def ailinks_cmd(_, message: Message):
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        state = "✅ вырезаются" if get_strip_links() else "🔴 сохраняются"
        await message.reply_text(
            f"🔗 <b>Ссылки в ответах:</b> {state}\n\n"
            "<code>.ailinks on / off</code>"
        )
        return
    val = args[1].strip().lower()
    if val in ("on", "1", "yes"):
        set_strip_links(True)
        await message.reply_text("✅ Ссылки будут вырезаться.")
    elif val in ("off", "0", "no"):
        set_strip_links(False)
        await message.reply_text("🔴 Ссылки сохраняются.")
    else:
        await message.reply_text("❌ Используйте <code>on</code> или <code>off</code>.")


@Client.on_message(filters.command("aiignore", prefix) & filters.me)
async def aiignore_cmd(_, message: Message):
    args = message.text.split(maxsplit=2)
    patterns = get_ignore_patterns()

    if len(args) < 2 or args[1].lower() == "list":
        text = f"<b>🚫 Игнор-паттерны ({len(patterns)}):</b>\n\n"
        for i, p in enumerate(patterns, 1):
            text += f"<b>{i}.</b> <code>{html.escape(p)}</code>\n"
        text += (
            "\n<b>Команды:</b>\n"
            "• <code>.aiignore add &lt;regex&gt;</code>\n"
            "• <code>.aiignore del &lt;regex&gt;</code>"
        )
        await message.reply_text(text)
        return

    sub = args[1].lower()
    if sub == "add":
        if len(args) < 3:
            await message.reply_text("❌ Укажите regex: <code>.aiignore add t\\.me/spambot</code>")
            return
        pattern = args[2].strip()
        try:
            re.compile(pattern)
        except re.error as e:
            await message.reply_text(f"❌ Неверный regex: <code>{html.escape(str(e))}</code>")
            return
        if add_ignore_pattern(pattern):
            await message.reply_text(f"✅ Добавлен: <code>{html.escape(pattern)}</code>")
        else:
            await message.reply_text("ℹ️ Уже есть.")
        return

    if sub == "del":
        if len(args) < 3:
            await message.reply_text("❌ Укажите паттерн для удаления.")
            return
        pattern = args[2].strip()
        if del_ignore_pattern(pattern):
            await message.reply_text(f"✅ Удалён: <code>{html.escape(pattern)}</code>")
        else:
            await message.reply_text("ℹ️ Не найден.")
        return

    await message.reply_text("❌ Используйте: <code>list / add / del</code>")


# ============================================================
# .CHE
# ============================================================

@Client.on_message(filters.command("che", prefix) & filters.me)
async def che_cmd(_, message: Message):
    args = message.text.split()
    sub = args[1].lower() if len(args) > 1 else ""

    if not sub:
        new_state = not _che_enabled()
        set_che_enabled(new_state)
        state = "включён" if new_state else "выключен"
        await message.reply_text(
            f"🌐 <b>Чеченский режим: {state}</b>\n\n"
            "<b>Словарь:</b>\n"
            "• <code>.che add слово=перевод</code>\n"
            "• <code>.che del слово</code>\n"
            "• <code>.che list</code>\n"
            "• <code>.che load</code> (ответ на PDF)"
        )
        return

    if sub in ("on", "1", "yes", "true"):
        set_che_enabled(True)
        await message.reply_text("🌐 Включён.")
        return
    if sub in ("off", "0", "no", "false"):
        set_che_enabled(False)
        await message.reply_text("🌐 Выключен.")
        return

    if sub == "list":
        glossary = _get_glossary()
        if not glossary:
            await message.reply_text("📖 <b>Словарь пуст.</b>")
            return
        text = f"<b>📖 Словарь ({len(glossary)}):</b>\n\n"
        text += "\n".join(f"• {html.escape(x)}" for x in glossary[:60])
        if len(glossary) > 60:
            text += f"\n\n<i>...ещё {len(glossary) - 60}</i>"
        await message.reply_text(text)
        return

    if sub == "add":
        rest = message.text.split(maxsplit=2)
        if len(rest) < 3:
            await message.reply_text("Использование: <code>.che add слово=перевод</code>")
            return
        entry = rest[2].strip()
        ok = add_glossary_word(entry)
        status = "добавлено" if ok else "уже есть / неверный формат"
        await message.reply_text(f"📖 <b>Слово {status}:</b> <code>{html.escape(entry)}</code>")
        return

    if sub == "load":
        reply = message.reply_to_message
        if not (reply and reply.document):
            await message.reply_text("Ответьте <code>.che load</code> на PDF.")
            return
        document = reply.document
        file_name = document.file_name or ""
        if not file_name.lower().endswith(".pdf"):
            await message.reply_text("❌ Только PDF.")
            return

        await message.reply_text("⏳ Обработка PDF...")
        path = None
        try:
            path = await reply.download()
            if not path:
                raise RuntimeError("Не удалось скачать документ.")
            added, error = await asyncio.to_thread(load_glossary_from_pdf, path)
            if error:
                await message.reply_text(f"❌ {error}")
                return
            await message.reply_text(
                f"✅ <b>Обновлено!</b>\n"
                f"• Добавлено: <b>{added}</b>\n"
                f"• Всего: <b>{len(_get_glossary())}</b>"
            )
        except Exception as e:
            await message.reply_text(f"❌ <code>{html.escape(str(e))}</code>")
        finally:
            if path and isinstance(path, str) and os.path.exists(path):
                try:
                    os.remove(path)
                except OSError:
                    pass
        return

    if sub == "del":
        rest = message.text.split(maxsplit=2)
        if len(rest) < 3:
            await message.reply_text("Использование: <code>.che del слово</code>")
            return
        word = rest[2].strip()
        ok = del_glossary_word(word)
        status = "удалено" if ok else "не найдено"
        await message.reply_text(f"📖 <b>Слово {status}:</b> <code>{html.escape(word)}</code>")
        return

    await message.reply_text(
        "<b>Команды:</b>\n"
        "• <code>.che on / off</code>\n"
        "• <code>.che list</code>\n"
        "• <code>.che add слово=перевод</code>\n"
        "• <code>.che del слово</code>\n"
        "• <code>.che load</code>"
    )


# ============================================================
# СПРАВКА
# ============================================================

modules_help["chatbot"] = {
    "aikey": "Задать API ключ: .aikey <ключ>",
    "aibase": "URL API: .aibase <url>",
    "aistatus": "Статус (+ .aistatus test для проверки API)",
    "aimodel": "Модели: list, add, del, выбор",
    "aipreset": "Характер: toxic/friendly/bro/default/custom",
    "aitokens": "Лимит токенов: .aitokens 4096",
    "ailinks": "Вырезать ссылки: .ailinks on/off",
    "aiignore": "Игнор-паттерны: list/add/del",
    "ailog": "Логи: .ailog here/me/<id>/test/off",
    "aitoggle": "Вкл/выкл бот: [on|off|here|status]",
    "che": "Чеченский: on/off/list/add/del/load",
}
