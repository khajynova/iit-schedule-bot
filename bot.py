# Основной код бота
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
import threading
from flask import Flask

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

# ============ KEEP ALIVE ФУНКЦИЯ ============
app = Flask(__name__)

@app.route('/')
def home():
    return "🤖 Бот для расписания БГУИР ФКТ работает!"

@app.route('/health')
def health():
    return "OK", 200

def run_flask():
    """Запускает Flask сервер для keep_alive"""
    try:
        port = int(os.environ.get('PORT', 10000))
        app.run(host='0.0.0.0', port=port, debug=False)
    except Exception as e:
        logger.error(f"Ошибка запуска Flask: {e}")

# ===========================================

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

            # Проверяем, есть ли следующая страница
            if not data.get("next"):
                break

            page += 1

        # Фильтруем занятия - оставляем только те, где есть ФИО преподавателя
        filtered_results = []
        for lesson in all_results:
            info = lesson.get("info", "")
            # Проверяем, содержит ли info точное ФИО преподавателя
            if teacher_name.lower() in info.lower():
                filtered_results.append(lesson)

        logger.info(f"Найдено {len(filtered_results)} занятий для {teacher_name} (из {len(all_results)} всего)")
        return filtered_results

    except requests.exceptions.RequestException as e:
        logger.error(f"Ошибка запроса к API: {e}")
        return []

def format_lessons_for_display(lessons, date_filter=None, compact=False):
    """
    Форматирует расписание для вывода пользователю.
    compact - компактный режим для длинных сообщений
    """
    if date_filter:
        filtered = [l for l in lessons if l.get("date") == date_filter]
    else:
        filtered = lessons

    if not filtered:
        return "📭 Занятий не найдено."

    # Группировка по датам
    result = []
    current_date = None

    # Сортируем по дате и времени
    sorted_lessons = sorted(filtered, key=lambda x: (x.get('date', ''), x.get('start_time', '')))

    for lesson in sorted_lessons:
        date = lesson.get('date', '')
        if date != current_date:
            current_date = date
            date_obj = datetime.strptime(date, "%Y-%m-%d")
            day_key = lesson.get('day_of_week_key', '').lower()
            day_name = DAY_NAMES.get(day_key, day_key.capitalize())
            result.append(f"\n📅 {date_obj.strftime('%d.%m.%Y')} ({day_name})")

        if compact:
            # Компактный формат
            result.append(
                f"  ⏰ {lesson['start_time']} - {lesson['end_time']} | "
                f"{lesson['info'][:30]}... | 🏫 {lesson['room']}"
            )
        else:
            # Полный формат
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

async def reset_webhook(application):
    """Принудительно удаляет вебхук перед запуском"""
    try:
        await application.bot.delete_webhook(drop_pending_updates=True)
        logger.info("✅ Вебхук успешно удален")
    except Exception as e:
        logger.error(f"❌ Ошибка удаления вебхука: {e}")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /start"""
    user = update.effective_user
    user_id = user.id

    # Проверяем, есть ли пользователь в базе
    db_user = db.get_user(user_id)
    if not db_user:
        # Создаем клавиатуру с кнопками
        keyboard = [
            [InlineKeyboardButton("📚 Установить преподавателя", callback_data="set_teacher")],
            [InlineKeyboardButton("📅 Расписание на сегодня", callback_data="today")],
            [InlineKeyboardButton("📅 Расписание на завтра", callback_data="tomorrow")],
            [InlineKeyboardButton("📊 Помощь", callback_data="help")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        welcome_text = (
            f"👋 Привет, {user.first_name}!\n\n"
            "Я бот для отслеживания расписания преподавателей БГУИР ФКТ.\n\n"
            "📌 Используй команду /set_teacher чтобы настроить преподавателя.\n"
            "📅 Используй /today чтобы посмотреть расписание на сегодня.\n"
            "🔔 Я буду автоматически уведомлять тебя об изменениях в расписании."
        )
        await update.message.reply_text(welcome_text, reply_markup=reply_markup)

        # Сохраняем пользователя с временным значением
        db.add_user(user_id, user.username, user.first_name, user.last_name, "")

        # Отправляем инструкцию по настройке
        await update.message.reply_text(
            "Для начала работы, пожалуйста, установи преподавателя командой:\n"
            "/set_teacher Фамилия И.О.\n\n"
            "Пример: /set_teacher Хаджинова Н.В."
        )
    else:
        teacher = db_user[4] if db_user[4] else "не установлен"

        keyboard = [
            [InlineKeyboardButton("📅 Сегодня", callback_data="today")],
            [InlineKeyboardButton("📅 Завтра", callback_data="tomorrow")],
            [InlineKeyboardButton("📅 Неделя", callback_data="week")],
            [InlineKeyboardButton("⚙️ Настройки", callback_data="settings")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await update.message.reply_text(
            f"👋 С возвращением, {user.first_name}!\n"
            f"Твой преподаватель: *{teacher}*\n\n"
            "Выбери действие:",
            reply_markup=reply_markup,
            parse_mode="Markdown"
        )

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик нажатий на кнопки"""
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
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

    if data == "settings":
        await settings_callback_handler(query, context)
        return

    if data == "help":
        await help_command(update, context)
        return

async def today_callback(query, context):
    """Показать расписание на сегодня (для кнопки)"""
    user_id = query.from_user.id
    db_user = db.get_user(user_id)

    if not db_user or not db_user[4]:
        await query.edit_message_text(
            "❌ Сначала установи преподавателя командой:\n"
            "/set_teacher Фамилия И.О."
        )
        return

    teacher_name = db_user[4]

    # Отправляем статус загрузки
    await query.edit_message_text("⏳ Загрузка расписания...")

    schedule = get_schedule_for_teacher(teacher_name)

    if not schedule:
        await query.edit_message_text("❌ Не удалось получить расписание.")
        return

    today_str = datetime.now().strftime("%Y-%m-%d")
    formatted = format_lessons_for_display(schedule, today_str)

    # Сохраняем кеш
    db.save_schedule_cache(user_id, teacher_name, schedule)

    # Создаем клавиатуру
    keyboard = [
        [InlineKeyboardButton("📅 Завтра", callback_data="tomorrow")],
        [InlineKeyboardButton("📅 Неделя", callback_data="week")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_text(
        f"📚 Расписание на *сегодня* ({datetime.now().strftime('%d.%m.%Y')}):\n\n{formatted}",
        parse_mode="Markdown",
        reply_markup=reply_markup
    )

async def tomorrow_callback(query, context):
    """Показать расписание на завтра (для кнопки)"""
    user_id = query.from_user.id
    db_user = db.get_user(user_id)

    if not db_user or not db_user[4]:
        await query.edit_message_text(
            "❌ Сначала установи преподавателя командой:\n"
            "/set_teacher Фамилия И.О."
        )
        return

    teacher_name = db_user[4]

    await query.edit_message_text("⏳ Загрузка расписания...")

    schedule = get_schedule_for_teacher(teacher_name)

    if not schedule:
        await query.edit_message_text("❌ Не удалось получить расписание.")
        return

    tomorrow_date = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
    formatted = format_lessons_for_display(schedule, tomorrow_date)

    keyboard = [
        [InlineKeyboardButton("📅 Сегодня", callback_data="today")],
        [InlineKeyboardButton("📅 Неделя", callback_data="week")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_text(
        f"📚 Расписание на *завтра* ({tomorrow_date}):\n\n{formatted}",
        parse_mode="Markdown",
        reply_markup=reply_markup
    )

async def week_callback(query, context):
    """Показать расписание на неделю (для кнопки)"""
    user_id = query.from_user.id
    db_user = db.get_user(user_id)

    if not db_user or not db_user[4]:
        await query.edit_message_text(
            "❌ Сначала установи преподавателя командой:\n"
            "/set_teacher Фамилия И.О."
        )
        return

    teacher_name = db_user[4]

    await query.edit_message_text("⏳ Загрузка расписания...")

    schedule = get_schedule_for_teacher(teacher_name)

    if not schedule:
        await query.edit_message_text("❌ Не удалось получить расписание.")
        return

    formatted = format_lessons_for_display(schedule)

    keyboard = [
        [InlineKeyboardButton("📅 Сегодня", callback_data="today")],
        [InlineKeyboardButton("📅 Завтра", callback_data="tomorrow")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    # Если сообщение слишком длинное - разбиваем
    if len(formatted) > 4000:
        parts = []
        current_part = "📚 Расписание на неделю:\n\n"
        for line in formatted.split('\n'):
            if len(current_part + line + '\n') > 4000:
                parts.append(current_part)
                current_part = "📚 Продолжение расписания:\n\n" + line + '\n'
            else:
                current_part += line + '\n'
        parts.append(current_part)

        # Отправляем первую часть
        await query.edit_message_text(parts[0], parse_mode="Markdown", reply_markup=reply_markup)

        # Отправляем остальные
        for part in parts[1:]:
            await context.bot.send_message(chat_id=user_id, text=part, parse_mode="Markdown")
    else:
        await query.edit_message_text(formatted, parse_mode="Markdown", reply_markup=reply_markup)

async def set_teacher(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Установка преподавателя для отслеживания"""
    user_id = update.effective_user.id

    # Проверяем, передано ли имя
    if not context.args:
        await update.message.reply_text(
            "❌ Пожалуйста, укажи ФИО преподавателя.\n"
            "Пример: /set_teacher Хаджинова Н.В."
        )
        return

    teacher_name = " ".join(context.args)

    # Показываем статус загрузки
    status_msg = await update.message.reply_text("⏳ Поиск расписания...")

    # Проверяем, есть ли такой преподаватель в системе
    schedule = get_schedule_for_teacher(teacher_name)
    if not schedule:
        await status_msg.edit_text(
            f"❌ Преподаватель '{teacher_name}' не найден.\n"
            "Проверь правильность написания ФИО."
        )
        return

    # Сохраняем преподавателя в базу
    db.add_user(user_id, update.effective_user.username,
                update.effective_user.first_name,
                update.effective_user.last_name,
                teacher_name)

    # Сохраняем кеш расписания для отслеживания изменений
    db.save_schedule_cache(user_id, teacher_name, schedule)

    # Группируем по дням
    grouped = get_lessons_by_date(schedule)

    await status_msg.edit_text(
        f"✅ Преподаватель *{teacher_name}* установлен!\n\n"
        f"📊 Найдено *{len(schedule)}* занятий в расписании.\n"
        f"📅 Всего дней: *{len(grouped)}*\n\n"
        "Теперь я буду отслеживать изменения.",
        parse_mode="Markdown"
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

    # Показываем статус загрузки
    status_msg = await update.message.reply_text("⏳ Загрузка расписания...")

    schedule = get_schedule_for_teacher(teacher_name)

    if not schedule:
        await status_msg.edit_text("❌ Не удалось получить расписание.")
        return

    today_str = datetime.now().strftime("%Y-%m-%d")
    formatted = format_lessons_for_display(schedule, today_str)

    # Сохраняем кеш для отслеживания изменений
    db.save_schedule_cache(user_id, teacher_name, schedule)

    # Создаем клавиатуру
    keyboard = [
        [InlineKeyboardButton("📅 Завтра", callback_data="tomorrow")],
        [InlineKeyboardButton("📅 Неделя", callback_data="week")]
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

    # Показываем статус загрузки
    status_msg = await update.message.reply_text("⏳ Загрузка расписания...")

    schedule = get_schedule_for_teacher(teacher_name)

    if not schedule:
        await status_msg.edit_text("❌ Не удалось получить расписание.")
        return

    tomorrow_date = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
    formatted = format_lessons_for_display(schedule, tomorrow_date)

    # Создаем клавиатуру
    keyboard = [
        [InlineKeyboardButton("📅 Сегодня", callback_data="today")],
        [InlineKeyboardButton("📅 Неделя", callback_data="week")]
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

    # Показываем статус загрузки
    status_msg = await update.message.reply_text("⏳ Загрузка расписания...")

    schedule = get_schedule_for_teacher(teacher_name)

    if not schedule:
        await status_msg.edit_text("❌ Не удалось получить расписание.")
        return

    formatted = format_lessons_for_display(schedule)

    # Создаем клавиатуру
    keyboard = [
        [InlineKeyboardButton("📅 Сегодня", callback_data="today")],
        [InlineKeyboardButton("📅 Завтра", callback_data="tomorrow")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    # Если слишком много занятий, разбиваем на части
    if len(formatted) > 4000:
        parts = []
        current_part = "📚 Расписание на неделю:\n\n"
        for line in formatted.split('\n'):
            if len(current_part + line + '\n') > 4000:
                parts.append(current_part)
                current_part = "📚 Продолжение расписания:\n\n" + line + '\n'
            else:
                current_part += line + '\n'
        parts.append(current_part)

        # Отправляем первую часть с клавиатурой
        await status_msg.edit_text(parts[0], parse_mode="Markdown", reply_markup=reply_markup)

        # Отправляем остальные части без клавиатуры
        for part in parts[1:]:
            await update.message.reply_text(part, parse_mode="Markdown")
    else:
        await status_msg.edit_text(formatted, parse_mode="Markdown", reply_markup=reply_markup)

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
        [InlineKeyboardButton("❌ Удалить преподавателя", callback_data="remove_teacher")]
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
        [InlineKeyboardButton("❌ Удалить преподавателя", callback_data="remove_teacher")]
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

async def settings_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик кнопок в настройках"""
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    data = query.data

    if data == "notifications_on":
        db.toggle_notifications(user_id, True)
        await query.edit_message_text("🔔 Уведомления включены!")
        return

    if data == "notifications_off":
        db.toggle_notifications(user_id, False)
        await query.edit_message_text("🔕 Уведомления выключены!")
        return

    if data == "stats":
        await show_stats(query, context)
        return

    if data == "remove_teacher":
        db.add_user(user_id, query.from_user.username,
                    query.from_user.first_name,
                    query.from_user.last_name, "")
        await query.edit_message_text("✅ Преподаватель удален.")

async def show_stats(query, context=None):
    """Показывает статистику"""
    if query:
        user_id = query.from_user.id
    else:
        user_id = query.from_user.id

    stats = db.get_stats(user_id)

    if not stats:
        msg = "❌ Нет данных для статистики. Установи преподавателя."
        if query:
            await query.edit_message_text(msg)
        else:
            await query.message.reply_text(msg)
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

    if query:
        await query.edit_message_text(msg, parse_mode="Markdown")
    else:
        await query.message.reply_text(msg, parse_mode="Markdown")

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Помощь"""
    help_text = (
        "🤖 *Помощь по боту*\n\n"
        "📌 *Основные команды:*\n"
        "/start - Начать работу с ботом\n"
        "/set_teacher ФИО - Установить преподавателя\n"
        "/today - Расписание на сегодня\n"
        "/tomorrow - Расписание на завтра\n"
        "/week - Расписание на неделю\n"
        "/stats - Статистика\n"
        "/settings - Настройки\n"
        "/remove_teacher - Удалить преподавателя\n"
        "/help - Эта справка\n\n"
        "🔔 Бот автоматически отслеживает изменения в расписании и уведомляет тебя.\n\n"
        "📱 Используй кнопки для быстрого доступа к функциям."
    )
    await update.message.reply_text(help_text, parse_mode="Markdown")

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда для просмотра статистики"""
    user_id = update.effective_user.id
    stats = db.get_stats(user_id)

    if not stats:
        await update.message.reply_text("❌ Нет данных для статистики. Установи преподавателя.")
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

async def check_changes(context: ContextTypes.DEFAULT_TYPE):
    """Фоновая задача для проверки изменений в расписании"""
    logger.info("Проверка изменений в расписании...")

    users = db.get_all_users_with_teacher()
    for user_id, teacher_name in users:
        try:
            # Получаем текущее расписание (уже отфильтрованное)
            current_schedule = get_schedule_for_teacher(teacher_name)
            if not current_schedule:
                continue

            # Получаем сохраненное расписание
            cached_schedule, _ = db.get_schedule_cache(user_id)

            if cached_schedule:
                # Сравниваем UUID занятий
                current_uuids = {lesson['uuid'] for lesson in current_schedule}
                cached_uuids = {lesson['uuid'] for lesson in cached_schedule}

                # Находим изменения
                new_uuids = current_uuids - cached_uuids
                removed_uuids = cached_uuids - current_uuids

                # Сохраняем изменения в базу
                for uuid in new_uuids:
                    lesson = next(l for l in current_schedule if l['uuid'] == uuid)
                    db.add_change(user_id, 'added', uuid, lesson)

                for uuid in removed_uuids:
                    lesson = next(l for l in cached_schedule if l['uuid'] == uuid)
                    db.add_change(user_id, 'removed', uuid, lesson)

                # Если есть изменения, отправляем уведомление
                if new_uuids or removed_uuids:
                    await notify_user(context.bot, user_id)

            # Обновляем кеш
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
    # Токен бота
    token = os.getenv('TELEGRAM_BOT_TOKEN')

    if not token:
        print("❌ Ошибка: TELEGRAM_BOT_TOKEN не найден")
        return

    # Создаем папку для данных
    os.makedirs("data", exist_ok=True)

    # ЗАПУСКАЕМ FLASK В ОТДЕЛЬНОМ ПРОЦЕССЕ (не в потоке)
    import multiprocessing
    flask_process = multiprocessing.Process(target=run_flask, daemon=True)
    flask_process.start()
    logger.info("🌐 Flask сервер запущен для keep_alive в отдельном процессе")

    # Создаем приложение Telegram бота
    application = Application.builder().token(token).build()

    # Удаляем вебхук при запуске
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
    application.add_handler(CallbackQueryHandler(button_callback, pattern="^(today|tomorrow|week|set_teacher|settings|help)$"))
    application.add_handler(CallbackQueryHandler(settings_callback, pattern="^(notifications_on|notifications_off|stats|remove_teacher)$"))

    # Настройка фоновой задачи для проверки изменений (каждые 5 минут)
    job_queue = application.job_queue
    if job_queue:
        job_queue.run_repeating(check_changes, interval=300, first=10)
        logger.info("⏰ Фоновый планировщик запущен (проверка каждые 5 минут)")
    else:
        logger.warning("⚠️ JobQueue не доступен. Установите: pip install 'python-telegram-bot[job-queue]'")

    # ЗАПУСКАЕМ БОТА
    logger.info("🚀 Бот запущен и готов к работе!")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == '__main__':
    main()
