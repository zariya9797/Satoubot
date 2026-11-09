"""Catalogue add-ons and Nuvio provider repository adapters."""
import asyncio
import hashlib
from functools import lru_cache
import subprocess
import json
import copy
import time
import re
import shutil
from pathlib import Path
from urllib.parse import quote, urljoin, urlparse

from .addon_health import set_status
from .plugin_network import fetch_public
from .media_identity import supports_resource, resolve_tmdb

import httpx

HDHUB = 'https://hdhub.thevolecitor.qzz.io/eyJ0b3Jib3giOiJ1bnNldCIsInF1YWxpdGllcyI6IjIxNjBwLDEwODBwLDcyMHAiLCJzb3J0IjoiZGVzYyJ9/manifest.json'

YORU = 'https://raw.githubusercontent.com/yoruix/nuvio-providers/refs/heads/main/manifest.json'
PRESETS = {'yoru': ('Yoru providers', YORU), 'hdhub': ('HDHub', HDHUB)}

PROVIDER_TIMEOUT = 30
SEARCH_TIMEOUT = 90


async def fetch_bytes(url, limit=8_000_000, headers=None):
    if urlparse(url).scheme not in ('https', 'http'):
        raise ValueError('Expected an HTTP(S) URL')
    async with httpx.AsyncClient(follow_redirects=True, timeout=25) as client:
        async with client.stream('GET', url, headers=headers) as response:
            response.raise_for_status()
            data = bytearray()
            async for chunk in response.aiter_bytes():
                data.extend(chunk)
                if len(data) > limit:
                    raise ValueError('Response too large')
            return bytes(data)


async def fetch_json(url):
    return json.loads(await fetch_bytes(url))


def stream_records(items, name):
    found = []
    for item in items[:30]:
        if not isinstance(item, dict): continue
        item = dict(item)
        if not item.get('url') and isinstance(item.get('infoHash'), str):
            from .torrent import normalize_magnet, TorrentError
            try: item['url'] = normalize_magnet('magnet:?xt=urn:btih:'+item['infoHash'])[0]
            except TorrentError: continue
        if not isinstance(item.get('url'), str): continue
        hints = item.get('behaviorHints') or {}
        headers = (hints.get('proxyHeaders') or {}).get('request', {})
        description = str(item.get('description') or item.get('title') or '')
        candidate = re.sub(r'^\s*(?:\[[^\]]*\]\s*)+', '', description.split('\n')[0])
        filename = hints.get('filename') or (candidate if re.search(r'\.(mkv|mp4|avi|webm)\b', candidate, re.I) else None)
        found.append(dict(item, headers=item.get('headers', headers), provider=name,
                          size=item.get('size', hints.get('videoSize')), filename=filename))
    return found


class Discovery:
    def __init__(self, store):
        self.store = store

    async def cached_json(self, url, ttl=60):
        cache = getattr(self.store, '_discovery_memo', None)
        if cache is None:
            cache = self.store._discovery_memo = {}
        entry = cache.get(url)
        if entry and entry[0] > time.monotonic():
            return copy.deepcopy(entry[1])
        data = await fetch_json(url)
        if len(cache) >= 256:
            cache.pop(next(iter(cache)))
        cache[url] = (time.monotonic() + ttl, copy.deepcopy(data))
        return data

    async def add(self, url):
        url = url.strip().replace('stremio://', 'https://', 1)
        if not urlparse(url).path.endswith('manifest.json'):
            url = url.rstrip('/') + '/manifest.json'
        try:
            manifest = await self.cached_json(url)
            result = self.register(url, manifest, invalidate=False)
        except (ValueError, TypeError):
            set_status(self.store, url, 'manifest', 'invalid')
            raise
        except Exception:
            set_status(self.store, url, 'manifest', 'error')
            raise
        set_status(self.store, url, 'manifest', 'ok')
        return result

    async def check_manifest(self, url, *, refresh=False):
        """Probe one saved manifest without testing providers or changing switches."""
        row = self.store.db.execute('SELECT kind FROM manifests WHERE url=?', (url,)).fetchone()
        if not row: return 'unknown'
        try:
            async with asyncio.timeout(8):
                manifest = await fetch_json(url)
            if not isinstance(manifest, dict): raise ValueError('Invalid manifest')
            if row['kind'] == 'providers':
                if not isinstance(manifest.get('scrapers'), list): raise ValueError('Invalid provider manifest')
            elif (isinstance(manifest.get('scrapers'), list) or
                  any(key in manifest and not isinstance(manifest[key], list) for key in ('catalogs', 'resources')) or
                  not any(isinstance(manifest.get(key), list) for key in ('catalogs', 'resources'))):
                raise ValueError('Invalid add-on manifest')
            if refresh:
                # Preserve switch settings and use only the same previously saved URL.
                self.register(url, manifest)
        except (ValueError, TypeError):
            set_status(self.store, url, 'manifest', 'invalid')
            return 'invalid'
        except Exception:
            set_status(self.store, url, 'manifest', 'error')
            return 'error'
        set_status(self.store, url, 'manifest', 'ok')
        return 'ok'

    def register(self, url, manifest, invalidate=True):
        if urlparse(url).scheme not in ('http', 'https'):
            raise ValueError('Expected an HTTP(S) base URL')
        if not urlparse(url).path.endswith('manifest.json'):
            url = url.rstrip('/') + '/manifest.json'
        if not isinstance(manifest, dict):
            raise ValueError('Invalid manifest')
        if invalidate:
            cache = getattr(self.store, '_discovery_memo', {})
            prefix = url.rsplit('/',1)[0] + '/'
            for key in list(cache):
                if key.startswith(prefix): cache.pop(key, None)
        kind = 'providers' if isinstance(manifest.get('scrapers'), list) else 'addon'
        if kind == 'addon' and not isinstance(manifest.get('catalogs', []), list):
            raise ValueError('Expected a Nuvio repository or catalogue add-on manifest')
        self.store.db.execute('INSERT OR REPLACE INTO manifests(url,kind,data) VALUES (?,?,?)',
                              (url, kind, json.dumps(manifest)))
        self.store.db.commit()
        return manifest.get('name', 'Add-on'), kind

    def enabled(self, url, provider=None, default_enabled=None):
        identity = url + ('#' + provider if provider else '')
        key = 'enabled:' + hashlib.sha256(identity.encode()).hexdigest()
        if default_enabled is None and provider:
            row = self.store.db.execute('SELECT data FROM manifests WHERE url=? AND kind=?', (url, 'providers')).fetchone()
            if row:
                scrapers = json.loads(row['data']).get('scrapers', [])
                provider_info = next((p for p in scrapers if isinstance(p, dict) and p.get('filename') == provider), {})
                default_enabled = provider_info.get('enabled', True)
        if default_enabled is None: default_enabled = True
        return self.store.setting(key, '1' if default_enabled else '0') == '1'

    def toggle(self, url, provider=None):
        identity = url + ('#' + provider if provider else '')
        key = 'enabled:' + hashlib.sha256(identity.encode()).hexdigest()
        self.store.set_setting(key, '0' if self.enabled(url, provider) else '1')
        self.store.set_setting('source_selection_explicit', '1')

    def manifests(self, active=True):
        return [(r['url'], r['kind'], json.loads(r['data'])) for r in self.store.db.execute('SELECT * FROM manifests')
                if not active or self.enabled(r['url'])]

    async def search(self, query):
        results, errors = [], []
        for url, kind, manifest in self.manifests():
            if kind != 'addon': continue
            queried = failures = matches_count = 0
            for catalog in manifest.get('catalogs', []):
                media_type = catalog.get('type')
                if not isinstance(media_type, str) or not media_type: continue
                extras = catalog.get('extra', [])
                if not any(e.get('name') == 'search' for e in extras if isinstance(e, dict)): continue
                try:
                    queried += 1
                    endpoint = f'catalog/{quote(media_type, safe="")}/{quote(str(catalog["id"]), safe="")}/search={quote(query, safe="")}.json'
                    data = await fetch_json(urljoin(url, endpoint))
                    metas = data.get('metas')
                    if not isinstance(metas, list): raise ValueError('Invalid catalogue response')
                    matches_count += sum(1 for meta in metas[:20] if isinstance(meta, dict) and meta.get('id') and meta.get('name'))
                    for meta in metas[:20]:
                        if not isinstance(meta, dict) or not meta.get('id') or not meta.get('name'): continue
                        existing = next((m for m in results if m['id'] == str(meta['id']) and m.get('type') == media_type), None)
                        source = {'name': manifest.get('name', 'Catalogue'), 'catalog': str(catalog['id']), 'url': url}
                        if existing:
                            existing.setdefault('catalogue_sources', []).append(source)
                            continue
                        results.append(dict(meta, id=str(meta['id']), type=media_type, addon=url, catalogue_sources=[source]))
                except Exception:
                    failures += 1
                    errors.append(manifest.get('name', 'Catalogue'))
            if queried:
                set_status(self.store, url, 'search', 'error' if failures == queried else 'partial' if failures else 'ok' if matches_count else 'empty')
        normalized = lambda text: ' '.join(re.findall(r'\w+', text.casefold()))
        term = normalized(query)
        terms=term.split()
        matches=[m for m in results if all(part in normalized(m['name']).split() for part in terms)] if terms else []
        if matches: results=matches
        results.sort(key=lambda m: (normalized(m['name']) != term, not normalized(m['name']).startswith(term)))
        return results[:20], errors

    async def details(self, movie):
        if not self.enabled(movie['addon']):
            raise ValueError('This catalogue is disabled')
        media_type, item_id = movie.get('type', 'movie'), movie['id']
        manifests = self.manifests()
        candidates = [movie['addon']] + [url for url, kind, manifest in manifests if kind == 'addon' and url != movie['addon'] and supports_resource(manifest, 'meta', media_type, item_id)]
        last_error = None
        for url in candidates:
            manifest = next((m for u, _, m in manifests if u == url), {})
            if manifest.get('resources') and not supports_resource(manifest, 'meta', media_type, item_id): continue
            try:
                data = await self.cached_json(urljoin(url, f'meta/{quote(media_type, safe="")}/{quote(item_id, safe="")}.json'))
                meta = data.get('meta')
                if not isinstance(meta, dict) or not meta: continue
                # Metadata enrichment must not replace the selected catalogue/episode identity.
                result = dict(movie, **meta)
                result['metadata_source'] = url
                if result.get('poster'):
                    result['artwork_source'] = url if meta.get('poster') else movie.get('artwork_source') or movie['addon']
                elif result.get('background'):
                    result['artwork_source'] = url if meta.get('background') else movie.get('artwork_source') or movie['addon']
                for key in ('id', 'type', 'addon', 'video_id', 'season', 'episode', 'embedded_streams', 'catalogue_sources'):
                    if key in movie: result[key] = movie[key]
                return result
            except Exception as exc:
                last_error = exc
                continue
        if last_error is not None: raise last_error
        return movie

    async def streams(self, movie, on_result=None):
        """Query sources with bounded concurrency and report each completed batch."""
        streams, errors, seen = [], [], set()
        media_type = movie.get('type', 'movie')
        provider_type = 'tv' if movie.get('season') is not None else 'movie'
        stream_id = movie.get('video_id') or movie['id']
        embedded = movie.get('embedded_streams', []) if movie.get('season') is not None else movie.get('streams', [])
        embedded_source = movie.get('metadata_source') or movie.get('addon')
        if embedded_source and not self.enabled(embedded_source): embedded = []
        direct = stream_records(embedded, 'Catalogue') if isinstance(embedded, list) else []
        if direct:
            if embedded_source: set_status(self.store, embedded_source, 'stream', 'ok')
            if on_result: await on_result(direct, None)
            return direct, []
        semaphore = asyncio.Semaphore(4)
        addon_limits = {}
        mapping_task = None

        async def plugin_identity():
            nonlocal mapping_task
            if mapping_task is None: mapping_task = asyncio.create_task(resolve_tmdb(self, movie))
            try: return await asyncio.shield(mapping_task)
            except ValueError as exc: raise ProviderRuntimeError('Plugin runtime: ' + str(exc)) from exc
            except Exception as exc: raise ProviderRuntimeError('Plugin runtime: Catalogue lookup failed. Retry later; stream add-ons may still return files.') from exc

        async def query(url, source, kind):
            name = source.get('name', 'Provider' if kind == 'providers' else 'Add-on')
            async with semaphore:
                if not self.enabled(url) or (kind == 'providers' and not self.enabled(url, source['filename'])):
                    return [], None
                try:
                    async with asyncio.timeout(PROVIDER_TIMEOUT):
                        if kind == 'providers':
                            tmdb = await plugin_identity()
                            code = (await fetch_bytes(urljoin(url, source['filename']))).decode()
                            found = await run_plugin(code, str(tmdb), 'tv', movie['season'], movie['episode']) if provider_type == 'tv' else await run_plugin(code, str(tmdb))
                            records = [dict(item, provider=name, source_manifest=url, source_provider=source['filename']) for item in found[:30]
                                       if isinstance(item, dict) and isinstance(item.get('url'), str)]
                            set_status(self.store, url, 'stream', 'ok' if records else 'empty', source['filename'])
                            return records, None
                        data = await fetch_json(urljoin(url, f'stream/{quote(media_type, safe="")}/{quote(stream_id, safe="")}.json'))
                        raw = data.get('streams')
                        if not isinstance(raw, list): raise ValueError('Invalid stream response')
                        records = [dict(item, source_manifest=url) for item in stream_records(raw, name)]
                        set_status(self.store, url, 'stream', 'ok' if records else 'empty')
                        return records, None
                except ProviderRuntimeError as exc:
                    set_status(self.store, url, 'stream', 'error', source.get('filename') if kind == 'providers' else None)
                    return [], str(exc)
                except Exception:
                    set_status(self.store, url, 'stream', 'error', source.get('filename') if kind == 'providers' else None)
                    return [], name

        async def addon_query(url, manifest):
            origin = urlparse(url).netloc.lower()
            limit = addon_limits.setdefault(origin, asyncio.Semaphore(1))
            async with limit:
                return await query(url, manifest, 'addon')

        manifests = self.manifests()
        tasks = set()
        all_tasks = set()

        def launch(coro):
            task = asyncio.create_task(coro)
            tasks.add(task)
            all_tasks.add(task)

        for url, kind, manifest in manifests:
            if kind == 'addon':
                if supports_resource(manifest, 'stream', media_type, stream_id):
                    launch(addon_query(url, manifest))
        for url, kind, manifest in manifests:
            if kind == 'providers':
                for p in manifest['scrapers']:
                    if not isinstance(p, dict) or not isinstance(p.get('filename'), str) or not p['filename']: continue
                    if provider_type in (p.get('supportedTypes') or []) and self.enabled(url, p['filename']):
                        launch(query(url, p, kind))
        try:
            async with asyncio.timeout(SEARCH_TIMEOUT):
                while tasks:
                    done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                    tasks.difference_update(done)
                    for task in done:
                        found, error = await task
                        fresh = []
                        for item in found or []:
                            if item['url'] not in seen:
                                seen.add(item['url']); streams.append(item); fresh.append(item)
                        if error and error not in errors: errors.append(error)
                        if on_result and (fresh or error):
                            await on_result(fresh, error)
        except TimeoutError:
            errors.append('Some providers timed out')
        finally:
            for task in all_tasks:
                if not task.done(): task.cancel()
            if all_tasks: await asyncio.gather(*all_tasks, return_exceptions=True)
            if mapping_task is not None:
                if not mapping_task.done(): mapping_task.cancel()
                await asyncio.gather(mapping_task, return_exceptions=True)
        return streams, errors


class ProviderRuntimeError(RuntimeError):
    """An actionable provider runtime configuration problem."""


@lru_cache(maxsize=8)
def node_major(node):
    try:
        result = subprocess.run([node, '--version'], capture_output=True, text=True,
                                check=True, timeout=5)
        match = re.fullmatch(r'v(\d+)\.\d+\.\d+(?:[-+].*)?', result.stdout.strip())
        if not match:
            raise ValueError('Unexpected version')
        major = int(match.group(1))
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        raise ProviderRuntimeError('Plugin runtime: could not determine the Node.js version.') from exc
    if major < 24:
        raise ProviderRuntimeError(f'Plugin runtime: Node.js 24 or newer is required (found {major}).')
    return major


async def run_plugin(code, tmdb, media_type='movie', season=None, episode=None):
    runtime = (Path(__file__).parent / 'plugin_runtime').resolve()
    node, bwrap = shutil.which('node'), shutil.which('bwrap')
    if not node or not bwrap:
        raise ProviderRuntimeError('Plugin runtime: Node.js 24+ and bubblewrap are required for provider isolation.')
    node_major(node)
    # Node 25 restricts networking by default; the isolated worker only uses the
    # broker via stdio, so it does not need --allow-net on any Node version.
    # Only the runtime and system libraries are visible; the isolated worker has
    # its own network namespace. The host broker is its only network path.
    process = await asyncio.create_subprocess_exec(
        bwrap, '--unshare-user', '--unshare-net', '--unshare-pid', '--unshare-ipc', '--unshare-uts', '--cap-drop', 'ALL', '--die-with-parent', '--new-session',
        '--ro-bind', '/usr', '/usr', '--ro-bind', '/lib', '/lib', '--ro-bind', '/lib64', '/lib64',
        '--dev', '/dev', '--dir', '/tmp', '--ro-bind', str(runtime), '/app',
        '--ro-bind', node, '/node', '--chdir', '/app',
        '/node', '--permission', '--allow-fs-read=/app', '--max-old-space-size=128', '/app/runner.cjs',
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        env={'PATH': '/usr/bin:/bin'}, limit=2_100_000,
    )
    requests = set()
    lock = asyncio.Lock()
    semaphore = asyncio.Semaphore(4)
    async def respond(message):
        try:
            async with semaphore:
                response = await fetch_public(message.get('url'), message.get('method'), message.get('headers'), message.get('body'))
            reply = {'id': message['id'], 'response': response}
        except Exception:
            reply = {'id': message['id'], 'error': 'Request failed or destination is not allowed'}
        async with lock:
            process.stdin.write((json.dumps(reply) + '\n').encode())
            await process.stdin.drain()
    async def run():
        process.stdin.write((json.dumps({'code': code, 'tmdb': tmdb, 'mediaType': media_type, 'season': season, 'episode': episode}) + '\n').encode())
        await process.stdin.drain()
        count = 0
        while line := await process.stdout.readline():
            message = json.loads(line)
            if message.get('type') == 'result':
                result = message.get('results')
                if not isinstance(result, list): raise ValueError('Invalid provider results')
                return result
            if message.get('type') != 'fetch' or not isinstance(message.get('id'), int):
                raise ValueError('Invalid provider request')
            count += 1
            if count > 100: raise ValueError('Too many provider requests')
            task = asyncio.create_task(respond(message))
            requests.add(task)
            task.add_done_callback(requests.discard)
        raise ValueError('Provider failed or isolation unavailable')
    try:
        return await asyncio.wait_for(run(), 65)
    finally:
        for task in requests: task.cancel()
        if requests: await asyncio.gather(*requests, return_exceptions=True)
        if process.returncode is None:
            process.kill()
        await process.wait()
