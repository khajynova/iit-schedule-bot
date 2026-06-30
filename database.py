# database.py - улучшенная версия с SQLite
import sqlite3
import json
from datetime import datetime
import logging

logger = logging.getLogger(__name__)

class Database:
    def __init__(self, db_path="data/schedule_bot.db"):
        self.db_path = db_path
        self.init_database()

    def get_connection(self):
        """Создает соединение с базой данных"""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def init_database(self):
        """Инициализирует базу данных с оптимизированными индексами"""
        conn = self.get_connection()
        try:
            cursor = conn.cursor()

            # Включаем поддержку внешних ключей
            cursor.execute("PRAGMA foreign_keys = ON")

            # Создаем таблицы
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    username TEXT,
                    first_name TEXT,
                    last_name TEXT,
                    teacher_name TEXT,
                    registered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    notification_enabled BOOLEAN DEFAULT 1,
                    CHECK (notification_enabled IN (0, 1))
                )
            ''')

            cursor.execute('''
                CREATE TABLE IF NOT EXISTS schedule_cache (
                    user_id INTEGER PRIMARY KEY,
                    teacher_name TEXT,
                    schedule_data TEXT,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (user_id) REFERENCES users (user_id) ON DELETE CASCADE
                )
            ''')

            cursor.execute('''
                CREATE TABLE IF NOT EXISTS schedule_changes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    change_type TEXT CHECK(change_type IN ('added', 'removed')),
                    lesson_uuid TEXT,
                    lesson_data TEXT,
                    detected_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    notified BOOLEAN DEFAULT 0,
                    FOREIGN KEY (user_id) REFERENCES users (user_id) ON DELETE CASCADE
                )
            ''')

            # Создаем индексы для ускорения запросов
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_changes_notified ON schedule_changes(user_id, notified)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_changes_detected ON schedule_changes(detected_at)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_cache_updated ON schedule_cache(updated_at)')

            conn.commit()
            logger.info("База данных инициализирована успешно")
        except Exception as e:
            logger.error(f"Ошибка инициализации базы данных: {e}")
            raise
        finally:
            conn.close()

    def add_user(self, user_id, username, first_name, last_name, teacher_name):
        """Добавляет или обновляет пользователя"""
        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT OR REPLACE INTO users
                (user_id, username, first_name, last_name, teacher_name)
                VALUES (?, ?, ?, ?, ?)
            ''', (user_id, username, first_name, last_name, teacher_name))
            conn.commit()
            return True
        except Exception as e:
            logger.error(f"Ошибка добавления пользователя: {e}")
            return False
        finally:
            conn.close()

    def get_user(self, user_id):
        """Получает данные пользователя"""
        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute('SELECT * FROM users WHERE user_id = ?', (user_id,))
            return cursor.fetchone()
        except Exception as e:
            logger.error(f"Ошибка получения пользователя: {e}")
            return None
        finally:
            conn.close()

    def get_all_users_with_teacher(self):
        """Получает всех пользователей с установленным преподавателем"""
        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute('''
                SELECT user_id, teacher_name
                FROM users
                WHERE teacher_name IS NOT NULL AND teacher_name != ''
                AND notification_enabled = 1
            ''')
            return cursor.fetchall()
        except Exception as e:
            logger.error(f"Ошибка получения списка пользователей: {e}")
            return []
        finally:
            conn.close()

    def save_schedule_cache(self, user_id, teacher_name, schedule_data):
        """Сохраняет кеш расписания"""
        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            schedule_json = json.dumps(schedule_data, ensure_ascii=False)
            cursor.execute('''
                INSERT OR REPLACE INTO schedule_cache
                (user_id, teacher_name, schedule_data, updated_at)
                VALUES (?, ?, ?, CURRENT_TIMESTAMP)
            ''', (user_id, teacher_name, schedule_json))
            conn.commit()
            return True
        except Exception as e:
            logger.error(f"Ошибка сохранения кеша: {e}")
            return False
        finally:
            conn.close()

    def get_schedule_cache(self, user_id):
        """Получает кеш расписания"""
        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute('''
                SELECT schedule_data, updated_at
                FROM schedule_cache
                WHERE user_id = ?
            ''', (user_id,))
            result = cursor.fetchone()
            if result:
                return json.loads(result['schedule_data']), result['updated_at']
            return None, None
        except Exception as e:
            logger.error(f"Ошибка получения кеша: {e}")
            return None, None
        finally:
            conn.close()

    def add_change(self, user_id, change_type, lesson_uuid, lesson_data):
        """Добавляет запись об изменении"""
        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO schedule_changes
                (user_id, change_type, lesson_uuid, lesson_data)
                VALUES (?, ?, ?, ?)
            ''', (user_id, change_type, lesson_uuid, json.dumps(lesson_data, ensure_ascii=False)))
            conn.commit()
            return True
        except Exception as e:
            logger.error(f"Ошибка добавления изменения: {e}")
            return False
        finally:
            conn.close()

    def get_unnotified_changes(self, user_id):
        """Получает неотправленные изменения"""
        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute('''
                SELECT * FROM schedule_changes
                WHERE user_id = ? AND notified = 0
                ORDER BY detected_at
            ''', (user_id,))
            return cursor.fetchall()
        except Exception as e:
            logger.error(f"Ошибка получения изменений: {e}")
            return []
        finally:
            conn.close()

    def mark_changes_notified(self, user_id):
        """Отмечает изменения как отправленные"""
        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute('''
                UPDATE schedule_changes
                SET notified = 1
                WHERE user_id = ? AND notified = 0
            ''', (user_id,))
            conn.commit()
            return True
        except Exception as e:
            logger.error(f"Ошибка отметки изменений: {e}")
            return False
        finally:
            conn.close()

    def toggle_notifications(self, user_id, enabled):
        """Включает/выключает уведомления"""
        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute('''
                UPDATE users
                SET notification_enabled = ?
                WHERE user_id = ?
            ''', (1 if enabled else 0, user_id))
            conn.commit()
            return True
        except Exception as e:
            logger.error(f"Ошибка изменения настроек уведомлений: {e}")
            return False
        finally:
            conn.close()
