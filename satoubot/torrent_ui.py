"""Torrent metadata review and single-file selection inside the existing menus."""
import asyncio
import secrets
import time
from pathlib import Path
from telegram import InlineKeyboardButton as Button, InlineKeyboardMarkup as Markup
from .menu import navigation
from .torrent import TorrentError, MAX_TORRENT_BYTES, inspect_torrent, magnet_metadata, download_torrent, normalize_magnet
from .transfer import MAX_DOWNLOAD_BYTES, safe_name


class TorrentFunctions:
    def torrent_folder(self):
        folder = self.media_dir.parent / 'torrents'
        folder.mkdir(exist_ok=True)
        return folder

    async def import_torrent(self, context, *, data=None, magnet=None):
        import shutil
        folder = self.torrent_folder() / secrets.token_hex(12)
        folder.mkdir(mode=0o700)
        generation = self.panel().generation
        try:
            if magnet:
                await self.show_panel(None, 'Fetching torrent metadata only… No content file has been approved.',
                                      reply_markup=Markup([navigation()]), context=context, remember=False)
                generation = self.panel().generation
                path, entries = await magnet_metadata(magnet, folder)
            else:
                clean, entries, digest = inspect_torrent(data)
                path = folder/(digest+'.torrent'); path.write_bytes(clean)
            key = self.store.cache('torrent', {'path':str(path), 'entries':entries, 'admin':self.admin_id})
            await self.torrent_menu(context, key, expected_generation=generation, remember=False)
        except asyncio.CancelledError:
            shutil.rmtree(folder, ignore_errors=True)
            raise
        except (TorrentError, OSError, TimeoutError) as exc:
            shutil.rmtree(folder, ignore_errors=True)
            await self.show_panel(None, str(exc)[:500], reply_markup=Markup([navigation()]), context=context, expected_generation=generation)

    async def torrent_document(self, update, context):
        document = update.message.document
        if context.user_data.get('manifest_pending') or not (document.file_name or '').lower().endswith('.torrent'):
            return await self.manifest_document(update, context)
        if not self.authorized(update): return
        if not document.file_size or document.file_size > MAX_TORRENT_BYTES:
            return await update.message.reply_text('Send a .torrent file under 4 MB.')
        remote = await document.get_file()
        data = bytes(await remote.download_as_bytearray())
        await self.start_panel_task('torrent', 'Reading torrent metadata…', lambda: self.import_torrent(context, data=data), context)

    async def torrent_menu(self, context, key, page=0, expected_generation=None, remember=True):
        value = self.store.cached(key, 'torrent')
        if not value or value.get('admin') != self.admin_id:
            return await self.show_panel(None, 'Torrent selection expired. Send it again.', context=context)
        entries = value['entries']
        page = max(0, min(page, (len(entries)-1)//8))
        rows = []
        for entry in entries[page*8:page*8+8]:
            if 0 < entry['size'] <= MAX_DOWNLOAD_BYTES:
                rows.append([Button(f'{entry["name"][-65:]} · {entry["size"]/1_000_000:.1f} MB', callback_data=f'torrentpick:{key}:{entry["index"]}')])
        paging = []
        if page: paging.append(Button('Previous', callback_data=f'torrentpage:{key}:{page-1}'))
        if (page+1)*8 < len(entries): paging.append(Button('More', callback_data=f'torrentpage:{key}:{page+1}'))
        if paging: rows.append(paging)
        rows.append(navigation())
        await self.show_panel(None, f'Torrent files · Page {page+1}\nChoose one file to review before downloading.\n'
                              'Empty files and files over 10 GB cannot be selected.\nAudio/subtitle languages are unknown until the file is downloaded.\n'
                              'Uses DHT and peer exchange; no continued seeding after completion.', reply_markup=Markup(rows), context=context, expected_generation=expected_generation, remember=remember)

    async def torrent_callback(self, update, context):
        parts = update.callback_query.data.split(':')
        try: action, key, index = parts[0], int(parts[1]), int(parts[2])
        except (ValueError, IndexError): return
        value = self.store.cached(key, 'torrent')
        if not value or value.get('admin') != self.admin_id:
            return await update.callback_query.message.reply_text('Torrent selection expired. Send it again.')
        if action == 'torrentpage': return await self.torrent_menu(context, key, index)
        entry = next((e for e in value['entries'] if e['index'] == index), None)
        if not entry or not 0 < entry['size'] <= MAX_DOWNLOAD_BYTES: return
        if action == 'torrentpick':
            rows = [[Button('Download only', callback_data=f'torrentlocal:{key}:{index}')],
                    [Button('Download → review upload', callback_data=f'torrentreview:{key}:{index}')], navigation()]
            options = self.transfer_options()
            from .media import FORMATS
            return await self.show_panel(update.callback_query.message,
                f'Torrent file: {entry["name"][-300:]}\nSize: {entry["size"]/1_000_000:.1f} MB\n'
                f'Output: {FORMATS[options["output_format"]]}\nNo upload without approval.', reply_markup=Markup(rows))
        if action not in ('torrentlocal','torrentreview'): return
        if not await self.transfer_gate(update.callback_query.message): return
        # Claim before awaiting anything else, so double clicks cannot start two jobs.
        self.store.db.execute("DELETE FROM discovery_cache WHERE id=? AND kind='torrent'", (key,)); self.store.db.commit()
        metadata = Path(value['path'])
        try: _, _, digest = inspect_torrent(metadata.read_bytes())
        except (OSError, TorrentError): return await update.callback_query.message.reply_text('Torrent metadata unavailable. Send it again.')
        source = f'magnet:?xt=urn:btih:{digest}&file={index}'
        previous = self.store.find_source(source)
        if previous: return await update.callback_query.message.reply_text(f'This file already has item #{previous["id"]}. Check Saved files or Upload approvals.')
        job_id = self.store.create(source, Path(entry['name']).stem, safe_name(Path(entry['name']).name), entry['size'])
        self.save_transfer_options(job_id, {'download_mode':'local' if action == 'torrentlocal' else 'review'})
        self.store.update(job_id, state='downloading')
        self.active_description = f'Torrent download #{job_id} · {entry["name"][-100:]}'
        self.spawn(self.perform_torrent(context, job_id, metadata, index), kind='download', job_id=job_id, label=f'Torrent #{job_id} · {entry["name"][-80:]}')
        await self.show_panel(update.callback_query.message, 'Torrent download started. Open Active transfers for progress.')

    async def perform_torrent(self, context, job_id, metadata, index):
        folder = self.media_dir/str(job_id)/'torrent'
        last = 0
        async def progress(done, total, speed):
            nonlocal last
            now = time.monotonic()
            if now-last < 5: return
            last = now
            await self.panel_status(context, 'transfer', f'Torrent #{job_id} · Approx. disk written {done/1_000_000:.1f} / {(total or 0)/1_000_000:.1f} MB')
        path = None
        try:
            path = await download_torrent(metadata, index, folder, progress)
            await self.finish_local(context, job_id, path)
        except asyncio.CancelledError:
            if path and path.is_file(): self.retain_local(job_id, path, 'Processing cancelled; local file retained')
            else: self.store.update(job_id, state='cancelled', error='Cancelled')
            raise
        except Exception as exc:
            if path and path.is_file(): self.retain_local(job_id, path, 'Review unavailable; local file retained')
            else: self.store.update(job_id, state='failed', error=type(exc).__name__)
            await context.bot.send_message(self.admin_id, f'Torrent #{job_id} failed. No upload started. Try again later.')
        finally:
            await self.panel_status(context, 'transfer')

    async def torrent_url(self, context, url):
        import httpx
        from .public_transport import PublicTransport
        try:
            data = bytearray()
            async with httpx.AsyncClient(transport=PublicTransport(), follow_redirects=True, max_redirects=5,
                                         trust_env=False, timeout=30) as client:
                async with client.stream('GET', url) as response:
                    response.raise_for_status()
                    async for chunk in response.aiter_bytes(8192):
                        data.extend(chunk)
                        if len(data) > MAX_TORRENT_BYTES: raise TorrentError('Torrent metadata exceeds 4 MB')
            await self.import_torrent(context, data=bytes(data))
        except (httpx.HTTPError, TorrentError, ValueError) as exc:
            await self.show_panel(None, 'Could not fetch a public torrent file. Send a .torrent document or magnet instead.',
                                  reply_markup=Markup([navigation()]), context=context)
