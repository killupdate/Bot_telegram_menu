import logging
import os
import signal
import threading
import time
from dataclasses import dataclass

from .api import APIError, Telegram
from .db import Store

LOG = logging.getLogger(__name__)
INTERESTS = {'price': 'Прайс', 'catalog': 'Каталог', 'delivery': 'Доставка',
             'terms': 'Условия сотрудничества'}
LABELS = {'price': '📦 Прайс', 'catalog': '🪑 Каталог', 'delivery': '🚚 Доставка',
          'terms': '🤝 Условия сотрудничества'}


@dataclass
class Config:
    token: str
    leads: int
    price_channel: int
    managers: set
    db_path: str = 'data/bot.sqlite3'
    price_file_id: str = ''
    dialog_ttl: int = 1800

    @classmethod
    def from_env(cls):
        token = os.environ.get('BOT_TOKEN', '').strip()
        leads = int(os.environ['LEADS_CHANNEL_ID'])
        managers = {int(x.strip()) for x in os.environ['MANAGER_IDS'].split(',') if x.strip()}
        ttl = int(os.environ.get('DIALOG_TTL_SECONDS', '1800'))
        price_channel = int(os.environ.get('PRICE_CHANNEL_ID') or leads)
        if not token or not managers or any(x <= 0 for x in managers) or leads >= 0 or price_channel >= 0 or ttl <= 0:
            raise ValueError('Check BOT_TOKEN, channel IDs, MANAGER_IDS and DIALOG_TTL_SECONDS')
        return cls(token, leads, price_channel, managers, os.environ.get('DB_PATH', 'data/bot.sqlite3'),
                   os.environ.get('PRICE_FILE_ID', ''), ttl)


class Bot:
    def __init__(self, cfg, store, api, clock=time.time):
        self.cfg, self.db, self.api, self.clock = cfg, store, api, clock
        self.update_id = 0
        if cfg.price_file_id and self.db.get('PRICE_FILE_ID') is None:
            self.db.set('PRICE_FILE_ID', cfg.price_file_id)

    def call(self, key, method, **payload):
        """Remember successful side effects until the current update is committed."""
        key = '%s:%s' % (self.update_id, key)
        saved = self.db.effect(key)
        if saved is not None:
            return saved
        result = self.api.call(method, **payload)
        self.db.save_effect(key, result)
        return result

    def send(self, key, chat, text, **kwargs):
        return self.call(key, 'sendMessage', chat_id=chat, text=text, **kwargs)

    def send_client(self, key, uid, text, **kwargs):
        try:
            return self.send(key, uid, text, **kwargs)
        except APIError as exc:
            if exc.transient:
                raise
            LOG.warning('Client acknowledgement unavailable, code=%s', exc.code)
            return None

    def buttons(self, uid):
        return {'inline_keyboard': [
            [{'text': '👤 Открыть пользователя', 'url': 'tg://user?id=%s' % uid}],
            [{'text': '✉️ Написать клиенту', 'callback_data': 'reply:%s' % uid}]]}

    def identity(self, uid):
        c = self.db.client(uid)
        username = '@' + c['username'] if c['username'] else 'отсутствует'
        return '👤 Username: %s\n🆔 Telegram ID: %s\n📦 Интерес: %s' % (
            username, uid, c['interest'] or 'Сообщение')

    def answer(self, query, text):
        try:
            self.api.call('answerCallbackQuery', callback_query_id=query['id'], text=text, show_alert=True)
        except APIError as exc:
            # Expired callback queries must not block delivery/state processing.
            if exc.code != 400:
                raise

    def handle(self, update):
        self.update_id = update['update_id']
        post = update.get('channel_post') or update.get('edited_channel_post')
        if post:
            self.channel_post(post)
            return
        query = update.get('callback_query')
        if query:
            self.callback(query)
            return
        msg = update.get('message')
        if not msg or msg['chat']['type'] != 'private' or not msg.get('from') or msg['from'].get('is_bot'):
            return
        uid = msg['from']['id']
        if uid in self.cfg.managers:
            self.manager_message(msg)
            return
        self.db.touch(msg['from'])
        text = msg.get('text', '')
        command = text.split()[0].split('@')[0] if text else ''
        if command == '/start':
            self.send_client('menu', uid, 'ОПТ МЕБЕЛЬ ЮГ\nВыберите, что вас интересует:',
                      reply_markup={'inline_keyboard': [[{'text': LABELS[k], 'callback_data': 'interest:' + k}]
                                                        for k in INTERESTS]})
        elif command == '/price':
            self.new_interest(uid, 'price')
        elif command.startswith('/'):
            self.send_client('help', uid, 'Нажмите /start для меню или напишите сообщение менеджеру.')
        else:
            self.client_message(msg)

    def channel_post(self, post):
        if post['chat']['id'] != self.cfg.price_channel or not post.get('document'):
            return
        if post.get('from', {}).get('is_bot') or post.get('via_bot'):
            return
        if post['chat']['id'] == self.cfg.leads and self.db.conn.execute(
                'SELECT 1 FROM own_posts WHERE message_id=?', (post['message_id'],)).fetchone():
            return
        # An edit of an older price must not replace a newer publication.
        previous = int(self.db.get('price_message_id', '0'))
        if post['message_id'] >= previous:
            with self.db.conn:
                self.db.conn.executemany('INSERT OR REPLACE INTO settings VALUES (?, ?)', [
                    ('PRICE_FILE_ID', post['document']['file_id']),
                    ('price_message_id', str(post['message_id']))])
            LOG.info('Price file updated')

    def callback(self, query):
        data = query.get('data', '')
        uid = query['from']['id']
        source = query.get('message', {}).get('chat', {})
        if data.startswith('reply:'):
            if uid not in self.cfg.managers or source.get('id') != self.cfg.leads:
                self.answer(query, 'Доступ только для менеджеров в канале «Заявки».')
                return
            try:
                client = int(data.split(':', 1)[1])
            except ValueError:
                self.answer(query, 'Некорректный клиент.')
                return
            if not self.db.client(client):
                self.answer(query, 'Клиент не найден.')
                return
            try:
                self.send('reply_prompt', uid, 'Ответ клиенту:\n' + self.identity(client) +
                          '\n\nСледующее сообщение будет отправлено этому клиенту. /cancel — отмена.')
            except APIError as exc:
                if exc.transient:
                    raise
                self.answer(query, 'Сначала откройте личный чат с ботом и нажмите /start. Затем повторите кнопку.')
                return
            self.db.select(uid, client, self.clock() + self.cfg.dialog_ttl)
            self.answer(query, 'Клиент выбран. Напишите ответ в личный чат с ботом.')
        elif data.startswith('interest:') and source.get('type') == 'private' and source.get('id') == uid:
            key = data.split(':', 1)[1]
            if key not in INTERESTS or uid in self.cfg.managers:
                self.answer(query, 'Недоступная команда.')
                return
            self.db.touch(query['from'])
            self.answer(query, 'Запрос принят.')
            self.new_interest(uid, key)
        else:
            self.answer(query, 'Откройте меню командой /start.')

    def new_interest(self, uid, key):
        now = self.clock()
        self.db.interest(uid, INTERESTS[key], now)
        if self.db.due(uid, now):
            self.send('interest_notice', self.cfg.leads, '🆕 НОВЫЙ ИНТЕРЕС\n\n' + self.identity(uid),
                      reply_markup=self.buttons(uid))
            self.db.execute('UPDATE clients SET last_notified_at=? WHERE telegram_user_id=?', (now, uid))
        if key == 'price':
            file_id = self.db.get('PRICE_FILE_ID')
            if file_id:
                try:
                    self.call('price', 'sendDocument', chat_id=uid, document=file_id)
                    return
                except APIError as exc:
                    if exc.transient:
                        raise
            self.send_client('price_unavailable', uid, 'Прайс пока недоступен. Менеджер получил ваш интерес; можете написать ему здесь.')
        else:
            self.send_client('interest_ack', uid, 'Ваш интерес: %s. Напишите вопрос здесь — передадим менеджеру.' % INTERESTS[key])

    def manager_message(self, msg):
        uid = msg['from']['id']
        text = msg.get('text', '')
        if text.startswith('/'):
            command = text.split()[0].split('@')[0]
            if command in ('/cancel', '/start'):
                self.db.cancel(uid)
                self.send('manager_help', uid, 'Режим ответа выключен. Выберите «✉️ Написать клиенту» в канале «Заявки».')
            else:
                self.send('manager_command', uid, 'Команды клиенту не отправляются. /cancel — отменить ответ.')
            return
        client = self.db.selected(uid, self.clock())
        if client is None:
            self.send('no_dialog', uid, 'Сначала выберите клиента кнопкой в канале «Заявки». Режим ответа мог истечь.')
            return
        try:
            self.call('manager_copy', 'copyMessage', chat_id=client, from_chat_id=uid, message_id=msg['message_id'])
        except APIError as exc:
            if exc.transient:
                raise
            self.send('delivery_failed', uid, 'Не удалось доставить сообщение (код %s). Возможно, клиент заблокировал бота или формат не поддерживается. Адресат сохранён: повторите сообщение или /cancel.' % exc.code)
            return
        # Clear only after delivery; stored effect prevents re-copy if confirmation fails.
        self.send('sent', uid, 'Сообщение отправлено клиенту %s. Для следующего ответа снова нажмите кнопку в канале.' % client)
        self.db.cancel(uid)

    def client_message(self, msg):
        uid = msg['from']['id']
        header = self.send('client_header', self.cfg.leads, '💬 СООБЩЕНИЕ КЛИЕНТА\n\n' + self.identity(uid),
                           reply_markup=self.buttons(uid))
        try:
            result = self.call('client_copy', 'copyMessage', chat_id=self.cfg.leads, from_chat_id=uid,
                              message_id=msg['message_id'], reply_markup=self.buttons(uid),
                              reply_parameters={'message_id': header['message_id']})
            self.db.execute('INSERT OR IGNORE INTO own_posts VALUES (?)', (result['message_id'],))
        except APIError as exc:
            if exc.transient:
                raise
            self.send_client('unsupported', uid, 'Не удалось передать сообщение. Пожалуйста, отправьте его текстом или обычным файлом.')
            self.send('copy_failed', self.cfg.leads, 'Содержимое сообщения клиента %s не удалось скопировать (код %s).' % (uid, exc.code))
            return
        self.send_client('client_ack', uid, 'Сообщение передано менеджеру.')


def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    cfg = Config.from_env()
    db = Store(cfg.db_path)
    api = Telegram(cfg.token)
    bot = Bot(cfg, db, api)
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    # Do not silently remove an existing n8n webhook.
    webhook = api.call('getWebhookInfo')
    if webhook.get('url'):
        raise RuntimeError('Active webhook found. Disable n8n and remove the webhook before starting polling; see README.')
    api.call('getMe')
    LOG.info('Bot started')
    delay = 1
    try:
        while not stop.is_set():
            try:
                updates = api.call('getUpdates', offset=int(db.get('offset', '0')), timeout=30,
                                   allowed_updates=['message', 'callback_query', 'channel_post', 'edited_channel_post'])
                for update in updates:
                    if stop.is_set():
                        break
                    bot.handle(update)
                    db.complete(update['update_id'])
                delay = 1
            except APIError as exc:
                LOG.error('Telegram error code=%s; update remains pending', exc.code)
                if exc.code in (401, 409):
                    raise RuntimeError('Invalid token or another polling instance; check configuration') from None
                stop.wait(max(delay, exc.retry_after))
                delay = min(delay * 2, 60)
    finally:
        db.conn.close()


if __name__ == '__main__':
    main()
