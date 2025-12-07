#!/usr/bin/env python3
"""
Telegram-бот для проверки статусов заявлений MIDPASS (https://info.midpass.ru/).

Функции:
- пользователь кидает номер заявления -> бот отвечает "принял, проверяю...",
  потом присылает статус + картинку (кружок из локальной папки);
- этот номер автоматически добавляется в список на ежедневную проверку;
- каждый день в 08:00 UTC бот проверяет все сохранённые номера и шлёт статус
  только если изменился процент (internalStatus.percent).

Зависимости:
    pip install "python-telegram-bot[job-queue]" requests pillow

Перед запуском:
    1. Создайте бота через @BotFather и получите токен.
    2. Установите переменную окружения TELEGRAM_BOT_TOKEN с токеном бота.
    3. Запустите один раз `slice_progress_sprite.py`, чтобы создать папку
       progress_icons/ с картинками progress_0.png ... progress_100.png .
"""

import asyncio
import json
import logging
import os
from dataclasses import dataclass
from datetime import time as dtime, timezone
from io import BytesIO
from typing import Dict, List, Optional, Any

import requests
import urllib3
from PIL import Image, ImageDraw, ImageFont
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from telegram.error import TimedOut, RetryAfter, NetworkError

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ---------------------- НАСТРОЙКИ ----------------------

API_URL = "https://info.midpass.ru/api/request/{}"
DAILY_CHECK_HOUR_UTC = 8
SUBSCRIPTIONS_FILE = "subscriptions.json"
CHAT_PREFS_FILE = "chat_prefs.json"
LABELS_FILE = "labels.json"

labels: Dict[int, Dict[str, str]] = {}

DEFAULT_NOTIFY_MODE = "on_change"  # или "daily" если захочешь другое по умолчанию
# chat_id -> "on_change" | "daily"
chat_notify_mode: Dict[int, str] = {}

# Папка с заранее нарезанными картинками:
# progress_icons/progress_0.png, progress_5.png, ... progress_100.png
PROGRESS_DIR = "progress_icons"
PROGRESS_STEPS = [0, 5, 10, 20, 30, 60, 70, 80, 90, 100]
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()

API_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:145.0) "
        "Gecko/20100101 Firefox/145.0"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.5",
    "Connection": "keep-alive",
}


# -------------------------------------------------------

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)


@dataclass
class PassportStatus:
    id: int
    name: str
    color: Optional[str]


@dataclass
class InternalStatus:
    name: str
    percent: Optional[int]


@dataclass
class RequestStatus:
    uid: str
    reception_date: Optional[str]
    passport_status: PassportStatus
    internal_status: InternalStatus


# chat_id -> { uid: last_percent_or_None }
subscriptions: Dict[int, Dict[str, Optional[int]]] = {}


def load_labels() -> None:
    global labels
    logger.info("Loading labels from %s", LABELS_FILE)
    if not os.path.exists(LABELS_FILE):
        labels = {}
        return
    try:
        with open(LABELS_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)

        parsed: Dict[int, Dict[str, str]] = {}
        for chat_id_str, inner in raw.items():
            try:
                chat_id = int(chat_id_str)
            except ValueError:
                continue
            if not isinstance(inner, dict):
                continue
            parsed[chat_id] = {str(uid): str(label) for uid, label in inner.items()}

        labels = parsed
        logger.info("Labels loaded: %s", labels)
    except Exception as e:
        logger.error("Failed to load labels: %s", e)
        labels = {}


def save_labels() -> None:
    logger.info("Saving labels to %s", LABELS_FILE)
    try:
        data = {
            str(chat_id): {str(uid): label for uid, label in inner.items()}
            for chat_id, inner in labels.items()
        }
        with open(LABELS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        logger.info("Labels saved")
    except Exception as e:
        logger.error("Failed to save labels: %s", e)


def get_label(chat_id: int, uid: str) -> Optional[str]:
    return labels.get(chat_id, {}).get(str(uid))


def set_label(chat_id: int, uid: str, label: Optional[str]) -> None:
    uid = str(uid)
    if not label or not label.strip():
        # delete label
        if chat_id in labels and uid in labels[chat_id]:
            del labels[chat_id][uid]
            if not labels[chat_id]:
                del labels[chat_id]
            save_labels()
        return

    if chat_id not in labels:
        labels[chat_id] = {}
    labels[chat_id][uid] = label.strip()
    save_labels()


def load_chat_prefs() -> None:
    global chat_notify_mode
    logger.info("Loading chat prefs from %s", CHAT_PREFS_FILE)
    if not os.path.exists(CHAT_PREFS_FILE):
        chat_notify_mode = {}
        return
    try:
        with open(CHAT_PREFS_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)

        prefs: Dict[int, str] = {}
        for chat_id_str, mode in raw.items():
            try:
                chat_id = int(chat_id_str)
            except ValueError:
                continue
            if mode in ("on_change", "daily"):
                prefs[chat_id] = mode
        chat_notify_mode = prefs
        logger.info("Chat prefs loaded: %s", chat_notify_mode)
    except Exception as e:
        logger.error("Failed to load chat prefs: %s", e)
        chat_notify_mode = {}


def save_chat_prefs() -> None:
    logger.info("Saving chat prefs to %s", CHAT_PREFS_FILE)
    try:
        data = {str(chat_id): mode for chat_id, mode in chat_notify_mode.items()}
        with open(CHAT_PREFS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        logger.info("Chat prefs saved")
    except Exception as e:
        logger.error("Failed to save chat prefs: %s", e)

def get_notify_mode(chat_id: int) -> str:
    return chat_notify_mode.get(chat_id, DEFAULT_NOTIFY_MODE)


# ---------------------- УТИЛИТЫ ХРАНИЛИЩА ----------------------
def _normalize_last_percent(value: Any) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return None
    return None


def load_subscriptions() -> None:
    """Загрузить подписки из JSON-файла в память (с миграцией старого формата)."""
    global subscriptions
    logger.info("Loading subscriptions from %s", SUBSCRIPTIONS_FILE)
    if not os.path.exists(SUBSCRIPTIONS_FILE):
        logger.info("Subscriptions file not found, starting with empty dict")
        subscriptions = {}
        return

    try:
        with open(SUBSCRIPTIONS_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)

        migrated: Dict[int, Dict[str, Optional[int]]] = {}
        for chat_id_str, v in raw.items():
            try:
                chat_id = int(chat_id_str)
            except ValueError:
                continue

            if isinstance(v, list):
                # старый формат: просто список UID
                migrated[chat_id] = {str(uid): None for uid in v}
            elif isinstance(v, dict):
                inner: Dict[str, Optional[int]] = {}
                for uid, last_p in v.items():
                    inner[str(uid)] = _normalize_last_percent(last_p)
                migrated[chat_id] = inner
            else:
                migrated[chat_id] = {}

        subscriptions = migrated
        logger.info("Subscriptions loaded (migrated): %s", subscriptions)
    except Exception as e:
        logger.error("Failed to load subscriptions: %s", e)
        subscriptions = {}


def save_subscriptions() -> None:
    """Сохранить подписки в JSON-файл (новый формат)."""
    logger.info("Saving subscriptions to %s", SUBSCRIPTIONS_FILE)
    try:
        data = {str(chat_id): inner for chat_id, inner in subscriptions.items()}
        with open(SUBSCRIPTIONS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        logger.info("Subscriptions saved")
    except Exception as e:
        logger.error("Failed to save subscriptions: %s", e)


def add_subscription(chat_id: int, uid: str, last_percent: Optional[int]) -> None:
    """Добавить uid в подписки конкретного чата (с последним процентом)."""
    uid = str(uid)
    logger.info(
        "Adding subscription: chat_id=%s uid=%s last_percent=%s",
        chat_id,
        uid,
        last_percent,
    )
    if chat_id not in subscriptions:
        subscriptions[chat_id] = {}
    prev = subscriptions[chat_id].get(uid)
    if prev != last_percent:
        subscriptions[chat_id][uid] = last_percent
        save_subscriptions()


def get_last_percent(chat_id: int, uid: str) -> Optional[int]:
    return subscriptions.get(chat_id, {}).get(str(uid))


def set_last_percent(chat_id: int, uid: str, percent: Optional[int]) -> None:
    uid = str(uid)
    if chat_id not in subscriptions:
        subscriptions[chat_id] = {}
    subscriptions[chat_id][uid] = percent
    save_subscriptions()


def remove_subscription(chat_id: int, uid: str) -> bool:
    """Удалить uid из подписки. Вернёт True, если реально удалили."""
    uid = str(uid)
    logger.info("Removing subscription: chat_id=%s uid=%s", chat_id, uid)
    if chat_id not in subscriptions:
        return False
    if uid not in subscriptions[chat_id]:
        return False
    del subscriptions[chat_id][uid]
    if not subscriptions[chat_id]:
        del subscriptions[chat_id]
    save_subscriptions()
    return True


# ---------------------- РАБОТА С API ----------------------
def fetch_status(uid: str, timeout: int = 10) -> Optional[RequestStatus]:
    """Синхронный запрос к API."""
    url = API_URL.format(uid)
    logger.info("Fetching status for uid=%s url=%s", uid, url)
    try:
        resp = requests.get(
            url,
            timeout=timeout,
            verify=False,
            headers=API_HEADERS,
        )
    except Exception as e:
        logger.error("Request error for %s: %s", uid, e)
        return None

    logger.info("Response for uid=%s: status_code=%s", uid, resp.status_code)
    if resp.status_code != 200:
        logger.warning("Non-200 response for %s: %s", uid, resp.status_code)
        return None

    try:
        data = resp.json()
        logger.debug("JSON for %s: %s", uid, json.dumps(data, ensure_ascii=False))
    except Exception as e:
        logger.error("JSON parse error for %s: %s", uid, e)
        return None

    try:
        passport = data.get("passportStatus") or {}
        internal = data.get("internalStatus") or {}

        passport_status = PassportStatus(
            id=int(passport.get("id", 0)),
            name=str(passport.get("name", "неизвестно")),
            color=passport.get("color"),
        )
        internal_status = InternalStatus(
            name=str(internal.get("name", "неизвестно")),
            percent=_normalize_last_percent(internal.get("percent")),
        )

        status = RequestStatus(
            uid=str(data.get("uid") or uid),
            reception_date=data.get("receptionDate"),
            passport_status=passport_status,
            internal_status=internal_status,
        )
        logger.info(
            "Built status object for uid=%s: passport=%s internal=%s",
            uid,
            passport_status,
            internal_status,
        )
        return status
    except Exception as e:
        logger.error("Failed to build status object for %s: %s", uid, e)
        return None


async def fetch_status_async(uid: str, timeout: int = 10) -> Optional[RequestStatus]:
    loop = asyncio.get_running_loop()
    logger.debug("Scheduling fetch_status in executor for uid=%s", uid)
    return await loop.run_in_executor(None, fetch_status, uid, timeout)


def format_status_text(status: RequestStatus, label: Optional[str] = None) -> str:
    header = f"Заявление: `{status.uid}`"
    if label:
        header += f" — {label}"
    lines = [header]

    if status.reception_date:
        lines.append(f"Дата подачи: {status.reception_date}")
    lines.append(f"Статус: *{status.passport_status.name}*")
    if status.internal_status.name:
        extra = f"Внутренний статус: {status.internal_status.name}"
        if status.internal_status.percent is not None:
            extra += f" ({status.internal_status.percent}%)"
        lines.append(extra)
    text = "\n".join(lines)
    logger.debug("Formatted text for uid=%s: %s", status.uid, text)
    return text



# ---------------------- КАРТИНКА С ПРОГРЕССОМ ----------------------
def create_status_image(status: RequestStatus) -> BytesIO:
    """
    Берём подходящую картинку из progress_icons по проценту.
    Если процента нет / он некорректный / файла нет — рисуем fallback.
    """
    raw_percent = status.internal_status.percent
    percent: Optional[int] = None
    if isinstance(raw_percent, int) and 0 <= raw_percent <= 100:
        percent = raw_percent

    if percent is not None:
        nearest = min(PROGRESS_STEPS, key=lambda v: abs(v - percent))
        filename = os.path.join(PROGRESS_DIR, f"progress_{nearest}.png")
        logger.info(
            "Using local icon for uid=%s: percent=%s -> nearest=%s file=%s",
            status.uid,
            percent,
            nearest,
            filename,
        )
        if os.path.exists(filename):
            with open(filename, "rb") as f:
                data = f.read()
            buf = BytesIO(data)
            buf.seek(0)
            return buf
        else:
            logger.warning("Icon file %s not found, using fallback image", filename)
    else:
        logger.warning(
            "Percent value %r for uid=%s is invalid; using fallback image",
            raw_percent,
            status.uid,
        )

    # Fallback: простая карточка с текстом процента
    img = Image.new("RGB", (300, 300), (60, 60, 60))
    draw = ImageDraw.Draw(img)
    try:
        font_big = ImageFont.truetype("DejaVuSans.ttf", 80)
        font_small = ImageFont.truetype("DejaVuSans.ttf", 32)
    except Exception:
        font_big = ImageFont.load_default()
        font_small = ImageFont.load_default()

    label = (
        f"{raw_percent}%"
        if raw_percent is not None
        else "?"
    )
    sub = "Завершено"

    bbox = font_big.getbbox(label)
    tw = bbox[2] - bbox[0]
    th = bbox[3] - bbox[1]
    x = (img.width - tw) // 2
    y = (img.height - th) // 2 - 20
    draw.text((x, y), label, font=font_big, fill=(255, 255, 255))

    bbox2 = font_small.getbbox(sub)
    tw2 = bbox2[2] - bbox2[0]
    th2 = bbox2[3] - bbox2[1]
    x2 = (img.width - tw2) // 2
    y2 = y + th + 10
    draw.text((x2, y2), sub, font=font_small, fill=(200, 200, 200))

    buf = BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf


# ---------------------- HANDLERS ----------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id if update.effective_chat else None
    logger.info("Received /start from chat_id=%s", chat_id)

    text = (
        "Привет!\n\n"
        "Пришли мне номер заявления (как на сайте info.midpass.ru), "
        "а я верну текущий статус и буду по умолчанию проверять его каждый день "
        "в 08:00 UTC.\n\n"
        "По умолчанию уведомления приходят только при изменении процента готовности.\n"
        "Если хочешь получать статусы каждый день, используй команду /mode_daily или /mode daily.\n\n"
        "Команды:\n"
        "/list — какие номера сейчас отслеживаются\n"
        "/remove <uid> — убрать номер из ежедневной проверки\n"
        "/clear — убрать все номера и ярлыки\n"
        "/check — проверить все номера прямо сейчас\n"
        "/label <uid> <ярлык> — задать имя номеру (например, \"паспорт жены\")\n"
        "/mode — посмотреть или изменить режим уведомлений\n"
        "/mode_daily — сразу включить ежедневные уведомления\n"
        "/mode_on_change — уведомлять только при изменении процента\n"
        "/erase_data — полностью удалить все данные этого чата (номера, настройки, ярлыки)\n"
    )


    await update.message.reply_text(text)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await start(update, context)


async def label_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    logger.info("/label from chat_id=%s args=%s", chat_id, context.args)

    if not context.args:
        await update.message.reply_text(
            "Использование:\n"
            "/label <uid> <ярлык> — установить или изменить ярлык\n"
            "/label <uid> — удалить ярлык для номера\n\n"
            "Примеры:\n"
            "/label 1234567890 Мои документы\n"
            "/label 1234567890"
        )
        return

    uid_text = context.args[0]
    uid = extract_uid(uid_text)
    if not uid:
        await update.message.reply_text("Не понял номер заявления.")
        return

    label = " ".join(context.args[1:]).strip()

    if not label:
        prev = get_label(chat_id, uid)
        set_label(chat_id, uid, None)
        if prev:
            await update.message.reply_text(
                f"Ярлык для номера `{uid}` удалён.",
                parse_mode="Markdown",
            )
        else:
            await update.message.reply_text(
                f"Для номера `{uid}` не было сохранено ярлыка.",
                parse_mode="Markdown",
            )
        return

    set_label(chat_id, uid, label)
    await update.message.reply_text(
        f"Для номера `{uid}` установлен ярлык: {label}",
        parse_mode="Markdown",
    )


def extract_uid(text: str) -> Optional[str]:
    digits = "".join(ch for ch in text if ch.isdigit())
    logging.debug("extract_uid: text=%r digits=%r", text, digits)
    if len(digits) < 10:
        return None
    return digits


async def handle_uid_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.text:
        logger.debug("handle_uid_message: no message or text")
        return

    raw_text = update.message.text.strip()
    chat_id = update.effective_chat.id if update.effective_chat else None
    logger.info("New text message from chat_id=%s: %r", chat_id, raw_text)

    uid = extract_uid(raw_text)
    if not uid:
        logger.info("No UID found in message from chat_id=%s", chat_id)
        return

    logger.info("Extracted uid=%s from chat_id=%s", uid, chat_id)

    # Быстрый отклик, чтобы не казалось, что бот подвис
    await update.message.reply_text(
        f"Принял номер `{uid}`, проверяю статус...",
        parse_mode="Markdown",
    )

    # Асинхронно дергаем API в отдельном потоке
    status = await fetch_status_async(uid)
    if not status:
        logger.info("Status for uid=%s not obtained (None)", uid)
        await update.message.reply_text(
            "Не удалось получить статус по этому номеру. "
            "Проверь, что номер корректный, или попробуй позже."
        )
        return

    # Текст + картинка
    label = get_label(chat_id, status.uid) if chat_id is not None else None
    caption = format_status_text(status, label)
    image_buf = create_status_image(status)

    logger.info("Sending photo with status for uid=%s to chat_id=%s", status.uid, chat_id)
    await update.message.reply_photo(photo=image_buf, caption=caption, parse_mode="Markdown")

    # добавляем в подписку и запоминаем процент
    add_subscription(chat_id, status.uid, status.internal_status.percent)


async def list_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    logger.info("/list from chat_id=%s", chat_id)
    uids = subscriptions.get(chat_id, {})
    if not uids:
        await update.message.reply_text("Для этого чата пока ничего не отслеживаю.")
        return
    lines = ["Отслеживаю следующие номера:"]
    for u in uids.keys():
        label = get_label(chat_id, u)
        if label:
            lines.append(f"- `{u}` — {label}")
        else:
            lines.append(f"- `{u}`")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def remove_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    logger.info("/remove from chat_id=%s args=%s", chat_id, context.args)
    if not context.args:
        await update.message.reply_text("Использование: /remove <uid>")
        return
    uid = extract_uid(" ".join(context.args)) or context.args[0]
    if not uid:
        await update.message.reply_text("Не понял номер заявления.")
        return
    ok = remove_subscription(chat_id, uid)
    if ok:
        await update.message.reply_text(f"Номер `{uid}` больше не отслеживается.", parse_mode="Markdown")
    else:
        await update.message.reply_text("Такой номер не был в списке.")

    set_label(chat_id, uid, None)



async def clear_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    logger.info("/clear from chat_id=%s", chat_id)

    had_any = False

    if chat_id in subscriptions:
        del subscriptions[chat_id]
        save_subscriptions()
        had_any = True

    if chat_id in labels:
        del labels[chat_id]
        save_labels()
        had_any = True

    if had_any:
        await update.message.reply_text("Все номера и ярлыки для этого чата удалены.")
    else:
        await update.message.reply_text("И так ничего не отслеживаю.")


async def manual_check_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Ручная проверка всех номеров этого чата.
    Всегда шлёт статусы, но при этом обновляет last_percent.
    """
    chat_id = update.effective_chat.id
    logger.info("/check from chat_id=%s", chat_id)
    uids = subscriptions.get(chat_id, {})
    if not uids:
        await update.message.reply_text("Для этого чата нет сохранённых номеров.")
        return

    await update.message.reply_text("Проверяю статусы...")

    for uid in list(uids.keys()):
        logger.info("Manual check uid=%s for chat_id=%s", uid, chat_id)
        status = await fetch_status_async(uid)
        if not status:
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"Не удалось получить статус по номеру `{uid}`.",
                parse_mode="Markdown",
            )
            continue

        label = get_label(chat_id, uid)
        caption = format_status_text(status, label)
        image_buf = create_status_image(status)
        await context.bot.send_photo(
            chat_id=chat_id,
            photo=image_buf,
            caption=caption,
            parse_mode="Markdown",
        )

        # обновляем last_percent
        set_last_percent(chat_id, uid, status.internal_status.percent)


# ---------------------- JOBQUEUE: ежедневная проверка ----------------------
async def scheduled_check(context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Ежедневная проверка всех UID во всех чатах.
    В режиме on_change шлём уведомление только если изменился процент,
    в режиме daily — всегда.
    """
    logger.info("Running daily scheduled check...")
    if not subscriptions:
        logger.info("No subscriptions to check")
        return

    for chat_id, uids in list(subscriptions.items()):
        logger.info("Checking chat_id=%s with uids=%s", chat_id, list(uids.keys()))
        for uid in list(uids.keys()):
            logger.info("Scheduled check uid=%s for chat_id=%s", uid, chat_id)
            status = await fetch_status_async(uid)
            if not status:
                try:
                    await context.bot.send_message(
                        chat_id=chat_id,
                        text=f"Не удалось получить статус по номеру `{uid}`.",
                        parse_mode="Markdown",
                    )
                except Exception as e:
                    logger.error("Failed to send error message to chat %s: %s", chat_id, e)
                continue

            last_percent = get_last_percent(chat_id, uid)
            current_raw = status.internal_status.percent
            current_percent = _normalize_last_percent(current_raw)

            mode = get_notify_mode(chat_id)

            logger.info(
                "UID %s in chat %s: mode=%s last_percent=%s current_percent=%s",
                uid,
                chat_id,
                mode,
                last_percent,
                current_percent,
            )

            if mode == "on_change" and last_percent == current_percent:
                logger.info(
                    "No change for uid=%s in chat_id=%s with mode=on_change, skip notify",
                    uid,
                    chat_id,
                )
                continue

            # процент изменился -> отправляем
            label = get_label(chat_id, uid)
            caption = format_status_text(status, label)
            image_buf = create_status_image(status)
            try:
                await context.bot.send_photo(
                    chat_id=chat_id,
                    photo=image_buf,
                    caption=caption,
                    parse_mode="Markdown",
                )
            except Exception as e:
                logger.error("Failed to send photo to chat %s: %s", chat_id, e)

            # обновляем сохранённый процент
            set_last_percent(chat_id, uid, current_percent)


# ---------------------- ERROR HANDLER ----------------------
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    err = context.error
    logger.debug("error_handler invoked with error=%r update=%r", err, update)
    try:
        raise err
    except RetryAfter as e:
        # это тот самый flood control от Telegram
        logger.info("Flood control: retry after %s seconds", e.retry_after)
        await asyncio.sleep(int(e.retry_after))
    except TimedOut:
        logger.info("Telegram TimedOut (сервер долго отвечал)")
    except NetworkError as e:
        logger.info("Telegram NetworkError: %s", e)
    except Exception:
        # Тут оставим ERROR, это реально неожиданные штуки
        logger.exception("Unexpected error:")


async def mode_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    logger.info("/mode from chat_id=%s args=%s", chat_id, context.args)

    current_mode = get_notify_mode(chat_id)

    if not context.args:
        if current_mode == "daily":
            desc = (
                "Сейчас режим: ежедневно.\n\n"
                "Я буду каждый день присылать статусы по всем отслеживаемым номерам, "
                "даже если процент не изменился.\n\n"
                "Быстрые команды:\n"
                "/mode_on_change — переключиться на уведомления только при изменении\n"
                "/mode — показать и изменить режим вручную"
            )
        else:
            desc = (
                "Сейчас режим: только при изменении.\n\n"
                "Я присылаю уведомления, только если изменился процент готовности.\n\n"
                "Быстрые команды:\n"
                "/mode_daily — включить ежедневную рассылку\n"
                "/mode — показать и изменить режим вручную"
            )
        await update.message.reply_text(desc)
        return

    arg = context.args[0].lower()

    if arg in ("daily", "ежедневно"):
        chat_notify_mode[chat_id] = "daily"
        save_chat_prefs()
        await update.message.reply_text(
            "Режим уведомлений изменён.\n"
            "Теперь я буду каждый день присылать статусы по всем отслеживаемым номерам, "
            "даже если процент не изменился."
        )
    elif arg in ("on_change", "change", "по_изменению"):
        chat_notify_mode[chat_id] = "on_change"
        save_chat_prefs()
        await update.message.reply_text(
            "Режим уведомлений изменён.\n"
            "Теперь я буду присылать уведомления только если изменился процент готовности."
        )
    else:
        await update.message.reply_text(
            "Не понял режим.\n"
            "Используй:\n"
            "/mode on_change – уведомлять только при изменении процента\n"
            "/mode daily – уведомлять каждый день"
        )


async def mode_daily_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    logger.info("/mode_daily from chat_id=%s", chat_id)
    chat_notify_mode[chat_id] = "daily"
    save_chat_prefs()
    await update.message.reply_text(
        "Режим уведомлений изменён.\n"
        "Теперь я буду каждый день присылать статусы по всем отслеживаемым номерам, "
        "даже если процент не изменился."
    )


async def mode_on_change_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    logger.info("/mode_on_change from chat_id=%s", chat_id)
    chat_notify_mode[chat_id] = "on_change"
    save_chat_prefs()
    await update.message.reply_text(
        "Режим уведомлений изменён.\n"
        "Теперь я буду присылать уведомления только если изменился процент готовности."
    )


async def erase_data_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    logger.info("/erase_data from chat_id=%s", chat_id)

    removed_anything = False

    if chat_id in subscriptions:
        del subscriptions[chat_id]
        save_subscriptions()
        removed_anything = True

    if chat_id in chat_notify_mode:
        del chat_notify_mode[chat_id]
        save_chat_prefs()
        removed_anything = True

    if chat_id in labels:
        del labels[chat_id]
        save_labels()
        removed_anything = True

    if removed_anything:
        await update.message.reply_text(
            "Все данные для этого чата удалены: номера, настройки уведомлений и ярлыки."
        )
    else:
        await update.message.reply_text(
            "Для этого чата не было сохранённых данных."
        )


# ---------------------- MAIN ----------------------
def main() -> None:
    if not TELEGRAM_BOT_TOKEN:
        raise SystemExit(
            "Не задан TELEGRAM_BOT_TOKEN. "
            "Установи переменную окружения TELEGRAM_BOT_TOKEN с токеном бота."
        )

    load_subscriptions()
    load_chat_prefs()
    load_labels()

    application = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN)
        .read_timeout(30.0)
        .write_timeout(30.0)
        .connect_timeout(30.0)
        .pool_timeout(30.0)
        .build()
    )
    application.add_error_handler(error_handler)

    # Команды
    application.add_handler(CommandHandler(["start", "help"], start))
    application.add_handler(CommandHandler("list", list_command))
    application.add_handler(CommandHandler("remove", remove_command))
    application.add_handler(CommandHandler("clear", clear_command))
    application.add_handler(CommandHandler("check", manual_check_command))
    application.add_handler(CommandHandler("mode", mode_command))
    application.add_handler(CommandHandler("mode_daily", mode_daily_command))
    application.add_handler(CommandHandler("mode_on_change", mode_on_change_command))
    application.add_handler(CommandHandler("erase_data", erase_data_command))
    application.add_handler(CommandHandler("label", label_command))



    # Любой текст — пытаемся вытащить из него номер заявления
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_uid_message)
    )

    # Планируем ежедневную задачу в 08:00 UTC
    application.job_queue.run_daily(
        callback=scheduled_check,
        time=dtime(hour=DAILY_CHECK_HOUR_UTC, minute=0, tzinfo=timezone.utc),
        name="daily_midpass_check",
    )

    logger.info("Bot started. Daily check at %02d:00 UTC", DAILY_CHECK_HOUR_UTC)
    application.run_polling()


if __name__ == "__main__":
    main()
