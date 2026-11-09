import asyncio
import json
import time
from pathlib import Path
from urllib.parse import urlparse, urlsplit
import re

from telegram import (InlineKeyboardButton as Button, InlineKeyboardMarkup as Markup,
                      InlineQueryResultArticle, InputTextMessageContent)
from telegram.error import BadRequest, TelegramError
from .menu import navigation
from .artwork import landscape
from .media import reported_tracks, FORMATS, inspect_remote_media, track_summary
from .discovery import Discovery, PRESETS
from .addon_health import get_status, set_status, label
from .transfer import MAX_BYTES, MAX_DOWNLOAD_BYTES, inspect_url, safe_name
from .file_info import probe, clean_title
from .source_history import record_check, reliability
from .hls import inspect_hls, HLSError


class CatalogueFunctions:
    async def addons(self, update, context, page=0, repository=None, *, expected_generation=None, update_in_place=False):
        if not self.authorized(update): return
        context.user_data.pop('manifest_pending', None)
        service = Discovery(self.store)
        manifests = service.manifests(active=False)
        entries = []
        for url, kind, manifest in manifests:
            if repository and url != repository: continue
            if repository:
                for scraper in manifest.get('scrapers', []):
                    if not isinstance(scraper, dict): continue
                    filename = scraper.get('filename')
                    if not filename: continue
                    entries.append((scraper.get('name', filename), url, filename))
            else:
                entries.append((manifest.get('name', 'Add-on'), url, None))
        page = max(0, min(page, max(0, (len(entries)-1)//8)))
        rows = []
        for name, url, provider in entries[page*8:page*8+8]:
            active = service.enabled(url, provider)
            parent = service.enabled(url) if provider else True
            icon, mode, status = label(self.store, url, provider, active=active, parent_active=parent)
            if not provider:
                source = next(((kind, manifest) for item_url, kind, manifest in manifests if item_url == url), None)
                if source and source[0] == 'addon' and not any(
                    isinstance(c, dict) and any(isinstance(e, dict) and e.get('name') == 'search' for e in (c.get('extra') or []))
                    for c in source[1].get('catalogs', []) if isinstance(c, dict)):
                    status += '; no title search'
            key = self.store.cache('addon_control', {'url':url, 'provider':provider, 'repository':repository, 'page':page})
            rows.append([Button(f'{icon} {mode} · {str(name)[:40]} · {status}'[:120], callback_data=f'addontoggle:{key}')])
            if not repository:
                controls = [Button('Check', callback_data=f'addoncheck:{key}'),
                            Button('Refresh manifest', callback_data=f'addonrefresh:{key}')]
                if any(u == url and k == 'providers' for u, k, _ in manifests):
                    controls.append(Button('Providers', callback_data=f'addonproviders:{key}:0'))
                rows.append(controls)
        key = self.store.cache('addon_control', {'repository':repository, 'page':page})
        paging = []
        if page: paging.append(Button('Previous', callback_data=f'addonpage:{key}:{page-1}'))
        if (page+1)*8 < len(entries): paging.append(Button('More', callback_data=f'addonpage:{key}:{page+1}'))
        if paging: rows.append(paging)
        if not repository:
            saved = {url for url, _, _ in manifests}
            preset_buttons = []
            for preset, (name, url) in PRESETS.items():
                if url in saved: continue
                state = get_status(self.store, url, 'manifest')
                prefix = '🔴 ' if state in ('error', 'invalid') else ''
                preset_buttons.append(Button(f'{prefix}Add {name}', callback_data=f'addonpreset:{preset}'))
            if preset_buttons: rows.append(preset_buttons)
            if entries: rows.append([Button('Check manifests on this page', callback_data=f'addoncheckall:{key}')])
            rows.append([Button('Catalogues / artwork sources', callback_data='menu:catalogues')])
        rows.append([Button('Add manifest', callback_data='menu:addmanifest')])
        rows.append(navigation())
        title = 'Providers' if repository else 'Add-ons'
        text = (f'{title} · Page {page+1}\nTap a source to switch it ON/OFF. Changes apply to new lookups.\n'
                '🟢 manifest reachable · 🔴 last check or lookup failed / returned no results · ⚪ not checked or OFF.\n'
                'Checks expire after 15 minutes. No results can mean this title is unavailable, not that the source is down.')
        if repository and not service.enabled(repository): text += '\nRepository is OFF; provider choices apply after it is turned on.'
        if not entries: text += '\nNo sources saved. Add a preset or manifest.'
        markup = Markup(rows)
        if update_in_place:
            panel = self.panel()
            async with panel.lock:
                if panel.generation != expected_generation or panel.message is None: return
                panel.text, panel.markup = text, markup
                await self._render_panel(panel, message=update.message)
            return
        shown = await self.show_panel(update.message, text, reply_markup=markup, expected_generation=expected_generation)
        if shown is False or repository: return
        # A bounded background probe checks only enabled, stale visible manifests.
        inflight = getattr(self, '_addon_health_inflight', None)
        if inflight is None: inflight = self._addon_health_inflight = set()
        urls = [url for _, url, provider in entries[page*8:page*8+8]
                if not provider and service.enabled(url) and get_status(self.store, url, 'manifest') == 'unknown' and url not in inflight]
        if not urls: return
        inflight.update(urls)
        async def auto_check():
            semaphore = asyncio.Semaphore(4)
            async def one(url):
                async with semaphore:
                    if service.enabled(url): await service.check_manifest(url)
            try:
                await asyncio.gather(*(one(url) for url in urls))
                panel = self.panel()
                # A toggle can redraw this same list while checks are running.
                # Update that newer list, but never replace a different screen.
                if panel.text.startswith(f'Add-ons · Page {page+1}\n'):
                    await self.addons(update, context, page, repository, expected_generation=panel.generation, update_in_place=True)
            finally:
                inflight.difference_update(urls)
        self.launch_discovery(auto_check())

    async def addon_callback(self, update, context):
        from types import SimpleNamespace
        parts = update.callback_query.data.split(':')
        action = parts[0]
        message = update.callback_query.message
        if action == 'addonpreset':
            preset = PRESETS.get(parts[1]) if len(parts) == 2 else None
            if not preset: return
            name, url = preset
            if any(u == url for u, _, _ in Discovery(self.store).manifests(active=False)):
                proxy = SimpleNamespace(message=message, effective_user=update.effective_user, effective_chat=update.effective_chat)
                return await self.addons(proxy, context)
            await self.show_panel(message, f'Adding {name}…')
            generation = self.panel().generation
            async def add_preset():
                try:
                    await asyncio.wait_for(Discovery(self.store).add(url), 10)
                    result = f'Saved {name}. Open Add-ons to turn sources ON/OFF.'
                except Exception:
                    if get_status(self.store, url, 'manifest') == 'unknown': set_status(self.store, url, 'manifest', 'error')
                    result = f'🔴 Could not load {name}. Open Add-ons and retry when it is reachable.'
                await self.show_panel(None, result, context=context, expected_generation=generation, remember=False)
            return await self.start_panel_task('addon_check', f'Adding {name}', add_preset, context)
        try: control = self.store.cached(int(parts[1]), 'addon_control')
        except (ValueError, IndexError): return
        if not control: return await self.show_panel(message, 'This add-on list expired. Open Add-ons again.')
        repository, page = control.get('repository'), control.get('page', 0)
        service = Discovery(self.store)
        if action in ('addoncheck', 'addonrefresh', 'addoncheckall'):
            urls = [control['url']] if action != 'addoncheckall' else [url for url, _, _ in service.manifests(active=False)][page*8:page*8+8]
            if not urls: return
            await self.show_panel(message, 'Checking saved manifests…')
            generation = self.panel().generation
            proxy = SimpleNamespace(message=message, effective_user=update.effective_user, effective_chat=update.effective_chat)
            async def check():
                semaphore = asyncio.Semaphore(4)
                async def one(url):
                    async with semaphore: await service.check_manifest(url, refresh=action == 'addonrefresh')
                await asyncio.gather(*(one(url) for url in dict.fromkeys(urls)))
                await self.addons(proxy, context, page, repository, expected_generation=generation)
            return await self.start_panel_task('addon_check', 'Checking manifests', check, context)
        if action == 'addontoggle':
            # Ignore stale controls for removed repositories or providers.
            found = next(((kind, manifest) for url, kind, manifest in service.manifests(active=False) if url == control['url']), None)
            if not found: return await self.show_panel(message, 'This add-on no longer exists.')
            if control.get('provider') and not any(isinstance(p, dict) and p.get('filename') == control['provider'] for p in found[1].get('scrapers', [])):
                return await self.show_panel(message, 'This provider is no longer in the repository.')
            service.toggle(control['url'], control.get('provider'))
        else:
            try: page = int(parts[2])
            except (ValueError, IndexError): return
            if action == 'addonproviders': repository = control['url']
        proxy = SimpleNamespace(message=message, effective_user=update.effective_user, effective_chat=update.effective_chat)
        await self.addons(proxy, context, page, repository)

    async def add_manifest_prompt(self, update, context):
        if not self.authorized(update): return
        context.user_data.pop('uploaded_manifest', None)
        context.user_data['manifest_pending'] = True
        names = [m.get('name', 'Add-on') for _, _, m in Discovery(self.store).manifests()]
        await self.show_panel(update.message, 'Send the catalogue/add-on manifest URL (Stremio format), or a Nuvio provider manifest URL.\nSaved: ' + (', '.join(names) or 'none') + '\nUse /search <movie name> after adding a catalogue.',
            reply_markup=Markup([navigation()]))

    async def manifest_document(self, update, context):
        if not self.authorized(update): return
        document = update.message.document
        caption = getattr(update.message, 'caption', None)
        caption = caption if isinstance(caption, str) else ''
        match = re.search(r'https?://[^\s<>]+', caption)
        original_url = match.group(0).rstrip('.,)') if match else None
        if original_url and not urlsplit(original_url).path.lower().endswith('/manifest.json'):
            original_url = None
        if not context.user_data.get('manifest_pending') and not original_url:
            await self.show_panel(update.message, 'Open Add-ons first to import a manifest file, or send its manifest URL with the file.')
            return
        if not document.file_size or document.file_size > 1_000_000:
            await self.show_panel(update.message, 'Send a JSON manifest under 1 MB, or paste its URL.')
            return
        try:
            remote = await document.get_file()
            data = await remote.download_as_bytearray()
            if len(data) > 1_000_000: raise ValueError()
            manifest = json.loads(data)
            if not isinstance(manifest, dict): raise ValueError()
            if original_url:
                name, kind = Discovery(self.store).register(original_url, manifest)
                context.user_data.pop('manifest_pending', None)
                context.user_data.pop('uploaded_manifest', None)
                await self.show_panel(update.message, f'Saved {name} ({kind}). Send a movie title to search.')
            else:
                context.user_data['uploaded_manifest'] = manifest
                await self.show_panel(update.message, 'Now send the original manifest URL or repository base URL. The bot needs it to resolve catalogue endpoints and plugin filenames.')
        except Exception:
            await self.show_panel(update.message, 'Could not read this JSON manifest. Paste the original manifest URL instead.')

    def launch_discovery(self, coro):
        if not hasattr(self, 'discovery_tasks'): self.discovery_tasks = set()
        task = asyncio.create_task(coro)
        self.discovery_tasks.add(task)
        task.add_done_callback(self.discovery_tasks.discard)
        return task

    async def online_search(self, update, context):
        if not self.authorized(update): return
        for key in ('manifest_pending', 'catalogue_search_pending', 'search_pending'):
            context.user_data.pop(key, None)
        query = ' '.join(context.args).strip()
        if not query:
            context.user_data['catalogue_search_pending'] = True
            await self.show_panel(update.message, 'Send a movie name. Searching does not start a download. For inline search, type @your_bot_username followed by the title in Telegram.')
            return
        await self.begin_search(update.message, query)

    async def begin_search(self, message, query):
        await self.cancel_panel_tasks()
        if not any(k == 'addon' for _, k, _ in Discovery(self.store).manifests()):
            await self.show_panel(message, 'Add or enable a catalogue manifest using Add-ons first.')
            return
        await self.show_panel(message, f'Searching for {query[:120]}…', reply_markup=Markup([navigation()]))
        await self.start_panel_task('search', f'Search: {query[:120]}', lambda: self.search_results(message, query), None)

    async def search_results(self, message, query):
        try:
            movies, errors = await Discovery(self.store).search(query)
            buttons = []
            for movie in movies:
                key = self.store.cache('movie', movie)
                label = f'{movie["name"]} ({movie.get("year") or movie.get("releaseInfo") or "year unknown"})'
                if movie.get('type', 'movie') != 'movie': label += ' · Series / episodes'
                buttons.append([Button(label[:100], callback_data=f'movie:{key}')])
            result_buttons = [(row[0].text, row[0].callback_data) for row in buttons]
            text = 'Choose a movie to see its image, details and available files. No download starts until you select one.' if movies else 'No matching movies found.'
            if errors:
                text += '\nSome catalogues failed. Try again later.'
            key = self.store.cache('search_panel', {'text': text, 'buttons': result_buttons})
            panel = self.panel()
            async with panel.lock:
                if panel.text == f'Searching for {query[:120]}…':
                    panel.text = text
                    panel.markup = Markup(buttons + [navigation()])
                else:
                    panel.results[key] = f'Results: {query[:70]}'
                while len(panel.results) > 2: panel.results.pop(next(iter(panel.results)))
                if panel.message is not None: await self._render_panel(panel, message=message)
        except Exception:
            key = self.store.cache('search_panel', {'text': 'Search failed. Please try again later.', 'buttons': []})
            panel = self.panel()
            async with panel.lock:
                panel.results[key] = f'Search failed: {query[:65]}'
                if panel.message is not None:
                    await self._render_panel(panel, message=message)

    async def movie_card(self, context, key):
        movie = self.store.cached(key, 'movie')
        if not movie:
            await context.bot.send_message(self.admin_id, 'Search result expired. Use /search again.')
            return
        service = Discovery(self.store)
        try:
            movie = await service.details(movie)
        except Exception:
            pass
        semaphore = asyncio.Semaphore(4)
        lock = asyncio.Lock()
        source_ids, checked, errors, tasks, seen = [], [], [], set(), set()
        selection = self.store.cache('pages', [])
        if not hasattr(self, 'selection_pages'): self.selection_pages = {}
        if len(self.selection_pages) >= 512: self.selection_pages.pop(next(iter(self.selection_pages)))
        self.selection_pages[selection] = 0
        pending_render = None
        card = None
        card_is_photo = False
        panel = self.panel()
        generation = panel.generation
        last_update = 0.0
        artwork = movie.get('poster') or movie.get('background')
        metadata_url = movie.get('metadata_source') or movie.get('addon')
        metadata_name = next((m.get('name', 'Catalogue') for u, _, m in service.manifests(active=False) if u == metadata_url), 'Selected catalogue')
        artwork_url = movie.get('artwork_source') or metadata_url
        artwork_name = next((m.get('name', 'Catalogue') for u, _, m in service.manifests(active=False) if u == artwork_url), metadata_name)
        art_kind = 'poster' if movie.get('poster') else 'background (no poster supplied)'
        artwork_task = asyncio.create_task(landscape(artwork)) if artwork else None

        async def render(final=False):
            nonlocal card, card_is_photo, last_update
            # Cancellation removes this selection; don't revive it on a later result.
            cursor = self.store.db.execute("UPDATE discovery_cache SET payload=? WHERE id=? AND kind='pages'", (json.dumps(source_ids), selection))
            self.store.db.commit()
            if not cursor.rowcount: return
            qualities = ', '.join(dict.fromkeys(str(s.get('quality') or 'unspecified')[:35] for s in checked))[:250]
            episode_label = f' · S{movie["season"]:02d}E{movie["episode"]:02d}' if movie.get('season') is not None else ''
            count_label = 'Available options' if any(s.get('manual_page') for s in checked) else 'Available files'
            caption = f'{movie["name"][:150]}{episode_label}\nYear: {movie.get("year") or movie.get("releaseInfo") or "unknown"}\nQualities: {qualities or "none found"}\n{count_label}: {len(source_ids)}'
            caption += f'\nMetadata: {str(metadata_name)[:80]}\nArtwork: {str(artwork_name)[:80]} · {art_kind}'
            caption += '\nDW: checked for download. Manual and Retry links follow. Select a file to review it.'
            if not final: caption += '\nSearching other providers…'
            if errors: caption += f'\n{len(errors)} source(s) failed or need configuration; search again to retry.'
            for error in dict.fromkeys(e for e in errors if e.startswith('Plugin runtime:')):
                caption += '\n' + error
            if final and not source_ids:
                caption += '\nNo links returned. Try again later.' if errors else '\nNo playable links returned. Try another source or movie.'
            markup = self.file_page(selection, source_ids, self.selection_pages.get(selection, 0))
            async with panel.lock:
                if panel.generation != generation:
                    return
                if card is None:
                    try:
                        if not artwork_task: raise ValueError()
                        photo = await asyncio.wait_for(asyncio.shield(artwork_task), 2)
                        card = await context.bot.send_photo(self.admin_id, photo=photo, caption=caption, reply_markup=markup)
                        card_is_photo = True
                    except Exception:
                        card = await context.bot.send_message(self.admin_id, caption + '\nImage unavailable.', reply_markup=markup)
                    previous, old_card, old_preview = panel.message, panel.card, panel.preview
                    panel.message, panel.card, panel.preview = None, card, None
                    await self._retire_message(panel, previous)
                    if old_card is not previous and old_card is not card:
                        await self._retire_message(panel, old_card)
                    if old_preview is not previous and old_preview is not old_card and old_preview is not card:
                        await self._retire_message(panel, old_preview)
                else:
                    # Pagination may have replaced the card while providers were still loading.
                    card = panel.card
                    if card is None: return
                    try:
                        if card_is_photo: await card.edit_caption(caption=caption, reply_markup=markup)
                        else: await card.edit_text(caption + '\nImage unavailable.', reply_markup=markup)
                    except TelegramError as exc:
                        if not (isinstance(exc, BadRequest) and 'message is not modified' in str(exc).lower()):
                            # Preserve newly checked files when Telegram can no longer edit the card.
                            replacement = await context.bot.send_message(self.admin_id, caption, reply_markup=markup)
                            old_card = card
                            panel.card = card = replacement
                            card_is_photo = False
                            await self._retire_message(panel, old_card)
            last_update = time.monotonic()

        async def flush():
            await asyncio.sleep(max(0, 1 - (time.monotonic() - last_update)))
            async with lock:
                await render()

        async def inspect(stream):
            nonlocal pending_render
            async with semaphore:
                stream = dict(stream)
                stream['size_verified'] = False
                stream['file_type'] = Path(stream.get('filename') or urlparse(stream['url']).path).suffix.lstrip('.').lower() or 'unknown'
                headers = stream.get('headers') or {}
                stream['headers'] = {str(k): str(v) for k,v in headers.items()} if isinstance(headers, dict) else {}
                try:
                    if stream['url'].startswith('magnet:'):
                        from .torrent import normalize_magnet
                        stream['url'] = normalize_magnet(stream['url'])[0]
                        stream['torrent'] = True
                        stream['file_type'] = 'torrent'
                        info = {'status':'manual', 'reason':'Torrent metadata and file selection required'}
                    elif stream.get('manual_page'):
                        info = {'status':'manual', 'reason':'Provider page; open it to choose or download manually'}
                        stream['file_type'] = 'page'
                    else:
                        info = await asyncio.wait_for(probe(stream['url'], stream['headers']), 8)
                    stream['check_status'] = info.get('status','ready')
                    stream['check_reason'] = info.get('reason','')
                    stream['retry_after'] = info.get('retry_after')
                    stream['hls_candidate'] = info.get('hls_candidate', False)
                    if stream['check_status'] == 'ready':
                        size, content_type = info['size'], info['content_type']
                        stream['filename'] = info['filename'] if Path(info['filename']).suffix.lower() in ('.mkv','.mp4','.avi','.webm','.mov') else stream.get('filename') or info['filename']
                        stream['file_type'] = Path(stream['filename']).suffix.lstrip('.').lower() or stream['file_type']
                        stream['file_type'] = 'hls' if 'mpegurl' in content_type.lower() else stream['file_type']
                        stream['file_type'] = 'page' if 'text/html' in content_type.lower() else stream['file_type']
                        stream['size'] = size
                        stream['size_verified'] = bool(size)
                        if stream['file_type'] in ('hls', 'm3u8', 'mpd', 'page') or 'dash+xml' in content_type.lower():
                            stream['size'] = None
                            stream['size_verified'] = False
                except Exception:
                    stream['check_status'] = 'temporary'
                    stream['check_reason'] = 'Link check failed'
            record_check(self.store, stream, {'status':stream.get('check_status'), 'size':stream.get('size') if stream.get('size_verified') else None, 'retry_after':stream.get('retry_after')})
            if movie.get('daily_identity') and not (stream.get('check_status') == 'ready' and stream.get('size_verified') and 0 < stream.get('size', 0) < MAX_BYTES): return
            async with lock:
                if self.store.cached(selection, 'pages') is None: return
                # Keep unverified and failed links out of the persistent ready list.
                stream['movie'] = movie
                if stream.get('size_verified') and stream.get('size') and Path(stream.get('filename') or '').suffix.lower() in ('.mp4','.mkv','.avi','.mov','.webm'):
                    stream['identity'] = (movie.get('video_id') or movie['id']) + ':' + stream['filename'].casefold() + ':' + str(stream['size'])
                kind = 'stream' if stream.get('check_status') in ('ready','manual') else 'unchecked'
                source_key = self.store.cache(kind, stream)
                if kind == 'unchecked': source_key = -source_key
                if source_key not in source_ids:
                    source_ids.append(source_key)
                    checked.append(stream)
                if card is None or time.monotonic() - last_update >= 1:
                    await render()
                elif pending_render is None or pending_render.done():
                    pending_render = asyncio.create_task(flush())

        async def on_result(found, error):
            if error and error not in errors: errors.append(error)
            for stream in found:
                if stream['url'] in seen: continue
                seen.add(stream['url'])
                task = asyncio.create_task(inspect(stream))
                tasks.add(task)

        try:
            streams, final_errors = await service.streams(movie, on_result=on_result)
            for error in final_errors:
                if error not in errors: errors.append(error)
            # Also supports providers returning all results in one batch.
            await on_result(streams, None)
            if tasks: await asyncio.gather(*tasks)
            if pending_render and not pending_render.done():
                pending_render.cancel()
                await asyncio.gather(pending_render, return_exceptions=True)
            async with lock:
                await render(final=True)
                if movie.get('daily_identity') and not source_ids:
                    self.daily_init()
                    self.store.db.execute("UPDATE daily_titles SET state='no_files' WHERE identity=? AND state='offered'", (movie['daily_identity'],)); self.store.db.commit()
        finally:
            if pending_render and not pending_render.done(): pending_render.cancel()
            if pending_render: await asyncio.gather(pending_render, return_exceptions=True)
            for task in tasks: task.cancel()
            if tasks: await asyncio.gather(*tasks, return_exceptions=True)
            if artwork_task:
                if not artwork_task.done(): artwork_task.cancel()
                await asyncio.gather(artwork_task, return_exceptions=True)

    async def catalogue_callback(self, update, context):
        q = update.callback_query
        if q.data == 'searchagain':
            context.user_data['catalogue_search_pending'] = True
            await self.show_panel(q.message, 'Send another movie name.')
            return
        try:
            action, raw = q.data.split(':', 1); key = int(raw)
        except (ValueError, AttributeError): return
        if action == 'page':
            selection, page = divmod(key, 1000)
            ids = self.store.cached(selection, 'pages')
            if ids is None:
                await q.message.reply_text('Results expired or cancelled. Search again.'); return
            if not hasattr(self, 'selection_pages'): self.selection_pages = {}
            self.selection_pages[selection] = page
            panel = self.panel()
            async with panel.lock:
                if not self.same_message(panel.card, q.message):
                    return
                markup = self.file_page(selection, ids, page)
                if q.message.photo:
                    new_card = await context.bot.send_photo(self.admin_id, photo=q.message.photo[-1].file_id,
                                                            caption=q.message.caption, reply_markup=markup)
                else:
                    new_card = await context.bot.send_message(self.admin_id, q.message.text, reply_markup=markup)
                old = panel.card
                panel.card = new_card
                await self._retire_message(panel, old)
        elif action == 'cancelpage':
            getattr(self, 'selection_pages', {}).pop(key, None)
            self.store.db.execute("DELETE FROM discovery_cache WHERE id=? AND kind='pages'",(key,));self.store.db.commit()
            await self.main_menu(q.message)
        elif action == 'link':
            stream = self.store.cached(key, 'confirmation')
            if stream: await self.show_panel(q.message, stream['url'], disable_preview=True)
        elif action in ('yes', 'yeslocal', 'no'):
            stream = self.store.cached(key, 'confirmation')
            if not stream:
                await q.message.reply_text('This confirmation expired or was already used.'); return
            if action != 'no' and not await self.transfer_gate(q.message): return
            self.store.db.execute("DELETE FROM discovery_cache WHERE id=? AND kind='confirmation'",(key,));self.store.db.commit()
            if action != 'no':
                if action == 'yeslocal': stream.setdefault('transfer_options', {})['download_mode'] = 'local'
                self.active_description = f'Checking {stream.get("filename") or stream["movie"]["name"]}'
                self.spawn(self.download_choice(context, stream), kind='download', label=f'Download · {stream.get("filename") or stream["movie"]["name"]}', daily_identity=stream['movie'].get('daily_identity'))
            await self.show_panel(q.message, 'Download cancelled.' if action == 'no' else 'Download started. Open Active transfers for progress.')
        elif action == 'movie':
            await self.show_panel(q.message, 'Finding available files and checking sizes…')
            await self.start_panel_task('movie', 'Finding available files and checking sizes…', lambda: self.safe_movie_card(context, key), context)
        elif action in ('file','retryfile'):
            stream = self.store.cached(key, 'unchecked' if action == 'retryfile' else 'stream')
            if not stream:
                await q.message.reply_text('File result expired. Use /search again.'); return
            await self.show_panel(q.message, 'Checking the selected file…')
            await self.start_panel_task('file', f'Checking {stream.get("filename") or stream["movie"]["name"]}…', lambda: self.prepare_choice(context, stream), context)

    def file_page(self, selection, ids, page):
        choices = []
        for key in ids:
            stream=self.store.cached(-key,'unchecked') if key < 0 else self.store.cached(key,'stream')
            if stream:
                size = stream.get('size')
                direct = (key > 0 and stream.get('check_status') == 'ready'
                          and isinstance(size, (int, float)) and 0 < size <= MAX_DOWNLOAD_BYTES)
                choices.append((not direct, key, stream))
        # Stable sort retains provider arrival order within each group.
        choices.sort(key=lambda item: (item[0], -reliability(self.store, item[2])))
        page=max(0,min(page,max(0,(len(choices)-1)//4)))
        rows=[]
        for _, key, stream in choices[page*4:page*4+4]:
            size=stream.get('size'); size_text=f'{size/1_000_000:.1f} MB' if isinstance(size,(int,float)) else 'size unknown'
            action=('Unavailable' if stream.get('check_status') == 'unavailable' else 'Retry') if key < 0 else ('Torrent' if stream.get('torrent') else 'HLS' if stream.get('hls_candidate') else 'Manual' if stream.get('check_status') == 'manual' else ('Link' if isinstance(size,(int,float)) and size>MAX_DOWNLOAD_BYTES else ('DW' if size else 'Size unknown')))
            if action == 'DW' and sum(info['kind'] == 'download' for _, info in self.active_transfers()) >= 2: action = 'Wait (busy)'
            quality=str(stream.get('quality') or stream.get('title') or stream.get('name') or 'quality unknown')[:40]
            host = (urlparse(stream['url']).hostname or ('torrent' if stream.get('torrent') else 'unknown host'))[:28]
            rows.append([Button(f'{action} {quality} | {host} | {size_text} | {stream.get("file_type","unknown")}'[:120],callback_data=f'{"retryfile" if key < 0 else "file"}:{abs(key)}')])
        navigation=[]
        if page: navigation.append(Button('Previous',callback_data=f'page:{selection*1000+page-1}'))
        if (page+1)*4<len(choices):navigation.append(Button('More',callback_data=f'page:{selection*1000+page+1}'))
        if navigation: rows.append(navigation)
        rows.append([Button('Back', callback_data='menu:back'), Button('Cancel',callback_data=f'cancelpage:{selection}'), Button('Main menu', callback_data='menu:main')])
        return Markup(rows)

    async def prepare_choice(self, context, stream):
        generation = self.panel().generation
        try:
            if stream.get('torrent') or stream['url'].startswith('magnet:'):
                return await self.import_torrent(context, magnet=stream['url'])
            if stream.get('manual_page'):
                await context.bot.send_message(self.admin_id, f"Provider page; open it to choose or download manually.\n{stream['url']}", disable_web_page_preview=True)
                return
            stream = dict(stream, transfer_options=self.transfer_options())
            info=await probe(stream['url'],stream.get('headers'))
            if self.panel().generation != generation: return
            state=info.get('status','ready')
            if state != 'ready' and info.get('hls_candidate'):
                try: plan = await inspect_hls(stream['url'], stream.get('headers'), stream.get('quality'))
                except HLSError as exc:
                    if self.panel().generation != generation: return
                    await context.bot.send_message(self.admin_id, f'{exc}. You can use this link manually.\n{stream["url"]}', disable_web_page_preview=True); return
                except Exception:
                    if self.panel().generation != generation: return
                    await context.bot.send_message(self.admin_id, f'Could not check this HLS playlist. You can use the link manually.\n{stream["url"]}', disable_web_page_preview=True); return
                stream = dict(stream, hls=True, hls_format=plan['format'], size=None, file_type=plan['format'])
                key = self.store.cache('confirmation',stream)
                await self.show_panel(None, f"Download HLS?\nCatalogue: {clean_title(stream['movie'],stream)}\n{reported_tracks(stream)}\nOutput: {FORMATS[self.transfer_options()['output_format']]}\nSize: unknown; local limit 10 GB.\nTelegram upload is offered only if the finished file is at most 2 GB.", reply_markup=Markup([[Button('Transfer',callback_data=f'yes:{key}'),Button('Download only',callback_data=f'yeslocal:{key}'),Button('Give link',callback_data=f'link:{key}'),Button('No',callback_data=f'no:{key}')], navigation()]), context=context, expected_generation=generation, remember=False); return
            if state != 'ready':
                retry = info.get('retry_after')
                hint = f' Retry after {retry}.' if retry else ''
                if state == 'manual':
                    stream['check_status']='manual';stream['check_reason']=info.get('reason')
                    self.store.cache('stream',stream)
                await context.bot.send_message(self.admin_id, f"{info.get('reason','Link check failed')}.{hint} You can try the original link manually:\n{stream['url']}", disable_web_page_preview=True)
                return
            size=info['size']
            if not size:
                await context.bot.send_message(self.admin_id,f"File size is unknown. No download started. You can try the original link manually:\n{stream['url']}", disable_web_page_preview=True);return
            actual=info['filename'] if Path(info['filename']).suffix else stream.get('filename') or info['filename']
            stream=dict(stream,filename=actual,size=size,confirmed_size=size,check_status='ready')
            stream['file_type']=Path(actual).suffix.lstrip('.').lower() or stream.get('file_type','unknown')
            self.store.cache('stream',stream)
            if size>MAX_DOWNLOAD_BYTES:
                await context.bot.send_message(self.admin_id,f"{clean_title(stream['movie'],stream)}\n{actual}\n{size/1_000_000:.1f} MB — above 10 GB. Saved as a link. Download it manually.\n{stream['url']}",disable_web_page_preview=True)
                return
            key=self.store.cache('confirmation',stream)
            track_text = reported_tracks(stream)
            try:
                metadata = await asyncio.wait_for(inspect_remote_media(stream['url'], size, stream.get('headers')), 12)
                track_text = 'File sample (may be incomplete):\n' + track_summary(metadata)
            except Exception:
                pass
            choices = [] if size > MAX_BYTES else [Button('Transfer',callback_data=f'yes:{key}')]
            choices += [Button('Download only',callback_data=f'yeslocal:{key}'),Button('Give link',callback_data=f'link:{key}'),Button('No',callback_data=f'no:{key}')]
            await self.show_panel(None,f"Download this file?\nCatalogue: {clean_title(stream['movie'],stream)}\nActual file: {stream['filename']}\nSize: {size/1_000_000:.1f} MB\n{'Above 2 GB: local download only. ' if size > MAX_BYTES else ''}Format: {stream.get('file_type','unknown')}\nSource: {stream.get('provider','unknown')}\n{track_text}\nOutput: {FORMATS[self.transfer_options()['output_format']]}",reply_markup=Markup([choices, navigation()]), context=context, expected_generation=generation, remember=False)
        except Exception:
            if self.panel().generation != generation: return
            await context.bot.send_message(self.admin_id,f"Could not verify this source and its size. No download started. You can try the original link manually:\n{stream['url']}", disable_web_page_preview=True)

    async def safe_movie_card(self, context, key):
        try:
            movie = self.store.cached(key, 'movie')
            if movie and movie.get('type', 'movie') != 'movie' and movie.get('season') is None:
                movie = await Discovery(self.store).details(movie)
                await self.series_menu(context, movie)
            else:
                await self.movie_card(context, key)
        except Exception:
            await context.bot.send_message(self.admin_id, 'Could not fetch file results. Please try /search again.')

    async def download_choice(self, context, stream):
        url = stream['url']
        identity=stream.get('identity')
        duplicate=self.store.db.execute("SELECT jobs.id FROM jobs JOIN settings ON settings.key='job:' || jobs.id || ':identity' WHERE settings.value=? AND jobs.state NOT IN ('failed','cancelled') LIMIT 1",(identity,)).fetchone() if identity else None
        if duplicate or self.store.find_source(url):
            await context.bot.send_message(self.admin_id, 'This source is already recorded. Check pending uploads or saved links.'); return
        info=await probe(url, stream.get('headers'))
        size, content_type=info.get('size'), info.get('content_type','')
        is_hls = bool(stream.get('hls'))
        if is_hls and not info.get('hls_candidate'):
            await context.bot.send_message(self.admin_id, f'HLS source changed. You can try the original link manually:\n{url}', disable_web_page_preview=True); return
        if not is_hls and (info.get('status','ready') != 'ready' or not size):
            await context.bot.send_message(self.admin_id, f"Link status changed: {info.get('reason','size unknown')}. You can try the original link manually:\n{url}", disable_web_page_preview=True); return
        if stream.get('confirmed_size') and size != stream['confirmed_size']:
            await context.bot.send_message(self.admin_id, f'The source size changed. Select the file again to review it or try the original link manually:\n{url}', disable_web_page_preview=True); return
        if not self.transfers_enabled():
            await context.bot.send_message(self.admin_id, 'Transfers are off. No download started.'); return
        movie = stream['movie']
        if movie.get('daily_identity'):
            self.daily_init()
            row = self.store.db.execute("SELECT state FROM daily_titles WHERE identity=?", (movie['daily_identity'],)).fetchone()
            if not row or row[0] != 'offered':
                await context.bot.send_message(self.admin_id, 'This daily selection is no longer active.'); return
        title_identity = movie.get('type', 'movie') + ':' + (movie.get('video_id') or movie.get('id') or url)
        existing_title = self.store.setting('title:' + title_identity)
        previous = self.store.get(int(existing_title)) if existing_title else None
        if previous and previous['state'] not in ('failed', 'cancelled'):
            await context.bot.send_message(self.admin_id, 'This title is already recorded. Check Upload approvals or the library.'); return
        quality = str(stream.get('quality') or stream.get('title') or 'unknown')[:80]
        suffix = Path(stream.get('filename') or urlparse(url).path).suffix.lower()
        if is_hls: suffix = '.' + stream['hls_format']
        elif suffix not in ('.mp4', '.mkv', '.avi', '.mov', '.webm', '.m4v'): suffix = '.mp4'
        # Other downloads may have completed the initial check while this
        # source was being probed. Claim it once, before any further await.
        duplicate = self.store.db.execute("SELECT jobs.id FROM jobs JOIN settings ON settings.key='job:' || jobs.id || ':identity' WHERE settings.value=? AND jobs.state NOT IN ('failed','cancelled') LIMIT 1", (identity,)).fetchone() if identity else None
        if duplicate or self.store.find_source(url):
            await context.bot.send_message(self.admin_id, 'This source is already recorded. Check pending uploads or saved links.'); return
        title = clean_title(movie,stream)
        filename = safe_name(title) + suffix
        job = self.store.create(url, title, filename, size or 0)
        self.bind_transfer_job(job, f'Download #{job} · {filename}')
        self.save_transfer_options(job, stream.get('transfer_options'))
        if is_hls:
            self.store.set_setting(f'job:{job}:hls', stream['hls_format'])
            self.store.set_setting(f'job:{job}:quality', stream.get('quality') or '')
        self.store.set_setting('title:' + title_identity, job)
        if movie.get('type', 'movie') == 'movie':
            from .daily import daily_title_alias
            self.store.set_setting(daily_title_alias(movie), 'job:' + str(job))
        if movie.get('daily_identity'):
            self.store.db.execute("UPDATE daily_titles SET state='transferring', job_id=? WHERE identity=?", (job, movie['daily_identity'])); self.store.db.commit()
        if identity: self.store.set_setting(f'job:{job}:identity',identity)
        self.store.set_setting(f'job:{job}:original', filename if is_hls else stream.get('filename') or filename)
        self.store.set_setting(f'job:{job}:headers', json.dumps(stream.get('headers') or {}))
        if not is_hls and (size > MAX_DOWNLOAD_BYTES or any(t in content_type.lower() for t in ('text/html', 'mpegurl', 'dash+xml')) or stream['file_type'] in ('hls', 'm3u8', 'mpd', 'page')):
            self.store.update(job, state='oversize' if size > MAX_DOWNLOAD_BYTES else 'link_only')
            await context.bot.send_message(self.admin_id, 'Saved this version as a link in Saved links. It exceeds the limit or uses a source format not supported by the downloader yet.'); return
        self.store.update(job, state='downloading')
        self.active_description = f'Downloading #{job} · {filename}'
        folder = self.media_dir / str(job); folder.mkdir(exist_ok=True)
        try:
            artwork = movie.get('poster') or movie.get('background')
            if artwork:
                image = await landscape(artwork, small=True)
                path = folder / 'catalogue.jpg'; path.write_bytes(image.getvalue())
                self.store.update(job, thumbnail=str(path))
        except asyncio.CancelledError:
            self.store.update(job, state='cancelled', error='Cancelled')
            raise
        except Exception:
            pass
        await self.perform_download(context, job)

    async def inline_search(self, update, context):
        q = update.inline_query
        if not q or not self.is_admin(q.from_user.id):
            if q: await q.answer([], cache_time=0, is_personal=True)
            return
        query = q.query.strip()
        results = []
        if len(query) >= 3:
            try:
                movies, _ = await asyncio.wait_for(Discovery(self.store).search(query), 8)
                for movie in movies[:15]:
                    key = self.store.cache('movie', movie)
                    results.append(InlineQueryResultArticle(str(key), movie['name'],
                        description=str(movie.get('year') or movie.get('releaseInfo') or ''),
                        thumbnail_url=movie.get('poster') or movie.get('background'),
                        input_message_content=InputTextMessageContent(f'{movie["name"]}\n{movie.get("year") or movie.get("releaseInfo") or ""}'),
                        reply_markup=Markup([[Button('View available files', url=f'https://t.me/{context.bot.username}?start=movie_{key}')]])))
            except Exception:
                pass
        await q.answer(results, cache_time=0, is_personal=True)

    async def catalogues_menu(self, message, page=0):
        sources = [(url, kind, manifest) for url, kind, manifest in Discovery(self.store).manifests(active=False) if kind == 'addon']
        page = max(0, min(page, max(0, (len(sources)-1)//5)))
        lines = ['Your catalogues and metadata sources', 'Artwork preference: poster; background only if no poster is supplied.']
        for url, _, manifest in sources[page*5:page*5+5]:
            enabled = Discovery(self.store).enabled(url)
            lines.append(f'\n{manifest.get("name", "Catalogue")} · {"ON" if enabled else "OFF"} · {urlparse(url).hostname}')
            catalogs = manifest.get('catalogs', [])
            for catalog in catalogs[:10]:
                if isinstance(catalog, dict):
                    lines.append(f'• {catalog.get("name") or catalog.get("id")} ({catalog.get("type", "unknown")})')
            if len(catalogs) > 10: lines.append(f'… {len(catalogs)-10} more catalogues')
            if not catalogs: lines.append('Stream / metadata add-on; no browse catalogues declared.')
        if not sources: lines.append('No catalogue add-ons saved. Add your manifest in Add-ons.')
        rows = []
        paging = []
        if page: paging.append(Button('Previous', callback_data=f'menu:cataloguespage:{page-1}'))
        if (page+1)*5 < len(sources): paging.append(Button('More', callback_data=f'menu:cataloguespage:{page+1}'))
        if paging: rows.append(paging)
        rows.append(navigation())
        await self.show_panel(message, '\n'.join(lines)[:3900], reply_markup=Markup(rows))
