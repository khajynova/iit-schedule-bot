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

# Функция для получения расписания
def get_schedule_for_teacher(teacher_name, page_limit=10):
    """
    Получает расписание для преподавателя.
    page_limit - максимальное количество страниц для загрузки (по умолчанию 10)
    """
    url = "https://iit.bsuir.by/api/v1/content/schedule/"
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

    # Находим начало недели (понедельник)
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

async def reset_webhook(application):
    """Принудительно удаляет вебхук перед запуском"""
    try:
        await application.bot.delete_webhook(drop_pending_updates=True)
        logger.info("✅ Вебхук успешно удален")
    except Exception as e:
        logger.error(f"❌ Ошибка удаления вебхука: {e}")

async def main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id=None):
    """Показывает главное меню с кнопками"""
    if user_id is None:
        user_id = update.effective_user.id

    db_user = db.get_user(user_id)
    teacher = db_user[4] if db_user and db_user[4] else "не установлен"

    keyboard = [
        [InlineKeyboardButton("📅 Сегодня", callback_data="today")],
        [InlineKeyboardButton("📅 Завтра", callback_data="tomorrow")],
        [InlineKeyboardButton("📅 Неделя", callback_data="week")],
        [InlineKeyboardButton("📅 Месяц", callback_data="month")],
        [InlineKeyboardButton("📚 Все расписание", callback_data="all_schedule")],
        [InlineKeyboardButton("⚙️ Настройки", callback_data="settings")]
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
        # Пользователь уже есть
        await main_menu(update, context)

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик нажатий на кнопки"""
    query = update.callback_query
    await query.answer()

    data = query.data

    if data == "set_teacher":
        await query.edit_message_text(
            "📚 Установи преподавателя командой:\n"
            "/set_teacher Фамилия И.О.\n\n"
            "Пример: /set_teacher Хаджинова Н.В."
        )
        return

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

    if data == "settings":
        await settings_callback_handler(query, context)
        return

    if data == "notifications_on":
        user_id = query.from_user.id
        db.toggle_notifications(user_id, True)
        await query.edit_message_text("🔔 Уведомления включены!")
        return

    if data == "notifications_off":
        user_id = query.from_user.id
        db.toggle_notifications(user_id, False)
        await query.edit_message_text("🔕 Уведомления выключены!")
        return

    if data == "stats":
        await show_stats(query, context)
        return

    if data == "remove_teacher":
        user_id = query.from_user.id
        db.add_user(user_id, query.from_user.username,
                    query.from_user.first_name,
                    query.from_user.last_name, "")
        await query.edit_message_text("✅ Преподаватель удален.")
        return

    if data == "back_to_menu":
        await main_menu(update, context, query.from_user.id)
        return

async def get_schedule_and_check(user_id, query=None):
    """Получает расписание и проверяет наличие преподавателя"""
    db_user = db.get_user(user_id)
    if not db_user or not db_user[4]:
        msg = "❌ Сначала установи преподавателя командой:\n/set_teacher Фамилия И.О."
        if query:
            await query.edit_message_text(msg)
        return None, None

    teacher_name = db_user[4]

    if query:
        await query.edit_message_text("⏳ Загрузка расписания...")

    schedule = get_schedule_for_teacher(teacher_name)
    return schedule, teacher_name

async def send_schedule_result(query, context, schedule, title, reply_markup=None):
    """Отправляет расписание с обработкой длинных сообщений"""
    formatted = format_lessons_for_display(schedule)

    if not formatted or formatted == "📭 Занятий не найдено.":
        await query.edit_message_text("📭 Занятий не найдено.", reply_markup=reply_markup)
        return

    if len(formatted) > 4000:
        parts = []
        current_part = f"{title}\n\n"
        for line in formatted.split('\n'):
            if len(current_part + line + '\n') > 4000:
                parts.append(current_part)
                current_part = "📚 Продолжение:\n\n" + line + '\n'
            else:
                current_part += line + '\n'
        parts.append(current_part)

        await query.edit_message_text(parts[0], parse_mode="Markdown", reply_markup=reply_markup)

        for part in parts[1:]:
            await context.bot.send_message(chat_id=query.from_user.id, text=part, parse_mode="Markdown")
    else:
        await query.edit_message_text(f"{title}\n\n{formatted}", parse_mode="Markdown", reply_markup=reply_markup)

async def today_callback(query, context):
    """Показать расписание на сегодня"""
    user_id = query.from_user.id
    schedule, teacher_name = await get_schedule_and_check(user_id, query)
    if not schedule:
        return

    db.save_schedule_cache(user_id, teacher_name, schedule)

    today_str = datetime.now().strftime("%Y-%m-%d")
    today_lessons = [l for l in schedule if l.get("date") == today_str]

    keyboard = [
        [InlineKeyboardButton("📅 Завтра", callback_data="tomorrow")],
        [InlineKeyboardButton("🔙 В меню", callback_data="back_to_menu")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    send_schedule_result(query, context, today_lessons,
                        f"📚 Расписание на *сегодня* ({datetime.now().strftime('%d.%m.%Y')})",
                        reply_markup)

async def tomorrow_callback(query, context):
    """Показать расписание на завтра"""
    user_id = query.from_user.id
    schedule, teacher_name = await get_schedule_and_check(user_id, query)
    if not schedule:
        return

    tomorrow_date = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
    tomorrow_lessons = [l for l in schedule if l.get("date") == tomorrow_date]

    keyboard = [
        [InlineKeyboardButton("📅 Сегодня", callback_data="today")],
        [InlineKeyboardButton("🔙 В меню", callback_data="back_to_menu")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    send_schedule_result(query, context, tomorrow_lessons,
                        f"📚 Расписание на *завтра* ({tomorrow_date})",
                        reply_markup)

async def week_callback(query, context):
    """Показать расписание на неделю"""
    user_id = query.from_user.id
    schedule, teacher_name = await get_schedule_and_check(user_id, query)
    if not schedule:
        return

    week_lessons = filter_lessons_by_week(schedule)
    db.save_schedule_cache(user_id, teacher_name, schedule)

    keyboard = [
        [InlineKeyboardButton("📅 Месяц", callback_data="month")],
        [InlineKeyboardButton("🔙 В меню", callback_data="back_to_menu")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    send_schedule_result(query, context, week_lessons,
                        "📚 Расписание на *текущую неделю*",
                        reply_markup)

async def month_callback(query, context):
    """Показать расписание на месяц"""
    user_id = query.from_user.id
    schedule, teacher_name = await get_schedule_and_check(user_id, query)
    if not schedule:
        return

    # Фильтруем по текущему месяцу
    now = datetime.now()
    month_lessons = []
    for lesson in schedule:
        date_str = lesson.get('date', '')
        if date_str:
            lesson_date = datetime.strptime(date_str, "%Y-%m-%d")
            if lesson_date.month == now.month and lesson_date.year == now.year:
                month_lessons.append(lesson)

    keyboard = [
        [InlineKeyboardButton("📚 Все расписание", callback_data="all_schedule")],
        [InlineKeyboardButton("🔙 В меню", callback_data="back_to_menu")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    send_schedule_result(query, context, month_lessons,
                        f"📚 Расписание на *{now.strftime('%B %Y')}*",
                        reply_markup)

async def all_schedule_callback(query, context):
    """Показать всё расписание"""
    user_id = query.from_user.id
    schedule, teacher_name = await get_schedule_and_check(user_id, query)
    if not schedule:
        return

    keyboard = [
        [InlineKeyboardButton("📅 Неделя", callback_data="week")],
        [InlineKeyboardButton("📅 Месяц", callback_data="month")],
        [InlineKeyboardButton("🔙 В меню", callback_data="back_to_menu")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    send_schedule_result(query, context, schedule,
                        "📚 *Все расписание*",
                        reply_markup)

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

    schedule = get_schedule_for_teacher(teacher_name)
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

async def settings_callback_handler(query, context):
    """Обработчик кнопки настроек"""
    user_id = query.from_user.id
    db_user = db.get_user(user_id)

    if not db_user:
        await query.edit_message_text("❌ Сначала используй /start")
        return

    keyboard = [
        [InlineKeyboardButton("🔔 Включить уведомления", callback_data="notifications_on")],
        [InlineKeyboardButton("🔕 Выключить уведомления", callback_data="notifications_off")],
        [InlineKeyboardButton("📊 Статистика", callback_data="stats")],
        [InlineKeyboardButton("❌ Удалить преподавателя", callback_data="remove_teacher")],
        [InlineKeyboardButton("🔙 В меню", callback_data="back_to_menu")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    teacher = db_user[4] if db_user[4] else "не установлен"
    notifications = "включены" if db_user[5] == 1 else "выключены"

    await query.edit_message_text(
        f"⚙️ *Настройки*\n\n"
        f"👨‍🏫 Преподаватель: *{teacher}*\n"
        f"🔔 Уведомления: *{notifications}*\n\n"
        "Выбери действие:",
        parse_mode="Markdown",
        reply_markup=reply_markup
    )

async def show_stats(query, context):
    """Показывает статистику"""
    user_id = query.from_user.id
    stats = db.get_stats(user_id)

    if not stats:
        await query.edit_message_text(
            "❌ Нет данных для статистики. Сначала получи расписание (/today или /week).",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 В меню", callback_data="back_to_menu")]
            ])
        )
        return

    msg = (
        f"📊 *Статистика*\n\n"
        f"👨‍🏫 Преподаватель: *{stats['teacher_name']}*\n"
        f"📅 Зарегистрирован: *{stats['registered_at'][:10]}*\n"
        f"📚 Всего занятий: *{stats['total_lessons']}*\n"
        f"📆 Дней с занятиями: *{stats['total_days']}*\n"
        f"📅 Первое занятие: *{stats['first_date']}*\n"
        f"📅 Последнее занятие: *{stats['last_date']}*\n"
    )

    keyboard = [[InlineKeyboardButton("🔙 В меню", callback_data="back_to_menu")]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_text(msg, parse_mode="Markdown", reply_markup=reply_markup)

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Помощь"""
    help_text = (
        "🤖 *Помощь по боту*\n\n"
        "📌 *Основные команды:*\n"
        "/start - Главное меню\n"
        "/set_teacher ФИО - Установить преподавателя\n"
        "/today - Расписание на сегодня\n"
        "/tomorrow - Расписание на завтра\n"
        "/week - Расписание на неделю\n"
        "/stats - Статистика\n"
        "/settings - Настройки\n"
        "/remove_teacher - Удалить преподавателя\n"
        "/help - Эта справка\n\n"
        "📱 Используй кнопки для быстрого доступа."
    )
    await update.message.reply_text(help_text, parse_mode="Markdown")

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда для просмотра статистики"""
    user_id = update.effective_user.id
    stats = db.get_stats(user_id)

    if not stats:
        await update.message.reply_text("❌ Нет данных для статистики. Сначала получи расписание (/today или /week).")
        return

    msg = (
        f"📊 *Статистика*\n\n"
        f"👨‍🏫 Преподаватель: *{stats['teacher_name']}*\n"
        f"📅 Зарегистрирован: *{stats['registered_at'][:10]}*\n"
        f"📚 Всего занятий: *{stats['total_lessons']}*\n"
        f"📆 Дней с занятиями: *{stats['total_days']}*\n"
        f"📅 Первое занятие: *{stats['first_date']}*\n"
        f"📅 Последнее занятие: *{stats['last_date']}*\n"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")

async def remove_teacher(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Удаляет преподавателя"""
    user_id = update.effective_user.id
    db.add_user(user_id, update.effective_user.username,
                update.effective_user.first_name,
                update.effective_user.last_name, "")
    await update.message.reply_text(
        "✅ Преподаватель удален.\n"
        "Используй /set_teacher чтобы установить нового."
    )

async def settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Настройки бота (команда)"""
    user_id = update.effective_user.id
    db_user = db.get_user(user_id)

    if not db_user:
        await update.message.reply_text("❌ Сначала используй /start")
        return

    keyboard = [
        [InlineKeyboardButton("🔔 Включить уведомления", callback_data="notifications_on")],
        [InlineKeyboardButton("🔕 Выключить уведомления", callback_data="notifications_off")],
        [InlineKeyboardButton("📊 Статистика", callback_data="stats")],
        [InlineKeyboardButton("❌ Удалить преподавателя", callback_data="remove_teacher")],
        [InlineKeyboardButton("🔙 В меню", callback_data="back_to_menu")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    teacher = db_user[4] if db_user[4] else "не установлен"
    notifications = "включены" if db_user[5] == 1 else "выключены"

    await update.message.reply_text(
        f"⚙️ *Настройки*\n\n"
        f"👨‍🏫 Преподаватель: *{teacher}*\n"
        f"🔔 Уведомления: *{notifications}*\n\n"
        "Выбери действие:",
        parse_mode="Markdown",
        reply_markup=reply_markup
    )

async def today(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показать расписание на сегодня (команда)"""
    user_id = update.effective_user.id
    db_user = db.get_user(user_id)

    if not db_user or not db_user[4]:
        await update.message.reply_text(
            "❌ Сначала установи преподавателя командой:\n"
            "/set_teacher Фамилия И.О."
        )
        return

    teacher_name = db_user[4]

    status_msg = await update.message.reply_text("⏳ Загрузка расписания...")

    schedule = get_schedule_for_teacher(teacher_name)

    if not schedule:
        await status_msg.edit_text("❌ Не удалось получить расписание.")
        return

    today_str = datetime.now().strftime("%Y-%m-%d")
    today_lessons = [l for l in schedule if l.get("date") == today_str]
    formatted = format_lessons_for_display(today_lessons)

    db.save_schedule_cache(user_id, teacher_name, schedule)

    keyboard = [
        [InlineKeyboardButton("📅 Завтра", callback_data="tomorrow")],
        [InlineKeyboardButton("🔙 В меню", callback_data="back_to_menu")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await status_msg.edit_text(
        f"📚 Расписание на *сегодня* ({datetime.now().strftime('%d.%m.%Y')}):\n\n{formatted}",
        parse_mode="Markdown",
        reply_markup=reply_markup
    )

async def tomorrow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показать расписание на завтра (команда)"""
    user_id = update.effective_user.id
    db_user = db.get_user(user_id)

    if not db_user or not db_user[4]:
        await update.message.reply_text(
            "❌ Сначала установи преподавателя командой:\n"
            "/set_teacher Фамилия И.О."
        )
        return

    teacher_name = db_user[4]

    status_msg = await update.message.reply_text("⏳ Загрузка расписания...")

    schedule = get_schedule_for_teacher(teacher_name)

    if not schedule:
        await status_msg.edit_text("❌ Не удалось получить расписание.")
        return

    tomorrow_date = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
    tomorrow_lessons = [l for l in schedule if l.get("date") == tomorrow_date]
    formatted = format_lessons_for_display(tomorrow_lessons)

    keyboard = [
        [InlineKeyboardButton("📅 Сегодня", callback_data="today")],
        [InlineKeyboardButton("🔙 В меню", callback_data="back_to_menu")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await status_msg.edit_text(
        f"📚 Расписание на *завтра* ({tomorrow_date}):\n\n{formatted}",
        parse_mode="Markdown",
        reply_markup=reply_markup
    )

async def week(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показать расписание на неделю (команда)"""
    user_id = update.effective_user.id
    db_user = db.get_user(user_id)

    if not db_user or not db_user[4]:
        await update.message.reply_text(
            "❌ Сначала установи преподавателя командой:\n"
            "/set_teacher Фамилия И.О."
        )
        return

    teacher_name = db_user[4]

    status_msg = await update.message.reply_text("⏳ Загрузка расписания...")

    schedule = get_schedule_for_teacher(teacher_name)

    if not schedule:
        await status_msg.edit_text("❌ Не удалось получить расписание.")
        return

    week_lessons = filter_lessons_by_week(schedule)
    formatted = format_lessons_for_display(week_lessons)

    db.save_schedule_cache(user_id, teacher_name, schedule)

    keyboard = [
        [InlineKeyboardButton("📅 Месяц", callback_data="month")],
        [InlineKeyboardButton("🔙 В меню", callback_data="back_to_menu")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    if len(formatted) > 4000:
        parts = []
        current_part = "📚 Расписание на неделю:\n\n"
        for line in formatted.split('\n'):
            if len(current_part + line + '\n') > 4000:
                parts.append(current_part)
                current_part = "📚 Продолжение:\n\n" + line + '\n'
            else:
                current_part += line + '\n'
        parts.append(current_part)

        await status_msg.edit_text(parts[0], parse_mode="Markdown", reply_markup=reply_markup)

        for part in parts[1:]:
            await update.message.reply_text(part, parse_mode="Markdown")
    else:
        await status_msg.edit_text(formatted, parse_mode="Markdown", reply_markup=reply_markup)

async def check_changes(context: ContextTypes.DEFAULT_TYPE):
    """Фоновая задача для проверки изменений в расписании"""
    logger.info("Проверка изменений в расписании...")

    users = db.get_all_users_with_teacher()
    for user_id, teacher_name in users:
        try:
            current_schedule = get_schedule_for_teacher(teacher_name)
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
    application.add_handler(CommandHandler("stats", stats_command))
    application.add_handler(CommandHandler("help", help_command))

    # Регистрируем обработчики кнопок
    application.add_handler(CallbackQueryHandler(button_callback, pattern="^(today|tomorrow|week|month|all_schedule|set_teacher|settings|help|notifications_on|notifications_off|stats|remove_teacher|back_to_menu)$"))

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
            import asyncio
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            application.run_polling(allowed_updates=Update.ALL_TYPES)
        else:
            raise

if __name__ == '__main__':
    main()
