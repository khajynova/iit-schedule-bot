import sqlite3
import json
from datetime import datetime
import logging
import os

logger = logging.getLogger(__name__)

class Database:
    def __init__(self, db_path="data/schedule_bot.db"):
        # Создаем папку для базы данных
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
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
