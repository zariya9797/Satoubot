import asyncio
import time
import logging
import json
from pathlib import Path
import httpx

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from .menu import navigation
from .progress import format_progress
from .media import inspect_remote_media, track_summary, FORMATS
from .file_info import probe
from .hls import download_hls, inspect_hls, HLSError
from .transfer import MAX_BYTES, MAX_DOWNLOAD_BYTES, download, filename_from_url, inspect_url, safe_name

log = logging.getLogger("satoubot")

def download_error_reason(error):
    if isinstance(error, BaseExceptionGroup):
        for child in error.exceptions:
            result = download_error_reason(child)
            if result != 'Check the source and search again to retry': return result
    if isinstance(error, HLSError): return str(error)
    if isinstance(error, httpx.HTTPStatusError):
        return f'Source returned HTTP {error.response.status_code}'
    if isinstance(error, (httpx.TimeoutException, TimeoutError)):
        return 'The source timed out'
    if isinstance(error, httpx.NetworkError): return 'The source connection failed'
    known = {'Source did not provide the expected byte range and size.',
             'Source did not honor the requested byte range.',
             'Source changed before the single connection retry.',
             'Source exceeded its byte range.', 'Source sent an incomplete byte range.',
             'The downloaded size does not match the advertised size.',
             'Download exceeded its reported size or the 10 GB local limit.'}
    if isinstance(error, ValueError) and str(error) in known: return str(error)
    return 'Check the source and search again to retry'


class DownloadFunctions:
    async def handle_source(self, update, text):
        old = self.store.find_source(text)
        if old:
            await self.show_panel(update.message, f'This link already has item #{old["id"]} ({old["state"]}).')
            return
        info=await probe(text)
        if info.get('hls_candidate'):
            try: plan = await inspect_hls(text)
            except HLSError as exc:
                manual = self.store.cache('manual_link', {'url':text})
                await self.show_panel(update.message, f'{exc}. Use the link manually.', reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('Give link', callback_data=f'directmanual:{manual}')], navigation()])); return
            except Exception:
                manual = self.store.cache('manual_link', {'url':text})
                await self.show_panel(update.message, 'Could not read the HLS playlist. Try again or use the link manually.', reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('Give link', callback_data=f'directmanual:{manual}')], navigation()])); return
            name = Path(filename_from_url(text)).stem + '.' + plan['format']
            job = self.store.create(text, Path(name).stem, name, 0)
            self.save_transfer_options(job)
            self.store.set_setting(f'job:{job}:hls', plan['format'])
            keyboard = InlineKeyboardMarkup([[InlineKeyboardButton('Transfer', callback_data=f'dl:{job}'), InlineKeyboardButton('Download only', callback_data=f'dllocal:{job}'), InlineKeyboardButton('Give link', callback_data=f'directlink:{job}'), InlineKeyboardButton('No', callback_data=f'skip:{job}')], navigation()])
            await self.show_panel(update.message, f'Item #{job}\nName: {name}\nSize: unknown. Local download limit: 10 GB. Telegram upload available only at or below 2 GB.\nOutput: {FORMATS[self.transfer_options()["output_format"]]}\nAudio/subtitle tracks: not supplied; inspected after download.\nDownload this file?', reply_markup=keyboard)
            return
        if info.get('status') != 'ready' or not info.get('size'):
            reason=info.get('reason','Size unknown')
            retry=info.get('retry_after')
            manual = self.store.cache('manual_link', {'url':text})
            keyboard=InlineKeyboardMarkup([[InlineKeyboardButton('Give link', callback_data=f'directmanual:{manual}')], navigation()])
            if info.get('status') == 'temporary':
                key=self.store.cache('direct_retry', {'url':text})
                keyboard=InlineKeyboardMarkup([[InlineKeyboardButton('Retry check', callback_data=f'directretry:{key}'), InlineKeyboardButton('Give link', callback_data=f'directmanual:{manual}')], navigation()])
            await self.show_panel(update.message, f"{reason}." + (f" Retry after {retry}." if retry else '') + ' Use Give link to try it manually.', reply_markup=keyboard)
            return
        size, content_type=info['size'],info.get('content_type','')
        if size > MAX_DOWNLOAD_BYTES:
            name = info.get('filename') or filename_from_url(text)
            job = self.store.create(text, Path(name).stem, name, size)
            self.store.update(job, state='oversize')
            buttons = InlineKeyboardMarkup([[InlineKeyboardButton('Report dead link', callback_data=f'report:{job}')]])
            await self.show_panel(update.message, f'Saved large file #{job}: {name} ({size / 1_000_000:.1f} MB). Download it manually.\n{text}', reply_markup=buttons)
            return
        if content_type.lower().startswith('text/html'):
            await self.show_panel(update.message, 'This appears to be a web page, not a direct file. Page link extraction is not yet supported.')
            return
        name = info.get('filename') or filename_from_url(text)
        tracks = 'Audio/subtitle tracks: unavailable before download.'
        try:
            tracks = 'File sample (may be incomplete):\n' + track_summary(await asyncio.wait_for(inspect_remote_media(text, size), 12))
        except Exception:
            pass
        job = self.store.create(text, Path(name).stem, name, size)
        self.save_transfer_options(job)
        choices = [] if size > MAX_BYTES else [InlineKeyboardButton('Transfer', callback_data=f'dl:{job}')]
        choices += [InlineKeyboardButton('Download only', callback_data=f'dllocal:{job}'), InlineKeyboardButton('Give link', callback_data=f'directlink:{job}'), InlineKeyboardButton('No', callback_data=f'skip:{job}')]
        keyboard = InlineKeyboardMarkup([choices, navigation()])
        await self.show_panel(update.message, f'Item #{job}\nName: {name}\nEstimated size: {size / 1_000_000:.1f} MB\n' + ('Above the 2 GB Telegram limit; this file will be saved locally.\n' if size > MAX_BYTES else '') + f'Output: {FORMATS[self.transfer_options()["output_format"]]}\n{tracks}\nDownload this file?', reply_markup=keyboard)


    async def perform_download(self, context, job_id):
        job = self.store.get(job_id)
        message = None
        done_bytes=0
        stage='Checking'
        last_bytes=0
        started=time.monotonic()
        original=self.store.setting(f'job:{job_id}:original',job['filename'])
        hls_format=self.store.setting(f'job:{job_id}:hls')
        actual_size=job['size']
        async def progress(done,total,speed):
            nonlocal done_bytes
            done_bytes=done
        async def ticker():
            nonlocal last_bytes
            last_time=started
            while True:
                await asyncio.sleep(5)
                now=time.monotonic()
                speed=(done_bytes-last_bytes)/max(now-last_time,.001)
                last_bytes=done_bytes;last_time=now
                eta=f'{(job["size"]-done_bytes)/speed/60:.1f} min' if speed else 'waiting for data'
                try:
                    if message:
                        await message.edit_text(format_progress(f'{stage} download #{job_id}',original,done_bytes,None if hls_format else job['size'],speed))
                    await self.panel_status(context, 'transfer', format_progress(f'{stage} download #{job_id}', original, done_bytes, None if hls_format else job['size'], speed))
                except Exception: pass
        timer=asyncio.create_task(ticker())
        try:
            await self.panel_status(context, 'transfer', f'Downloading #{job_id} · {original}')
            folder = self.media_dir / str(job_id)
            folder.mkdir(exist_ok=True)
            path = folder / safe_name(original)
            try:
                message = await context.bot.send_message(self.admin_id, f'Connecting download #{job_id}\n{original}\nSize: ' + ('unknown' if hls_format else f'{job["size"]/1_000_000:.1f} MB'))
            except Exception as exc:
                log.warning('Download progress message %s failed: %s', job_id, type(exc).__name__)
            if message:
                try: await message.edit_text(f'Checking source for {original}…')
                except Exception: pass
            source_headers=json.loads(self.store.setting(f'job:{job_id}:headers', '{}'))
            check=await probe(job['source'],source_headers)
            if (hls_format and not check.get('hls_candidate')) or (not hls_format and (check.get('status') != 'ready' or check.get('size') != job['size'])):
                self.store.update(job_id, state='failed', error='Source changed')
                await context.bot.send_message(self.admin_id, f"Link status changed: {check.get('reason','size changed')}. You can try the original link manually:\n{job['source']}", disable_web_page_preview=True)
                return
            if not self.transfers_enabled():
                self.store.update(job_id, state='cancelled', error='Transfers switched off')
                if message: await message.edit_text('Transfers are off. Download cancelled before starting.')
                return
            stage='Transferring'
            if hls_format:
                actual_size=await download_hls(job['source'], path, hls_format, progress, headers=source_headers, quality=self.store.setting(f'job:{job_id}:quality'))
            else:
                await download(job['source'], path, job['size'], progress, headers=source_headers)
            stage='Checking'
            approved=folder/job['filename']
            if approved != path: path.rename(approved)
            self.store.update(job_id, state='processing', path=str(approved), size=actual_size)
        except asyncio.CancelledError:
            self.store.update(job_id, state='cancelled', error='Cancelled')
            if message:
                try: await message.edit_text(f'Download cancelled: {original}')
                except Exception: pass
            raise
        except Exception as exc:
            log.error('Download %s failed: %s', job_id, type(exc).__name__)
            self.store.update(job_id, state='failed', error=type(exc).__name__)
            if message:
                try: await message.edit_text(f'Download failed: {original}. Search again to retry.')
                except Exception: pass
            reason = download_error_reason(exc)
            await context.bot.send_message(self.admin_id, f"Download #{job_id} failed: {reason}. You can try the original link manually:\n{job['source']}", disable_web_page_preview=True)
            return
        finally:
            timer.cancel()
            await asyncio.gather(timer,return_exceptions=True)
            await self.panel_status(context, 'transfer')
        try:
            await self.finish_local(context, job_id, approved)
        except asyncio.CancelledError:
            self.retain_local(job_id, approved, 'Processing cancelled; local file retained')
            raise
        except Exception as exc:
            log.warning('Local review %s failed: %s', job_id, type(exc).__name__)
            self.retain_local(job_id, approved, 'Review unavailable; local file retained')
        finally:
            await self.panel_status(context, 'transfer')
