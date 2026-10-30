"""Bounded polling worker for GitHub Actions; Arabic, single-owner bot."""
import argparse
import getpass
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import requests
from scraper import (Fetcher, listing, article, fields, is_job, digest, norm,
                     public_channel, parse_date)
from storage import open_store

LOG = logging.getLogger('monitor')
TZ = ZoneInfo('Asia/Baghdad')


def now():
    return datetime.now(TZ).isoformat(timespec='seconds')


def today():
    return datetime.now(TZ).date().isoformat()


class TelegramRejected(Exception):
    """Explicit Telegram refusal: the message was not accepted."""


class TelegramUnknown(Exception):
    """Delivery outcome uncertain. Never retry automatically."""


class Telegram:
    def __init__(self, token, chat_id):
        if not token or not chat_id:
            raise RuntimeError('أضف TELEGRAM_BOT_TOKEN وTELEGRAM_CHAT_ID إلى Secrets')
        self.base = 'https://api.telegram.org/bot' + token + '/'
        self.chat_id = str(chat_id)
        self.session = requests.Session()

    def call(self, method, payload):
        for attempt in range(3):
            try:
                r = self.session.post(self.base + method, json=payload, timeout=(8, 20))
                data = r.json()
            except (requests.RequestException, ValueError):
                raise TelegramUnknown('تعذر تأكيد استجابة تيليگرام') from None
            if data.get('ok') is True:
                return data['result']
            if data.get('error_code') == 429:
                delay = data.get('parameters', {}).get('retry_after', 5)
                if attempt < 2 and delay <= 30:
                    time.sleep(delay)
                    continue
                raise TelegramRejected('تيليگرام طلب تأجيل الإرسال')
            if r.status_code >= 500:
                raise TelegramUnknown('خطأ تيليگرام؛ حالة التسليم غير مؤكدة')
            raise TelegramRejected('رفض Telegram API الطلب: ' + str(data.get('error_code', r.status_code)))
        raise TelegramRejected('عدد محاولات محدود')

    def send(self, text, keyboard=False):
        payload = {'chat_id': self.chat_id, 'text': text[:4000],
                   'link_preview_options': {'is_disabled': True}}
        if keyboard:
            payload['reply_markup'] = {'inline_keyboard': [
                [{'text': '📍 البصرة', 'callback_data': '/basra'}, {'text': '🇮🇶 العراق', 'callback_data': '/iraq'}],
                [{'text': '🆕 اليوم', 'callback_data': '/today'}, {'text': '⚙️ الحالة', 'callback_data': '/status'}]]}
        return self.call('sendMessage', payload)['message_id']

    def updates(self, offset):
        return self.call('getUpdates', {'offset': offset, 'limit': 100, 'timeout': 0,
            'allowed_updates': ['message', 'callback_query', 'channel_post', 'edited_channel_post']})


def ad_message(ad, update=False):
    title = '🔄 تحديث مهم لإعلان تعيين حكومي' if update else '🔔 تعيين حكومي جديد'
    parts = [title, f"🏢 الجهة المعلنة: {ad['issuer']}", f"📍 المحافظة: {ad['province']}",
             f"💼 عنوان التعيين: {ad['title'][:450]}", f"🎓 المؤهل المطلوب: {ad['qualification']}",
             f"📅 تاريخ الإعلان: {ad['published']}", f"⏳ آخر موعد للتقديم: {ad['deadline']}",
             f"📝 طريقة التقديم: {ad['method']}", '🔗 رابط الإعلان الرسمي:\n' + ad['url']]
    if ad.get('warnings'):
        parts.append('📎 ' + '؛ '.join(ad['warnings']))
    parts.append('يرجى مراجعة النص الرسمي قبل التقديم.')
    return '\n\n'.join(parts)[:4000]


def enqueue(store, event_id, text, priority=1):
    if event_id not in store.state['outbox']:
        store.state['outbox'][event_id] = {'status': 'pending', 'text': text,
            'created': now(), 'priority': priority, 'attempts': 0}


def fresh(date):
    if not date:
        return False
    age = (datetime.now(TZ).date() - datetime.fromisoformat(date).date()).days
    return 0 <= age <= 7


def record_ad(store, source, doc, baseline=False):
    state = store.state
    ident = digest(source['id'] + '|' + doc['url'])
    previous = state['ads'].get(ident)
    if not is_job(doc['title'], doc['text']) and not previous:
        return False
    ad = fields(doc, source)
    # Previously confirmed jobs can become cancelled/closed: preserve as an update.
    if previous:
        changed = previous['signature'] != ad['signature']
        ad['first_seen'] = previous['first_seen']
        ad['eligible'] = previous.get('eligible', False)
        ad['baseline'] = previous.get('baseline', False)
        ad['last_seen'] = now()
        if changed and not baseline and ad['eligible']:
            ad['baseline'] = False
            enqueue(store, ident + ':' + ad['signature'], ad_message(ad, True),
                    0 if 'البصرة' in ad['province'] else 1)
    else:
        # Exact-content and canonical-link repost dedupe across configured sources.
        duplicate = any(x['fingerprint'] == ad['fingerprint'] or x['url'] == ad['url']
                        for x in state['ads'].values())
        ad.update(first_seen=now(), last_seen=now(), baseline=baseline,
                  eligible=fresh(doc.get('date')) and not duplicate)
        if not baseline and ad['eligible']:
            enqueue(store, ident + ':' + ad['signature'], ad_message(ad),
                    0 if 'البصرة' in ad['province'] else 1)
        elif not baseline and not duplicate:
            state['review'][ident] = {'title': ad['title'], 'url': ad['url'],
                'reason': 'تاريخ مفقود أو قديم؛ لم يرسل إشعار', 'at': now()}
    state['ads'][ident] = ad
    return True


def monitor_sources(store, config, fetcher):
    successful = 0
    for source in sorted(config['sources'], key=lambda s: s.get('priority', 1)):
        if not source.get('enabled'):
            continue
        status = store.state['sources'].setdefault(source['id'], {'initialized': False, 'seen': {}})
        status['attempt'] = now()
        docs, failed = [], False
        try:
            items = listing(fetcher, source)
            for item in items:
                key = digest(item['url'])
                known_job = any(a['url'] == item['url'] for a in store.state['ads'].values())
                # Revisit known jobs each run; unchanged non-job articles need no repeated fetch.
                if status['seen'].get(key) == digest(item['title']) and not known_job:
                    continue
                try:
                    doc = article(fetcher, source, item)
                    docs.append((key, item, doc))
                except Exception:
                    failed = True
                    LOG.warning('تعذرت قراءة مقال من %s', source['name'])
        except Exception:
            failed = True
        # First-source baseline completes only after a fully successful listing/detail pass.
        baseline = not status['initialized']
        for key, item, doc in docs:
            record_ad(store, source, doc, baseline=baseline)
            status['seen'][key] = digest(item['title'])
        # Limit non-job URL cache; ad identities/fingerprints are never pruned automatically.
        if len(status['seen']) > 1500:
            status['seen'] = dict(list(status['seen'].items())[-1000:])
        status['error'] = 'فحص جزئي أو مصدر متعطل؛ راجع البنية/الحماية والسجلات' if failed else None
        if not failed:
            status['initialized'] = True
            status['success'] = now()
            successful += 1
        store.save()  # Persistence errors must stop all subsequent sends.
        LOG.info('%s: %s؛ %s مقال', source['name'], 'جزئي/متعطل' if failed else 'ناجح', len(docs))
    return successful


def record_channel(store, channel, posts, baseline=False):
    username = channel['username'].lstrip('@').lower()
    state = store.state['channels'].setdefault(username, {'initialized': False, 'seen': {}})
    for post in posts:
        h = digest(norm(post['text']))
        previous = state['seen'].get(post['id'])
        if previous == h:
            continue
        state['seen'][post['id']] = h
        if baseline or not channel.get('notify_news', False) or not fresh(post.get('date')):
            continue
        # Channel reports are intentionally never inserted into confirmed government jobs.
        pattern = channel.get('keyword_pattern')
        if pattern and not re.search(pattern, post['text'], re.I):
            continue
        message = ('📣 ' + ('تحديث خبر قناة' if previous else 'خبر جديد من قناة') + '\n\n'
                   + '@' + username + '\n\n' + post['text'][:2600]
                   + '\n\n⚠️ خبر قناة؛ مو تعيين حكومي مؤكد. تحقق من المصدر الأصلي.\n' + post['url'])
        enqueue(store, 'channel:' + post['id'] + ':' + h, message, 2)
    if len(state['seen']) > 1000:
        state['seen'] = dict(list(state['seen'].items())[-750:])


def monitor_channels(store, config, fetcher):
    for channel in config.get('channels', []):
        if not channel.get('enabled') or channel.get('mode') != 'public_preview':
            continue
        username = channel['username'].lstrip('@').lower()
        status = store.state['channels'].setdefault(username, {'initialized': False, 'seen': {}})
        try:
            posts = public_channel(fetcher, channel)
            record_channel(store, channel, posts, baseline=not status['initialized'])
            status['initialized'] = True
            status['success'] = now()
            status['error'] = None
        except Exception:
            status['error'] = 'تعذرت معاينة القناة؛ لا تجاوز للحماية'
        store.save()


def flush_outbox(store, telegram, limit=12):
    sent = 0
    queue = sorted(store.state['outbox'].items(), key=lambda pair: (pair[1]['priority'], pair[1]['created']))
    for event_id, item in queue:
        if item['status'] != 'pending' or sent >= limit:
            continue
        if item.get('retry_at', '') > now():
            continue
        item['status'] = 'sending'
        item['attempts'] += 1
        store.save()  # Write-ahead marker: a crash here must not cause a duplicate.
        try:
            message_id = telegram.send(item['text'])
        except TelegramRejected as error:
            item['error'] = str(error)
            item['status'] = 'pending' if item['attempts'] < 5 else 'failed'
            item['retry_at'] = (datetime.now(TZ) + timedelta(hours=1)).isoformat(timespec='seconds')
            store.save()
            continue
        except TelegramUnknown:
            item['status'] = 'uncertain'
            item['error'] = 'التسليم غير مؤكد؛ يلزم فحص المحادثة وقرار يدوي'
            store.save()
            continue
        item['status'] = 'sent'
        item['message_id'] = message_id
        item['sent_at'] = now()
        # If this save fails, durable status is sending; held for manual reconciliation.
        item.pop('text', None)
        store.save()
        sent += 1
    return sent


HELP = ('أهلاً بيك 👋\nبوت مراقبة التعيينات الحكومية الرسمية.\n\n'
        '/latest — آخر التعيينات\n/basra — البصرة والإعلانات الشاملة لكل العراق\n'
        '/iraq — جميع المحافظات\n/today — المكتشفة اليوم بتوقيت بغداد\n'
        '/search كلمة — بحث بعنوان أو مؤهل\n/status — حالة المراقبة\n/help — التعليمات\n\n'
        'الأوامر تنرد عند تشغيل مهمة GitHub التالية، عادة كل نصف ساعة وقد تتأخر. '
        'التغطية تخص المصادر المفعلة فقط، ومو كل دوائر العراق. '
        'أخبار القنوات منفصلة وغير مؤكدة. الإعلانات القديمة في التهيئة الأولى ما تنرسل كإشعارات.')


def command_reply(command, state, config):
    parts = command.strip().split(maxsplit=1)
    cmd = parts[0].split('@')[0] if parts else '/help'
    if cmd in ('/start', '/help'):
        return HELP
    if cmd == '/status':
        statuses = []
        for source in config['sources']:
            s = state['sources'].get(source['id'], {})
            statuses.append(source['name'] + ': ' + ('معطل بالإعدادات' if not source.get('enabled') else
                ('خلل/فحص جزئي' if s.get('error') else ('ناجح' if s.get('success') else 'لم يفحص')))
                + '\nآخر نجاح: ' + str(s.get('success', 'غير متوفر')))
        held = sum(x['status'] in ('sending', 'uncertain', 'failed') for x in state['outbox'].values())
        pending = sum(x['status'] == 'pending' for x in state['outbox'].values())
        channels = [f"@{k}: {v.get('error') or v.get('success') or 'لم يفحص'}" for k, v in state['channels'].items()]
        return ('⚙️ حالة المراقبة\nآخر تشغيل: ' + str(state.get('last_run') or 'غير متوفر')
            + '\nآخر دورة اكتملت لكل المصادر المفعلة: ' + str(state.get('last_success') or 'غير متوفر')
            + '\n\n' + '\n'.join(statuses + channels) + f'\n\nمعلقة للمراجعة: {held}؛ قيد الإرسال: {pending}'
            + f"\nإعلانات تحتاج مراجعة: {len(state['review'])}\nالأوقات بتوقيت بغداد.")
    ads = [x for x in state['ads'].values() if x.get('eligible') and not x.get('baseline')]
    if cmd == '/basra':
        ads = [x for x in ads if 'البصرة' in x['province'] or 'جميع محافظات العراق' in x['province']]
    elif cmd == '/today':
        ads = [x for x in ads if x['first_seen'][:10] == today()]
    elif cmd == '/search':
        if len(parts) < 2:
            return 'اكتب كلمة بعد الأمر، مثال: /search حاسوب'
        q = norm(parts[1]).lower()
        ads = [x for x in ads if q in norm(x['title'] + ' ' + x['qualification'] + ' ' + x['issuer']).lower()]
    elif cmd not in ('/latest', '/iraq'):
        return 'الأمر غير معروف. اكتب /help'
    ads.sort(key=lambda x: x['first_seen'], reverse=True)
    if not ads:
        return 'ماكو إعلانات مطابقة اكتُشفت بعد التهيئة الأولى. هذا مو معناه عدم وجود تعيينات خارج المصادر المراقبة.'
    lines = [f"{i}. {a['title'][:180]}\n📍 {a['province']} | 📅 {a['published']}\n{a['url']}" for i, a in enumerate(ads[:6], 1)]
    return ('📋 آخر الإعلانات المطابقة (راجع الموعد في المصدر):\n\n' + '\n\n'.join(lines))[:4000]


def process_updates(store, telegram, config):
    for _ in range(3):  # At most 300 updates/run; no permanent server.
        updates = telegram.updates(store.state['offset'])
        if not updates:
            break
        for update in updates:
            if update['update_id'] < store.state['offset']:
                continue
            msg = update.get('message') or update.get('callback_query', {}).get('message')
            callback = update.get('callback_query')
            if msg and str(msg.get('chat', {}).get('id')) == telegram.chat_id:
                text = callback.get('data', '') if callback else msg.get('text', '')
                if text.startswith('/'):
                    reply = command_reply(text, store.state, config)
                    enqueue(store, 'command:' + str(update['update_id']), reply, 0)
                if callback:
                    try:
                        telegram.call('answerCallbackQuery', {'callback_query_id': callback['id']})
                    except (TelegramRejected, TelegramUnknown):
                        pass
            post = update.get('channel_post') or update.get('edited_channel_post')
            if post:
                username = post.get('chat', {}).get('username', '').lower()
                for channel in config.get('channels', []):
                    if channel.get('enabled') and channel.get('mode') == 'bot_api' and channel['username'].lstrip('@').lower() == username:
                        text = post.get('text') or post.get('caption')
                        if text:
                            record_channel(store, channel, [{'id': username + '/' + str(post['message_id']),
                                'text': text, 'date': datetime.fromtimestamp(post['date'], TZ).date().isoformat(),
                                'url': f"https://t.me/{username}/{post['message_id']}"}],
                                baseline=not store.state.get('bot_api_initialized', False))
            # Reply event and offset are committed together; getUpdates itself does not send replies.
            store.state['offset'] = update['update_id'] + 1
            store.save()
        if len(updates) < 100:
            break
    store.state['bot_api_initialized'] = True
    store.save()


def check_sources(config):
    fetcher = Fetcher()
    ok = 0
    for source in config['sources']:
        if not source.get('enabled'):
            print(source['id'], 'معطل:', source.get('verification_note', 'يلزم تحقق'))
            continue
        try:
            items = listing(fetcher, source)
            doc = article(fetcher, source, items[0])
            print(source['id'], 'ناجح', len(items), 'روابط؛ مثال:', doc['title'], 'تاريخ:', doc['date'])
            ok += 1
        except (requests.RequestException, ValueError, KeyError, TypeError) as error:
            print(source['id'], 'فشل', type(error).__name__)
    channels_ok = True
    # Test pending public previews even while disabled, without enabling or notifying.
    for channel in config.get('channels', []):
        if channel.get('mode') != 'public_preview':
            print(channel['username'], 'يتطلب عضوية البوت بالقناة؛ لا يفحص عبر المعاينة')
            continue
        try:
            posts = public_channel(fetcher, channel)
            print(channel['username'], 'معاينة ناجحة؛ منشورات نصية:', len(posts),
                  'آخر تاريخ ظاهر:', posts[-1]['date'],
                  '؛ الحالة:', 'مفعلة' if channel.get('enabled') else 'غير مفعلة — فعّلها بعد مراجعة المحتوى')
        except Exception as error:
            channels_ok = False
            print(channel['username'], 'تعذر فحص المعاينة:', type(error).__name__)
    return 0 if channels_ok and ok and ok == sum(bool(s.get('enabled')) for s in config['sources']) else 1


def main():
    logging.basicConfig(level=logging.INFO, format='%(levelname)s %(message)s')
    p = argparse.ArgumentParser(description='مراقب التعيينات الحكومية')
    p.add_argument('--check-sources', action='store_true')
    p.add_argument('--dry-run', action='store_true', help='قراءة المصادر فقط بلا إرسال أو تعديل السجل')
    p.add_argument('--test-notification', action='store_true')
    p.add_argument('--get-chat-id', action='store_true')
    p.add_argument('--resolve', metavar='EVENT_ID')
    p.add_argument('--decision', choices=['sent', 'retry'])
    args = p.parse_args()
    config = json.loads(open('sources.json', encoding='utf-8').read())
    if args.check_sources or args.dry_run:
        return check_sources(config)
    if args.get_chat_id:
        token = os.getenv('TELEGRAM_BOT_TOKEN') or getpass.getpass('Bot Token (مخفي): ')
        tg = Telegram(token, 'setup')
        for u in tg.updates(0):
            m = u.get('message', {})
            if m.get('chat', {}).get('type') == 'private':
                print('اسم المحادثة:', m['chat'].get('first_name', ''), 'Chat ID:', m['chat']['id'])
        return 0
    telegram = Telegram(os.getenv('TELEGRAM_BOT_TOKEN'), os.getenv('TELEGRAM_CHAT_ID'))
    store = open_store()
    if args.resolve:
        if not args.decision:
            p.error('--resolve يحتاج --decision')
        item = store.state['outbox'][args.resolve]
        if item['status'] not in ('sending', 'uncertain', 'failed'):
            raise ValueError('هذه الرسالة مو معلقة')
        item['status'] = 'sent' if args.decision == 'sent' else 'pending'
        item['resolution'] = now()
        item.pop('retry_at', None)
        if args.decision == 'retry':
            item['attempts'] = 0
        store.save()
        return 0
    if args.test_notification:
        enqueue(store, 'test:' + now(), '🧪 هذا إشعار تجريبي لفحص الاتصال فقط.\nمو إعلان تعيين حقيقي.', 0)
        store.save()
        flush_outbox(store, telegram)
        return 0
    store.state['last_run'] = now()
    store.save()
    fetcher = Fetcher()
    successes = monitor_sources(store, config, fetcher)
    monitor_channels(store, config, fetcher)
    if successes and successes == sum(bool(s.get('enabled')) for s in config['sources']):
        store.state['last_success'] = now()
        store.save()
    process_updates(store, telegram, config)
    flush_outbox(store, telegram)
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as error:
        # Never log full request exceptions: Telegram URLs embed the secret token.
        LOG.error('توقفت المهمة بأمان: %s. راجع إعدادات المصدر/السجل/Secrets؛ لم تطبع الأسرار.', type(error).__name__)
        sys.exit(1)
