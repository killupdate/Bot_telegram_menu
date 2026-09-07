import json
import sqlite3
from pathlib import Path


class Store:
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript('''
            PRAGMA journal_mode=WAL;
            PRAGMA foreign_keys=ON;
            PRAGMA busy_timeout=5000;
            CREATE TABLE IF NOT EXISTS clients (
                telegram_user_id INTEGER PRIMARY KEY, username TEXT,
                interest TEXT, last_interest_at REAL, last_notified_at REAL);
            CREATE TABLE IF NOT EXISTS dialogs (
                manager_id INTEGER PRIMARY KEY,
                client_id INTEGER NOT NULL REFERENCES clients(telegram_user_id),
                expires_at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS effects (
                key TEXT PRIMARY KEY, result TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS own_posts (message_id INTEGER PRIMARY KEY);
        ''')

    def execute(self, sql, args=()):
        with self.conn:
            return self.conn.execute(sql, args)

    def get(self, key, default=None):
        row = self.conn.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
        return row[0] if row else default

    def set(self, key, value):
        self.execute('INSERT OR REPLACE INTO settings VALUES (?, ?)', (key, str(value)))

    def client(self, uid):
        return self.conn.execute('SELECT * FROM clients WHERE telegram_user_id=?', (uid,)).fetchone()

    def touch(self, user):
        self.execute('''INSERT INTO clients (telegram_user_id, username) VALUES (?, ?)
            ON CONFLICT(telegram_user_id) DO UPDATE SET username=excluded.username''',
                     (user['id'], user.get('username')))

    def interest(self, uid, interest, now):
        self.execute('UPDATE clients SET interest=?, last_interest_at=? WHERE telegram_user_id=?',
                     (interest, now, uid))

    def due(self, uid, now):
        last = self.client(uid)['last_notified_at']
        return last is None or now - last >= 86400

    def select(self, manager, client, expires):
        self.execute('INSERT OR REPLACE INTO dialogs VALUES (?, ?, ?)', (manager, client, expires))

    def selected(self, manager, now):
        self.execute('DELETE FROM dialogs WHERE expires_at<=?', (now,))
        row = self.conn.execute('SELECT client_id FROM dialogs WHERE manager_id=?', (manager,)).fetchone()
        return row[0] if row else None

    def cancel(self, manager):
        self.execute('DELETE FROM dialogs WHERE manager_id=?', (manager,))

    def effect(self, key):
        row = self.conn.execute('SELECT result FROM effects WHERE key=?', (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def save_effect(self, key, result):
        self.execute('INSERT OR REPLACE INTO effects VALUES (?, ?)', (key, json.dumps(result)))

    def complete(self, update_id):
        with self.conn:
            self.conn.execute('INSERT OR REPLACE INTO settings VALUES (?, ?)', ('offset', str(update_id + 1)))
            self.conn.execute('DELETE FROM effects')
