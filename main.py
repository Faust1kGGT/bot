# ============================================================
# Telegram-бот для получения информации об игроках Roblox
# с системой лимитов и платной подписки
# ============================================================
# Установка зависимостей:
#   pip install aiogram aiohttp
#
# Запуск:
#   python bot.py
#
# Перед запуском:
#   1. Вставьте токен бота в переменную BOT_TOKEN
#   2. Вставьте токен платёжной системы в PAYMENT_TOKEN
#   3. Настройте цены подписок по желанию
#
# Получить токен бота: @BotFather
# Получить платёжный токен: @BotFather → /mybots → Payments
# ============================================================

import asyncio
import psycopg2
import json
import logging
import os
import time
from datetime import datetime, timedelta
from typing import Optional

import aiohttp
from aiogram import Bot, Dispatcher, Router, F
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    BufferedInputFile,
    LabeledPrice,
    PreCheckoutQuery,
    ContentType,
)
from aiogram.filters import CommandStart, Command
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties

# ===================== НАСТРОЙКИ =====================

# Токен бота (получить у @BotFather)
BOT_TOKEN = "7947605764:AAGWTfndHVIyN3SV7_zpe3Zr9CoTTI7F8SI"

# Токен платёжной системы (получить у @BotFather → /mybots → Payments)
# Для тестов используйте тестовый токен провайдера (например Stripe TEST)
# Примеры провайдеров: Stripe, ЮKassa, Sberbank, LiqPay, и др.
PAYMENT_TOKEN = " "

# Количество бесплатных запросов для новых пользователей
FREE_REQUESTS = 2

# Цены подписок (в копейках/центах — зависит от валюты)
# 1 звезда Telegram = 1 (для Telegram Stars используйте "XTR" как валюту)
SUBSCRIPTION_PLANS = {
    "week": {
        "name": "📅 Неделя",
        "duration_days": 7,
        "price": 15,  
        "label": "Подписка на 7 дней",
    },
    "month": {
        "name": "📆 Месяц",
        "duration_days": 30,
        "price": 50,  
        "label": "Подписка на 30 дней",
    },
    "year": {
        "name": "📅 Год",
        "duration_days": 365,
        "price": 500,  
        "label": "Подписка на 365 дней",
    },
    "forever": {
        "name": "♾ Навсегда",
        "duration_days": 99999,
        "price": 1488,  
        "label": "Подписка навсегда",
    },
}

# Валюта (RUB, USD, EUR, UAH, XTR для Telegram Stars)
CURRENCY = "XTR"

# Файл базы данных пользователей (JSON)
DATABASE_FILE = "users_db.json"

# Таймаут для HTTP-запросов
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=10)

# ====================================================================

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

# Роутер
router = Router()

# Месяцы на русском
MONTHS_RU = {
    1: "января", 2: "февраля", 3: "марта", 4: "апреля",
    5: "мая", 6: "июня", 7: "июля", 8: "августа",
    9: "сентября", 10: "октября", 11: "ноября", 12: "декабря",
}


# ======================== БАЗА ДАННЫХ (JSON) ========================

class UserDatabase:
    """
    Простая JSON-база данных для хранения информации о пользователях.

    Структура записи:
    {
        "user_id": {
            "username": "имя",
            "free_requests": 2,
            "subscription_until": null или timestamp,
            "total_requests": 0,
            "registered_at": timestamp
        }
    }
    """

    def __init__(self, filepath: str):
        self.filepath = filepath
        self.data: dict = {}
        self._load()

    def _load(self):
        """Загружает данные из файла."""
        if os.path.exists(self.filepath):
            try:
                with open(self.filepath, "r", encoding="utf-8") as f:
                    self.data = json.load(f)
                logger.info(f"📂 База данных загружена: {len(self.data)} пользователей")
            except Exception as e:
                logger.error(f"Ошибка загрузки БД: {e}")
                self.data = {}
        else:
            self.data = {}
            self._save()

    def _save(self):
        """Сохраняет данные в файл."""
        try:
            with open(self.filepath, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"Ошибка сохранения БД: {e}")

    def get_user(self, user_id: int) -> dict:
        """Получает или создаёт пользователя."""
        uid = str(user_id)
        if uid not in self.data:
            self.data[uid] = {
                "username": "",
                "free_requests": FREE_REQUESTS,
                "subscription_until": None,
                "total_requests": 0,
                "registered_at": time.time(),
            }
            self._save()
            logger.info(f"👤 Новый пользователь: {user_id}")
        return self.data[uid]

    def update_user(self, user_id: int, **kwargs):
        """Обновляет поля пользователя."""
        uid = str(user_id)
        if uid in self.data:
            self.data[uid].update(kwargs)
            self._save()

    def has_access(self, user_id: int) -> bool:
        """Проверяет, есть ли у пользователя доступ (бесплатные запросы или подписка)."""
        user = self.get_user(user_id)

        # Проверяем подписку
        sub_until = user.get("subscription_until")
        if sub_until and time.time() < sub_until:
            return True

        # Проверяем бесплатные запросы
        if user.get("free_requests", 0) > 0:
            return True

        return False

    def use_request(self, user_id: int):
        """Тратит один запрос (бесплатный, если нет подписки)."""
        user = self.get_user(user_id)
        uid = str(user_id)

        # Увеличиваем общий счётчик
        self.data[uid]["total_requests"] = user.get("total_requests", 0) + 1

        # Если есть активная подписка — не тратим бесплатные
        sub_until = user.get("subscription_until")
        if sub_until and time.time() < sub_until:
            self._save()
            return

        # Тратим бесплатный запрос
        free = user.get("free_requests", 0)
        if free > 0:
            self.data[uid]["free_requests"] = free - 1
            self._save()

    def get_remaining_free(self, user_id: int) -> int:
        """Возвращает количество оставшихся бесплатных запросов."""
        user = self.get_user(user_id)
        return user.get("free_requests", 0)

    def is_subscribed(self, user_id: int) -> bool:
        """Проверяет, есть ли активная подписка."""
        user = self.get_user(user_id)
        sub_until = user.get("subscription_until")
        if sub_until and time.time() < sub_until:
            return True
        return False

    def get_subscription_end(self, user_id: int) -> Optional[str]:
        """Возвращает дату окончания подписки в красивом формате."""
        user = self.get_user(user_id)
        sub_until = user.get("subscription_until")
        if sub_until and time.time() < sub_until:
            dt = datetime.fromtimestamp(sub_until)
            return f"{dt.day} {MONTHS_RU[dt.month]} {dt.year} г."
        return None

    def add_subscription(self, user_id: int, days: int):
        """Добавляет подписку на указанное количество дней."""
        user = self.get_user(user_id)
        uid = str(user_id)

        # Если подписка уже есть — продлеваем от текущей даты окончания
        sub_until = user.get("subscription_until")
        if sub_until and time.time() < sub_until:
            base_time = sub_until
        else:
            base_time = time.time()

        new_until = base_time + (days * 86400)  # 86400 секунд в дне
        self.data[uid]["subscription_until"] = new_until
        self._save()
        logger.info(f"💳 Подписка для {user_id}: +{days} дней (до {datetime.fromtimestamp(new_until)})")

    def get_stats(self, user_id: int) -> dict:
        """Возвращает статистику пользователя."""
        user = self.get_user(user_id)
        return {
            "free_requests": user.get("free_requests", 0),
            "total_requests": user.get("total_requests", 0),
            "is_subscribed": self.is_subscribed(user_id),
            "subscription_end": self.get_subscription_end(user_id),
            "registered_at": user.get("registered_at", 0),
        }


# Инициализация базы данных
db = UserDatabase(DATABASE_FILE)


# ======================== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ========================

def format_date(iso_date: str) -> str:
    """Форматирует ISO-дату в русский формат."""
    try:
        dt = datetime.fromisoformat(iso_date.replace("Z", "+00:00"))
        return f"{dt.day} {MONTHS_RU[dt.month]} {dt.year} г."
    except Exception:
        return iso_date


def truncate_text(text: str, max_length: int = 200) -> str:
    """Обрезает текст."""
    if not text:
        return "Не указано"
    if len(text) <= max_length:
        return text
    return text[:max_length].rstrip() + "..."


def format_visits(visits: int) -> str:
    """Форматирует число визитов."""
    if visits >= 1_000_000_000:
        return f"{visits / 1_000_000_000:.1f}B"
    if visits >= 1_000_000:
        return f"{visits / 1_000_000:.1f}M"
    if visits >= 1_000:
        return f"{visits / 1_000:.1f}K"
    return str(visits)


def format_price(price_kopecks: int) -> str:
    """Форматирует цену из копеек в рубли."""
    if CURRENCY == "RUB":
        return f"{price_kopecks / 100:.0f} ₽"
    elif CURRENCY == "USD":
        return f"${price_kopecks / 100:.2f}"
    elif CURRENCY == "EUR":
        return f"€{price_kopecks / 100:.2f}"
    elif CURRENCY == "UAH":
        return f"{price_kopecks / 100:.0f} ₴"
    elif CURRENCY == "XTR":
        return f"{price_kopecks} ⭐"
    return f"{price_kopecks / 100} {CURRENCY}"


# ======================== ROBLOX API ========================

async def get_user_id_by_username(session: aiohttp.ClientSession, username: str) -> Optional[dict]:
    """Получает ID пользователя по username."""
    url = "https://users.roblox.com/v1/usernames/users"
    payload = {"usernames": [username], "excludeBannedUsers": False}
    try:
        async with session.post(url, json=payload, timeout=REQUEST_TIMEOUT) as resp:
            if resp.status == 200:
                data = await resp.json()
                if data.get("data") and len(data["data"]) > 0:
                    return data["data"][0]
            return None
    except Exception as e:
        logger.error(f"Ошибка запроса username '{username}': {e}")
        return None


async def get_user_by_id(session: aiohttp.ClientSession, user_id: int) -> Optional[dict]:
    """Получает информацию по ID."""
    url = f"https://users.roblox.com/v1/users/{user_id}"
    try:
        async with session.get(url, timeout=REQUEST_TIMEOUT) as resp:
            if resp.status == 200:
                data = await resp.json()
                if data.get("errors"):
                    return None
                return data
            return None
    except Exception as e:
        logger.error(f"Ошибка запроса user ID {user_id}: {e}")
        return None


async def get_avatar_url(session: aiohttp.ClientSession, user_id: int) -> Optional[str]:
    """Получает URL аватарки."""
    url = (
        f"https://thumbnails.roblox.com/v1/users/avatar-headshot"
        f"?userIds={user_id}&size=420x420&format=Png&isCircular=false"
    )
    try:
        async with session.get(url, timeout=REQUEST_TIMEOUT) as resp:
            if resp.status == 200:
                data = await resp.json()
                if data.get("data") and len(data["data"]) > 0:
                    return data["data"][0].get("imageUrl")
            return None
    except Exception as e:
        logger.error(f"Ошибка запроса аватарки {user_id}: {e}")
        return None


async def get_friends_count(session: aiohttp.ClientSession, user_id: int) -> Optional[int]:
    """Получает количество друзей."""
    url = f"https://friends.roblox.com/v1/users/{user_id}/friends/count"
    try:
        async with session.get(url, timeout=REQUEST_TIMEOUT) as resp:
            if resp.status == 200:
                data = await resp.json()
                return data.get("count", 0)
            return None
    except Exception as e:
        logger.error(f"Ошибка запроса друзей {user_id}: {e}")
        return None


async def get_presence(session: aiohttp.ClientSession, user_id: int) -> Optional[dict]:
    """Получает онлайн-статус."""
    url = "https://presence.roblox.com/v1/presence/users"
    payload = {"userIds": [user_id]}
    try:
        async with session.post(url, json=payload, timeout=REQUEST_TIMEOUT) as resp:
            if resp.status == 200:
                data = await resp.json()
                if data.get("userPresences") and len(data["userPresences"]) > 0:
                    return data["userPresences"][0]
            return None
    except Exception as e:
        logger.error(f"Ошибка запроса присутствия {user_id}: {e}")
        return None


async def get_user_games(session: aiohttp.ClientSession, user_id: int) -> Optional[list]:
    """Получает список игр."""
    url = f"https://games.roblox.com/v2/users/{user_id}/games?sortOrder=Desc&limit=10"
    try:
        async with session.get(url, timeout=REQUEST_TIMEOUT) as resp:
            if resp.status == 200:
                data = await resp.json()
                return data.get("data", [])
            return None
    except Exception as e:
        logger.error(f"Ошибка запроса игр {user_id}: {e}")
        return None


async def get_roblox_badges(session: aiohttp.ClientSession, user_id: int) -> Optional[list]:
    """Получает значки Roblox."""
    url = f"https://accountinformation.roblox.com/v1/users/{user_id}/roblox-badges"
    try:
        async with session.get(url, timeout=REQUEST_TIMEOUT) as resp:
            if resp.status == 200:
                return await resp.json()
            return None
    except Exception as e:
        logger.error(f"Ошибка запроса значков {user_id}: {e}")
        return None


async def download_image(session: aiohttp.ClientSession, url: str) -> Optional[bytes]:
    """Скачивает изображение."""
    try:
        async with session.get(url, timeout=REQUEST_TIMEOUT) as resp:
            if resp.status == 200:
                return await resp.read()
            return None
    except Exception as e:
        logger.error(f"Ошибка скачивания изображения: {e}")
        return None


# ======================== СБОРКА КАРТОЧКИ ========================

async def build_player_card(user_id: int) -> tuple[Optional[str], Optional[bytes], Optional[str]]:
    """Собирает карточку игрока. Возвращает (text, avatar_bytes, error)."""
    async with aiohttp.ClientSession() as session:
        user_info = await get_user_by_id(session, user_id)
        if not user_info:
            return None, None, (
                "❌ Игрок с таким username/ID не найден.\n"
                "Проверь правильность и попробуй снова! 🔄"
            )

        # Параллельные запросы
        avatar_url, friends_count, presence, badges = await asyncio.gather(
            get_avatar_url(session, user_id),
            get_friends_count(session, user_id),
            get_presence(session, user_id),
            get_roblox_badges(session, user_id),
        )

        # Данные
        display_name = user_info.get("displayName", "N/A")
        name = user_info.get("name", "N/A")
        uid = user_info.get("id", user_id)
        description = truncate_text(user_info.get("description", ""), 200)
        created = format_date(user_info.get("created", ""))
        is_banned = user_info.get("isBanned", False)
        has_verified_badge = user_info.get("hasVerifiedBadge", False)

        # Статус
        status_text = "🔴 Офлайн"
        if presence:
            pt = presence.get("userPresenceType", 0)
            loc = presence.get("lastLocation", "")
            if pt == 1:
                status_text = "🟢 Онлайн (на сайте)"
            elif pt == 2:
                status_text = f'🟢 В игре 🎮 "<i>{loc or "Неизвестная игра"}</i>"'
            elif pt == 3:
                status_text = "🟡 В Roblox Studio"

        friends_text = str(friends_count) if friends_count is not None else "—"
        ban_text = "Да ❌" if is_banned else "Нет ✅"
        verified_text = "Да ✅" if has_verified_badge else "Нет"

        badges_text = ""
        if badges and isinstance(badges, list) and len(badges) > 0:
            badge_names = [b.get("name", "?") for b in badges[:10]]
            badges_text = f"\n🏆 <b>Значки Roblox:</b> {', '.join(badge_names)}"

        text = (
            f"🎮 <b>ИНФОРМАЦИЯ ОБ ИГРОКЕ</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"👤 <b>Никнейм:</b> {display_name} (<code>@{name}</code>)\n"
            f"🆔 <b>ID:</b> <code>{uid}</code>\n"
            f"📝 <b>Описание:</b> {description}\n"
            f"📅 <b>Дата регистрации:</b> {created}\n"
            f"{'─' * 23}\n"
            f"🌐 <b>Статус:</b> {status_text}\n"
            f"👥 <b>Друзей:</b> {friends_text}\n"
            f"🚫 <b>Бан:</b> {ban_text}\n"
            f"✅ <b>Верификация:</b> {verified_text}\n"
            f"{badges_text}\n\n"
            f"━━━━━━━━━━━━━━━━━━━━━"
        )

        avatar_bytes = None
        if avatar_url:
            avatar_bytes = await download_image(session, avatar_url)

        return text, avatar_bytes, None


# ======================== КЛАВИАТУРЫ ========================

def get_start_keyboard() -> InlineKeyboardMarkup:
    """Главная клавиатура."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔍 Найти игрока", callback_data="search_player")],
        [
            InlineKeyboardButton(text="📖 Помощь", callback_data="help"),
            InlineKeyboardButton(text="👤 Мой профиль", callback_data="my_profile"),
        ],
        [InlineKeyboardButton(text="⭐ Подписка", callback_data="subscription")],
        [InlineKeyboardButton(text="👨‍💻 Автор", callback_data="about")],
    ])


def get_player_keyboard(user_id: int) -> InlineKeyboardMarkup:
    """Клавиатура под карточкой."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="🌐 Открыть профиль",
            url=f"https://www.roblox.com/users/{user_id}/profile",
        )],
        [
            InlineKeyboardButton(text="🔄 Обновить", callback_data=f"refresh_{user_id}"),
            InlineKeyboardButton(text="🎮 Игры игрока", callback_data=f"games_{user_id}"),
        ],
        [InlineKeyboardButton(text="🔍 Искать другого", callback_data="search_player")],
    ])


def get_back_keyboard() -> InlineKeyboardMarkup:
    """Кнопка назад."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔍 Искать другого", callback_data="search_player")],
        [InlineKeyboardButton(text="🏠 В начало", callback_data="start")],
    ])


def get_subscription_keyboard() -> InlineKeyboardMarkup:
    """Клавиатура выбора подписки."""
    buttons = []
    for plan_id, plan in SUBSCRIPTION_PLANS.items():
        price_text = format_price(plan["price"])
        buttons.append([
            InlineKeyboardButton(
                text=f"{plan['name']} — {price_text}",
                callback_data=f"buy_{plan_id}",
            )
        ])
    buttons.append([InlineKeyboardButton(text="🏠 В начало", callback_data="start")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_no_access_keyboard() -> InlineKeyboardMarkup:
    """Клавиатура при отсутствии доступа."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⭐ Купить подписку", callback_data="subscription")],
        [InlineKeyboardButton(text="🏠 В начало", callback_data="start")],
    ])


# ======================== ОБРАБОТЧИКИ КОМАНД ========================

@router.message(CommandStart())
async def cmd_start(message: Message):
    """Обработчик /start."""
    # Регистрируем пользователя в базе
    user = db.get_user(message.from_user.id)
    db.update_user(
        message.from_user.id,
        username=message.from_user.username or "",
    )

    stats = db.get_stats(message.from_user.id)
    free = stats["free_requests"]
    is_sub = stats["is_subscribed"]

    if is_sub:
        status_line = f"⭐ <b>Статус:</b> Премиум (до {stats['subscription_end']})"
    elif free > 0:
        status_line = f"🆓 <b>Бесплатных запросов:</b> {free} из {FREE_REQUESTS}"
    else:
        status_line = "🔒 <b>Бесплатные запросы закончились.</b> Оформи подписку!"

    text = (
        f"🎮 <b>Добро пожаловать в Roblox OSINT account!</b> 🚀\n\n"
        f"👾 Этот бот поможет тебе узнать всю доступную информацию\n"
        f"об игроках платформы <b>Roblox</b>.\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"{status_line}\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"🔍 <b>Что умеет бот:</b>\n"
        f"  ⭐ Показывает профиль игрока с аватаркой\n"
        f"  ⭐ Отображает онлайн-статус\n"
        f"  ⭐ Показывает количество друзей\n"
        f"  ⭐ Выводит список игр игрока\n"
        f"  ⭐ Показывает значки Roblox\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"📌 <b>Как пользоваться:</b>\n"
        f"Просто отправь мне <b>username</b> или <b>ID</b> игрока!\n\n"
        f"  💡 Например: <code>Roblox</code> или <code>1</code>"
    )
    await message.answer(text, reply_markup=get_start_keyboard())


@router.message(Command("help"))
async def cmd_help(message: Message):
    """Обработчик /help."""
    await send_help_text(message)


@router.message(Command("profile"))
async def cmd_profile(message: Message):
    """Обработчик /profile — показывает профиль пользователя."""
    await show_profile(message)


@router.message(Command("subscribe"))
async def cmd_subscribe(message: Message):
    """Обработчик /subscribe — показывает варианты подписки."""
    await show_subscription(message)


# ======================== ТЕКСТОВЫЕ УТИЛИТЫ ========================

async def send_help_text(target, edit: bool = False):
    """Отправляет помощь."""
    text = (
        f"📖 <b>ПОМОЩЬ</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"🔍 <b>Поиск по username:</b>\n"
        f"Отправь никнейм игрока в чат.\n"
        f"  📝 Пример: <code>Roblox</code>\n"
        f"  📝 Пример: <code>builderman</code>\n\n"
        f"─────────────────────\n\n"
        f"🔍 <b>Поиск по ID:</b>\n"
        f"Отправь числовой ID.\n"
        f"  📝 Пример: <code>1</code>\n"
        f"  📝 Пример: <code>156</code>\n\n"
        f"─────────────────────\n\n"
        f"💰 <b>Система запросов:</b>\n"
        f"  🆓 Новым пользователям — <b>{FREE_REQUESTS}</b> бесплатных запроса\n"
        f"  ⭐ Далее нужна подписка\n"
        f"  📌 Команда /subscribe — купить подписку\n"
        f"  📌 Команда /profile — ваш профиль и статистика\n\n"
        f"─────────────────────\n\n"
        f"🎯 <b>Что показывает бот:</b>\n"
        f"  👤 Никнейм и Display Name\n"
        f"  🆔 Уникальный ID\n"
        f"  📝 Описание профиля\n"
        f"  📅 Дату регистрации\n"
        f"  🟢 Онлайн-статус\n"
        f"  👥 Количество друзей\n"
        f"  🚫 Статус бана\n"
        f"  ✅ Верификацию\n"
        f"  🏆 Значки Roblox\n"
        f"  🎮 Список игр игрока\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━"
    )
    kb = get_back_keyboard()
    if edit and isinstance(target, CallbackQuery):
        await target.message.edit_text(text, reply_markup=kb)
    else:
        await target.answer(text, reply_markup=kb)


async def show_profile(target, edit: bool = False):
    """Показывает профиль пользователя."""
    if isinstance(target, CallbackQuery):
        user_id = target.from_user.id
    else:
        user_id = target.from_user.id

    stats = db.get_stats(user_id)

    # Дата регистрации в боте
    reg_ts = stats.get("registered_at", 0)
    if reg_ts:
        reg_dt = datetime.fromtimestamp(reg_ts)
        reg_text = f"{reg_dt.day} {MONTHS_RU[reg_dt.month]} {reg_dt.year} г."
    else:
        reg_text = "Неизвестно"

    if stats["is_subscribed"]:
        sub_text = f"⭐ <b>Премиум</b> (до {stats['subscription_end']})"
        access_text = "♾ Безлимитные запросы"
    elif stats["free_requests"] > 0:
        sub_text = "🆓 Бесплатный тариф"
        access_text = f"📊 Осталось запросов: <b>{stats['free_requests']}</b> из {FREE_REQUESTS}"
    else:
        sub_text = "🔒 Запросы закончились"
        access_text = "❌ Бесплатные запросы израсходованы"

    text = (
        f"👤 <b>ВАШ ПРОФИЛЬ</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"🆔 <b>Telegram ID:</b> <code>{user_id}</code>\n"
        f"📅 <b>Зарегистрирован:</b> {reg_text}\n"
        f"📊 <b>Всего запросов:</b> {stats['total_requests']}\n\n"
        f"─────────────────────\n\n"
        f"📌 <b>Тариф:</b> {sub_text}\n"
        f"{access_text}\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━"
    )

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⭐ Купить подписку", callback_data="subscription")],
        [InlineKeyboardButton(text="🏠 В начало", callback_data="start")],
    ])

    if edit and isinstance(target, CallbackQuery):
        await target.message.edit_text(text, reply_markup=kb)
    else:
        await target.answer(text, reply_markup=kb)


async def show_subscription(target, edit: bool = False):
    """Показывает варианты подписки."""
    plans_text = ""
    for plan_id, plan in SUBSCRIPTION_PLANS.items():
        price = format_price(plan["price"])
        plans_text += f"  {plan['name']} — <b>{price}</b> ({plan['duration_days']} дн.)\n"

    if isinstance(target, CallbackQuery):
        user_id = target.from_user.id
    else:
        user_id = target.from_user.id

    stats = db.get_stats(user_id)
    if stats["is_subscribed"]:
        current = f"⭐ Текущая подписка активна до: <b>{stats['subscription_end']}</b>"
    else:
        remaining = stats["free_requests"]
        current = f"🆓 Бесплатных запросов осталось: <b>{remaining}</b>"

    text = (
        f"⭐ <b>ПОДПИСКА</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"{current}\n\n"
        f"─────────────────────\n\n"
        f"🛒 <b>Доступные тарифы:</b>\n\n"
        f"{plans_text}\n"
        f"─────────────────────\n\n"
        f"✅ <b>Что даёт подписка:</b>\n"
        f"  🔓 Безлимитные запросы\n"
        f"  ⚡ Точные и улучшенные данные\n"
        f"  🎮 Полная информация об игроках\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"👇 Выбери тариф и оплати прямо в Telegram:"
    )

    kb = get_subscription_keyboard()

    if edit and isinstance(target, CallbackQuery):
        await target.message.edit_text(text, reply_markup=kb)
    else:
        await target.answer(text, reply_markup=kb)


# ======================== CALLBACK ОБРАБОТЧИКИ ========================

@router.callback_query(F.data == "start")
async def callback_start(callback: CallbackQuery):
    """Возврат на главную."""
    stats = db.get_stats(callback.from_user.id)
    free = stats["free_requests"]
    is_sub = stats["is_subscribed"]

    if is_sub:
        status_line = f"⭐ <b>Статус:</b> Премиум (до {stats['subscription_end']})"
    elif free > 0:
        status_line = f"🆓 <b>Бесплатных запросов:</b> {free} из {FREE_REQUESTS}"
    else:
        status_line = "🔒 <b>Бесплатные запросы закончились.</b> Оформи подписку!"

    text = (
        f"🎮 <b>Roblox OSINT account</b> 🚀\n\n"
        f"{status_line}\n\n"
        f"📌 Отправь мне <b>username</b> или <b>ID</b> игрока!\n"
        f"💡 Например: <code>Roblox</code> или <code>1</code>"
    )
    await callback.message.edit_text(text, reply_markup=get_start_keyboard())
    await callback.answer()


@router.callback_query(F.data == "search_player")
async def callback_search_player(callback: CallbackQuery):
    """Кнопка 'Найти игрока'."""
    # Проверяем доступ
    if not db.has_access(callback.from_user.id):
        text = (
            f"🔒 <b>Доступ ограничен</b>\n\n"
            f"Твои бесплатные запросы закончились!\n"
            f"Оформи подписку, чтобы продолжить поиск. ⭐"
        )
        await callback.message.edit_text(text, reply_markup=get_no_access_keyboard())
        await callback.answer()
        return

    stats = db.get_stats(callback.from_user.id)
    if stats["is_subscribed"]:
        remaining_text = "⭐ У тебя активная подписка — безлимитные запросы!"
    else:
        remaining_text = f"🆓 Осталось бесплатных запросов: <b>{stats['free_requests']}</b>"

    text = (
        f"🔍 <b>Поиск игрока</b>\n\n"
        f"Отправь мне <b>username</b> или <b>ID</b> игрока Roblox:\n\n"
        f"💡 Примеры: <code>Roblox</code>, <code>builderman</code>, <code>1</code>\n\n"
        f"{remaining_text}"
    )
    await callback.message.delete()
    await callback.message.answer(text, reply_markup=get_back_keyboard())    
    await callback.answer()


@router.callback_query(F.data == "help")
async def callback_help(callback: CallbackQuery):
    """Кнопка 'Помощь'."""
    await send_help_text(callback, edit=True)
    await callback.answer()


@router.callback_query(F.data == "about")
async def callback_about(callback: CallbackQuery):
    """Кнопка 'Автор'."""
    text = (
        f"👨‍💻 <b>О боте</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"🤖 <b>Roblox OSINT account</b>\n\n"
        f"🛠 Прочее:\n"
        f"  • Поддержка: @V1xlove\n"
        f"  • За отлиз любой каприз\n"
        f"  • Используются API с открытых источников\n"
        f"  • Валюта: Telegram Stars\n\n"
        f"📡 Бот работает 24/7\n\n"
        f"💰 Оплата через встроенную систему\n"
        f"платежей Telegram (Telegram Payments).\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🏠 В начало", callback_data="start")],
    ])
    await callback.message.edit_text(text, reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data == "my_profile")
async def callback_my_profile(callback: CallbackQuery):
    """Кнопка 'Мой профиль'."""
    await show_profile(callback, edit=True)
    await callback.answer()


@router.callback_query(F.data == "subscription")
async def callback_subscription(callback: CallbackQuery):
    """Кнопка 'Подписка'."""
    await show_subscription(callback, edit=True)
    await callback.answer()


# ======================== ОПЛАТА ========================

@router.callback_query(F.data.startswith("buy_"))
async def callback_buy(callback: CallbackQuery, bot: Bot):
    """Обработка покупки подписки — отправляет инвойс."""
    plan_id = callback.data.split("_", 1)[1]
    plan = SUBSCRIPTION_PLANS.get(plan_id)

    if not plan:
        await callback.answer("⚠️ Тариф не найден!", show_alert=True)
        return

    price = plan["price"]
    label = plan["label"]

    # Проверяем, настроен ли платёжный токен
    if PAYMENT_TOKEN == "ВАШ_ПЛАТЁЖНЫЙ_ТОКЕН" or not PAYMENT_TOKEN:
        await callback.answer(
            "⚠️ Платёжная система не настроена! "
            "Администратору нужно указать PAYMENT_TOKEN.",
            show_alert=True,
        )
        return

    # Формируем payload (передаём plan_id для идентификации после оплаты)
    payload = f"sub_{plan_id}_{callback.from_user.id}"

    prices = [LabeledPrice(label=label, amount=price)]

    try:
        await bot.send_invoice(
            chat_id=callback.message.chat.id,
            title=f"⭐ Подписка: {plan['name']}",
            description=(
                f"Подписка на Roblox OSINT account\n"
                f"Срок: {plan['duration_days']} дней\n"
                f"Безлимитные запросы об игроках Roblox"
            ),
            payload=payload,
            provider_token=PAYMENT_TOKEN,
            currency=CURRENCY,
            prices=prices,
            start_parameter=f"sub_{plan_id}",
            photo_url=" ",
            photo_width=1200,
            photo_height=1200,
            need_name=False,
            need_phone_number=False,
            need_email=False,
            is_flexible=False,
        )
        await callback.answer()
    except Exception as e:
        logger.error(f"Ошибка при создании инвойса: {e}")
        await callback.answer(
            "⚠️ Ошибка при создании платежа. Попробуй позже!",
            show_alert=True,
        )


@router.pre_checkout_query()
async def process_pre_checkout(pre_checkout_query: PreCheckoutQuery, bot: Bot):
    """
    Обработчик предварительной проверки платежа.
    Telegram отправляет этот запрос перед списанием средств.
    Нужно ответить в течение 10 секунд.
    """
    # Здесь можно добавить дополнительные проверки
    # (например, проверить, что товар ещё доступен)
    await bot.answer_pre_checkout_query(pre_checkout_query.id, ok=True)
    logger.info(f"💳 Pre-checkout от пользователя {pre_checkout_query.from_user.id}: {pre_checkout_query.invoice_payload}")


@router.message(F.content_type == ContentType.SUCCESSFUL_PAYMENT)
async def process_successful_payment(message: Message):
    """
    Обработчик успешного платежа.
    Вызывается после успешного списания средств.
    """
    payment = message.successful_payment
    payload = payment.invoice_payload
    user_id = message.from_user.id

    logger.info(
        f"✅ Успешный платёж от {user_id}: "
        f"payload={payload}, "
        f"amount={payment.total_amount} {payment.currency}"
    )

    # Парсим payload: "sub_{plan_id}_{user_id}"
    try:
        parts = payload.split("_")
        plan_id = parts[1]
    except (IndexError, ValueError):
        logger.error(f"Ошибка парсинга payload: {payload}")
        await message.answer("⚠️ Ошибка обработки платежа. Обратитесь в поддержку.")
        return

    plan = SUBSCRIPTION_PLANS.get(plan_id)
    if not plan:
        logger.error(f"Тариф не найден: {plan_id}")
        await message.answer("⚠️ Ошибка обработки платежа. Обратитесь в поддержку.")
        return

    # Активируем подписку
    days = plan["duration_days"]
    db.add_subscription(user_id, days)

    end_date = db.get_subscription_end(user_id)

    text = (
        f"🎉 <b>Оплата прошла успешно!</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"⭐ <b>Подписка активирована!</b>\n\n"
        f"📌 <b>Тариф:</b> {plan['name']}\n"
        f"📅 <b>Срок:</b> {days} дней\n"
        f"🗓 <b>Действует до:</b> {end_date}\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"🔓 Теперь у тебя безлимитные запросы!\n"
        f"Отправь username или ID игрока, чтобы начать 🎮"
    )
    await message.answer(text, reply_markup=get_start_keyboard())


# ======================== ОБНОВЛЕНИЕ / ИГРЫ ========================

@router.callback_query(F.data.startswith("refresh_"))
async def callback_refresh(callback: CallbackQuery):
    """Обновление карточки игрока."""
    try:
        roblox_user_id = int(callback.data.split("_", 1)[1])
    except (ValueError, IndexError):
        await callback.answer("⚠️ Ошибка!", show_alert=True)
        return

    await callback.answer("⏳ Обновляю...")

    text, avatar_bytes, error = await build_player_card(roblox_user_id)

    if error:
        try:
            await callback.message.edit_caption(caption=error, reply_markup=get_back_keyboard())
        except Exception:
            await callback.message.edit_text(error, reply_markup=get_back_keyboard())
        return

    keyboard = get_player_keyboard(roblox_user_id)

    try:
        if avatar_bytes:
            from aiogram.types import InputMediaPhoto
            photo = BufferedInputFile(avatar_bytes, filename="avatar.png")
            await callback.message.edit_media(
                media=InputMediaPhoto(media=photo, caption=text, parse_mode=ParseMode.HTML),
                reply_markup=keyboard,
            )
        else:
            await callback.message.edit_caption(caption=text, reply_markup=keyboard)
    except Exception as e:
        logger.error(f"Ошибка обновления: {e}")
        try:
            if avatar_bytes:
                photo = BufferedInputFile(avatar_bytes, filename="avatar.png")
                await callback.message.answer_photo(photo=photo, caption=text, reply_markup=keyboard)
            else:
                await callback.message.answer(text, reply_markup=keyboard)
        except Exception as e2:
            logger.error(f"Повторная ошибка: {e2}")


@router.callback_query(F.data.startswith("games_"))
async def callback_games(callback: CallbackQuery):
    """Показывает игры игрока."""
    try:
        roblox_user_id = int(callback.data.split("_", 1)[1])
    except (ValueError, IndexError):
        await callback.answer("⚠️ Ошибка!", show_alert=True)
        return

    await callback.answer("⏳ Загружаю игры...")

    async with aiohttp.ClientSession() as session:
        games = await get_user_games(session, roblox_user_id)
        user_info = await get_user_by_id(session, roblox_user_id)

    username = user_info.get("name", "Неизвестно") if user_info else "Неизвестно"

    if games is None:
        text = "⚠️ Ошибка при загрузке игр. Попробуй позже! 🔧"
    elif len(games) == 0:
        text = (
            f"🎮 <b>Игры игрока @{username}</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"😔 У этого игрока нет опубликованных игр."
        )
    else:
        lines = []
        for i, game in enumerate(games[:10], 1):
            name = game.get("name", "Без названия")
            visits = game.get("placeVisits", 0)
            lines.append(f"  {i}. 🎯 <b>{name}</b>\n      👥 {format_visits(visits)} визитов")

        text = (
            f"🎮 <b>Игры игрока @{username}</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"{chr(10).join(lines)}\n\n"  # Используем chr(10) вместо \n в f-string
            f"━━━━━━━━━━━━━━━━━━━━━"
        )

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 К профилю", callback_data=f"refresh_{roblox_user_id}")],
        [InlineKeyboardButton(text="🔍 Искать другого", callback_data="search_player")],
    ])

    try:
        await callback.message.edit_caption(caption=text, reply_markup=kb)
    except Exception:
        try:
            await callback.message.edit_text(text, reply_markup=kb)
        except Exception:
            await callback.message.answer(text, reply_markup=kb)


# ======================== ПОИСК ИГРОКА ========================

@router.message(F.text & ~F.text.startswith("/"))
async def handle_search(message: Message):
    """Обработчик поиска — username или ID."""
    query = message.text.strip()
    if not query:
        return

    user_id = message.from_user.id

    # Регистрируем пользователя если новый
    db.get_user(user_id)

    # === ПРОВЕРКА ДОСТУПА ===
    if not db.has_access(user_id):
        text = (
            f"🔒 <b>Доступ ограничен</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"Твои <b>{FREE_REQUESTS}</b> бесплатных запроса закончились! 😔\n\n"
            f"Чтобы продолжить использовать бот,\n"
            f"оформи подписку ⭐\n\n"
            f"━━━━━━━━━━━━━━━━━━━━━"
        )
        await message.answer(text, reply_markup=get_no_access_keyboard())
        return

    # Сообщение-загрузка
    loading_msg = await message.answer("⏳ <b>Ищу информацию об игроке...</b> 🔍")

    roblox_user_id = None

    # Определяем: ID или username
    if query.isdigit():
        roblox_user_id = int(query)
    else:
        async with aiohttp.ClientSession() as session:
            user_data = await get_user_id_by_username(session, query)
            if user_data:
                roblox_user_id = user_data.get("id")

    if roblox_user_id is None:
        await loading_msg.edit_text(
            "❌ Игрок с таким username/ID не найден.\n"
            "Проверь правильность и попробуй снова! 🔄",
            reply_markup=get_back_keyboard(),
        )
        return

    # Собираем карточку
    try:
        text, avatar_bytes, error = await build_player_card(roblox_user_id)
    except Exception as e:
        logger.error(f"Ошибка сборки карточки: {e}")
        await loading_msg.edit_text(
            "⚠️ Произошла ошибка при получении данных. Попробуй позже! 🔧",
            reply_markup=get_back_keyboard(),
        )
        return

    if error:
        await loading_msg.edit_text(error, reply_markup=get_back_keyboard())
        return

    # === ТРАТИМ ЗАПРОС ===
    db.use_request(user_id)

    # Информация об оставшихся запросах
    stats = db.get_stats(user_id)
    if stats["is_subscribed"]:
        remaining_info = "\n\n⭐ <i>Подписка активна — безлимитные запросы</i>"
    else:
        remaining = stats["free_requests"]
        if remaining > 0:
            remaining_info = f"\n\n🆓 <i>Осталось бесплатных запросов: {remaining}</i>"
        else:
            remaining_info = "\n\n⚠️ <i>Это был твой последний бесплатный запрос!</i>"

    text += remaining_info

    keyboard = get_player_keyboard(roblox_user_id)

    # Удаляем загрузочное сообщение
    try:
        await loading_msg.delete()
    except Exception:
        pass

    # Отправляем карточку
    try:
        if avatar_bytes:
            photo = BufferedInputFile(avatar_bytes, filename="avatar.png")
            await message.answer_photo(photo=photo, caption=text, reply_markup=keyboard)
        else:
            await message.answer(text, reply_markup=keyboard)
    except Exception as e:
        logger.error(f"Ошибка отправки: {e}")
        try:
            await message.answer(text, reply_markup=keyboard)
        except Exception:
            await message.answer(
                "⚠️ Ошибка при отправке данных. Попробуй позже! 🔧",
                reply_markup=get_back_keyboard(),
            )


# ======================== ЗАПУСК ========================

async def main():
    """Главная функция запуска."""
    # Проверки
    if BOT_TOKEN == "ВАШ_ТОКЕН_БОТА" or not BOT_TOKEN:
        print("\n" + "=" * 55)
        print("❌ ОШИБКА: Не указан токен бота!")
        print("Откройте bot.py и вставьте токен в BOT_TOKEN")
        print("Получить: @BotFather → /newbot")
        print("=" * 55 + "\n")
        return

    if PAYMENT_TOKEN == "ВАШ_ПЛАТЁЖНЫЙ_ТОКЕН" or not PAYMENT_TOKEN:
        print("\n" + "=" * 55)
        print("⚠️  ВНИМАНИЕ: Платёжный токен не настроен!")
        print("Оплата не будет работать до настройки PAYMENT_TOKEN")
        print("Получить: @BotFather → /mybots → Payments")
        print("=" * 55 + "\n")

    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher()
    dp.include_router(router)

    logger.info("🚀 Бот запускается...")
    print("\n" + "=" * 55)
    print("🎮  Roblox OSINT account запущен!")
    print(f"💰  Валюта: {CURRENCY}")
    print(f"🆓  Бесплатных запросов: {FREE_REQUESTS}")
    print(f"📂  База данных: {DATABASE_FILE}")
    print("📡  Ожидаю сообщения...")
    print("=" * 55 + "\n")

    try:
        await bot.delete_webhook(drop_pending_updates=True)
        await dp.start_polling(bot)
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
