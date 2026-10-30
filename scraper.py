"""Allowlisted official sources only. No CAPTCHA or access-control bypass."""
import hashlib
import html
import io
import json
import re
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urljoin, urlsplit, urlunsplit, parse_qsl, urlencode
from urllib.robotparser import RobotFileParser
import feedparser
import requests
from bs4 import BeautifulSoup
from pypdf import PdfReader
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

USER_AGENT = 'IraqGovJobsMonitor/1.0 (+respectful public-announcement monitoring)'
AR_DIGITS = str.maketrans('٠١٢٣٤٥٦٧٨٩', '0123456789')


def norm(text):
    return re.sub(r'\s+', ' ', re.sub(r'[\u064b-\u065fـ]', '', str(text))).strip().translate(AR_DIGITS)


def digest(text):
    return hashlib.sha256(text.encode()).hexdigest()


def canonical(url):
    p = urlsplit(url)
    query = [(k, v) for k, v in parse_qsl(p.query) if not k.lower().startswith('utm_') and k not in ('fbclid', 'gclid')]
    return urlunsplit((p.scheme.lower(), p.netloc.lower(), p.path.rstrip('/') or '/', urlencode(sorted(query)), ''))


def host_allowed(url, source):
    p = urlsplit(url)
    return p.scheme == 'https' and not p.username and not p.password and p.port in (None, 443) and p.hostname in source['hosts']


def parse_date(value):
    if not value:
        return None
    s = norm(value)
    try:
        return datetime.fromisoformat(s.replace('Z', '+00:00')).date().isoformat()
    except ValueError:
        pass
    m = re.search(r'(20\d{2})[-/](\d{1,2})[-/](\d{1,2})', s)
    if m:
        try:
            return datetime(*map(int, m.groups())).date().isoformat()
        except ValueError:
            pass
    m = re.search(r'(\d{1,2})[/-](\d{1,2})[/-](20\d{2})', s)
    if m:
        d, month, y = map(int, m.groups())
        try:
            return datetime(y, month, d).date().isoformat()
        except ValueError:
            pass
    try:
        return parsedate_to_datetime(s).date().isoformat()
    except (ValueError, TypeError, OverflowError):
        return None


class Fetcher:
    def __init__(self, pause=0.5):
        self.session = requests.Session()
        self.session.headers['User-Agent'] = USER_AGENT
        self.session.mount('https://', HTTPAdapter(max_retries=Retry(
            total=2, backoff_factor=1, status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=['GET'], respect_retry_after_header=True)))
        self.robots = {}
        self.last = {}
        self.pause = pause

    def _get(self, url, source, limit):
        for _ in range(5):
            if not host_allowed(url, source):
                raise ValueError('رابط خارج النطاقات المعتمدة')
            host = urlsplit(url).hostname
            time.sleep(max(0, self.pause - (time.monotonic() - self.last.get(host, 0))))
            self.last[host] = time.monotonic()
            with self.session.get(url, timeout=(8, 18), stream=True, allow_redirects=False) as r:
                if r.is_redirect:
                    url = urljoin(url, r.headers['Location'])
                    # A redirect target must pass robots as well as host checks.
                    self.check_robots(url, source)
                    continue
                r.raise_for_status()
                chunks, size = [], 0
                for chunk in r.iter_content(16384):
                    size += len(chunk)
                    if size > limit:
                        raise ValueError('المحتوى يتجاوز حد الحجم')
                    chunks.append(chunk)
                return b''.join(chunks), r.headers.get('Content-Type', '')
        raise ValueError('تحويلات كثيرة')

    def check_robots(self, url, source):
        if not host_allowed(url, source):
            raise ValueError('نطاق غير معتمد')
        p = urlsplit(url)
        origin = f'{p.scheme}://{p.netloc}'
        if origin not in self.robots:
            # Fail closed on network/403 errors; robots 404 means no published rules.
            try:
                r = self.session.get(origin + '/robots.txt', timeout=(8, 12), allow_redirects=False)
                if r.status_code not in (200, 404):
                    raise ValueError('تعذر التحقق من robots.txt')
                robot = RobotFileParser()
                robot.parse(r.text.splitlines() if r.status_code == 200 else [])
                self.robots[origin] = robot
            except requests.RequestException:
                raise ValueError('تعذر التحقق من robots.txt') from None
        robot = self.robots[origin]
        if not robot.can_fetch(USER_AGENT, url):
            raise ValueError('robots.txt يمنع قراءة هذا المسار')
        self.pause = max(self.pause, float(robot.crawl_delay(USER_AGENT) or 0))

    def get(self, url, source, limit=3_000_000):
        self.check_robots(url, source)
        return self._get(url, source, limit)


def soup_of(data):
    s = BeautifulSoup(data, 'html.parser')
    text = s.get_text(' ', strip=True).lower()
    if any(x in text for x in ['verify you are human', 'just a moment...', 'بوابة الحماية', 'access denied', 'site unavailable']):
        raise ValueError('صفحة حماية؛ لن يتم تجاوزها')
    return s


def listing(fetcher, source):
    raw, _ = fetcher.get(source['url'], source)
    kind = source['kind']
    if kind == 'rss':
        feed = feedparser.parse(raw)
        if feed.bozo and not feed.entries:
            raise ValueError('RSS غير صالح')
        items = [{'url': e.get('link'), 'title': BeautifulSoup(e.get('title', ''), 'html.parser').get_text(),
                  'date': parse_date(e.get('published'))} for e in feed.entries]
    elif kind == 'api':
        value = json.loads(raw)
        for part in source.get('items_path', '').split('.'):
            if part:
                value = value[part]
        items = [{'url': x[source['url_key']], 'title': BeautifulSoup(
            str(x[source['title_key']]), 'html.parser').get_text(),
            'date': parse_date(x.get(source.get('date_key', 'date')))} for x in value]
    else:
        s = soup_of(raw)
        items = []
        for a in s.select(source['link_selector']):
            href = urljoin(source['url'], a.get('href', ''))
            if re.search(source['link_pattern'], href):
                title = a.get_text(' ', strip=True)
                if len(title) >= 5 and title not in ['قراءة المزيد', 'عرض التفاصيل']:
                    items.append({'url': href, 'title': title, 'date': None})
    unique = {}
    for x in items:
        if x['url'] and host_allowed(x['url'], source):
            x['url'] = canonical(x['url'])
            unique.setdefault(x['url'], x)
    if not unique:
        raise ValueError('لا روابط إعلانات؛ يلزم فحص تغير بنية المصدر')
    return list(unique.values())[:source.get('limit', 30)]


class ImageOnlyArticle(ValueError):
    def __init__(self, title, date):
        self.title, self.date = title, date
        super().__init__('صفحة مصورة بلا نص قابل للقراءة؛ تحتاج مراجعة')


def extract_document(raw, source, url, hint=None, content_type=''):
    if 'pdf' in content_type.lower() or urlsplit(url).path.lower().endswith('.pdf'):
        reader = PdfReader(io.BytesIO(raw))
        if len(reader.pages) > 40:
            raise ValueError('PDF يتجاوز 40 صفحة')
        text = '\n'.join(p.extract_text() or '' for p in reader.pages)
        if len(norm(text)) < 30:
            raise ValueError('PDF مصور يحتاج OCR؛ لا يمكن تأكيد محتواه')
        return (hint or {}).get('title', 'غير مذكور'), text, (hint or {}).get('date'), []
    s = soup_of(raw)
    title = s.select_one(source['title_selector'])
    body = s.select_one(source['body_selector'])
    if not title or not body:
        raise ValueError('فشل استخراج عنوان أو نص الإعلان')
    date_node = s.select_one(source.get('date_selector', 'time[datetime]'))
    date = parse_date((date_node.get('datetime') or date_node.get('content') or date_node.get_text(' ', strip=True)) if date_node else None)
    if not date:
        date = (hint or {}).get('date')
    if len(body.get_text(strip=True)) < 25:
        if body.select_one('img,iframe,object,embed'):
            raise ImageOnlyArticle(title.get_text(' ', strip=True), date)
        raise ValueError('نص مقال مفقود؛ يحتاج تحقق من البنية')
    for junk in body.select('script,style,nav,footer,aside,form'):
        junk.decompose()
    links = [urljoin(url, a['href']) for a in body.select('a[href]')
             if urlsplit(a['href']).path.lower().endswith('.pdf')]
    return title.get_text(' ', strip=True), body.get_text('\n', strip=True), date, links


def article(fetcher, source, item):
    raw, ct = fetcher.get(item['url'], source, 12_000_000)
    try:
        title, body, date, pdfs = extract_document(raw, source, item['url'], item, ct)
    except ImageOnlyArticle as issue:
        return {'url': item['url'], 'title': norm(issue.title), 'text': '',
                'date': issue.date, 'warnings': [], 'needs_review': True,
                'review_reason': str(issue)}
    warnings = []
    for link in dict.fromkeys(pdfs):
        if len(warnings) >= 2:
            break
        if not host_allowed(link, source):
            warnings.append('مرفق خارج النطاق المعتمد')
            continue
        try:
            pdf, typ = fetcher.get(link, source, 12_000_000)
            _, text, _, _ = extract_document(pdf, source, link, item, typ)
            body += '\n' + text
            warnings.append('تمت قراءة مرفق PDF')
        except Exception:
            warnings.append('تعذرت قراءة مرفق PDF؛ راجع الأصل')
    return {'url': item['url'], 'title': norm(title), 'text': body[:120000],
            'date': date, 'warnings': warnings}


GOVERNORATES = ['البصرة', 'بغداد', 'نينوى', 'الأنبار', 'كربلاء', 'النجف', 'بابل', 'واسط',
                'ميسان', 'ذي قار', 'المثنى', 'الديوانية', 'القادسية', 'ديالى', 'صلاح الدين',
                'كركوك', 'أربيل', 'السليمانية', 'دهوك', 'حلبجة']
JOB = r'تعيين|تعيينات|درجات وظيفية|وظائف شاغرة|فرص عمل|التوظيف|بصفة عقد|vacanc|recruit|job opening|employment'
OPEN = r'فتح باب|فتح التقديم|التقديم على|التقديم للتعيين|استقبال طلبات|يعلن.{0,55}(?:حاجت|توفر)|تعلن.{0,55}(?:حاجت|توفر)|استمارة التقديم|ندعو.{0,50}التقديم|applications? (?:are )?(?:open|invited)|apply (?:now|by|for)|seeking|hiring'
EXCLUDE = r'مناقصة|مزايدة|اسماء المقبولين|أسماء المقبولين|نتائج التعيين|اسماء الفائزين|أسماء الفائزين|نقل خدمات|إعادة نشر|اعادة نشر|تعيين.{0,25}(?:مديرا|مدير عام|رئيس جامعة)|شركة خاصة|private company|tender|admission'


def is_job(title, text):
    t = norm(title + '\n' + text)
    return bool(re.search(JOB, t, re.I) and re.search(OPEN, t, re.I) and not re.search(EXCLUDE, norm(title), re.I))


def fields(doc, source):
    text = doc['text']
    flat = norm(text)
    places = [p for p in GOVERNORATES if p in flat]
    if re.search(r'جميع المحافظات|كافة المحافظات|عموم العراق|all governorates|nationwide', flat, re.I):
        province = 'جميع محافظات العراق'
    else:
        province = ' / '.join(places) or source.get('province') or 'غير مذكور'
    lines = [norm(x) for x in text.splitlines() if norm(x)]
    def evidence(pattern):
        return next((x[:400] for x in lines if re.search(pattern, x, re.I)), 'غير مذكور')
    qualification = evidence(r'المؤهل|الشهادة المطلوبة|بكالوريوس|بكلوريوس|دبلوم|ماجستير|دكتوراه|bachelor|qualification|degree')
    deadline = evidence(r'آخر موعد|اخر موعد|لغاية|ينتهي التقديم|موعد غلق|موعد اغلاق|deadline|closing date|apply by')
    method = evidence(r'التقديم.{0,30}(?:الكتروني|إلكتروني|حضور|رابط|موقع)|عبر.{0,30}(?:المنصة|الرابط)|apply.{0,30}(?:online|via|in person)')
    # Only application-related statements affect update notifications, not navigation or counters.
    relevant = sorted(set(x for x in lines if re.search(JOB + '|' + OPEN + r'|موعد|تمديد|إلغاء|الغاء|deadline|qualification|المؤهل|الشهادة', x, re.I)))
    return {'issuer': source['name'], 'province': province, 'title': doc['title'],
            'qualification': qualification, 'published': doc.get('date') or 'غير مذكور',
            'deadline': deadline, 'method': method, 'url': doc['url'],
            'warnings': doc.get('warnings', []), 'signature': digest(json.dumps(
                [doc['title'], qualification, deadline, method, relevant], ensure_ascii=False)),
            'fingerprint': digest(norm(text))}


def public_channel(fetcher, channel):
    username = channel['username'].lstrip('@')
    if not re.fullmatch(r'[A-Za-z0-9_]{5,32}', username):
        raise ValueError('يوزر قناة غير صالح')
    source = {'hosts': ['t.me']}
    raw, _ = fetcher.get('https://t.me/s/' + username, source)
    s = soup_of(raw)
    posts = []
    for node in s.select('.tgme_widget_message[data-post]'):
        text = node.select_one('.tgme_widget_message_text')
        tm = node.select_one('time[datetime]')
        if text and tm:
            posts.append({'id': node['data-post'], 'text': text.get_text('\n', strip=True),
                          'date': parse_date(tm['datetime']), 'url': 'https://t.me/' + node['data-post']})
    if not posts:
        raise ValueError('معاينة القناة غير متاحة أو لا منشورات نصية؛ لا يتم تجاوز الحماية')
    return posts
