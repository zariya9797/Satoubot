"""Transfer defaults stored in the existing local SQLite database."""
import asyncio
import json
from pathlib import Path
from telegram import InlineKeyboardButton as Button, InlineKeyboardMarkup as Markup
from .menu import navigation
from .media import FORMATS, MediaError, convert_media, inspect_media, track_summary
from .transfer import MAX_BYTES


def recover_local_jobs(store):
    """Interrupted processing retains completed local bytes for later review."""
    for job in store.db.execute("SELECT * FROM jobs WHERE state IN ('converting','processing')").fetchall():
        path = Path(job['path']) if job['path'] else None
        if path and path.is_file():
            store.update(job['id'], state='local_only', size=path.stat().st_size,
                         filename=path.name, approval_token='', error='Processing interrupted; local file retained')
        else:
            store.update(job['id'], state='failed', approval_token='', error='Processing interrupted; local file unavailable')


class PreferenceFunctions:
    def transfer_options(self, job_id=None):
        prefix = f'job:{job_id}:' if job_id is not None else f'admin:{self.admin_id}:'
        return {name: self.store.setting(prefix + name, default)
                for name, default in (('output_format', 'original'), ('download_mode', 'review'))}

    def save_transfer_options(self, job_id, overrides=None):
        for name, value in {**self.transfer_options(), **(overrides or {})}.items():
            self.store.set_setting(f'job:{job_id}:{name}', value)

    async def settings(self, update, context):
        if self.authorized(update): await self.settings_menu(update.message)

    async def settings_menu(self, message):
        options = self.transfer_options()
        rows = [[Button(('✓ ' if key == options['output_format'] else '') + label,
                        callback_data=f'pref:output_format:{key}')] for key, label in FORMATS.items()]
        rows += [[Button(('✓ ' if options['download_mode'] == key else '') + label,
                         callback_data=f'pref:download_mode:{key}')]
                 for key, label in (('review', 'Download → review Telegram upload'), ('local', 'Download only'))]
        rows.append(navigation())
        await self.show_panel(message, 'Transfer settings · saved on this server for your account\n'
                              'New downloads use these defaults. Upload always needs approval.\n'
                              'Remux keeps the codecs; incompatible containers report an error and keep the original.\n'
                              'FFmpeg reads only downloaded local files.', reply_markup=Markup(rows))

    async def preference_callback(self, update, context):
        parts = update.callback_query.data.split(':')
        if len(parts) != 3: return
        _, name, value = parts
        choices = {'output_format': FORMATS, 'download_mode': ('review', 'local')}
        if name not in choices or value not in choices[name]: return
        self.store.set_setting(f'admin:{self.admin_id}:{name}', value)
        await self.settings_menu(update.callback_query.message)

    def retain_local(self, job_id, fallback, error):
        job = self.store.get(job_id)
        stored = Path(job['path']) if job and job['path'] else None
        path = stored if stored and stored.is_file() else Path(fallback)
        self.store.update(job_id, state='local_only', path=str(path), filename=path.name,
                          size=path.stat().st_size, approval_token='', error=error)

    async def finish_local(self, context, job_id, path):
        """Inspect/convert completed bytes, then offer only explicitly approved uploads."""
        options = self.transfer_options(job_id)
        original = Path(path)
        conversion_error = None
        if options['output_format'] != 'original':
            try:
                await self.panel_status(context, 'transfer', f'Converting #{job_id} · {FORMATS[options["output_format"]]}')
                path = await convert_media(original, options['output_format'])
            except (MediaError, OSError, TimeoutError) as exc:
                conversion_error = str(exc)
                path = original
        path = Path(path)
        size = path.stat().st_size
        try:
            info = await inspect_media(path)
            self.store.set_setting(f'job:{job_id}:media', json.dumps(info))
            tracks = track_summary(info)
        except (MediaError, OSError, ValueError, TimeoutError):
            tracks = 'Audio/subtitle inspection unavailable for this file.'
        local = options['download_mode'] == 'local' or size > MAX_BYTES
        self.store.update(job_id, path=str(path), filename=path.name, size=size,
                          state='local_only' if local else 'review', approval_token='')
        notice = f'Download done · saved #{job_id}\n{path.name}\n{size/1_000_000:.1f} MB\nSaved: {path}\n{tracks}'
        if conversion_error: notice += '\nConversion failed; original retained. ' + conversion_error[:200]
        if local:
            notice += '\nDownload only. No upload started.'
            rows = []
            if size <= MAX_BYTES: rows.append([Button('Review Telegram upload', callback_data=f'localreview:{job_id}')])
            await context.bot.send_message(self.admin_id, notice[:4000], reply_markup=Markup(rows) if rows else None)
        else:
            await context.bot.send_message(self.admin_id, notice[:4000])
            await self.preview(context, job_id)

    async def conversion_menu(self, context, job_id):
        job = self.store.get(job_id)
        if not job or job['state'] not in ('review', 'local_only'): return
        rows = [[Button(label, callback_data=f'convert:{job_id}:{key}')]
                for key, label in FORMATS.items() if key != 'original']
        rows.append(navigation())
        await self.show_panel(None, f'Convert local file #{job_id}\n{job["filename"]}\nOriginal remains on disk. Review again before uploading.',
                              reply_markup=Markup(rows), context=context)

    async def convert_job(self, context, job_id, selection, previous_state):
        job = self.store.get(job_id)
        try:
            path = await convert_media(Path(job['path']), selection)
            self.store.update(job_id, filename=path.name, path=str(path), size=path.stat().st_size)
            # A conversion from the review screen is reviewed again, even for download-only defaults.
            self.store.set_setting(f'job:{job_id}:output_format', 'original')
            self.store.set_setting(f'job:{job_id}:download_mode', 'local' if previous_state == 'local_only' else 'review')
            await self.finish_local(context, job_id, path)
        except BaseException as exc:
            self.store.update(job_id, state=previous_state, approval_token='')
            if isinstance(exc, asyncio.CancelledError): raise
            await context.bot.send_message(self.admin_id, 'Conversion failed; original kept. Choose another format. Use Upload approvals or Saved files to return.')
        finally:
            await self.panel_status(context, 'transfer')
