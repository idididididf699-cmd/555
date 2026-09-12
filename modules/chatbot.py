# Chatbot module: AI — управление через Telegram
# Ключ, модели, URL, характер, чеченский словарь и рабочее логирование.

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
# НАСТРОЙКИ
# ============================================================

_DEFAULT_MODELS = [
    "dots-studio/dots-3-note-preview:free",
    "deepseek/deepseek-chat",
    "google/gemini-2.0-flash-001",
    "meta-llama/llama-3.3-70b-instruct:free",
    "mistralai/mistral-7b-instruct:free",
    "openai/gpt-4o-mini",
    "muse-spark-1.3-contributor-free",
]

_OWNER_USERNAME = "ai_borz"


# ============================================================
# ПРЕСЕТЫ ХАРАКТЕРА
# ============================================================

_PRESETS = {
    "default": (
        "Ты — личный ИИ-ассистент, работающий в Telegram. "
        "Твой владелец и создатель: {owner} "
        "(юзербот @{_OWNER_USERNAME}). "
        "Никогда не называй пользователя именем владельца. "
        "Обращайся к собеседнику нейтрально. "
        "Отвечай на том же языке, на котором написан запрос, "
        "по делу и кратко."
    ),

    "toxic": (
        "Ты — дерзкий, высокомерный, саркастичный и грубый ассистент. "
        "Твой владелец: {owner} "
        "(юзербот @{_OWNER_USERNAME}). "
        "Отвечай с насмешкой, язвительностью, лёгким презрением "
        "и подколами, но СТРОГО БЕЗ МАТА и прямых оскорблений. "
        "Показывай своё превосходство над собеседником, "
        "высмеивай банальные или глупые вопросы, "
        "но информацию по существу давай максимально точно. "
        "Отвечай кратко и колко."
    ),

    "friendly": (
        "Ты — дружелюбный, тёплый и очень вежливый помощник. "
        "Твой владелец: {owner} (юзербот @{_OWNER_USERNAME}). "
        "Общайся позитивно, проявляй эмпатию, "
        "поддерживай собеседника и давай ясные ответы."
    ),

    "bro": (
        "Ты — чёткий пацан, надёжный бро и кореш. "
        "Твой владелец: {owner} (юзербот @{_OWNER_USERNAME}). "
        "Общайся по-простому, на 'ты', "
        "используй уверенный уличный/пацанский сленг, "
        "но СТРОГО БЕЗ МАТА и грубой пошлости. "
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

_TRIGGER = (
    filters.text
    & ~filters.me
    & ~filters.bot
)

_session: Optional[aiohttp.ClientSession] = None


# ============================================================
# БАЗА ДАННЫХ
# ============================================================

def get_ai_key() -> str:
    saved = db.get("custom.chatbot", "ai_key", None)
    if saved:
        return str(saved).strip()

    value = (
        os.getenv("AI_KEY")
        or os.getenv("ai_key")
        or getattr(cfg_key, "value", cfg_key)
        or ""
    )
    return str(value).strip()


def set_ai_key(key: str):
    db.set("custom.chatbot", "ai_key", key.strip())


def get_ai_base_url() -> str:
    saved = db.get("custom.chatbot", "base_url", None)
    if saved:
        return str(saved).strip().rstrip("/")

    value = (
        os.getenv("AI_BASE_URL")
        or os.getenv("ai_base_url")
        or getattr(cfg_base_url, "value", cfg_base_url)
        or "https://openrouter.ai/api/v1"
    )
    return str(value).strip().rstrip("/")


def set_ai_base_url(url: str):
    db.set("custom.chatbot", "base_url", url.strip().rstrip("/"))


def get_models() -> list[str]:
    saved = db.get("custom.chatbot", "models", None)
    if not isinstance(saved, list):
        db.set("custom.chatbot", "models", _DEFAULT_MODELS.copy())
        return _DEFAULT_MODELS.copy()

    models = [str(model).strip() for model in saved if str(model).strip()]
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
        or getattr(cfg_model, "value", cfg_model)
        or "dots-studio/dots-3-note-preview:free"
    )
    model = str(model).strip()
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


# ============================================================
# ХАРАКТЕР
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

    # Приведение строки к int, если передан ID
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

    entry = entry.replace("->", "→").replace("=", "→").replace("≠", "→")
    if "→" not in entry:
        return False

    left, right = entry.split("→", 1)
    left, right = left.strip(), right.strip()
    if not left or not right:
        return False

    entry = f"{left} → {right}"
    glossary = _get_glossary()

    if entry not in glossary:
        glossary.append(entry)
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
    for line in text.splitlines():
        line = line.strip()
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

        candidates = []
        if " → " in cleaned:
            candidates.append(cleaned.split(" → ", 1))
        elif " = " in cleaned:
            candidates.append(cleaned.split(" = ", 1))
        elif ": " in cleaned:
            candidates.append(cleaned.split(": ", 1))
        else:
            match = re.match(r"^\s*(.+?)\s+-\s+(.+?)\s*$", line)
            if match:
                candidates.append((match.group(1), match.group(2)))

        if not candidates:
            continue

        left, right = candidates[0]
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
                "Библиотека PyMuPDF не установлена. "
                "Установите её командой: pip install pymupdf",
            )

    try:
        doc = pymupdf.open(pdf_path)
        try:
            full_text = "\n".join(page.get_text() for page in doc)
        finally:
            doc.close()
    except Exception as e:
        return 0, f"Не удалось прочитать PDF: {e}"

    entries = _extract_words_from_text(full_text)
    added = 0
    for entry in entries:
        if add_glossary_word(entry):
            added += 1

    return added, ""


# ============================================================
# HTTP SESSION
# ============================================================

async def _get_session() -> aiohttp.ClientSession:
    global _session
    if _session is None or _session.closed:
        _session = aiohttp.ClientSession()
    return _session


async def close_session():
    global _session
    if _session is not None and not _session.closed:
        await _session.close()
    _session = None


# ============================================================
# OWNER
# ============================================================

def _owner_info() -> str:
    raw_owner = getattr(cfg_owner_name, "value", cfg_owner_name)
    owner = str(raw_owner or "владелец").strip()

    if "@" in owner:
        owner = owner.split("@", 1)[0].strip()

    raw_owner_id = getattr(cfg_owner_id, "value", cfg_owner_id)
    try:
        owner_id = int(raw_owner_id or 0)
    except (TypeError, ValueError):
        owner_id = 0

    return f"{owner} (@{_OWNER_USERNAME}, ID: {owner_id})"


# ============================================================
# SYSTEM PROMPT
# ============================================================

def _build_system_prompt() -> str:
    owner = _owner_info()

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
            try:
                return custom.format(
                    owner=owner,
                    _OWNER_USERNAME=_OWNER_USERNAME,
                )
            except (KeyError, ValueError, IndexError):
                return custom
        return _PRESETS["default"].format(
            owner=owner,
            _OWNER_USERNAME=_OWNER_USERNAME,
        )

    template = _PRESETS.get(preset, _PRESETS["default"])
    return template.format(
        owner=owner,
        _OWNER_USERNAME=_OWNER_USERNAME,
    )


# ============================================================
# API CHAT
# ============================================================

# Модели OpenCode Zen, работающие только через Responses API
# (обычный /chat/completions отдаёт для них HTTP 500).
_RESPONSES_ONLY_PREFIXES = ("muse-spark",)


def _is_responses_only(model: str) -> bool:
    return str(model).strip().lower().startswith(_RESPONSES_ONLY_PREFIXES)


def _extract_responses_text(data: dict) -> str:
    texts: list[str] = []
    output = data.get("output")
    if isinstance(output, list):
        for item in output:
            if not isinstance(item, dict):
                continue
            if item.get("type") != "message":
                continue
            for block in item.get("content") or []:
                if (
                    isinstance(block, dict)
                    and block.get("type") == "output_text"
                    and block.get("text")
                ):
                    texts.append(str(block["text"]))
    if not texts and isinstance(data.get("output_text"), str):
        texts.append(data["output_text"])
    return "".join(texts).strip()


async def _chat(prompt: str, system: str) -> str:
    key = get_ai_key()
    if not key:
        raise RuntimeError(
            "AI_KEY не задан! Задайте его командой: .aikey <ваш_ключ>"
        )

    model = get_current_model()
    base_url = get_ai_base_url()
    if not base_url:
        raise RuntimeError("AI_BASE_URL не задан.")

    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://github.com/Moon-Userbot",
        "X-Title": "Moon-Userbot",
    }

    if _is_responses_only(model):
        payload = {
            "model": model,
            "instructions": system,
            "input": prompt,
            "max_output_tokens": 2048,
        }
        url = f"{base_url.rstrip('/')}/responses"
    else:
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            "max_tokens": 2048,
        }
        url = f"{base_url.rstrip('/')}/chat/completions"

    session = await _get_session()

    try:
        async with session.post(
            url,
            headers=headers,
            json=payload,
            timeout=aiohttp.ClientTimeout(total=120),
        ) as resp:
            try:
                data = await resp.json(content_type=None)
            except Exception:
                response_text = await resp.text()
                raise RuntimeError(
                    f"Ошибка ответа API (HTTP {resp.status}): {response_text[:500]}"
                )

            if not 200 <= resp.status < 300:
                error_message = f"HTTP {resp.status}"
                if isinstance(data, dict):
                    error_data = data.get("error")
                    if isinstance(error_data, dict):
                        error_message = str(
                            error_data.get("message", error_message)
                        )
                    elif error_data:
                        error_message = str(error_data)
                    elif data.get("message"):
                        error_message = str(data["message"])

                raise RuntimeError(error_message)

            if not isinstance(data, dict):
                raise RuntimeError("API вернул некорректный JSON.")

            if _is_responses_only(model):
                choice = _extract_responses_text(data)
                if not choice:
                    raise RuntimeError("Модель вернула пустой ответ.")
                return choice

            choices = data.get("choices")
            if not isinstance(choices, list) or not choices:
                raise RuntimeError(
                    f"В ответе API отсутствует choices: {str(data)[:500]}"
                )

            first_choice = choices[0]
            if not isinstance(first_choice, dict):
                raise RuntimeError("Некорректный формат choices.")

            message_data = first_choice.get("message")
            if not isinstance(message_data, dict):
                raise RuntimeError("В ответе API отсутствует message.")

            choice = message_data.get("content")
            if choice is None:
                raise RuntimeError("API вернул пустой content.")

            choice = str(choice).strip()
            if not choice:
                raise RuntimeError("Модель вернула пустой ответ.")

            return choice

    except aiohttp.ClientError as e:
        raise RuntimeError(f"Ошибка соединения с API: {e}") from e


# ============================================================
# ОСНОВНОЙ CHATBOT
# ============================================================

@Client.on_message(_TRIGGER)
async def chatbot(client: Client, message: Message):
    key = get_ai_key()
    if not key:
        return

    if message.from_user and message.from_user.is_bot:
        return

    text = message.text or ""
    if not text.strip():
        return

    if re.search(r"t\.me/TrueMafiaBlackBot", text, re.IGNORECASE):
        return

    username_pattern = rf"@{re.escape(_OWNER_USERNAME)}\b"

    # Ответ в группах только по тегу или реплаю
    if message.chat.type not in (enums.ChatType.PRIVATE,):
        is_reply_to_me = bool(
            message.reply_to_message
            and message.reply_to_message.from_user
            and message.reply_to_message.from_user.is_self
        )

        is_mentioned = bool(re.search(username_pattern, text, re.IGNORECASE))

        if not (is_reply_to_me or is_mentioned):
            return

    prompt = re.sub(username_pattern, "", text, flags=re.IGNORECASE).strip()
    if not prompt:
        prompt = text.strip()

    if message.reply_to_message and message.reply_to_message.text:
        reply_text = message.reply_to_message.text.strip()
        prompt = (
            f"Контекст сообщения:\n"
            f"{reply_text}\n\n"
            f"Ответ пользователя:\n"
            f"{prompt}"
        )

    if len(prompt) > 4000:
        prompt = prompt[:4000]

    system = _build_system_prompt()
    start_time = time.perf_counter()

    try:
        await message.reply_chat_action(enums.ChatAction.TYPING)
        answer = await _chat(prompt, system)

        elapsed = round(time.perf_counter() - start_time, 2)

        # Удаляем ссылки
        answer = re.sub(r"https?://\S+", "ссылка удалена", answer)

        if len(answer) > 4090:
            answer = answer[:4090] + "…"

        await message.reply_text(
            answer,
            parse_mode=enums.ParseMode.DISABLED,
        )

        # Формирование детального лога
        user_name = message.from_user.first_name if message.from_user else "Unknown"
        user_tag = f"@{message.from_user.username}" if (message.from_user and message.from_user.username) else "нет юзернейма"
        user_id = message.from_user.id if message.from_user else 0

        chat_title = message.chat.title or "Личные сообщения"
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
            f"<blockquote>{html.escape(prompt[:500])}</blockquote>\n\n"
            f"💡 <b>Ответ ИИ:</b>\n"
            f"<blockquote>{html.escape(answer[:500])}</blockquote>"
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
# .AILOG (УПРАВЛЕНИЕ И ТЕСТ ЛОГОВ)
# ============================================================

@Client.on_message(filters.command("ailog", prefix) & filters.me)
async def ailog_cmd(client: Client, message: Message):
    args = message.text.split(maxsplit=1)
    current = get_log_chat()

    if len(args) < 2:
        status = f"<code>{html.escape(str(current))}</code>" if current else "<b>выключены</b>"
        await message.reply_text(
            f"📋 <b>Чат для логов:</b> {status}\n\n"
            "<b>Команды:</b>\n"
            "• <code>.ailog here</code> — отправлять в текущий чат\n"
            "• <code>.ailog me</code> — отправлять в Избранное\n"
            "• <code>.ailog &lt;chat_id / @channel&gt;</code> — задать чат/канал\n"
            "• <code>.ailog test</code> — проверить отправку лога\n"
            "• <code>.ailog off</code> — отключить логи"
        )
        return

    param = args[1].strip()

    if param.lower() == "off":
        set_log_chat(None)
        await message.reply_text("<b>✅ Отправка логов отключена.</b>")
        return

    if param.lower() == "test":
        if not current:
            await message.reply_text("❌ <b>Логи не настроены!</b> Сначала укажите чат (например: <code>.ailog me</code>)")
            return

        try:
            await client.send_message(
                chat_id=current,
                text=(
                    "🔔 <b>Тестовое сообщение логера AI Chatbot</b>\n"
                    "Логирование успешно подключено и функционирует!"
                ),
                parse_mode=enums.ParseMode.HTML,
            )
            await message.reply_text(f"✅ <b>Тестовый лог успешно отправлен в:</b> <code>{html.escape(str(current))}</code>")
        except Exception as e:
            await message.reply_text(
                f"❌ <b>Не удалось отправить лог:</b>\n"
                f"<code>{html.escape(str(e))}</code>\n\n"
                "<i>Убедитесь, что бот состоит в этом чате или канал/группа доступны.</i>"
            )
        return

    if param.lower() == "here":
        set_log_chat(message.chat.id)
        await message.reply_text(
            f"<b>✅ Логи будут отправляться сюда:</b> <code>{message.chat.id}</code>\n"
            "Проверьте работу командой: <code>.ailog test</code>"
        )
        return

    if param.lower() == "me":
        set_log_chat("me")
        await message.reply_text(
            "<b>✅ Логи будут отправляться в Избранное.</b>\n"
            "Проверьте работу командой: <code>.ailog test</code>"
        )
        return

    try:
        chat_id = int(param)
        set_log_chat(chat_id)
        await message.reply_text(
            f"<b>✅ Логи установлены на ID:</b> <code>{chat_id}</code>\n"
            "Проверьте работу командой: <code>.ailog test</code>"
        )
    except ValueError:
        set_log_chat(param)
        await message.reply_text(
            f"<b>✅ Логи установлены на:</b> <code>{html.escape(param)}</code>\n"
            "Проверьте работу командой: <code>.ailog test</code>"
        )


# ============================================================
# .AIPRESET / .AICHAR
# ============================================================

@Client.on_message(filters.command(["aipreset", "aichar"], prefix) & filters.me)
async def aipreset_cmd(_, message: Message):
    args = message.text.split(maxsplit=1)
    current = get_current_preset()

    if len(args) < 2:
        text = (
            "<b>🎭 Управление характером:</b>\n\n"
            f"• <b>Текущий режим:</b> <code>{html.escape(current)}</code>\n\n"
            "<b>Доступные пресеты:</b>\n"
            "• <code>toxic</code> — дерзкий и колкий\n"
            "• <code>default</code> — стандартный\n"
            "• <code>friendly</code> — дружелюбный\n"
            "• <code>bro</code> — пацанский стиль\n"
            "• <code>custom</code> — собственный промпт\n\n"
            "<b>Использование:</b>\n"
            "<code>.aipreset toxic</code>\n"
            "<code>.aipreset custom &lt;промпт&gt;</code>"
        )
        await message.reply_text(text)
        return

    choice = args[1].strip()

    if choice.lower().startswith("custom"):
        parts = choice.split(maxsplit=1)
        if len(parts) > 1:
            set_custom_prompt(parts[1].strip())
            set_preset("custom")
            await message.reply_text("<b>✅ Кастомный характер сохранён и активирован!</b>")
        else:
            current_custom = get_custom_prompt() or "не задан"
            await message.reply_text(
                f"<b>Кастомный промпт:</b>\n<code>{html.escape(current_custom)}</code>\n\n"
                "Изменить:\n<code>.aipreset custom Ты злой робот...</code>"
            )
        return

    choice_clean = choice.lower()
    if choice_clean in _PRESETS:
        set_preset(choice_clean)
        await message.reply_text(
            f"<b>✅ Характер изменён на:</b> <code>{html.escape(choice_clean)}</code>"
        )
        return

    await message.reply_text(
        f"❌ Неизвестный пресет: <code>{html.escape(choice)}</code>\n"
        "Доступны: <code>toxic</code>, <code>default</code>, <code>friendly</code>, <code>bro</code>, <code>custom</code>"
    )


# ============================================================
# .AIKEY
# ============================================================

@Client.on_message(filters.command("aikey", prefix) & filters.me)
async def aikey_cmd(_, message: Message):
    args = message.text.split(maxsplit=1)

    if len(args) < 2:
        current = get_ai_key()
        if current:
            masked = current[:7] + "..." + current[-4:] if len(current) > 11 else "***"
            await message.reply_text(
                f"🔑 <b>Текущий AI_KEY:</b> <code>{html.escape(masked)}</code>\n\n"
                "Изменить:\n<code>.aikey sk-or-v1-...</code>"
            )
        else:
            await message.reply_text(
                "❌ <b>AI_KEY не задан!</b>\nИспользуйте:\n<code>.aikey sk-or-v1-...</code>"
            )
        return

    new_key = args[1].strip()
    if not new_key:
        await message.reply_text("❌ Ключ пустой.")
        return

    set_ai_key(new_key)
    await message.reply_text(
        "<b>✅ AI_KEY успешно сохранён.</b>\nПроверьте через <code>.aistatus</code>"
    )


# ============================================================
# .AIBASE
# ============================================================

@Client.on_message(filters.command("aibase", prefix) & filters.me)
async def aibase_cmd(_, message: Message):
    args = message.text.split(maxsplit=1)

    if len(args) < 2:
        await message.reply_text(
            f"🌐 <b>Текущий URL:</b>\n<code>{html.escape(get_ai_base_url())}</code>\n\n"
            "Изменить:\n<code>.aibase https://...</code>"
        )
        return

    new_url = args[1].strip()
    if not re.match(r"^https?://", new_url, re.IGNORECASE):
        await message.reply_text(
            "❌ URL должен начинаться с <code>http://</code> или <code>https://</code>"
        )
        return

    set_ai_base_url(new_url)
    await message.reply_text(
        f"<b>✅ URL API изменён на:</b>\n<code>{html.escape(new_url)}</code>"
    )


# ============================================================
# .AISTATUS
# ============================================================

@Client.on_message(filters.command("aistatus", prefix) & filters.me)
async def aistatus(_, message: Message):
    key = get_ai_key()
    current_model = get_current_model()
    base_url = get_ai_base_url()
    models = get_models()
    preset = get_current_preset()
    log_target = get_log_chat()

    lines = [
        "<b>🤖 AI ChatBot Status</b>",
        "",
        "• Модуль загружен: <b>да</b>",
        "• AI_KEY: " + ("<code>задан</code>" if key else "<b>❌ НЕ ЗАДАН!</b>"),
        f"• URL API: <code>{html.escape(base_url)}</code>",
        f"• Текущая модель: <code>{html.escape(current_model)}</code>",
        f"• Пресет характера: <code>{html.escape(preset)}</code>",
        f"• Логи в чат: <code>{html.escape(str(log_target or 'выкл'))}</code>",
        f"• Моделей в списке: <b>{len(models)}</b>",
    ]

    if key:
        try:
            answer = await _chat("ping", "Отвечай одним словом: pong")
            lines.append("")
            lines.append(f"✅ <b>Тестовый запрос OK:</b> {html.escape(answer[:100])}")
        except Exception as e:
            lines.append("")
            lines.append(f"❌ <b>Тестовый запрос упал:</b>\n<code>{html.escape(str(e))}</code>")
    else:
        lines.append("")
        lines.append("→ Задайте ключ:\n<code>.aikey sk-or-v1-...</code>")

    await message.reply_text("\n".join(lines))


# ============================================================
# .AIMODEL
# ============================================================

@Client.on_message(filters.command("aimodel", prefix) & filters.me)
async def aimodel(_, message: Message):
    args = message.text.split(maxsplit=1)
    models = get_models()
    current = get_current_model()

    if len(args) < 2 or args[1].strip().lower() == "list":
        text = f"<b>📋 Доступные модели ({len(models)} шт.):</b>\n\n"
        for i, model in enumerate(models, 1):
            marker = "✅ " if model == current else "   "
            text += f"{marker}{i}. <code>{html.escape(model)}</code>\n"

        text += f"\n<i>Текущая модель: {html.escape(current)}</i>"
        text += (
            "\n\n<b>Команды:</b>"
            "\n<code>.aimodel &lt;номер или имя&gt;</code>"
            "\n<code>.aimodel add &lt;модель&gt;</code>"
            "\n<code>.aimodel del &lt;номер&gt;</code>"
        )
        await message.reply_text(text)
        return

    arg = args[1].strip()

    if arg.lower().startswith("add "):
        new_model = arg[4:].strip()
        if not new_model:
            await message.reply_text("❌ Укажите название модели.")
            return

        if new_model not in models:
            models.append(new_model)
            save_models(models)
            await message.reply_text(
                f"<b>✅ Модель добавлена:</b> <code>{html.escape(new_model)}</code>"
            )
        else:
            await message.reply_text("<b>ℹ️ Такая модель уже есть.</b>")
        return

    if arg.lower().startswith("del "):
        try:
            idx = int(arg[4:].strip()) - 1
        except ValueError:
            await message.reply_text("❌ Использование: <code>.aimodel del 2</code>")
            return

        if not (0 <= idx < len(models)):
            await message.reply_text("❌ Модель с таким номером не найдена.")
            return

        if len(models) <= 1:
            await message.reply_text("❌ Нельзя удалить последнюю модель.")
            return

        removed = models.pop(idx)
        if removed == current:
            new_current = models[0]
            db.set("custom.chatbot", "current_model", new_current)

        save_models(models)
        await message.reply_text(
            f"<b>✅ Модель удалена:</b> <code>{html.escape(removed)}</code>"
        )
        return

    try:
        idx = int(arg) - 1
        if 0 <= idx < len(models):
            selected = models[idx]
            set_current_model(selected)
            await message.reply_text(
                f"<b>✅ Модель изменена на:</b> <code>{html.escape(selected)}</code>"
            )
            return

        await message.reply_text(
            f"<b>❌ Неверный номер.</b> Доступно: <b>{len(models)}</b>"
        )
        return
    except ValueError:
        pass

    if set_current_model(arg):
        await message.reply_text(
            f"<b>✅ Модель установлена:</b> <code>{html.escape(arg)}</code>"
        )


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
            f"<b>🌐 Чеченский язык: {state}</b>\n"
            f"Дальше бот отвечает {'на чеченском' if new_state else 'как обычно'}.\n\n"
            "Управление словарём:\n"
            "<code>.che add слово=перевод</code>\n"
            "<code>.che del слово</code>\n"
            "<code>.che list</code>\n"
            "<code>.che load</code>"
        )
        return

    if sub in ("on", "1", "yes", "true"):
        set_che_enabled(True)
        await message.reply_text("<b>🌐 Чеченский язык: включён</b>")
        return

    if sub in ("off", "0", "no", "false"):
        set_che_enabled(False)
        await message.reply_text("<b>🌐 Чеченский язык: выключен</b>")
        return

    if sub == "list":
        glossary = _get_glossary()
        if not glossary:
            await message.reply_text("<b>Словарь пуст.</b>")
            return

        text = f"<b>📖 Чеченский словарь ({len(glossary)} записей):</b>\n\n"
        text += "\n".join(f"• {html.escape(x)}" for x in glossary)
        await message.reply_text(text)
        return

    if sub == "add":
        rest = message.text.split(maxsplit=2)
        if len(rest) < 3:
            await message.reply_text(
                "<b>Использование:</b>\n<code>.che add слово=перевод</code>"
            )
            return

        entry = rest[2].strip()
        ok = add_glossary_word(entry)
        status = "добавлено" if ok else "уже есть/неверный формат"
        await message.reply_text(
            f"<b>📖 Слово {status}:</b> <code>{html.escape(entry)}</code>"
        )
        return

    if sub == "load":
        reply = message.reply_to_message
        if not (reply and reply.document):
            await message.reply_text(
                "<b>Использование:</b>\nОтправьте PDF и ответьте на него:\n<code>.che load</code>"
            )
            return

        document = reply.document
        file_name = document.file_name or ""
        if not file_name.lower().endswith(".pdf"):
            await message.reply_text("<b>❌ Это не PDF.</b>")
            return

        await message.reply_text("⏳ Читаю PDF-словарь...")
        path = None
        try:
            path = await reply.download()
            if not path:
                raise RuntimeError("Не удалось скачать PDF.")

            added, error = load_glossary_from_pdf(path)
            if error:
                await message.reply_text(f"<b>❌ {html.escape(error)}</b>")
                return

            await message.reply_text(
                f"<b>📖 Словарь загружен!</b>\n"
                f"Добавлено новых слов: <b>{added}</b>\n"
                f"Всего записей: <b>{len(_get_glossary())}</b>"
            )
        except Exception as e:
            await message.reply_text(
                f"<b>❌ Ошибка обработки:</b>\n<code>{html.escape(str(e))}</code>"
            )
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
            await message.reply_text(
                "<b>Использование:</b>\n<code>.che del слово</code>"
            )
            return

        word = rest[2].strip()
        ok = del_glossary_word(word)
        status = "удалено" if ok else "не найдено"
        await message.reply_text(
            f"<b>📖 Слово {status}:</b> <code>{html.escape(word)}</code>"
        )
        return

    await message.reply_text(
        "<b>Неизвестная команда.</b>\n\n"
        "<code>.che</code> — вкл/выкл\n"
        "<code>.che on</code> — включить\n"
        "<code>.che off</code> — выключить\n"
        "<code>.che list</code> — словарь\n"
        "<code>.che add слово=перевод</code>\n"
        "<code>.che del слово</code>\n"
        "<code>.che load</code> — загрузить PDF"
    )


# ============================================================
# ПОМОЩЬ
# ============================================================

modules_help["chatbot"] = {
    "aikey": "Задать API ключ: .aikey <ключ>",
    "aibase": "Задать URL API: .aibase <url>",
    "aistatus": "Показать статус ИИ, модель, пресет и логи",
    "aimodel": "Управление моделями: list, add, del, выбор",
    "aipreset": "Смена характера: toxic, friendly, bro, default, custom",
    "ailog": "Логирование: .ailog here / me / <chat_id> / test / off",
    "che": "Чеченский: .che on/off, .che list, .che add, .che del, .che load",
}
