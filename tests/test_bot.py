import tempfile
import unittest
from pathlib import Path

from bot.api import APIError
from bot.app import Bot, Config
from bot.db import Store


class FakeAPI:
    def __init__(self):
        self.calls = []
        self.fail = None
        self.mid = 100

    def call(self, method, **payload):
        if self.fail and self.fail(method, payload):
            raise APIError(self.code)
        self.calls.append((method, payload))
        self.mid += 1
        return True if method == 'answerCallbackQuery' else {'message_id': self.mid}


class BotTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / 'db.sqlite3')
        self.db = Store(self.path)
        self.api = FakeAPI()
        self.now = 100000
        self.cfg = Config('fake', -1001, -1001, {10, 11}, self.path, 'initial')
        self.bot = Bot(self.cfg, self.db, self.api, lambda: self.now)
        self.seq = 0

    def tearDown(self):
        self.db.conn.close()
        self.tmp.cleanup()

    def handle(self, **payload):
        self.seq += 1
        update = dict(update_id=self.seq, **payload)
        self.bot.handle(update)
        self.db.complete(self.seq)
        return update

    def msg(self, uid=20, text='Здравствуйте', **extra):
        message = {'message_id': self.seq + 1, 'chat': {'id': uid, 'type': 'private'},
                   'from': {'id': uid}, 'text': text}
        message.update(extra)
        return message

    def callback(self, data, uid=20, channel=None):
        return {'id': str(self.seq), 'from': {'id': uid}, 'data': data,
                'message': {'chat': {'id': channel or uid, 'type': 'channel' if channel else 'private'}}}

    def interest(self, uid=20, key='price'):
        self.handle(callback_query=self.callback('interest:' + key, uid))

    def select(self, uid=10, client=20):
        self.handle(callback_query=self.callback('reply:%s' % client, uid, -1001))

    def calls(self, method):
        return [p for m, p in self.api.calls if m == method]

    def notices(self):
        return [p for p in self.calls('sendMessage') if p['text'].startswith('🆕')]

    def test_start_no_interest(self):
        self.handle(message=self.msg(text='/start'))
        self.assertIsNotNone(self.db.client(20))
        self.assertIsNone(self.db.client(20)['last_interest_at'])
        self.assertFalse(self.notices())

    def test_missing_username_and_buttons(self):
        self.interest()
        notice = self.notices()[0]
        self.assertIn('Username: отсутствует', notice['text'])
        self.assertIn('Telegram ID: 20', notice['text'])
        self.assertEqual(notice['reply_markup']['inline_keyboard'][0][0]['url'], 'tg://user?id=20')
        self.assertEqual(notice['reply_markup']['inline_keyboard'][1][0]['callback_data'], 'reply:20')

    def test_username_updated_and_removed(self):
        self.handle(message=self.msg(text='/start', **{'from': {'id': 20, 'username': 'client'}}))
        self.assertIn('@client', self.bot.identity(20))
        self.handle(message=self.msg(text='/start'))
        self.assertIn('отсутствует', self.bot.identity(20))

    def test_dedup_boundary_and_price_always_sent(self):
        self.interest()
        self.now += 86399
        self.interest(key='catalog')
        self.assertEqual(len(self.notices()), 1)
        self.assertEqual(self.db.client(20)['last_interest_at'], self.now)
        self.assertEqual(self.db.client(20)['interest'], 'Каталог')
        self.now += 1
        self.interest()
        self.assertEqual(len(self.notices()), 2)
        self.assertEqual(len(self.calls('sendDocument')), 2)

    def test_dialog_one_message_and_isolation(self):
        self.interest(20)
        self.interest(21)
        self.select(10, 20)
        self.select(11, 21)
        self.handle(message=self.msg(10, 'Ответ первому'))
        self.handle(message=self.msg(11, 'Ответ второму'))
        self.assertEqual([p['chat_id'] for p in self.calls('copyMessage')], [20, 21])
        self.assertIsNone(self.db.selected(10, self.now))
        self.handle(message=self.msg(10, 'Не должно уйти'))
        self.assertEqual(len(self.calls('copyMessage')), 2)

    def test_forbidden_manager_and_wrong_channel(self):
        self.interest()
        self.select(99)
        self.handle(callback_query=self.callback('reply:20', 10, -999))
        self.assertIsNone(self.db.selected(99, self.now))
        self.assertIsNone(self.db.selected(10, self.now))

    def test_manager_must_start_bot(self):
        self.interest()
        self.api.code = 403
        self.api.fail = lambda m, p: m == 'sendMessage' and p['chat_id'] == 10
        self.select()
        self.assertIsNone(self.db.selected(10, self.now))

    def test_cancel_and_expiry(self):
        self.interest()
        self.select()
        self.handle(message=self.msg(10, '/cancel'))
        self.assertIsNone(self.db.selected(10, self.now))
        self.select()
        self.now += self.cfg.dialog_ttl
        self.handle(message=self.msg(10, 'Поздно'))
        self.assertFalse(self.calls('copyMessage'))

    def test_blocked_client_keeps_recipient(self):
        self.interest()
        self.select()
        self.api.code = 403
        self.api.fail = lambda m, p: m == 'copyMessage'
        self.handle(message=self.msg(10, 'Ответ'))
        self.assertEqual(self.db.selected(10, self.now), 20)
        self.assertTrue(any('Не удалось доставить' in p['text'] for p in self.calls('sendMessage')))

    def test_client_replies_not_deduplicated(self):
        self.interest()
        self.handle(message=self.msg(text='Первый вопрос'))
        self.handle(message=self.msg(text='Второй вопрос'))
        headers = [p for p in self.calls('sendMessage') if p['text'].startswith('💬')]
        self.assertEqual(len(headers), 2)
        for p in headers:
            self.assertIn('Telegram ID: 20', p['text'])
            self.assertIn('reply_markup', p)
        self.assertEqual(len(self.calls('copyMessage')), 2)

    def test_price_source_and_persistence(self):
        def post(chat, mid, fid):
            return {'chat': {'id': chat}, 'message_id': mid, 'document': {'file_id': fid}}
        self.handle(channel_post=post(-777, 10, 'wrong'))
        self.assertEqual(self.db.get('PRICE_FILE_ID'), 'initial')
        self.handle(channel_post=post(-1001, 11, 'new'))
        self.handle(edited_channel_post=post(-1001, 9, 'old'))
        self.assertEqual(self.db.get('PRICE_FILE_ID'), 'new')
        self.interest()
        self.assertEqual(self.calls('sendDocument')[-1]['document'], 'new')
        self.select()
        self.db.conn.close()
        self.db = Store(self.path)
        self.bot = Bot(self.cfg, self.db, self.api, lambda: self.now)
        self.assertEqual(self.db.get('PRICE_FILE_ID'), 'new')
        self.assertEqual(self.db.selected(10, self.now), 20)
        self.assertFalse(self.db.due(20, self.now))

    def test_client_document_never_becomes_price(self):
        self.handle(message=self.msg(document={'file_id': 'client_file'}, text=''))
        copied = self.db.conn.execute('SELECT message_id FROM own_posts').fetchone()[0]
        self.handle(channel_post={'chat': {'id': -1001}, 'message_id': copied,
                                  'document': {'file_id': 'client_file'}})
        self.assertEqual(self.db.get('PRICE_FILE_ID'), 'initial')

    def test_failed_notification_does_not_use_dedup_window(self):
        self.db.touch({'id': 20})
        self.api.code = 500
        self.api.fail = lambda m, p: m == 'sendMessage' and p['chat_id'] == -1001
        with self.assertRaises(APIError):
            self.interest()
        self.assertTrue(self.db.due(20, self.now))
        self.api.fail = None
        self.interest()
        self.assertEqual(len(self.notices()), 1)

    def test_retry_does_not_recopy_after_confirmation_failure(self):
        self.interest()
        self.select()
        self.seq += 1
        update = {'update_id': self.seq, 'message': self.msg(10, 'Ответ')}
        self.api.code = 500
        self.api.fail = lambda m, p: m == 'sendMessage' and p['text'].startswith('Сообщение отправлено')
        with self.assertRaises(APIError):
            self.bot.handle(update)
        self.api.fail = None
        self.db.conn.close()
        self.db = Store(self.path)
        self.bot = Bot(self.cfg, self.db, self.api, lambda: self.now)
        self.bot.handle(update)
        self.db.complete(self.seq)
        self.assertEqual(len(self.calls('copyMessage')), 1)
        self.assertIsNone(self.db.selected(10, self.now))

    def test_media_delivery(self):
        self.interest()
        self.select()
        self.handle(message=self.msg(10, '', document={'file_id': 'document'}))
        self.assertEqual(self.calls('copyMessage')[0]['chat_id'], 20)

    def test_blocked_ack_does_not_block_queue(self):
        self.api.code = 403
        self.api.fail = lambda m, p: m == 'sendMessage' and p['chat_id'] == 20
        self.handle(message=self.msg())
        self.assertEqual(len(self.calls('copyMessage')), 1)
        self.assertEqual(self.db.get('offset'), str(self.seq + 1))

    def test_non_private_messages_ignored(self):
        self.handle(message=self.msg(chat={'id': -123, 'type': 'supergroup'}))
        self.assertFalse(self.api.calls)
        self.assertIsNone(self.db.client(20))

    def test_no_price(self):
        self.db.set('PRICE_FILE_ID', '')
        self.interest()
        self.assertFalse(self.calls('sendDocument'))
        self.assertTrue(any('Прайс пока недоступен' in p['text'] for p in self.calls('sendMessage')))


if __name__ == '__main__':
    unittest.main()
