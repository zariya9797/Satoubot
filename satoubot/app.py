import asyncio
import contextvars
from functools import wraps
import logging
import os
import secrets
import tempfile
import warnings
from pathlib import Path
from urllib.parse import urlsplit

import psutil
from PIL import Image, ImageOps
from dotenv import load_dotenv
from telegram import (InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton,
                      KeyboardButtonRequestChat, ReplyKeyboardMarkup, ReplyKeyboardRemove, Update)
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler,
                          MessageHandler, InlineQueryHandler, filters)
from telegram.error import TelegramError

from .preferences import PreferenceFunctions, recover_local_jobs
from .torrent_ui import TorrentFunctions
from .task_controls import TaskControlFunctions
from .task_context import transfer_id
from .store import Store
from .transfer import safe_name
from .download import DownloadFunctions
from .upload import UploadFunctions
from .search import SearchFunctions
from .group_management import GroupManagementFunctions
from .migration import MigrationFunctions
from .sessions import SessionManager
from .destinations import DestinationFunctions
from .catalogue import CatalogueFunctions
from .discovery import Discovery
from .menu import MenuFunctions, navigation
from .interface import InterfaceFunctions
from .series import SeriesFunctions
from .transfer import MAX_BYTES
from .daily import DailyFunctions
from .source_history import SourceHistoryFunctions
import time

log = logging.getLogger('satoubot')
current_admin = contextvars.ContextVar('current_admin', default=None)
cancelled_command_work = contextvars.ContextVar('cancelled_command_work', default=False)

def admin_session(handler):
    @wraps(handler)
    async def wrapped(update, context):
        user = update.effective_user or (update.inline_query.from_user if update.inline_query else None)
        token = current_admin.set(user.id if user else None)
        try:
            owner = getattr(handler, '__self__', None)
            if owner and owner.authorized(update): owner.touch_panel(context)
            return await handler(update, context)
        finally: current_admin.reset(token)
    return wrapped


def latest_command(handler, *, clear_prompt=True):
    """Allow a newer command from the same admin to replace temporary work."""
    session_handler = admin_session(handler)
    @wraps(handler)
    async def wrapped(update, context):
        owner = handler.__self__
        if not owner.authorized(update): return
        user_id = update.effective_user.id
        if not hasattr(owner, 'command_tasks'): owner.command_tasks = {}
        current = asyncio.current_task()
        previous = owner.command_tasks.get(user_id)
        owner.command_tasks[user_id] = current
        stopped = False
        try:
            if previous and previous is not current and not previous.done():
                stopped = True
                previous.cancel()
                await asyncio.gather(previous, return_exceptions=True)
            token = current_admin.set(user_id)
            try:
                stopped = bool(await owner.cancel_panel_tasks()) or stopped
                if clear_prompt: owner.clear_prompt(context)
            finally: current_admin.reset(token)
            stopped_token = cancelled_command_work.set(stopped)
            try: return await session_handler(update, context)
            finally: cancelled_command_work.reset(stopped_token)
        finally:
            if owner.command_tasks.get(user_id) is current:
                owner.command_tasks.pop(user_id, None)
    return wrapped


class Bot(TaskControlFunctions, PreferenceFunctions, TorrentFunctions, SourceHistoryFunctions, DailyFunctions, SeriesFunctions, InterfaceFunctions, MenuFunctions, DestinationFunctions, CatalogueFunctions, DownloadFunctions, UploadFunctions, SearchFunctions, GroupManagementFunctions, MigrationFunctions):
    def __init__(self, store, media_dir, admin_id, uploader, upload_parallel_parts=1):
        self.store, self.media_dir, self.owner_id, self.uploader = store, media_dir, admin_id, uploader
        if not 1 <= upload_parallel_parts <= 4:
            raise ValueError('UPLOAD_PARALLEL_PARTS must be between 1 and 4')
        self.upload_parallel_parts = upload_parallel_parts
        self.tasks = set()
        self.task_info = {}
        self.task_sequence = 0
        self.last_user_activity = time.monotonic()
        self.last_idle_check = time.monotonic()

    @property
    def admin_id(self):
        return current_admin.get() or self.owner_id

    def is_admin(self, user_id):
        return user_id == self.owner_id or bool(self.store.db.execute('SELECT 1 FROM admins WHERE user_id=?',(user_id,)).fetchone())

    def authorized(self, update):
        return bool(update.effective_user and self.is_admin(update.effective_user.id)
                    and update.effective_chat and update.effective_chat.type == 'private')

    def spawn(self, coro, *, kind='download', job_id=None, label=None, daily_identity=None):
        self.task_sequence += 1
        ident = self.task_sequence
        info = {'id': ident, 'kind': kind, 'job_id': job_id, 'label': label or kind.title(),
                'daily_identity': daily_identity, 'owner': self.admin_id, 'started': False}
        async def run():
            token = transfer_id.set(ident)
            info['started'] = True
            try: return await coro
            finally: transfer_id.reset(token)
        task = asyncio.create_task(run())
        self.tasks.add(task)
        self.task_info[ident] = (task, info)
        def finished(done):
            self.tasks.discard(done)
            self.task_info.pop(ident, None)
            if not info['started']:
                coro.close()
                job = self.store.get(info['job_id']) if info['job_id'] else None
                if job and job['state'] in ('downloading', 'uploading', 'converting'):
                    state = 'upload_unknown' if job['state'] == 'uploading' else 'local_only' if job['state'] == 'converting' else 'cancelled'
                    self.store.update(job['id'], state=state, error='Cancelled before starting', approval_token='')
        task.add_done_callback(finished)
        return task

    def bind_transfer_job(self, job_id, label=None):
        entry = self.task_info.get(transfer_id.get())
        if entry:
            entry[1]['job_id'] = job_id
            if label: entry[1]['label'] = label

    def transfers_enabled(self):
        return self.store.setting('transfers_enabled', '1') == '1'

    def transfer_busy(self):
        return any(not task.done() for task in self.tasks)

    def active_transfers(self):
        return [(task, info) for task, info in self.task_info.values() if not task.done()]

    async def transfer_gate(self, message, kind='download'):
        if not self.transfers_enabled():
            await message.reply_text('Transfers are off. Press Start transfers to enable downloads and uploads.')
            return False
        count = sum(info['kind'] == kind for _, info in self.active_transfers())
        if count >= 2:
            await message.reply_text(f'Two {kind}s are active. Wait for one to finish, then try again.')
            return False
        return True

    async def start(self, update, context):
        if not self.authorized(update): return
        if self.store.setting('stop_by_command') == '1':
            self.store.set_setting('transfers_enabled', '1')
            self.store.set_setting('stop_by_command', '0')
        if context.args and context.args[0].startswith('movie_'):
            try:
                key = int(context.args[0].split('_', 1)[1])
            except ValueError:
                return
            await self.start_panel_task('movie', 'Finding available files and checking sizes…', lambda: self.safe_movie_card(context, key), context)
            return
        notice = await update.message.reply_text('Opening menu…', reply_markup=ReplyKeyboardRemove())
        try:
            await self.main_menu(update.message)
        finally:
            try: await notice.delete()
            except TelegramError: self.panel().retired.append(notice)

    async def status(self, update, context):
        if not self.authorized(update): return
        process = psutil.Process()
        memory = process.memory_info().rss / (1024 * 1024)
        cpu = psutil.cpu_percent(interval=None)
        target = self.store.setting('target', 'not selected')
        state = 'on' if self.transfers_enabled() else 'off'
        await self.show_panel(update.message, f'Transfers: {state}\nDestination: {target}\nSystem CPU: {cpu:.1f}%\nBot memory: {memory:.1f} MiB\nActive transfers: {sum(not task.done() for task in self.tasks)}')

    async def stop(self, update, context):
        if not self.authorized(update): return
        self.set_transfers(False)
        self.store.set_setting('stop_by_command', '1')
        uploading = any(info['kind'] == 'upload' for _, info in self.active_transfers())
        await self.cancel_work(all_work=True)
        await self.cancel_daily_scan(context)
        text = 'Stopped active work. Transfers and Daily Uploads are off.'
        if uploading: text += '\nCancelled uploads may already have reached Telegram; check the destination before retrying.'
        await self.show_panel(update.message, text, reply_markup=InlineKeyboardMarkup([navigation()]))

    async def cancel_command(self, update, context):
        if not self.authorized(update): return
        self.discard_current_confirmation()
        self.clear_prompt(context)
        self.panel().prompt.clear()
        await self.cancel_menu(update.message)


    async def announce_startup(self, telegram_bot):
        try:
            await telegram_bot.send_message(self.owner_id, "I'm Back")
        except TelegramError as exc:
            log.warning('Startup notice failed: %s', type(exc).__name__)

    async def chat_shared(self, update, context):
        if not self.authorized(update): return
        chat = update.message.chat_shared
        self.store.set_setting('target', chat.chat_id)
        await update.message.reply_text(f'Destination selected: {chat.title or chat.chat_id}. Ensure the uploading account can post there and the bot has the required admin access.')

    async def text(self, update, context):
        if not self.authorized(update): return
        text = update.message.text.strip()
        if context.user_data.pop('admin_add_pending', False) is True:
            if update.effective_user.id != self.owner_id: return
            if not text.isascii() or not text.isdigit() or not 0 < int(text) < 2**63:
                await update.message.reply_text('Invalid user ID. Open Admin management and try again.'); return
            self.store.db.execute('INSERT OR IGNORE INTO admins(user_id) VALUES (?)',(int(text),)); self.store.db.commit()
            await self.admin_menu(update.message, self.owner_id); return
        if text == 'My channels': return await self.destination_menu(context)
        if text == 'Status': return await self.status(update, context)
        if text == 'Help': return await self.start(update, context)
        if text in ('Start transfers', 'Stop transfers'):
            enabled = text == 'Start transfers'
            self.set_transfers(enabled)
            notice = ' Transfers already running will finish.' if not enabled and self.transfer_busy() else ''
            await update.message.reply_text(f'Transfers are {"on" if enabled else "off"}.{notice}')
            return
        if text in ('Download', 'Search', 'Library search', 'Add-ons'):
            for key in ('manifest_pending', 'catalogue_search_pending', 'search_pending'):
                context.user_data.pop(key, None)
        if text == 'Download':
            await update.message.reply_text('Send a direct HTTP(S) media link. The bot will check the size before asking to download.')
            return
        if text == 'Uploads awaiting approval': return await self.pending(update, context)
        if text == 'Add-ons': return await self.addons(update, context)
        if text == 'Library search':
            context.user_data['search_pending'] = True
            await update.message.reply_text('Send a title to search published items, or use /find <title>.')
            return
        if text == 'Saved links': return await self.saved(update, context)
        if text == 'Group management': return await self.group_menu(update, context)
        if text == 'Migration': return await self.migration_menu(update, context)
        is_manifest = text.startswith(('http://', 'https://')) and urlsplit(text).path.lower().endswith('/manifest.json')
        if context.user_data.pop('manifest_pending', False) or is_manifest:
            try:
                uploaded = context.user_data.pop('uploaded_manifest', None)
                if uploaded is not None:
                    name, kind = Discovery(self.store).register(text, uploaded)
                else:
                    name, kind = await Discovery(self.store).add(text)
                await self.show_panel(update.message, f'Saved {name} ({kind}). Send a movie title to search.', reply_markup=InlineKeyboardMarkup([navigation()]))
            except Exception:
                await self.show_panel(update.message, 'Could not read that manifest. Open Add-ons and send its original manifest URL.', reply_markup=InlineKeyboardMarkup([navigation()]))
            return
        if context.user_data.pop('catalogue_search_pending', False):
            return await self.begin_search(update.message, text)
        if context.user_data.pop('search_pending', False):
            return await self.search_query(update, text)
        if text.startswith(('http://','https://')) and urlsplit(text).path.lower().endswith('.torrent'):
            return await self.start_panel_task('torrent', 'Fetching torrent metadata…', lambda: self.torrent_url(context, text), context)
        if text.startswith('magnet:'):
            return await self.start_panel_task('torrent', 'Fetching torrent metadata…', lambda: self.import_torrent(context, magnet=text), context)
        if not text.startswith(('https://', 'http://')):
            return await self.begin_search(update.message, text)
        await self.handle_source(update, text)

    async def callback(self, update, context):
        if not self.authorized(update): return
        q = update.callback_query
        # Temporary menus and reviews are valid only while they are the current screen.
        scoped = ('menu:', 'adminremove:', 'addon', 'season:', 'seasons:', 'page:', 'cancelpage:',
                  'movie:', 'file:', 'retryfile:', 'searchagain', 'yes:', 'yeslocal:', 'no:', 'link:',
                  'pref:', 'cancelwork:', 'directmanual:', 'directlink:', 'directretry:',
                  'dl:', 'dllocal:', 'skip:', 'report:', 'upload:', 'thumb:', 'keeplocal:', 'convert:',
                  'convertmenu:', 'torrentpage:', 'torrentpick:', 'torrentlocal:', 'torrentreview:',
                  'daily:', 'dest:', 'destcancel', 'choose_dest:')
        source_message = getattr(q, 'message', None)
        if isinstance(getattr(source_message, 'message_id', None), int) and q.data.startswith(scoped):
            panel = self.panel()
            if not any(self.same_message(source_message, item) for item in (panel.message, panel.card, panel.preview)):
                await q.answer('This menu has expired. Send /start or /pending.', show_alert=True)
                return
        await q.answer()
        if q.data.startswith('cancelwork:'):
            return await self.cancel_callback(update, context)
        if q.data.startswith('pref:'):
            return await self.preference_callback(update, context)
        if q.data.startswith(('torrentpage:', 'torrentpick:', 'torrentlocal:', 'torrentreview:')):
            return await self.torrent_callback(update, context)
        if q.data.startswith('localreview:'):
            try: job_id = int(q.data.split(':')[1])
            except ValueError: return
            job = self.store.get(job_id)
            if job and job['state'] == 'local_only' and job['size'] <= MAX_BYTES:
                self.store.update(job_id, state='review')
                return await self.preview(context, job_id)
            return
        if q.data.startswith('convertmenu:'):
            try:
                parts = q.data.split(':'); job_id = int(parts[1])
            except (ValueError, IndexError): return
            job = self.store.get(job_id)
            if not job: return
            if job['state'] == 'review' and (len(parts) != 3 or parts[2] != job['approval_token']):
                return await q.message.reply_text('This preview is out of date. Use /pending to open it again.')
            return await self.conversion_menu(context, job_id)
        if q.data.startswith('convert:'):
            try: _, number, selection = q.data.split(':'); job_id = int(number)
            except ValueError: return
            from .media import FORMATS
            job = self.store.get(job_id)
            if not job or job['state'] not in ('review', 'local_only') or selection not in FORMATS or selection == 'original': return
            if not await self.transfer_gate(q.message): return
            previous_state = job['state']
            self.store.update(job_id, state='converting', approval_token='')
            self.active_description = f'Converting #{job_id}'
            self.spawn(self.convert_job(context, job_id, selection, previous_state), kind='download', job_id=job_id, label=f'Converting #{job_id} · {job["filename"]}')
            await self.show_panel(q.message, 'Conversion started. Open Active transfers for progress.')
            return
        if q.data.startswith('daily:'):
            return await self.daily_callback(update, context)
        if q.data.startswith('directmanual:'):
            try: entry = self.store.cached(int(q.data.split(':')[1]), 'manual_link')
            except ValueError: return
            if entry: await self.show_panel(q.message, entry['url'], disable_preview=True)
            return
        if q.data.startswith('directlink:'):
            try: job = self.store.get(int(q.data.split(':')[1]))
            except ValueError: return
            if job:
                await self.show_panel(q.message, job['source'], disable_preview=True)
            return
        if q.data.startswith(('season:', 'seasons:')):
            return await self.series_callback(update, context)
        if q.data.startswith(('addonpage:', 'addonproviders:', 'addontoggle:', 'addonpreset:', 'addoncheck:', 'addonrefresh:', 'addoncheckall:')):
            return await self.addon_callback(update, context)
        if q.data.startswith('directretry:'):
            try: source=self.store.cached(int(q.data.split(':')[1]), 'direct_retry')
            except (ValueError, IndexError): return
            if not source:
                await q.message.reply_text('This check expired. Send the link again.'); return
            from types import SimpleNamespace
            return await self.handle_source(SimpleNamespace(message=q.message), source['url'])
        if q.data.startswith(('menu:', 'adminremove:')):
            return await self.menu_callback(update, context)
        if q.data == 'destcancel' or q.data.startswith(('dest:', 'choose_dest:')):
            return await self.select_destination(update, context)
        if q.data == 'searchagain' or q.data.startswith(('movie:', 'file:', 'retryfile:', 'page:', 'cancelpage:', 'yes:', 'yeslocal:', 'no:', 'link:')):
            return await self.catalogue_callback(update, context)
        try:
            parts = q.data.split(':')
            action, job_id = parts[0], int(parts[1])
            job = self.store.get(job_id)
        except (ValueError, AttributeError): return
        if not job: return
        if action == 'keeplocal' and job['state'] == 'review':
            if len(parts) != 3 or parts[2] != job['approval_token']: return
            self.store.update(job_id, state='local_only', approval_token='')
            await q.message.reply_text('Kept locally. Open Saved files to review or convert later.')
            return
        if action == 'report' and job['state'] in ('oversize', 'reported', 'link_only'):
            self.store.update(job_id, state='reported')
            await q.message.reply_text('Link reported and saved. Automatic source searches are not available yet.')
        elif action == 'thumb' and job['state'] == 'review':
            if len(parts) != 3 or parts[2] != job['approval_token']:
                await q.message.reply_text('This preview is out of date. Use the latest preview.')
                return
            context.user_data['thumbnail_request'] = (job_id, parts[2])
            await q.message.reply_text(f'Send a photo for item #{job_id}. Use /pending to return to the preview.')
        elif action == 'skip' and job['state'] == 'ready':
            self.store.update(job_id,state='cancelled')
            await self.show_panel(q.message, 'Download cancelled.')
        elif action in ('dl', 'dllocal') and job['state'] == 'ready':
            if not await self.transfer_gate(q.message): return
            if action == 'dllocal': self.store.set_setting(f'job:{job_id}:download_mode', 'local')
            self.active_description = f'Downloading #{job_id} · {job["filename"]}'
            self.store.update(job_id, state='downloading')
            self.spawn(self.perform_download(context, job_id), kind='download', job_id=job_id, label=f'Download #{job_id} · {job["filename"]}')
            await self.show_panel(q.message, 'Download started. Open Active transfers for progress.')
        elif action == 'upload' and job['state'] == 'review':
            if job['size'] > MAX_BYTES:
                await q.message.reply_text('This file exceeds the 2 GB Telegram upload limit and stays on your PC.'); return
            target = self.store.setting('target')
            if len(parts) != 3 or parts[2] != job['approval_token']:
                await q.message.reply_text('This preview is out of date. Use the latest preview.')
                return
            if not await self.transfer_gate(q.message, 'upload'): return
            if not target:
                await self.destination_menu(context,job_id)
                return
            if target != job['target']:
                await q.message.reply_text('The destination has changed. Review the updated preview.')
                await self.preview(context, job_id)
                return
            try:
                member=await context.bot.get_chat_member(int(target),context.bot.id)
                if member.status!='administrator' or member.can_post_messages is False:
                    raise ValueError()
            except Exception:
                await q.message.reply_text('The bot needs administrator access and permission to post in this destination. Select a channel again.')
                await self.destination_menu(context,job_id)
                return
            if not await self.transfer_gate(q.message, 'upload'): return
            current = self.store.get(job_id)
            if not self.authorized(update) or not current or current['state'] != 'review' or current['approval_token'] != parts[2] or current['target'] != target or self.store.setting('target') != target:
                await q.message.reply_text('This preview is out of date. Use the latest preview.')
                return
            self.active_description = f'Uploading #{job_id} · {job["filename"]}'
            self.store.update(job_id, state='uploading', approval_token='')
            self.spawn(self.perform_upload(context, job_id), kind='upload', job_id=job_id, label=f'Upload #{job_id} · {job["filename"]}')
            try: await q.edit_message_reply_markup(reply_markup=None)
            except TelegramError: pass
            await self.show_panel(None, 'Upload started. Open Active transfers for progress.', context=context)
        else:
            await q.message.reply_text('This action is no longer available.')


    async def preview(self, context, job_id):
        job = self.store.get(job_id)
        if not job or job['state'] != 'review': return
        target = self.store.setting('target', '')
        token = secrets.token_hex(4)
        self.store.update(job_id, target=target, approval_token=token)
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton('Approve upload', callback_data=f'upload:{job_id}:{token}')],
                                         [InlineKeyboardButton('Convert format', callback_data=f'convertmenu:{job_id}:{token}')],
                                         [InlineKeyboardButton('Keep locally', callback_data=f'keeplocal:{job_id}:{token}')],
                                         [InlineKeyboardButton('Set thumbnail', callback_data=f'thumb:{job_id}:{token}')],
                                         [InlineKeyboardButton('Select channel', callback_data=f'choose_dest:{job_id}')],
                                         navigation()])
        text = f'Ready for review #{job_id}\nName: {job["filename"]}\nSize: {job["size"]/1_000_000:.1f} MB\nSaved: {job["path"]}\nDestination: {target or "not selected"}\nUse /rename {job_id} new name before approval.'
        import json
        from .media import track_summary
        metadata = self.store.setting(f'job:{job_id}:media')
        if metadata:
            try: text += '\n' + track_summary(json.loads(metadata))
            except (ValueError, TypeError): pass
        text = text[:4000]
        preview_message = None
        if job['thumbnail']:
            with open(job['thumbnail'], 'rb') as photo:
                sent = await context.bot.send_photo(self.admin_id, photo=photo, caption=text if len(text) <= 1000 else job['filename'], reply_markup=keyboard if len(text) <= 1000 else None)
                if len(text) <= 1000: preview_message = sent
        if not job['thumbnail'] or len(text) > 1000:
            preview_message = await context.bot.send_message(self.admin_id, text, reply_markup=keyboard)
        await self.register_preview(preview_message)
        # Destination selection is explicit; opening review never forces an upload setup.

    async def thumbnail_photo(self, update, context):
        if not self.authorized(update): return
        request = context.user_data.pop('thumbnail_request', None)
        if not request:
            await update.message.reply_text('Choose Set thumbnail on an item preview first.')
            return
        job_id, token = request
        job = self.store.get(job_id)
        if not job or job['state'] != 'review' or job['approval_token'] != token:
            await update.message.reply_text('This preview is out of date. Use /pending to select the latest one.')
            return
        photo = update.message.photo[-1]
        if photo.file_size and photo.file_size > 10_000_000:
            await update.message.reply_text('Photo is too large. Choose Set thumbnail and send a photo under 10 MB.')
            return
        folder = self.media_dir / str(job_id)
        folder.mkdir(exist_ok=True)
        source = None
        output = folder / f'thumb-{secrets.token_hex(8)}.jpg'
        try:
            with tempfile.NamedTemporaryFile(dir=folder, delete=False) as temporary:
                source = Path(temporary.name)
            remote = await photo.get_file()
            await remote.download_to_drive(custom_path=source)
            if source.stat().st_size > 10_000_000:
                raise ValueError('photo too large')
            with warnings.catch_warnings():
                warnings.simplefilter('error', Image.DecompressionBombWarning)
                with Image.open(source) as image:
                    image = ImageOps.exif_transpose(image).convert('RGB')
                    image.thumbnail((320, 320))
                    for quality in (85, 70, 55, 40, 25):
                        image.save(output, 'JPEG', quality=quality, optimize=True)
                        if output.stat().st_size < 20_000:
                            break
            if output.stat().st_size >= 20_000:
                raise ValueError('thumbnail too large')
            job = self.store.get(job_id)
            if not job or job['state'] != 'review' or job['approval_token'] != token:
                output.unlink(missing_ok=True)
                await update.message.reply_text('This preview is out of date. Use /pending to select the latest one.')
                return
            previous = job['thumbnail']
            self.store.update(job_id, thumbnail=str(output), approval_token='')
            if previous:
                try:
                    Path(previous).unlink(missing_ok=True)
                except OSError:
                    log.warning('Could not remove old thumbnail for item %s', job_id)
        except Exception as exc:
            output.unlink(missing_ok=True)
            log.warning('Thumbnail %s failed: %s', job_id, type(exc).__name__)
            await update.message.reply_text('Could not use that photo. Choose Set thumbnail and try another photo.')
            return
        finally:
            if source: source.unlink(missing_ok=True)
        await self.preview(context, job_id)


    async def rename(self, update, context):
        if not self.authorized(update): return
        try:
            job_id = int(context.args[0]); title = ' '.join(context.args[1:]).strip()
            job = self.store.get(job_id)
            if not job or job['state'] != 'review' or not title: raise ValueError()
            suffix = Path(job['filename']).suffix
            name = safe_name(title)
            while suffix and name.lower().endswith(suffix.lower()): name = name[:-len(suffix)]
            name = name.rstrip(' .') + suffix
            old = Path(job['path']); new = old.with_name(name)
            if new != old and new.exists(): raise ValueError()
            old.rename(new)
            self.store.update(job_id, title=title, filename=name, path=str(new))
            await self.preview(context, job_id)
        except (ValueError, IndexError, OSError):
            await update.message.reply_text('Use /rename <item number> <new name> for an item awaiting review.')

    async def pending(self, update, context):
        if not self.authorized(update): return
        rows = self.store.db.execute("SELECT id FROM jobs WHERE state='review' ORDER BY id LIMIT 20").fetchall()
        if not rows:
            await self.show_panel(update.message, 'No files awaiting review.', reply_markup=InlineKeyboardMarkup([navigation()])); return
        keyboard = [[InlineKeyboardButton(f'Item #{row["id"]}', callback_data=f'menu:review:{row["id"]}')] for row in rows]
        keyboard.append(navigation())
        await self.show_panel(update.message, 'Choose a file to review.', reply_markup=InlineKeyboardMarkup(keyboard))


def main():
    load_dotenv()
    token = os.environ['BOT_TOKEN']; admin = int(os.environ['ADMIN_USER_ID'])
    api_id = int(os.environ['TELEGRAM_API_ID']); api_hash = os.environ['TELEGRAM_API_HASH']
    root = Path(os.getenv('DATA_DIR', './data')).resolve()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    root.chmod(0o700)
    media = root / 'media'; media.mkdir(exist_ok=True)
    logging.basicConfig(filename=root/'satoubot.log', level=logging.WARNING, format='%(asctime)s %(levelname)s %(message)s')
    store = Store(root/'satoubot.sqlite3')
    # An interrupted upload may already be present; require manual inspection.
    store.db.execute("UPDATE jobs SET state='upload_unknown' WHERE state='uploading'")
    store.db.execute("UPDATE jobs SET state='failed' WHERE state='downloading'")
    recover_local_jobs(store)
    store.db.commit()
    sessions = SessionManager(root, api_id, api_hash)
    uploader = sessions.user
    try:
        upload_parallel_parts = int(os.getenv('UPLOAD_PARALLEL_PARTS', '1'))
    except ValueError as exc:
        raise ValueError('UPLOAD_PARALLEL_PARTS must be an integer from 1 to 4') from exc
    bot = Bot(store, media, admin, uploader, upload_parallel_parts)

    async def post_init(app):
        await sessions.start()
        await bot.announce_startup(app.bot)
        bot.cleanup_task = asyncio.create_task(bot.panel_cleaner())
        bot.daily_task = asyncio.create_task(bot.daily_loop(app.bot))
        bot.source_task = asyncio.create_task(bot.idle_source_loop())
    async def post_shutdown(app):
        source_task = getattr(bot, 'source_task', None)
        if source_task:
            source_task.cancel()
            await asyncio.gather(source_task, return_exceptions=True)
        daily_task = getattr(bot, 'daily_task', None)
        if daily_task:
            daily_task.cancel()
            await asyncio.gather(daily_task, return_exceptions=True)
        cleanup_task = getattr(bot, 'cleanup_task', None)
        if cleanup_task:
            cleanup_task.cancel()
            await asyncio.gather(cleanup_task, return_exceptions=True)
        for task in list(bot.tasks):
            task.cancel()
        if bot.tasks:
            await asyncio.gather(*bot.tasks, return_exceptions=True)
        discovery_tasks = list(getattr(bot, 'discovery_tasks', set()))
        for task in discovery_tasks: task.cancel()
        if discovery_tasks: await asyncio.gather(*discovery_tasks, return_exceptions=True)
        await sessions.stop()
    endpoint = os.getenv('BOT_API_URL', 'http://127.0.0.1:8081').rstrip('/')
    application = (Application.builder().token(token).base_url(endpoint+'/bot').base_file_url(endpoint+'/file/bot')
                   .post_init(post_init).post_shutdown(post_shutdown).build())
    application.add_handler(CommandHandler('start', latest_command(bot.start), block=False))
    application.add_handler(CommandHandler('stop', latest_command(bot.stop), block=False))
    application.add_handler(CommandHandler('cancel', admin_session(bot.cancel_command), block=False))
    application.add_handler(CommandHandler('status', latest_command(bot.status), block=False))
    application.add_handler(CommandHandler('rename', latest_command(bot.rename), block=False))
    application.add_handler(CommandHandler('find', latest_command(bot.find), block=False))
    application.add_handler(CommandHandler('search', latest_command(bot.online_search), block=False))
    application.add_handler(CommandHandler('settings', latest_command(bot.settings), block=False))
    application.add_handler(CommandHandler('addons', latest_command(bot.addons), block=False))
    application.add_handler(InlineQueryHandler(admin_session(bot.inline_search)))
    application.add_handler(CommandHandler('saved', latest_command(bot.saved), block=False))
    application.add_handler(CommandHandler('pending', latest_command(bot.pending), block=False))
    application.add_handler(CommandHandler('migration', latest_command(bot.migration_menu), block=False))
    application.add_handler(CallbackQueryHandler(admin_session(bot.callback)))
    application.add_handler(MessageHandler(filters.StatusUpdate.CHAT_SHARED, admin_session(bot.chat_shared)))
    application.add_handler(MessageHandler(filters.PHOTO, admin_session(bot.thumbnail_photo)))
    application.add_handler(MessageHandler(filters.Document.ALL, admin_session(bot.torrent_document)))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, latest_command(bot.text, clear_prompt=False), block=False))
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == '__main__': main()
