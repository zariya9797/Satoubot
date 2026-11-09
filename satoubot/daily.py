"""Persistent, approval-driven daily release selection."""
import asyncio
import json
import logging
import re
from datetime import datetime, timezone, timedelta
from types import SimpleNamespace
from urllib.parse import quote, urljoin

from telegram import InlineKeyboardButton as Button, InlineKeyboardMarkup as Markup
from .discovery import Discovery
from .menu import navigation

log = logging.getLogger('satoubot')


def release_date(movie):
    for key in ('released', 'release_date', 'releaseInfo'):
        try: return datetime.fromisoformat(str(movie.get(key, ''))[:10]).date()
        except ValueError: pass
    return None


def daily_title_alias(movie):
    date = release_date(movie)
    year = date.year if date else str(movie.get('year') or movie.get('releaseInfo') or '')[:4]
    return 'daily-title:' + re.sub(r'[\W_]+', '', movie['name'].casefold()) + ':' + str(year)


class DailyFunctions:
    def daily_init(self):
        self.store.db.execute('''CREATE TABLE IF NOT EXISTS daily_titles (
            identity TEXT PRIMARY KEY, payload TEXT NOT NULL, state TEXT NOT NULL,
            job_id INTEGER, published_day TEXT)''')
        self.store.db.commit()

    def daily_day(self):
        return datetime.now(timezone.utc).date().isoformat()

    def daily_count(self):
        self.daily_init()
        return self.store.db.execute("SELECT count(*) FROM daily_titles WHERE state='published' AND published_day=?", (self.daily_day(),)).fetchone()[0]

    def set_transfers(self, enabled):
        self.store.set_setting('transfers_enabled', '1' if enabled else '0')
        # Only an explicit start enables automatic selection on existing installs.
        self.store.set_setting('daily_enabled', '1' if enabled else '0')
        if enabled: self.store.set_setting('daily_next_check', '0')

    async def daily_menu(self, message, context):
        self.daily_init()
        quantity = int(self.store.setting('daily_quantity', '4'))
        rows = [[Button(str(n) + (' ✓' if n == quantity else ''), callback_data=f'daily:quantity:{n}') for n in range(4,8)],
                [Button(str(n) + (' ✓' if n == quantity else ''), callback_data=f'daily:quantity:{n}') for n in range(8,11)],
                [Button('Stop transfers' if self.transfers_enabled() and self.store.setting('daily_enabled') == '1' else 'Start daily transfers', callback_data='daily:toggle')],
                [Button('Choose upload channel', callback_data='menu:channels')],
                [Button('Cancel one', callback_data='daily:cancelone'), Button('Cancel all daily', callback_data='daily:cancelall')]]
        current = self.store.db.execute("SELECT identity FROM daily_titles WHERE state='offered' LIMIT 1").fetchone()
        if current:
            key = self.store.cache('daily_title', {'identity':current[0]})
            rows.append([Button('Review release', callback_data=f'daily:review:{key}'), Button('Skip release', callback_data=f'daily:skip:{key}')])
        rows.append(navigation())
        await self.show_panel(message, f'Daily Uploads · {self.daily_count()}/{quantity} uploaded today (UTC)\n'
                              'Transfers ON starts selection of recent movie releases from enabled catalogues. '
                              'Choose a checked file under 2 GB, then review its name, thumbnail and channel before upload. '
                              'The cycle stops transfers at the quota. Start again when you want another cycle.\n'
                              + self.store.setting('daily_status', 'Ready to start.'), reply_markup=Markup(rows), context=context)

    async def daily_callback(self, update, context):
        if update.effective_user.id != self.owner_id:
            return await self.show_panel(update.callback_query.message, 'Only the owner can configure Daily Uploads.')
        parts = update.callback_query.data.split(':')
        if parts[1] == 'cancelone': return await self.cancel_menu(update.callback_query.message, daily_only=True)
        if parts[1] == 'cancelall':
            uploading = any(info['kind'] == 'upload' and self.is_daily_transfer(info) for _, info in self.active_transfers())
            await self.cancel_daily_all()
            self.store.set_setting('daily_status', 'Daily tasks cancelled.' + (' Check Telegram for a possibly completed upload before retrying.' if uploading else ''))
            await self.cancel_daily_scan(context)
            return await self.daily_menu(update.callback_query.message, context)
        if parts[1] == 'quantity':
            try: quantity = int(parts[2])
            except (ValueError, IndexError): return
            if not 4 <= quantity <= 10: return
            self.store.set_setting('daily_quantity', quantity)
        elif parts[1] == 'toggle': self.set_transfers(not (self.transfers_enabled() and self.store.setting('daily_enabled') == '1'))
        elif parts[1] in ('review', 'skip'):
            try: entry = self.store.cached(int(parts[2]), 'daily_title')
            except (ValueError, IndexError): return
            if not entry: return
            row = self.store.db.execute("SELECT * FROM daily_titles WHERE identity=? AND state='offered'", (entry['identity'],)).fetchone()
            if row:
                if parts[1] == 'skip':
                    self.store.db.execute("UPDATE daily_titles SET state='skipped' WHERE identity=? AND state='offered'", (row['identity'],));self.store.db.commit()
                    self.store.set_setting('daily_next_check', '0')
                    tasks = [task for _, key, task, panel in self.lookup_tasks(all_admins=True) if panel.task_daily.get(key) == row['identity']]
                    for task in tasks: task.cancel()
                    if tasks: await asyncio.gather(*tasks, return_exceptions=True)
                else:
                    movie = json.loads(row['payload'])
                    key = self.store.cache('movie', dict(movie, daily_identity=row['identity']))
                    await self.start_panel_task('movie', 'Checking Daily Upload files…', lambda: self.safe_movie_card(context, key), context, daily_identity=row['identity'])
        await self.daily_menu(update.callback_query.message, context)

    async def recent_releases(self):
        service = Discovery(self.store)
        candidates = []
        today = datetime.now(timezone.utc).date()
        for url, kind, manifest in service.manifests():
            if kind != 'addon': continue
            catalogs = [c for c in manifest.get('catalogs', []) if c.get('type') == 'movie' and not any(e.get('isRequired') for e in c.get('extra', []) if isinstance(e, dict))]
            catalogs.sort(key=lambda c: not any(word in str(c.get('id', '')).lower() for word in ('recent','new','release','trending')))
            for catalog in catalogs[:4]:
                try:
                    data = await service.cached_json(urljoin(url, f'catalog/movie/{quote(str(catalog["id"]), safe="")}.json'), ttl=600)
                    for meta in data.get('metas', [])[:20]:
                        if not isinstance(meta, dict) or not meta.get('id') or not meta.get('name'): continue
                        movie = dict(meta, id=str(meta['id']), type='movie', addon=url)
                        date = release_date(movie)
                        if date and today-timedelta(days=90) <= date <= today: candidates.append(movie)
                except Exception: continue
        return sorted(candidates, key=release_date, reverse=True)

    async def daily_tick(self, context):
        self.daily_init()
        # Reconcile persisted jobs first, even after restart or while transfers are off.
        for row in self.store.db.execute("SELECT * FROM daily_titles WHERE state='transferring'").fetchall():
            job = self.store.get(row['job_id'])
            if job and job['state'] == 'published':
                self.store.db.execute("UPDATE daily_titles SET state='published', published_day=? WHERE identity=?", (self.daily_day(), row['identity']))
            elif job and job['state'] == 'cancelled':
                self.store.db.execute("UPDATE daily_titles SET state='skipped' WHERE identity=?", (row['identity'],))
            elif not job or job['state'] in ('failed','upload_unknown'):
                self.store.db.execute("UPDATE daily_titles SET state='needs_attention' WHERE identity=?", (row['identity'],))
        self.store.db.commit()
        if not self.transfers_enabled() or self.store.setting('daily_enabled') != '1': return
        quota = int(self.store.setting('daily_quantity', '4'))
        if self.daily_count() >= quota:
            self.set_transfers(False)
            self.store.set_setting('daily_status', 'Daily quota reached. Transfers are now off.')
            await context.bot.send_message(self.owner_id, 'Daily Uploads complete: quota reached. Transfers are now off.')
            return
        if sum(info['kind'] == 'download' for _, info in self.active_transfers()) >= 2 or getattr(self, 'discovery_tasks', None): return
        if self.store.db.execute("SELECT 1 FROM daily_titles WHERE state IN ('offered','transferring') LIMIT 1").fetchone(): return
        now = datetime.now(timezone.utc).timestamp()
        if now < float(self.store.setting('daily_next_check', '0')): return
        self.store.set_setting('daily_next_check', now+600)
        for movie in await self.recent_releases():
            if not self.transfers_enabled() or self.store.setting('daily_enabled') != '1': return
            # Also suppress the same title/year advertised under different catalogue IDs.
            alias = daily_title_alias(movie)
            if self.store.setting(alias): continue
            identity = movie['type'] + ':' + movie['id']
            if self.store.setting('title:' + identity) or self.store.db.execute('SELECT 1 FROM daily_titles WHERE identity=?', (identity,)).fetchone(): continue
            self.store.db.execute("INSERT INTO daily_titles(identity,payload,state) VALUES (?,?,'offered')", (identity,json.dumps(movie))); self.store.db.commit()
            self.store.set_setting('daily_next_check', '0')
            self.store.set_setting(alias, identity)
            key = self.store.cache('movie', dict(movie, daily_identity=identity))
            self.store.set_setting('daily_status', 'Awaiting your file choice: ' + movie['name'])
            await self.start_panel_task('movie', 'Checking Daily Upload files…', lambda: self.safe_movie_card(context, key), context, daily_identity=identity)
            await self.daily_menu(None, context)
            return
        self.store.set_setting('daily_status', 'No new dated releases in the past 90 days. Checking again in 10 minutes.')

    async def daily_loop(self, telegram_bot):
        context = SimpleNamespace(bot=telegram_bot, user_data={})
        while True:
            try: await self.daily_tick(context)
            except Exception as exc: log.warning('Daily selection failed: %s', type(exc).__name__)
            await asyncio.sleep(15)
