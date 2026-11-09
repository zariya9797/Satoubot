"""Small, local records of manifest checks and recent source results."""
import hashlib
import json
import time

STATUS_AGE = 15 * 60


def status_key(url, kind, provider=None):
    identity = json.dumps([url, provider, kind], ensure_ascii=True, separators=(',', ':'))
    return 'addon_status:' + hashlib.sha256(identity.encode()).hexdigest()


def set_status(store, url, kind, state, provider=None):
    if kind not in ('manifest', 'search', 'stream') or state not in ('ok', 'empty', 'error', 'partial', 'invalid', 'unknown'):
        raise ValueError('Invalid add-on status')
    store.set_setting(status_key(url, kind, provider), json.dumps({'state':state, 'time':time.time()}))


def get_status(store, url, kind, provider=None, *, now=None):
    try:
        data = json.loads(store.setting(status_key(url, kind, provider), '{}'))
        age = (time.time() if now is None else now) - float(data['time'])
        if not 0 <= age <= STATUS_AGE: return 'unknown'
        state = data['state']
        return state if state in ('ok', 'empty', 'error', 'partial', 'invalid') else 'unknown'
    except (ValueError, TypeError, KeyError):
        return 'unknown'


def label(store, url, provider=None, *, active=True, parent_active=True):
    manifest = get_status(store, url, 'manifest')
    search = get_status(store, url, 'search') if provider is None else 'unknown'
    stream = get_status(store, url, 'stream', provider)
    problems = []
    if manifest == 'error': problems.append('manifest unavailable')
    if manifest == 'invalid': problems.append('invalid manifest')
    if provider is None and search == 'error': problems.append('search failed')
    if provider is None and search == 'partial': problems.append('some searches failed')
    if provider is None and search == 'empty': problems.append('last search: no results')
    if stream == 'error': problems.append('last file lookup failed')
    if stream == 'empty': problems.append('last file lookup: no results')
    effective = active and parent_active
    icon = '🔴' if problems else ('🟢' if effective and manifest == 'ok' else '⚪')
    mode = 'ON' if active else 'OFF'
    if not parent_active: mode += ' (repository OFF)'
    if problems: return icon, mode, '; '.join(problems)
    if manifest == 'unknown': return icon, mode, 'manifest not checked'
    return icon, mode, 'manifest reachable'
