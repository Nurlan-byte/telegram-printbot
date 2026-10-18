"""
PrintBot — Telegram-бот, который сразу печатает присланные файлы через CUPS.

Поддерживает: PDF, Word (.doc/.docx/.odt/.rtf — конвертируется через LibreOffice), JPG/PNG и фото.
Подпись к файлу "2" / "x2" / "2 шт" / "2 копии" = количество копий.
Новые пользователи получают доступ только после одобрения администратором (кнопки в Telegram).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    User,
)

BASE_DIR = Path(__file__).resolve().parent


def load_env(path: Path) -> None:
    """Простейший загрузчик .env (KEY=VALUE), чтобы не тянуть лишних зависимостей."""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


load_env(BASE_DIR / ".env")

BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
ADMIN_IDS = {int(x) for x in re.split(r"[,\s]+", os.environ.get("ADMIN_IDS", "")) if x.strip().isdigit()}
PRINTER = os.environ.get("PRINTER_NAME", "").strip()
MAX_COPIES = int(os.environ.get("MAX_COPIES", "10") or 10)
DATA_DIR = Path(os.environ.get("DATA_DIR", "") or BASE_DIR / "data")

MAX_FILE_BYTES = 20 * 1024 * 1024  # лимит Telegram Bot API на скачивание файлов

USERS_FILE = DATA_DIR / "users.json"
SPOOL_DIR = DATA_DIR / "spool"
LO_PROFILE = DATA_DIR / "lo-profile"

PDF_EXT = {".pdf"}
IMAGE_EXT = {".jpg", ".jpeg", ".png"}
OFFICE_EXT = {".doc", ".docx", ".odt", ".rtf"}
SUPPORTED_EXT = PDF_EXT | IMAGE_EXT | OFFICE_EXT

log = logging.getLogger("printbot")
router = Router()
convert_lock = asyncio.Lock()  # LibreOffice не любит параллельные конвертации с одним профилем

HELP_TEXT = (
    "Присылай файл — я сразу отправлю его на принтер.\n\n"
    "Поддерживаю: PDF, Word (.doc, .docx), JPG/PNG и фото.\n"
    "Нужно несколько копий — напиши число в подписи к файлу, например «2».\n\n"
    "Команды:\n"
    "/status — состояние принтера и очередь\n"
    "/id — твой Telegram ID"
)

ADMIN_HELP = (
    "\n\nДля администратора:\n"
    "/users — кому открыт доступ\n"
    "/remove <id> — закрыть доступ\n"
    "/clear — отменить все задания в очереди"
)


# ---------------------------------------------------------------- пользователи
class Users:
    """Хранит разрешённых и ожидающих пользователей в data/users.json."""

    def __init__(self, path: Path):
        self.path = path
        self.allowed: dict[str, str] = {}
        self.pending: dict[str, str] = {}
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            self.allowed = data.get("allowed", {})
            self.pending = data.get("pending", {})

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps({"allowed": self.allowed, "pending": self.pending}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp.replace(self.path)

    def is_allowed(self, user_id: int) -> bool:
        return user_id in ADMIN_IDS or str(user_id) in self.allowed


users: Users  # создаётся в main()


def display_name(user: User) -> str:
    name = user.full_name or str(user.id)
    return f"{name} (@{user.username})" if user.username else name


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


async def ensure_allowed(msg: Message) -> bool:
    """True — можно печатать. Иначе отправляет запрос администраторам."""
    user = msg.from_user
    if user is None:
        return False
    if users.is_allowed(user.id):
        return True

    uid = str(user.id)
    if uid in users.pending:
        await msg.answer("Запрос на доступ уже отправлен администратору, подожди подтверждения.")
        return False

    if not ADMIN_IDS:
        log.warning("ADMIN_IDS пуст — некому одобрить пользователя %s", user.id)
        await msg.answer(
            "Бот ещё не настроен: не указан администратор.\n"
            f"Твой Telegram ID: {user.id}"
        )
        return False

    users.pending[uid] = display_name(user)
    users.save()

    kb = InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(text="✅ Разрешить", callback_data=f"acc:ok:{uid}"),
            InlineKeyboardButton(text="🚫 Отклонить", callback_data=f"acc:no:{uid}"),
        ]]
    )
    for admin_id in ADMIN_IDS:
        try:
            await msg.bot.send_message(
                admin_id,
                f"Новый пользователь хочет печатать:\n{display_name(user)}\nID: {uid}",
                reply_markup=kb,
            )
        except Exception:
            log.exception("Не удалось уведомить администратора %s", admin_id)

    await msg.answer("У тебя пока нет доступа. Я отправил запрос администратору — как только он подтвердит, присылай файлы.")
    return False


# ---------------------------------------------------------------- печать
async def run(*cmd: str, timeout: int = 120) -> str:
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise RuntimeError(f"{cmd[0]}: превышено время ожидания")
    if proc.returncode != 0:
        text = err.decode(errors="replace").strip() or out.decode(errors="replace").strip()
        raise RuntimeError(text or f"{cmd[0]} завершился с кодом {proc.returncode}")
    return out.decode(errors="replace")


async def convert_to_pdf(src: Path) -> Path:
    async with convert_lock:
        await run(
            "soffice",
            f"-env:UserInstallation=file://{LO_PROFILE}",
            "--headless", "--norestore", "--nolockcheck",
            "--convert-to", "pdf",
            "--outdir", str(src.parent),
            str(src),
            timeout=180,
        )
    pdf = src.with_suffix(".pdf")
    if not pdf.exists():
        raise RuntimeError("LibreOffice не смог сконвертировать документ в PDF")
    return pdf


async def send_to_printer(path: Path, copies: int, title: str, fit_to_page: bool) -> str:
    cmd = ["lp"]
    if PRINTER:
        cmd += ["-d", PRINTER]
    cmd += ["-n", str(copies), "-t", title[:100]]
    if fit_to_page:
        cmd += ["-o", "fit-to-page"]
    cmd.append(str(path))
    out = await run(*cmd, timeout=60)
    match = re.search(r"request id is (\S+)", out)
    return match.group(1) if match else ""


COPIES_RE = re.compile(r"^\s*[xх×*]?\s*(\d{1,2})\s*(шт|штук[и]?|копи[йяи]|экз)?\.?\s*$", re.IGNORECASE)


def parse_copies(caption: str | None) -> int:
    """Подпись должна состоять только из числа копий — «Накладная №5» не превратится в 5 копий."""
    if not caption:
        return 1
    match = COPIES_RE.match(caption)
    if not match:
        return 1
    return max(1, min(int(match.group(1)), MAX_COPIES))


async def process_file(msg: Message, downloadable, filename: str, ext: str) -> None:
    copies = parse_copies(msg.caption)
    status = await msg.reply("⏳ Принял, готовлю к печати…")
    job_dir = Path(tempfile.mkdtemp(dir=SPOOL_DIR))
    try:
        src = job_dir / f"input{ext}"
        await msg.bot.download(downloadable, destination=src)

        to_print = src
        if ext in OFFICE_EXT:
            await status.edit_text("⏳ Конвертирую Word в PDF…")
            to_print = await convert_to_pdf(src)

        job_id = await send_to_printer(to_print, copies, filename, fit_to_page=ext in IMAGE_EXT)

        text = f"🖨 Отправлено на печать: {filename}"
        if copies > 1:
            text += f" × {copies}"
        await status.edit_text(text)
        log.info("Печать: %s ×%d от %s, задание %s", filename, copies, display_name(msg.from_user), job_id or "?")
    except Exception as exc:
        log.exception("Ошибка печати %s", filename)
        await status.edit_text(f"❌ Не получилось напечатать {filename}:\n{str(exc)[:500]}")
    finally:
        # CUPS уже скопировал файл в свою очередь, наша копия больше не нужна
        shutil.rmtree(job_dir, ignore_errors=True)


# ---------------------------------------------------------------- обработчики
@router.message(CommandStart())
@router.message(Command("help"))
async def cmd_start(msg: Message) -> None:
    if not await ensure_allowed(msg):
        return
    await msg.answer(HELP_TEXT + (ADMIN_HELP if is_admin(msg.from_user.id) else ""))


@router.message(Command("id"))
async def cmd_id(msg: Message) -> None:
    await msg.answer(f"Твой Telegram ID: {msg.from_user.id}")


@router.message(Command("status"))
async def cmd_status(msg: Message) -> None:
    if not await ensure_allowed(msg):
        return
    try:
        printers = await run("lpstat", "-p", *( [PRINTER] if PRINTER else [] ))
        queue = await run("lpstat", "-o", *( [PRINTER] if PRINTER else [] ))
        default = await run("lpstat", "-d")
    except Exception as exc:
        await msg.answer(f"Не удалось получить статус: {exc}")
        return
    text = f"{default.strip()}\n\n{printers.strip() or 'Принтеры не найдены'}\n\nОчередь:\n{queue.strip() or 'пусто'}"
    await msg.answer(text[:3500])


@router.message(Command("users"))
async def cmd_users(msg: Message) -> None:
    if not is_admin(msg.from_user.id):
        return
    if not users.allowed:
        await msg.answer("Пока доступ никому не выдан (кроме администраторов).")
        return
    lines = [f"{uid} — {name}" for uid, name in users.allowed.items()]
    await msg.answer("Доступ есть у:\n" + "\n".join(lines) + "\n\nУбрать: /remove <id>")


@router.message(Command("remove"))
async def cmd_remove(msg: Message, command: CommandObject) -> None:
    if not is_admin(msg.from_user.id):
        return
    uid = (command.args or "").strip()
    if not uid.isdigit():
        await msg.answer("Использование: /remove <id> (список — /users)")
        return
    name = users.allowed.pop(uid, None)
    users.pending.pop(uid, None)
    users.save()
    await msg.answer(f"Доступ закрыт: {name or uid}" if name else f"Пользователя {uid} нет в списке.")


@router.message(Command("clear"))
async def cmd_clear(msg: Message) -> None:
    if not is_admin(msg.from_user.id):
        return
    try:
        await run("cancel", "-a", *( [PRINTER] if PRINTER else [] ))
        await msg.answer("Очередь печати очищена.")
    except Exception as exc:
        await msg.answer(f"Не удалось очистить очередь: {exc}")


@router.callback_query(F.data.startswith("acc:"))
async def on_access_decision(cb: CallbackQuery) -> None:
    if not is_admin(cb.from_user.id):
        await cb.answer("Это может решить только администратор.", show_alert=True)
        return
    _, action, uid = cb.data.split(":", 2)
    name = users.pending.pop(uid, None) or users.allowed.get(uid) or uid

    if action == "ok":
        users.allowed[uid] = name
        result, notice = f"✅ Доступ выдан: {name}", "✅ Администратор открыл доступ. Присылай файлы — я их распечатаю.\n\n" + HELP_TEXT
    else:
        users.allowed.pop(uid, None)
        result, notice = f"🚫 Отклонено: {name}", "Администратор отклонил запрос на доступ."
    users.save()

    try:
        await cb.bot.send_message(int(uid), notice)
    except Exception:
        log.warning("Не удалось уведомить пользователя %s", uid)
    if cb.message:
        await cb.message.edit_text(result)
    await cb.answer()


@router.message(F.document)
async def on_document(msg: Message) -> None:
    if not await ensure_allowed(msg):
        return
    doc = msg.document
    filename = doc.file_name or f"file_{msg.message_id}"
    ext = Path(filename).suffix.lower()
    if ext not in SUPPORTED_EXT:
        await msg.reply("Такой формат я не печатаю. Подходят: PDF, Word (.doc, .docx, .odt, .rtf), JPG, PNG.")
        return
    if doc.file_size and doc.file_size > MAX_FILE_BYTES:
        await msg.reply("Файл больше 20 МБ — Telegram не даёт ботам скачивать такие файлы.")
        return
    await process_file(msg, doc, filename, ext)


@router.message(F.photo)
async def on_photo(msg: Message) -> None:
    if not await ensure_allowed(msg):
        return
    await process_file(msg, msg.photo[-1], f"photo_{msg.message_id}.jpg", ".jpg")


@router.message()
async def on_other(msg: Message) -> None:
    if not await ensure_allowed(msg):
        return
    await msg.answer("Пришли файл (PDF, Word или фото), и я его распечатаю. /help — подсказка.")


# ---------------------------------------------------------------- запуск
async def main() -> None:
    global users
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    if not BOT_TOKEN:
        log.error("BOT_TOKEN не задан — заполни файл .env")
        sys.exit(1)

    SPOOL_DIR.mkdir(parents=True, exist_ok=True)
    LO_PROFILE.mkdir(parents=True, exist_ok=True)
    for leftover in SPOOL_DIR.iterdir():  # мусор после аварийной остановки
        shutil.rmtree(leftover, ignore_errors=True)

    users = Users(USERS_FILE)
    if not ADMIN_IDS:
        log.warning("ADMIN_IDS не задан: отправь боту /id и впиши свой ID в .env")

    bot = Bot(BOT_TOKEN)
    dp = Dispatcher()
    dp.include_router(router)
    me = await bot.get_me()
    log.info("PrintBot @%s запущен, принтер: %s", me.username, PRINTER or "по умолчанию")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
