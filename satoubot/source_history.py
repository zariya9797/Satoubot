"""Remember recent source checks and recheck saved links while the bot is idle."""
import asyncio
import hashlib
import json
import logging
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse

from .discovery import Discovery
from .file_info import probe

log = logging.getLogger('satoubot')


def host_key(url):
    try: host = urlparse(url).hostname
    except (ValueError, TypeError): return None
    return hashlib.sha256(host.lower().encode()).hexdigest() if host else None


def init_history(store):
    if getattr(store, '_source_history_ready', False): return
    store.db.execute('''CREATE TABLE IF NOT EXISTS source_history (
        host_hash TEXT PRIMARY KEY, host TEXT NOT NULL, ready INTEGER NOT NULL DEFAULT 0,
        manual INTEGER NOT NULL DEFAULT 0, temporary INTEGER NOT NULL DEFAULT 0,
        unavailable INTEGER NOT NULL DEFAULT 0, updated REAL NOT NULL, retry_until REAL NOT NULL DEFAULT 0)''')
    store.db.execute('''CREATE TABLE IF NOT EXISTS source_rechecks (
        fingerprint TEXT PRIMARY KEY, next_check REAL NOT NULL)''')
    store.db.commit()
    store._source_history_ready = True


def record_check(store, stream, result):
    key = host_key(stream.get('url', ''))
    if not key: return
    init_history(store)
    now = time.time()
    row = store.db.execute('SELECT * FROM source_history WHERE host_hash=?', (key,)).fetchone()
    counts = {name: row[name] if row and row['updated'] > now - 7*86400 else 0 for name in ('ready', 'manual', 'temporary', 'unavailable')}
    status = result.get('status')
    if status == 'ready' and not result.get('size'): status = 'manual'
    if status not in counts: status = 'temporary'
    counts[status] += 1
    delay = 86400
    retry = result.get('retry_after')
    if retry:
        try:
            delay = max(delay, float(retry))
        except ValueError:
            try: delay = max(delay, (parsedate_to_datetime(retry) - datetime.now(timezone.utc)).total_seconds())
            except (ValueError, TypeError, OverflowError): pass
    retry_until = max(row['retry_until'] if row else 0, now + delay if result.get('status') == 'temporary' and retry else 0)
    store.db.execute('INSERT OR REPLACE INTO source_history VALUES (?,?,?,?,?,?,?,?)', (key, urlparse(stream['url']).hostname, counts['ready'], counts['manual'], counts['temporary'], counts['unavailable'], now, retry_until))
    fingerprint = hashlib.sha256(stream['url'].encode()).hexdigest()
    store.db.execute('INSERT OR REPLACE INTO source_rechecks VALUES (?,?)', (fingerprint, now + delay))
    store.db.commit()


def reliability(store, stream):
    key = host_key(stream.get('url', ''))
    if not key: return 0.0
    init_history(store)
    row = store.db.execute('SELECT * FROM source_history WHERE host_hash=?', (key,)).fetchone()
    if not row or row['updated'] < time.time()-7*86400 or sum(row[k] for k in ('ready','manual','temporary','unavailable')) < 3: return 0.0
    return row['ready'] / sum(row[k] for k in ('ready','manual','temporary','unavailable'))


class SourceHistoryFunctions:
    async def idle_source_check(self):
        if self.store.setting('idle_source_checks', '1') != '1': return
        if time.monotonic() - self.last_user_activity < 300 or self.transfer_busy() or getattr(self, 'discovery_tasks', None): return
        if time.monotonic() - self.last_idle_check < 900: return
        self.last_idle_check = time.monotonic()
        init_history(self.store)
        service = Discovery(self.store)
        rows = self.store.db.execute('SELECT payload FROM source_links ORDER BY RANDOM() LIMIT 100').fetchall()
        for row in rows:
            try: stream = json.loads(row['payload'])
            except (TypeError, ValueError): continue
            origin = stream.get('source_manifest')
            provider = stream.get('source_provider')
            if not origin or not service.enabled(origin) or (provider and not service.enabled(origin, provider)): continue
            # Also require the original manifest still to be installed.
            manifest = self.store.db.execute('SELECT data FROM manifests WHERE url=?', (origin,)).fetchone()
            if not manifest: continue
            if provider:
                try: scrapers = json.loads(manifest['data']).get('scrapers', [])
                except (TypeError, ValueError): continue
                if not any(p.get('filename') == provider for p in scrapers if isinstance(p, dict)): continue
            url = stream.get('url', '')
            key = host_key(url)
            if not key: continue
            host = self.store.db.execute('SELECT retry_until FROM source_history WHERE host_hash=?', (key,)).fetchone()
            if host and host[0] > time.time(): continue
            fingerprint = hashlib.sha256(url.encode()).hexdigest()
            retry = self.store.db.execute('SELECT next_check FROM source_rechecks WHERE fingerprint=?', (fingerprint,)).fetchone()
            if retry and retry[0] > time.time(): continue
            result = await probe(url, stream.get('headers'))
            record_check(self.store, stream, result)
            return

    async def idle_source_loop(self):
        while True:
            await asyncio.sleep(60)
            try:
                self.idle_check_task = asyncio.create_task(self.idle_source_check())
                await self.idle_check_task
            except asyncio.CancelledError:
                if asyncio.current_task().cancelling(): raise
            except Exception as exc:
                log.warning('Idle source check failed: %s', type(exc).__name__)
