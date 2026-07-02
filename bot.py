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
import time
from flask import Flask, Response
import urllib.parse
import re

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

# ============ FLASK KEEP-ALIVE ============
flask_app = Flask(__name__)

@flask_app.route('/')
@flask_app.route('/health')
def health_check():
    return "✅ Бот работает!", 200


# ============ ICS ЭНДПОИНТ ДЛЯ GOOGLE КАЛЕНДАРЯ ============
@flask_app.route('/schedule/<path:query>.ics')
def get_ics_calendar(query):
    """
    Генерирует ICS-файл для Google Календаря.
    """
    from icalendar import Calendar, Event
    from flask import Response

    # 1. Правильно декодируем запрос из URL
    try:
        search_query = urllib.parse.unquote(query)
    except Exception:
        search_query = query

    logger.info(f"📅 Запрос ICS для: {search_query}")

    try:
        # 2. Получаем РЕЗУЛЬТАТ, который уже отфильтрован функцией get_schedule_for_search
        lessons = get_schedule_for_search(search_query, page_limit=10)

        if not lessons:
            logger.warning(f"❌ Не найдено занятий для {search_query}")
            return f"Занятий для '{search_query}' не найдено", 404

        # 3. Создаем календарь и добавляем ВСЕ занятия из lessons (без дополнительной фильтрации)
        cal = Calendar()
        cal.add('prodid', '-//IIT Schedule Bot//iit.bsuir.by//')
        cal.add('version', '2.0')
        cal.add('calscale', 'GREGORIAN')
        cal.add('x-wr-calname', f'Расписание {search_query}')

        for lesson in lessons:
            location = lesson.get('room', '')
            start_datetime_str = f"{lesson['date']} {lesson['start_time']}"
            end_datetime_str = f"{lesson['date']} {lesson['end_time']}"

            start_dt = datetime.strptime(start_datetime_str, "%Y-%m-%d %H:%M")
            end_dt = datetime.strptime(end_datetime_str, "%Y-%m-%d %H:%M")

            event = Event()
            event.add('summary', lesson['info'])
            event.add('dtstart', start_dt)
            event.add('dtend', end_dt)
            event.add('location', location)
            event.add('description', f"Дата: {lesson['date']}\nВремя: {lesson['start_time']} - {lesson['end_time']}\nАудитория: {location}")

            cal.add_component(event)

        # 4. Генерируем ICS
        ics_content = cal.to_ical()

        # 5. Создаем ответ для скачивания
        response = Response(
            ics_content,
            mimetype='text/calendar; charset=utf-8',
            headers={
                'Content-Disposition': f'attachment; filename="{search_query}.ics"',
                'Content-Type': 'text/calendar; charset=utf-8',
                'Cache-Control': 'no-cache',
                'Access-Control-Allow-Origin': '*'
            }
        )

        logger.info(f"✅ Создан ICS для {search_query} ({len(cal.subcomponents)} событий)")
        return response

    except Exception as e:
        logger.error(f"❌ Ошибка создания ICS: {e}")
        return f"Ошибка: {e}", 500


# ============ ТЕСТОВЫЙ ICS ЭНДПОИНТ ============
@flask_app.route('/test.ics')
def test_ics():
    """Тестовый ICS для проверки"""
    from flask import Response
    test_content = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Test//EN
BEGIN:VEVENT
SUMMARY:Тестовое событие
DTSTART:20260702T190000
DTEND:20260702T200000
END:VEVENT
END:VCALENDAR"""
    response = Response(test_content, mimetype='text/calendar')
    response.headers['Content-Disposition'] = 'attachment; filename="test.ics"'
    return response


# ===========================================

def run_flask():
    """Запускает Flask сервер для keep-alive"""
    port = int(os.environ.get('PORT', 10000))
    flask_app.run(host='0.0.0.0', port=port, debug=False, threaded=True)

def keep_alive():
    """Пинг самого себя для поддержания активности"""
    while True:
        time.sleep(300)  # Каждые 5 минут
        try:
            port = int(os.environ.get('PORT', 10000))
            response = requests.get(f'http://localhost:{port}/health', timeout=5)
            logger.info(f"🔄 Keep-alive ping: {response.status_code}")
        except Exception as e:
            logger.error(f"Ошибка keep-alive: {e}")

# ===========================================

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

def get_schedule_for_group(group_name, page_limit=10, date_filter=None):
    """
    Получает расписание для группы.
    group_name - название группы (например, "60131")
    """
    url = "https://iit.bsuir.by/api/v1/content/schedule/"

    # Если фильтр по дате - загружаем только одну страницу (БЫСТРО)
    if date_filter:
        params = {
            "page": 1,
            "group": group_name
        }
        try:
            response = requests.get(url, params=params, timeout=10)
            response.raise_for_status()
            data = response.json()
            results = data.get("results", [])
            # Фильтруем по дате и группе
            filtered = [l for l in results if l.get("date") == date_filter and group_name in l.get("info", "")]
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
                "group": group_name
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

        # Фильтруем занятия - оставляем только те, где есть группа
        filtered_results = []
        for lesson in all_results:
            info = lesson.get("info", "")
            if group_name in info:
                filtered_results.append(lesson)

        logger.info(f"Найдено {len(filtered_results)} занятий для группы {group_name} (из {len(all_results)} всего)")
        return filtered_results

    except requests.exceptions.RequestException as e:
        logger.error(f"Ошибка запроса к API: {e}")
        return []

def get_schedule_for_search(search_query, page_limit=10, date_filter=None):
    """
    Универсальная функция - определяет, что искать автоматически
    Если запрос состоит только из цифр и дефисов - это группа, иначе преподаватель
    """
    # Очищаем запрос от лишних пробелов
    clean_query = search_query.strip()
    # Проверяем, состоит ли запрос только из цифр и дефисов (с возможными пробелами)
    clean_for_check = clean_query.replace(" ", "").replace("-", "")
    is_group = clean_for_check.isdigit()

    if is_group:
        return get_schedule_for_group(clean_query, page_limit, date_filter)
    else:
        return get_schedule_for_teacher(clean_query, page_limit, date_filter)

def get_cached_schedule(search_query, date_filter=None):
    """
    Получает расписание с кешированием для ускорения
    """
    cache_key = f"{search_query}_{date_filter if date_filter else 'all'}"

    # Проверяем кеш
    if cache_key in schedule_cache:
        cache_data, timestamp = schedule_cache[cache_key]
        if (datetime.now() - timestamp).seconds < CACHE_DURATION:
            logger.info(f"Используем кеш для {cache_key}")
            return cache_data

    # Загружаем свежие данные
    lessons = get_schedule_for_search(search_query, date_filter=date_filter)

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
    search_query = db_user[4] if db_user and db_user[4] else "❌ не установлен"

    # Определяем тип запроса для отображения
    if search_query != "❌ не установлен":
        clean_query = search_query.replace(" ", "").replace("-", "")
        if clean_query.isdigit():
            display_text = f"Группа: {search_query}"
        else:
            display_text = f"Преподаватель: {search_query}"
    else:
        display_text = "❌ не установлен"

    keyboard = [
        [InlineKeyboardButton("📚 Расписание", callback_data="schedule_menu")],
        [InlineKeyboardButton("⚙️ Настройки", callback_data="settings_menu")],
        [InlineKeyboardButton("📅 Google Календарь", callback_data="calendar_help")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    text = (
        f"👋 Главное меню\n\n"
        f"🔍 {display_text}\n\n"
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
            "❌ Сначала установи преподавателя или группу!\n\n"
            "Используй команды:\n"
            "/set_teacher Фамилия И.О.\n"
            "/set_group Номер_группы\n\n"
            "Примеры:\n"
            "/set_teacher Хаджинова Н.В.\n"
            "/set_group 60131",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return

    search_query = db_user[4]

    # Определяем тип запроса для отображения
    clean_query = search_query.replace(" ", "").replace("-", "")
    if clean_query.isdigit():
        display_text = f"Группа: {search_query}"
    else:
        display_text = f"Преподаватель: {search_query}"

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
        f"🔍 {display_text}",
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
            "❌ Сначала используй /start\n\n"
            "Или установи преподавателя/группу:\n"
            "/set_teacher Фамилия И.О.\n"
            "/set_group Номер_группы",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return

    search_query = db_user[4] if db_user[4] else "не установлен"
    notifications = "🔔 Включены" if db_user[5] == 1 else "🔕 Выключены"

    # Определяем тип запроса для отображения
    if search_query != "не установлен":
        clean_query = search_query.replace(" ", "").replace("-", "")
        if clean_query.isdigit():
            display_text = f"Группа: {search_query}"
        else:
            display_text = f"Преподаватель: {search_query}"
    else:
        display_text = "не установлен"

    keyboard = [
        [InlineKeyboardButton("🔔 Включить уведомления", callback_data="notifications_on")],
        [InlineKeyboardButton("🔕 Выключить уведомления", callback_data="notifications_off")],
        [InlineKeyboardButton("❌ Удалить преподавателя/группу", callback_data="remove_teacher")],
        [InlineKeyboardButton("🔙 В меню", callback_data="back_to_main")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_text(
        f"⚙️ *Настройки*\n\n"
        f"🔍 {display_text}\n"
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
            "Я бот для отслеживания расписания ИИТ БГУИР.\n\n"
            "📌 Установи преподавателя или группу:\n"
            "/set_teacher Фамилия И.О. - преподаватель\n"
            "/set_group Номер_группы - группа\n\n"
            "Примеры:\n"
            "/set_teacher Хаджинова Н.В.\n"
            "/set_group 60131\n\n"
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

    teacher_name = " ".join(context.args).strip()

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

# ============ УСТАНОВКА ГРУППЫ ============
async def set_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Установка группы для отслеживания"""
    user_id = update.effective_user.id

    if not context.args:
        await update.message.reply_text(
            "❌ Пожалуйста, укажи номер группы.\n"
            "Пример: /set_group 60131"
        )
        return

    group_name = " ".join(context.args).strip()

    status_msg = await update.message.reply_text("⏳ Поиск расписания...")

    schedule = get_schedule_for_group(group_name, page_limit=3)

    if not schedule:
        await status_msg.edit_text(
            f"❌ Группа '{group_name}' не найдена.\n"
            "Проверь правильность написания."
        )
        return

    db.add_user(user_id, update.effective_user.username,
                update.effective_user.first_name,
                update.effective_user.last_name,
                group_name)

    db.save_schedule_cache(user_id, group_name, schedule)

    grouped = get_lessons_by_date(schedule)

    await status_msg.edit_text(
        f"✅ Группа *{group_name}* установлена!\n\n"
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

    if data == "calendar_help":
        await calendar_help(update, context)
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
        await asyncio.sleep(0.5)
        await settings_menu(update, context)
        return

    if data == "notifications_off":
        user_id = query.from_user.id
        db.toggle_notifications(user_id, False)
        await query.edit_message_text("🔕 Уведомления выключены!")
        await asyncio.sleep(0.5)
        await settings_menu(update, context)
        return

    if data == "remove_teacher":
        user_id = query.from_user.id
        db.add_user(user_id, query.from_user.username,
                    query.from_user.first_name,
                    query.from_user.last_name, "")
        await query.edit_message_text("✅ Преподаватель/группа удалены!")
        await asyncio.sleep(0.5)
        await main_menu(update, context, query.from_user.id)
        return

# ============ ПОМОЩЬ ПО КАЛЕНДАРЮ ============
async def calendar_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает инструкцию по добавлению в Google Календарь"""
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    db_user = db.get_user(user_id)

    if not db_user or not db_user[4]:
        await query.edit_message_text(
            "❌ Сначала установи преподавателя или группу!\n\n"
            "Используй команды:\n"
            "/set_teacher Фамилия И.О.\n"
            "/set_group Номер_группы"
        )
        return

    search_query = db_user[4]
    encoded_query = urllib.parse.quote(search_query)
    ics_url = f"https://iit-schedule-bot.onrender.com/schedule/{encoded_query}.ics"

    help_text = (
        f"📅 *Google Календарь*\n\n"
        f"Чтобы добавить расписание *{search_query}* в Google Календарь:\n\n"
        f"1️⃣ Скопируй ссылку:\n"
        f"`{ics_url}`\n\n"
        f"2️⃣ Открой Google Календарь\n"
        f"3️⃣ Нажми ➕ рядом с 'Другие календари'\n"
        f"4️⃣ Выбери 'Добавить по URL'\n"
        f"5️⃣ Вставь ссылку и нажми 'Добавить календарь'\n\n"
        f"✅ После добавления расписание будет обновляться автоматически!"
    )

    keyboard = [[InlineKeyboardButton("🔙 В меню", callback_data="back_to_main")]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_text(
        help_text,
        parse_mode="Markdown",
        reply_markup=reply_markup
    )

# ============ ФУНКЦИИ РАСПИСАНИЯ ============
async def today_callback(query, context):
    """Показать расписание на сегодня"""
    user_id = query.from_user.id
    db_user = db.get_user(user_id)

    if not db_user or not db_user[4]:
        await query.edit_message_text("❌ Сначала установи преподавателя или группу.")
        return

    search_query = db_user[4]

    await query.edit_message_text("⏳ Загрузка...")

    today_str = datetime.now().strftime("%Y-%m-%d")
    schedule = get_cached_schedule(search_query, date_filter=today_str)

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
        await query.edit_message_text("❌ Сначала установи преподавателя или группу.")
        return

    search_query = db_user[4]

    await query.edit_message_text("⏳ Загрузка...")

    tomorrow_date = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
    schedule = get_cached_schedule(search_query, date_filter=tomorrow_date)

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
        await query.edit_message_text("❌ Сначала установи преподавателя или группу.")
        return

    search_query = db_user[4]

    await query.edit_message_text("⏳ Загрузка...")

    schedule = get_schedule_for_search(search_query, page_limit=5)

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
        await query.edit_message_text("❌ Сначала установи преподавателя или группу.")
        return

    search_query = db_user[4]

    await query.edit_message_text("⏳ Загрузка...")

    schedule = get_schedule_for_search(search_query, page_limit=10)

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
        await query.edit_message_text("❌ Сначала установи преподавателя или группу.")
        return

    search_query = db_user[4]

    await query.edit_message_text("⏳ Загрузка...")

    schedule = get_schedule_for_search(search_query, page_limit=10)

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
        await update.message.reply_text("❌ Сначала установи преподавателя или группу.")
        return

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
        await update.message.reply_text("❌ Сначала установи преподавателя или группу.")
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
        await update.message.reply_text("❌ Сначала установи преподавателя или группу.")
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
        "✅ Преподаватель/группа удалены.\n"
        "Используй /set_teacher или /set_group чтобы установить новый."
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
        "/set_group Номер_группы - Установить группу\n"
        "/today - Расписание на сегодня\n"
        "/tomorrow - Расписание на завтра\n"
        "/week - Расписание на неделю\n"
        "/settings - Настройки\n"
        "/remove_teacher - Удалить преподавателя/группу\n"
        "/help - Эта справка\n\n"
        "📱 Используй кнопки для быстрого доступа.\n\n"
        "Примеры:\n"
        "/set_teacher Хаджинова Н.В.\n"
        "/set_group 60131"
    )
    await update.message.reply_text(help_text, parse_mode="Markdown")

# ============ ФОНОВЫЕ ЗАДАЧИ ============
async def check_changes(context: ContextTypes.DEFAULT_TYPE):
    """Фоновая задача для проверки изменений в расписании"""
    logger.info("Проверка изменений в расписании...")

    users = db.get_all_users_with_teacher()
    for user_id, search_query in users:
        try:
            current_schedule = get_schedule_for_search(search_query, page_limit=5)
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

            db.save_schedule_cache(user_id, search_query, current_schedule)

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

    # Запускаем Flask в отдельном потоке
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    logger.info("🌐 Flask сервер запущен для keep-alive")

    # Запускаем keep-alive пинг в отдельном потоке
    keep_alive_thread = threading.Thread(target=keep_alive, daemon=True)
    keep_alive_thread.start()
    logger.info("🔄 Keep-alive пинг запущен")

    # Создаем новый event loop для Python 3.14
    try:
        loop = asyncio.get_event_loop()
        if loop.is_closed():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

    application = Application.builder().token(token).build()

    # Удаляем вебхук при запуске
    try:
        loop.run_until_complete(reset_webhook(application))
        logger.info("🔗 Вебхук успешно сброшен")
    except Exception as e:
        logger.error(f"Ошибка при удалении вебхука: {e}")

    # Регистрируем обработчики команд
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("set_teacher", set_teacher))
    application.add_handler(CommandHandler("set_group", set_group))
    application.add_handler(CommandHandler("today", today))
    application.add_handler(CommandHandler("tomorrow", tomorrow))
    application.add_handler(CommandHandler("week", week))
    application.add_handler(CommandHandler("remove_teacher", remove_teacher))
    application.add_handler(CommandHandler("settings", settings))
    application.add_handler(CommandHandler("help", help_command))

    # Регистрируем обработчики кнопок
    application.add_handler(CallbackQueryHandler(button_callback, pattern="^(schedule_menu|settings_menu|back_to_main|today|tomorrow|week|month|all_schedule|notifications_on|notifications_off|remove_teacher|calendar_help)$"))

    # Настройка фоновой задачи для проверки изменений (каждые 5 минут)
    job_queue = application.job_queue
    if job_queue:
        job_queue.run_repeating(check_changes, interval=300, first=10)
        logger.info("⏰ Фоновый планировщик запущен (проверка каждые 5 минут)")

    # Очистка старых уведомлений при запуске
    try:
        conn = db.get_connection()
        cursor = conn.cursor()
        cursor.execute("UPDATE schedule_changes SET notified = 1 WHERE notified = 0")
        conn.commit()
        conn.close()
        logger.info("✅ Старые уведомления очищены")
    except Exception as e:
        logger.error(f"Ошибка очистки уведомлений: {e}")

    logger.info("🚀 Бот запущен и готов к работе!")

    # Запускаем бота
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == '__main__':
    main()
