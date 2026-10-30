"""Durable compare-and-swap checkpoints; fail closed on persistence errors."""
import base64
import json
import os
from pathlib import Path
from urllib.parse import quote
import requests


def empty_state():
    return {'version': 1, 'sources': {}, 'ads': {}, 'outbox': {}, 'offset': 0,
            'last_run': None, 'last_success': None, 'review': {}, 'channels': {}}


class LocalStore:
    def __init__(self, path='state.json'):
        self.path = Path(path)
        self.state = json.loads(self.path.read_text()) if self.path.exists() else empty_state()

    def save(self):
        tmp = self.path.with_suffix('.json.tmp')
        with tmp.open('w') as f:
            json.dump(self.state, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        tmp.replace(self.path)


class GitHubStore:
    """No local cache/artifact dependency; each save updates bot-state/state.json."""
    def __init__(self, repo, token, branch='bot-state'):
        if not repo or not token:
            raise RuntimeError('بيانات GitHub ناقصة')
        self.base = f'https://api.github.com/repos/{repo}'
        self.branch = branch
        self.session = requests.Session()
        self.session.headers.update({'Authorization': f'Bearer {token}',
                                     'Accept': 'application/vnd.github+json',
                                     'X-GitHub-Api-Version': '2022-11-28'})
        self.sha = None
        r = self.session.get(self.base + '/contents/state.json', params={'ref': branch}, timeout=25)
        if r.status_code == 200:
            value = r.json()
            self.sha = value['sha']
            self.state = json.loads(base64.b64decode(value['content']))
            if self.state.get('version') != 1:
                raise RuntimeError('نسخة حالة غير مدعومة')
        elif r.status_code == 404:
            # Distinguish missing branch from missing state. Never silently reset a branch.
            ref = self.session.get(self.base + '/git/ref/heads/' + quote(branch, safe=''), timeout=25)
            if ref.status_code != 404:
                raise RuntimeError('فرع الحالة موجود لكن السجل مفقود؛ يجب استعادته يدوياً')
            meta = self.session.get(self.base, timeout=25)
            if meta.status_code != 200:
                raise RuntimeError('تعذر قراءة المستودع')
            default = meta.json()['default_branch']
            head = self.session.get(self.base + '/git/ref/heads/' + quote(default, safe=''), timeout=25)
            if head.status_code != 200:
                raise RuntimeError('تعذر قراءة الفرع الرئيسي')
            created = self.session.post(self.base + '/git/refs', json={
                'ref': 'refs/heads/' + branch, 'sha': head.json()['object']['sha']}, timeout=25)
            if created.status_code != 201:
                raise RuntimeError('تعذر إنشاء فرع الحالة')
            self.state = empty_state()
            self.save()
        else:
            raise RuntimeError(f'تعذر تحميل السجل: HTTP {r.status_code}')

    def save(self):
        content = json.dumps(self.state, ensure_ascii=False, separators=(',', ':')).encode()
        if len(content) > 850_000:
            raise RuntimeError('السجل اقترب من حد القراءة؛ انقل التخزين إلى قاعدة بيانات قبل المتابعة')
        payload = {'message': 'حفظ حالة مراقبة التعيينات', 'branch': self.branch,
                   'content': base64.b64encode(content).decode()}
        if self.sha:
            payload['sha'] = self.sha
        # Never retry an uncertain write blindly. A subsequent run reloads authoritative state.
        r = self.session.put(self.base + '/contents/state.json', json=payload, timeout=25)
        if r.status_code not in (200, 201):
            raise RuntimeError(f'فشل حفظ الحالة HTTP {r.status_code}؛ أوقف الإرسال')
        self.sha = r.json()['content']['sha']


def open_store():
    if os.getenv('GITHUB_ACTIONS') == 'true':
        return GitHubStore(os.getenv('GITHUB_REPOSITORY'), os.getenv('GITHUB_TOKEN'))
    return LocalStore(os.getenv('STATE_PATH', 'state.json'))
