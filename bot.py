import logging
import asyncio
from datetime import datetime, timedelta
import requests
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)
import os
from dotenv import load_dotenv
import json

# Импортируем нашу базу данных
from database import Database

# Загружаем переменные окружения
load_dotenv()

# Настройка логирования
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Инициализация базы данных
db = Database()

# Словарь для перевода дней недели
DAY_NAMES = {
    'monday': 'Понедельник',
    'tuesday': 'Вторник',
    'wednesday': 'Среда',
    'thursday': 'Четверг',
    'friday': 'Пятница',
    'saturday': 'Суббота',
    'sunday': 'Воскресенье'
}

# Кеш для расписания
schedule_cache = {}
CACHE_DURATION = 3600  # 1 час

def get_schedule_for_teacher(teacher_name, page_limit=10, date_filter=None):
    """
    Получает расписание для преподавателя.
    date_filter - если указана дата, загружаем только одну страницу
    """
    url = "https://iit.bsuir.by/api/v1/content/schedule/"

    # Если фильтр по дате - загружаем только одну страницу (БЫСТРО)
    if date_filter:
        params = {
            "page": 1,
            "teacher": teacher_name
        }
        try:
            response = requests.get(url, params=params, timeout=10)
            response.raise_for_status()
            data = response.json()
            results = data.get("results", [])
            # Фильтруем по дате и ФИО
            filtered = [l for l in results if l.get("date") == date_filter and teacher_name.lower() in l.get("info", "").lower()]
            return filtered
        except requests.exceptions.RequestException as e:
            logger.error(f"Ошибка запроса к API: {e}")
            return []

    # Иначе загружаем все страницы (для недели/месяца)
    all_results = []
    page = 1
    pages_loaded = 0

    try:
        while pages_loaded < page_limit:
            params = {
                "page": page,
                "teacher": teacher_name
            }
            response = requests.get(url, params=params, timeout=10)
            response.raise_for_status()
            data = response.json()

            results = data.get("results", [])
            if not results:
                break

            all_results.extend(results)
            pages_loaded += 1

            if not data.get("next"):
                break

            page += 1

        # Фильтруем занятия - оставляем только те, где есть ФИО преподавателя
        filtered_results = []
        for lesson in all_results:
            info = lesson.get("info", "")
            if teacher_name.lower() in info.lower():
                filtered_results.append(lesson)

        logger.info(f"Найдено {len(filtered_results)} занятий для {teacher_name} (из {len(all_results)} всего)")
        return filtered_results

    except requests.exceptions.RequestException as e:
        logger.error(f"Ошибка запроса к API: {e}")
        return []

def get_cached_schedule(teacher_name, date_filter=None):
    """
    Получает расписание с кешированием для ускорения
    """
    cache_key = f"{teacher_name}_{date_filter if date_filter else 'all'}"

    # Проверяем кеш
    if cache_key in schedule_cache:
        cache_data, timestamp = schedule_cache[cache_key]
        if (datetime.now() - timestamp).seconds < CACHE_DURATION:
            logger.info(f"Используем кеш для {cache_key}")
            return cache_data

    # Загружаем свежие данные
    lessons = get_schedule_for_teacher(teacher_name, date_filter=date_filter)

    # Сохраняем в кеш
    schedule_cache[cache_key] = (lessons, datetime.now())

    return lessons

def format_lessons_for_display(lessons, date_filter=None):
    """Форматирует расписание для вывода пользователю."""
    if date_filter:
        filtered = [l for l in lessons if l.get("date") == date_filter]
    else:
        filtered = lessons

    if not filtered:
        return "📭 Занятий не найдено."

    result = []
    current_date = None

    sorted_lessons = sorted(filtered, key=lambda x: (x.get('date', ''), x.get('start_time', '')))

    for lesson in sorted_lessons:
        date = lesson.get('date', '')
        if date != current_date:
            current_date = date
            date_obj = datetime.strptime(date, "%Y-%m-%d")
            day_key = lesson.get('day_of_week_key', '').lower()
            day_name = DAY_NAMES.get(day_key, day_key.capitalize())
            result.append(f"\n📅 {date_obj.strftime('%d.%m.%Y')} ({day_name})")

        result.append(
            f"  ⏰ {lesson['start_time']} - {lesson['end_time']}\n"
            f"  📚 {lesson['info']}\n"
            f"  🏫 Ауд. {lesson['room']}\n"
        )

    return "\n".join(result)

def get_lessons_by_date(lessons):
    """Группирует занятия по датам"""
    grouped = {}
    for lesson in lessons:
        date = lesson.get('date', '')
        if date:
            if date not in grouped:
                grouped[date] = []
            grouped[date].append(lesson)
    return grouped

def filter_lessons_by_week(lessons, target_date=None):
    """Фильтрует занятия по текущей неделе"""
    if target_date is None:
        target_date = datetime.now()

    start_of_week = target_date - timedelta(days=target_date.weekday())
    start_of_week = start_of_week.replace(hour=0, minute=0, second=0, microsecond=0)
    end_of_week = start_of_week + timedelta(days=6)
    end_of_week = end_of_week.replace(hour=23, minute=59, second=59, microsecond=999999)

    filtered = []
    for lesson in lessons:
        date_str = lesson.get('date', '')
        if date_str:
            lesson_date = datetime.strptime(date_str, "%Y-%m-%d")
            if start_of_week <= lesson_date <= end_of_week:
                filtered.append(lesson)

    return filtered

def filter_lessons_by_month(lessons, target_date=None):
    """Фильтрует занятия по текущему месяцу"""
    if target_date is None:
        target_date = datetime.now()

    filtered = []
    for lesson in lessons:
        date_str = lesson.get('date', '')
        if date_str:
            lesson_date = datetime.strptime(date_str, "%Y-%m-%d")
            if lesson_date.month == target_date.month and lesson_date.year == target_date.year:
                filtered.append(lesson)

    return filtered

async def reset_webhook(application):
    """Принудительно удаляет вебхук перед запуском"""
    try:
        await application.bot.delete_webhook(drop_pending_updates=True)
        logger.info("✅ Вебхук успешно удален")
    except Exception as e:
        logger.error(f"❌ Ошибка удаления вебхука: {e}")

# ============ ГЛАВНОЕ МЕНЮ ============
async def main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id=None):
    """Главное меню с двумя кнопками"""
    if user_id is None:
        user_id = update.effective_user.id

    db_user = db.get_user(user_id)
    teacher = db_user[4] if db_user and db_user[4] else "❌ не установлен"

    keyboard = [
        [InlineKeyboardButton("📚 Расписание", callback_data="schedule_menu")],
        [InlineKeyboardButton("⚙️ Настройки", callback_data="settings_menu")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    text = (
        f"👋 Главное меню\n\n"
        f"👨‍🏫 Преподаватель: *{teacher}*\n\n"
        "Выбери действие:"
    )

    if update.callback_query:
        await update.callback_query.edit_message_text(
            text,
            reply_markup=reply_markup,
            parse_mode="Markdown"
        )
    else:
        await update.message.reply_text(
            text,
            reply_markup=reply_markup,
            parse_mode="Markdown"
        )

# ============ МЕНЮ РАСПИСАНИЯ ============
async def schedule_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Меню с выбором периода расписания"""
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    db_user = db.get_user(user_id)

    if not db_user or not db_user[4]:
        keyboard = [[InlineKeyboardButton("🔙 В меню", callback_data="back_to_main")]]
        await query.edit_message_text(
            "❌ Сначала установи преподавателя!\n\n"
            "Используй команду:\n"
            "/set_teacher Фамилия И.О.",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return

    teacher = db_user[4]

    keyboard = [
        [InlineKeyboardButton("📅 Сегодня", callback_data="today")],
        [InlineKeyboardButton("📅 Завтра", callback_data="tomorrow")],
        [InlineKeyboardButton("📅 Неделя", callback_data="week")],
        [InlineKeyboardButton("📅 Месяц", callback_data="month")],
        [InlineKeyboardButton("📚 Все расписание", callback_data="all_schedule")],
        [InlineKeyboardButton("🔙 В меню", callback_data="back_to_main")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_text(
        f"📚 *Выбери период*\n\n"
        f"👨‍🏫 Преподаватель: *{teacher}*",
        parse_mode="Markdown",
        reply_markup=reply_markup
    )

# ============ МЕНЮ НАСТРОЕК ============
async def settings_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Меню настроек"""
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    db_user = db.get_user(user_id)

    if not db_user:
        keyboard = [[InlineKeyboardButton("🔙 В меню", callback_data="back_to_main")]]
        await query.edit_message_text(
            "❌ Сначала используй /start",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return

    teacher = db_user[4] if db_user[4] else "не установлен"
    notifications = "🔔 Включены" if db_user[5] == 1 else "🔕 Выключены"

    keyboard = [
        [InlineKeyboardButton("🔔 Включить уведомления", callback_data="notifications_on")],
        [InlineKeyboardButton("🔕 Выключить уведомления", callback_data="notifications_off")],
        [InlineKeyboardButton("❌ Удалить преподавателя", callback_data="remove_teacher")],
        [InlineKeyboardButton("🔙 В меню", callback_data="back_to_main")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_text(
        f"⚙️ *Настройки*\n\n"
        f"👨‍🏫 Преподаватель: *{teacher}*\n"
        f"🔔 Уведомления: *{notifications}*\n\n"
        "Выбери действие:",
        parse_mode="Markdown",
        reply_markup=reply_markup
    )

# ============ КОМАНДА /START ============
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /start"""
    user = update.effective_user
    user_id = user.id

    db_user = db.get_user(user_id)

    if not db_user:
        # Новый пользователь
        welcome_text = (
            f"👋 Привет, {user.first_name}!\n\n"
            "Я бот для отслеживания расписания преподавателей ИИТ БГУИР.\n\n"
            "📌 Сначала установи преподавателя командой:\n"
            "/set_teacher Фамилия И.О.\n\n"
            "Пример: /set_teacher Хаджинова Н.В.\n\n"
            "🔔 После установки я буду отслеживать изменения в расписании."
        )
        await update.message.reply_text(welcome_text)
        db.add_user(user_id, user.username, user.first_name, user.last_name, "")
    else:
        # Пользователь уже есть - показываем главное меню
        await main_menu(update, context)

# ============ УСТАНОВКА ПРЕПОДАВАТЕЛЯ ============
async def set_teacher(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Установка преподавателя для отслеживания"""
    user_id = update.effective_user.id

    if not context.args:
        await update.message.reply_text(
            "❌ Пожалуйста, укажи ФИО преподавателя.\n"
            "Пример: /set_teacher Хаджинова Н.В."
        )
        return

    teacher_name = " ".join(context.args)

    status_msg = await update.message.reply_text("⏳ Поиск расписания...")

    schedule = get_schedule_for_teacher(teacher_name, page_limit=3)
    if not schedule:
        await status_msg.edit_text(
            f"❌ Преподаватель '{teacher_name}' не найден.\n"
            "Проверь правильность написания ФИО."
        )
        return

    db.add_user(user_id, update.effective_user.username,
                update.effective_user.first_name,
                update.effective_user.last_name,
                teacher_name)

    db.save_schedule_cache(user_id, teacher_name, schedule)

    grouped = get_lessons_by_date(schedule)

    await status_msg.edit_text(
        f"✅ Преподаватель *{teacher_name}* установлен!\n\n"
        f"📊 Найдено *{len(schedule)}* занятий в расписании.\n"
        f"📅 Всего дней: *{len(grouped)}*\n\n"
        "Теперь я буду отслеживать изменения.",
        parse_mode="Markdown"
    )

    # Показываем главное меню
    await main_menu(update, context)

# ============ ОБРАБОТЧИК КНОПОК ============
async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик нажатий на кнопки"""
    query = update.callback_query
    await query.answer()

    data = query.data

    # Навигация по меню
    if data == "schedule_menu":
        await schedule_menu(update, context)
        return

    if data == "settings_menu":
        await settings_menu(update, context)
        return

    if data == "back_to_main":
        await main_menu(update, context, query.from_user.id)
        return

    # Расписание
    if data == "today":
        await today_callback(query, context)
        return

    if data == "tomorrow":
        await tomorrow_callback(query, context)
        return

    if data == "week":
        await week_callback(query, context)
        return

    if data == "month":
        await month_callback(query, context)
        return

    if data == "all_schedule":
        await all_schedule_callback(query, context)
        return

    # Настройки
    if data == "notifications_on":
        user_id = query.from_user.id
        db.toggle_notifications(user_id, True)
        await query.edit_message_text("🔔 Уведомления включены!")
        await settings_menu(update, context)
        return

    if data == "notifications_off":
        user_id = query.from_user.id
        db.toggle_notifications(user_id, False)
        await query.edit_message_text("🔕 Уведомления выключены!")
        await settings_menu(update, context)
        return

    if data == "remove_teacher":
        user_id = query.from_user.id
        db.add_user(user_id, query.from_user.username,
                    query.from_user.first_name,
                    query.from_user.last_name, "")
        await query.edit_message_text("✅ Преподаватель удален!")
        # Возвращаем в главное меню, но показываем, что преподаватель не установлен
        await main_menu(update, context, query.from_user.id)
        return

# ============ ФУНКЦИИ РАСПИСАНИЯ ============
async def today_callback(query, context):
    """Показать расписание на сегодня"""
    user_id = query.from_user.id
    db_user = db.get_user(user_id)

    if not db_user or not db_user[4]:
        await query.edit_message_text("❌ Сначала установи преподавателя.")
        return

    teacher_name = db_user[4]

    await query.edit_message_text("⏳ Загрузка...")

    today_str = datetime.now().strftime("%Y-%m-%d")
    schedule = get_cached_schedule(teacher_name, date_filter=today_str)

    keyboard = [
        [InlineKeyboardButton("📅 Завтра", callback_data="tomorrow")],
        [InlineKeyboardButton("🔙 В расписание", callback_data="schedule_menu")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    formatted = format_lessons_for_display(schedule, today_str)

    if not schedule or formatted == "📭 Занятий не найдено.":
        await query.edit_message_text(
            f"📭 На *сегодня* ({datetime.now().strftime('%d.%m.%Y')}) занятий нет.",
            parse_mode="Markdown",
            reply_markup=reply_markup
        )
        return

    await query.edit_message_text(
        f"📚 *Сегодня* ({datetime.now().strftime('%d.%m.%Y')})\n\n{formatted}",
        parse_mode="Markdown",
        reply_markup=reply_markup
    )

async def tomorrow_callback(query, context):
    """Показать расписание на завтра"""
    user_id = query.from_user.id
    db_user = db.get_user(user_id)

    if not db_user or not db_user[4]:
        await query.edit_message_text("❌ Сначала установи преподавателя.")
        return

    teacher_name = db_user[4]

    await query.edit_message_text("⏳ Загрузка...")

    tomorrow_date = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
    schedule = get_cached_schedule(teacher_name, date_filter=tomorrow_date)

    keyboard = [
        [InlineKeyboardButton("📅 Сегодня", callback_data="today")],
        [InlineKeyboardButton("🔙 В расписание", callback_data="schedule_menu")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    formatted = format_lessons_for_display(schedule, tomorrow_date)

    if not schedule or formatted == "📭 Занятий не найдено.":
        await query.edit_message_text(
            f"📭 На *завтра* ({tomorrow_date}) занятий нет.",
            parse_mode="Markdown",
            reply_markup=reply_markup
        )
        return

    await query.edit_message_text(
        f"📚 *Завтра* ({tomorrow_date})\n\n{formatted}",
        parse_mode="Markdown",
        reply_markup=reply_markup
    )

async def week_callback(query, context):
    """Показать расписание на неделю"""
    user_id = query.from_user.id
    db_user = db.get_user(user_id)

    if not db_user or not db_user[4]:
        await query.edit_message_text("❌ Сначала установи преподавателя.")
        return

    teacher_name = db_user[4]

    await query.edit_message_text("⏳ Загрузка...")

    schedule = get_schedule_for_teacher(teacher_name, page_limit=5)

    if not schedule:
        await query.edit_message_text("❌ Не удалось получить расписание.")
        return

    week_lessons = filter_lessons_by_week(schedule)
    formatted = format_lessons_for_display(week_lessons)

    keyboard = [
        [InlineKeyboardButton("📅 Месяц", callback_data="month")],
        [InlineKeyboardButton("🔙 В расписание", callback_data="schedule_menu")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    if not week_lessons:
        await query.edit_message_text(
            "📭 На текущей неделе занятий нет.",
            reply_markup=reply_markup
        )
        return

    if len(formatted) > 4000:
        parts = []
        current_part = "📚 *Неделя*\n\n"
        for line in formatted.split('\n'):
            if len(current_part + line + '\n') > 4000:
                parts.append(current_part)
                current_part = "📚 *Продолжение*\n\n" + line + '\n'
            else:
                current_part += line + '\n'
        parts.append(current_part)

        await query.edit_message_text(parts[0], parse_mode="Markdown", reply_markup=reply_markup)
        for part in parts[1:]:
            await context.bot.send_message(chat_id=user_id, text=part, parse_mode="Markdown")
    else:
        await query.edit_message_text(formatted, parse_mode="Markdown", reply_markup=reply_markup)

async def month_callback(query, context):
    """Показать расписание на месяц"""
    user_id = query.from_user.id
    db_user = db.get_user(user_id)

    if not db_user or not db_user[4]:
        await query.edit_message_text("❌ Сначала установи преподавателя.")
        return

    teacher_name = db_user[4]

    await query.edit_message_text("⏳ Загрузка...")

    schedule = get_schedule_for_teacher(teacher_name, page_limit=10)

    if not schedule:
        await query.edit_message_text("❌ Не удалось получить расписание.")
        return

    month_lessons = filter_lessons_by_month(schedule)
    formatted = format_lessons_for_display(month_lessons)
    month_name = datetime.now().strftime("%B %Y")

    keyboard = [
        [InlineKeyboardButton("📚 Все расписание", callback_data="all_schedule")],
        [InlineKeyboardButton("🔙 В расписание", callback_data="schedule_menu")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    if not month_lessons:
        await query.edit_message_text(
            f"📭 На {month_name} занятий нет.",
            reply_markup=reply_markup
        )
        return

    if len(formatted) > 4000:
        parts = []
        current_part = f"📚 *{month_name}*\n\n"
        for line in formatted.split('\n'):
            if len(current_part + line + '\n') > 4000:
                parts.append(current_part)
                current_part = "📚 *Продолжение*\n\n" + line + '\n'
            else:
                current_part += line + '\n'
        parts.append(current_part)

        await query.edit_message_text(parts[0], parse_mode="Markdown", reply_markup=reply_markup)
        for part in parts[1:]:
            await context.bot.send_message(chat_id=user_id, text=part, parse_mode="Markdown")
    else:
        await query.edit_message_text(formatted, parse_mode="Markdown", reply_markup=reply_markup)

async def all_schedule_callback(query, context):
    """Показать всё расписание"""
    user_id = query.from_user.id
    db_user = db.get_user(user_id)

    if not db_user or not db_user[4]:
        await query.edit_message_text("❌ Сначала установи преподавателя.")
        return

    teacher_name = db_user[4]

    await query.edit_message_text("⏳ Загрузка...")

    schedule = get_schedule_for_teacher(teacher_name, page_limit=10)

    if not schedule:
        await query.edit_message_text("❌ Не удалось получить расписание.")
        return

    formatted = format_lessons_for_display(schedule)

    keyboard = [
        [InlineKeyboardButton("🔙 В расписание", callback_data="schedule_menu")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    if not schedule:
        await query.edit_message_text(
            "📭 Расписания нет.",
            reply_markup=reply_markup
        )
        return

    if len(formatted) > 4000:
        parts = []
        current_part = "📚 *Все расписание*\n\n"
        for line in formatted.split('\n'):
            if len(current_part + line + '\n') > 4000:
                parts.append(current_part)
                current_part = "📚 *Продолжение*\n\n" + line + '\n'
            else:
                current_part += line + '\n'
        parts.append(current_part)

        await query.edit_message_text(parts[0], parse_mode="Markdown", reply_markup=reply_markup)
        for part in parts[1:]:
            await context.bot.send_message(chat_id=user_id, text=part, parse_mode="Markdown")
    else:
        await query.edit_message_text(formatted, parse_mode="Markdown", reply_markup=reply_markup)

# ============ КОМАНДЫ (ДЛЯ ОБРАТНОЙ СОВМЕСТИМОСТИ) ============
async def today(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /today"""
    user_id = update.effective_user.id
    db_user = db.get_user(user_id)
    if not db_user or not db_user[4]:
        await update.message.reply_text("❌ Сначала установи преподавателя.")
        return

    # Создаем callback_query и вызываем today_callback
    class MockQuery:
        def __init__(self, user_id):
            self.from_user = type('obj', (object,), {'id': user_id})
            self.message = None
        async def edit_message_text(self, *args, **kwargs):
            await update.message.reply_text(*args, **kwargs)
        async def answer(self):
            pass

    await today_callback(MockQuery(user_id), context)

async def tomorrow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /tomorrow"""
    user_id = update.effective_user.id
    db_user = db.get_user(user_id)
    if not db_user or not db_user[4]:
        await update.message.reply_text("❌ Сначала установи преподавателя.")
        return

    class MockQuery:
        def __init__(self, user_id):
            self.from_user = type('obj', (object,), {'id': user_id})
            self.message = None
        async def edit_message_text(self, *args, **kwargs):
            await update.message.reply_text(*args, **kwargs)
        async def answer(self):
            pass

    await tomorrow_callback(MockQuery(user_id), context)

async def week(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /week"""
    user_id = update.effective_user.id
    db_user = db.get_user(user_id)
    if not db_user or not db_user[4]:
        await update.message.reply_text("❌ Сначала установи преподавателя.")
        return

    class MockQuery:
        def __init__(self, user_id):
            self.from_user = type('obj', (object,), {'id': user_id})
            self.message = None
        async def edit_message_text(self, *args, **kwargs):
            await update.message.reply_text(*args, **kwargs)
        async def answer(self):
            pass

    await week_callback(MockQuery(user_id), context)

async def remove_teacher(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /remove_teacher"""
    user_id = update.effective_user.id
    db.add_user(user_id, update.effective_user.username,
                update.effective_user.first_name,
                update.effective_user.last_name, "")
    await update.message.reply_text(
        "✅ Преподаватель удален.\n"
        "Используй /set_teacher чтобы установить нового."
    )

async def settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /settings"""
    user_id = update.effective_user.id
    db_user = db.get_user(user_id)
    if not db_user:
        await update.message.reply_text("❌ Сначала используй /start")
        return

    class MockQuery:
        def __init__(self, user_id):
            self.from_user = type('obj', (object,), {'id': user_id})
            self.message = None
        async def edit_message_text(self, *args, **kwargs):
            await update.message.reply_text(*args, **kwargs)
        async def answer(self):
            pass

    await settings_menu(MockQuery(user_id), context)

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /help"""
    help_text = (
        "🤖 *Помощь по боту*\n\n"
        "📌 *Основные команды:*\n"
        "/start - Главное меню\n"
        "/set_teacher ФИО - Установить преподавателя\n"
        "/today - Расписание на сегодня\n"
        "/tomorrow - Расписание на завтра\n"
        "/week - Расписание на неделю\n"
        "/settings - Настройки\n"
        "/remove_teacher - Удалить преподавателя\n"
        "/help - Эта справка\n\n"
        "📱 Используй кнопки для быстрого доступа."
    )
    await update.message.reply_text(help_text, parse_mode="Markdown")

# ============ ФОНОВЫЕ ЗАДАЧИ ============
async def check_changes(context: ContextTypes.DEFAULT_TYPE):
    """Фоновая задача для проверки изменений в расписании"""
    logger.info("Проверка изменений в расписании...")

    users = db.get_all_users_with_teacher()
    for user_id, teacher_name in users:
        try:
            current_schedule = get_schedule_for_teacher(teacher_name, page_limit=5)
            if not current_schedule:
                continue

            cached_schedule, _ = db.get_schedule_cache(user_id)

            if cached_schedule:
                current_uuids = {lesson['uuid'] for lesson in current_schedule}
                cached_uuids = {lesson['uuid'] for lesson in cached_schedule}

                new_uuids = current_uuids - cached_uuids
                removed_uuids = cached_uuids - current_uuids

                for uuid in new_uuids:
                    lesson = next(l for l in current_schedule if l['uuid'] == uuid)
                    db.add_change(user_id, 'added', uuid, lesson)

                for uuid in removed_uuids:
                    lesson = next(l for l in cached_schedule if l['uuid'] == uuid)
                    db.add_change(user_id, 'removed', uuid, lesson)

                if new_uuids or removed_uuids:
                    await notify_user(context.bot, user_id)

            db.save_schedule_cache(user_id, teacher_name, current_schedule)

        except Exception as e:
            logger.error(f"Ошибка при проверке для пользователя {user_id}: {e}")

async def notify_user(bot, user_id):
    """Отправляет уведомления пользователю об изменениях"""
    changes = db.get_unnotified_changes(user_id)

    if not changes:
        return

    message = "🔔 *Обнаружены изменения в расписании!*\n\n"

    for change in changes:
        change_id, _, change_type, lesson_uuid, lesson_data_json, _, _ = change
        lesson_data = json.loads(lesson_data_json)

        if change_type == 'added':
            message += f"➕ *Добавлено:*\n"
            message += f"   📅 {lesson_data['date']}\n"
            message += f"   ⏰ {lesson_data['start_time']} - {lesson_data['end_time']}\n"
            message += f"   📚 {lesson_data['info']}\n"
            message += f"   🏫 Ауд. {lesson_data['room']}\n\n"
        else:
            message += f"➖ *Удалено:*\n"
            message += f"   📅 {lesson_data['date']}\n"
            message += f"   ⏰ {lesson_data['start_time']} - {lesson_data['end_time']}\n"
            message += f"   📚 {lesson_data['info']}\n\n"

    try:
        await bot.send_message(chat_id=user_id, text=message, parse_mode="Markdown")
        db.mark_changes_notified(user_id)
        logger.info(f"Уведомление отправлено пользователю {user_id}")
    except Exception as e:
        logger.error(f"Ошибка отправки уведомления пользователю {user_id}: {e}")

# ============ ЗАПУСК ============
def main():
    """Основная функция запуска бота"""
    token = os.getenv('TELEGRAM_BOT_TOKEN')

    if not token:
        print("❌ Ошибка: TELEGRAM_BOT_TOKEN не найден в .env файле")
        return

    os.makedirs("data", exist_ok=True)

    application = Application.builder().token(token).build()

    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(reset_webhook(application))
        loop.close()
        logger.info("🔗 Вебхук успешно сброшен")
    except Exception as e:
        logger.error(f"Ошибка при удалении вебхука: {e}")

    # Регистрируем обработчики команд
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("set_teacher", set_teacher))
    application.add_handler(CommandHandler("today", today))
    application.add_handler(CommandHandler("tomorrow", tomorrow))
    application.add_handler(CommandHandler("week", week))
    application.add_handler(CommandHandler("remove_teacher", remove_teacher))
    application.add_handler(CommandHandler("settings", settings))
    application.add_handler(CommandHandler("help", help_command))

    # Регистрируем обработчики кнопок
    application.add_handler(CallbackQueryHandler(button_callback, pattern="^(schedule_menu|settings_menu|back_to_main|today|tomorrow|week|month|all_schedule|notifications_on|notifications_off|remove_teacher)$"))

    job_queue = application.job_queue
    if job_queue:
        job_queue.run_repeating(check_changes, interval=300, first=10)
        logger.info("⏰ Фоновый планировщик запущен (проверка каждые 5 минут)")

    logger.info("🚀 Бот запущен и готов к работе!")

    try:
        application.run_polling(allowed_updates=Update.ALL_TYPES)
    except RuntimeError as e:
        if "Event loop is closed" in str(e):
            logger.warning("Перезапуск с новым event loop...")
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            application.run_polling(allowed_updates=Update.ALL_TYPES)
        else:
            raise

if __name__ == '__main__':
    main()
