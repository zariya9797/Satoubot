"""Explicit, bounded public-torrent downloads through a task-owned aria2 process.

HTTP files retain the public-only HTTP downloader. Web seeds and trackers are
removed here: torrents use DHT/peer exchange, with no private tracker credentials.
"""
import asyncio
import base64
import hashlib
import re
import shutil
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .transfer import MAX_DOWNLOAD_BYTES

MAX_TORRENT_BYTES = 4_000_000


class TorrentError(ValueError):
    pass


def decode(data):
    if not isinstance(data, bytes) or len(data) > MAX_TORRENT_BYTES: raise TorrentError('Invalid or oversized torrent')
    pos = 0
    def read(depth=0):
        nonlocal pos
        if depth > 32 or pos >= len(data): raise TorrentError('Invalid torrent structure')
        marker = data[pos:pos+1]
        if marker == b'i':
            end = data.find(b'e', pos)
            token = data[pos+1:end]
            if end < 0 or not re.fullmatch(rb'0|-?[1-9][0-9]*', token) or len(token) > 20: raise TorrentError('Invalid torrent integer')
            pos = end+1
            return int(token)
        if marker in (b'l', b'd'):
            pos += 1
            result = [] if marker == b'l' else {}
            previous = None
            while pos < len(data) and data[pos:pos+1] != b'e':
                key = read(depth+1)
                if marker == b'l': result.append(key)
                else:
                    if not isinstance(key, bytes) or (previous is not None and key <= previous): raise TorrentError('Invalid torrent dictionary')
                    previous = key
                    result[key] = read(depth+1)
            if pos >= len(data): raise TorrentError('Truncated torrent')
            pos += 1
            return result
        colon = data.find(b':', pos)
        token = data[pos:colon]
        if colon < 0 or not re.fullmatch(rb'0|[1-9][0-9]*', token) or len(token) > 10: raise TorrentError('Invalid torrent string')
        length = int(token); pos = colon+1
        if pos+length > len(data): raise TorrentError('Truncated torrent string')
        value = data[pos:pos+length]; pos += length
        return value
    result = read()
    if pos != len(data) or not isinstance(result, dict): raise TorrentError('Invalid torrent file')
    return result


def encode(value):
    if isinstance(value, int): return b'i'+str(value).encode()+b'e'
    if isinstance(value, bytes): return str(len(value)).encode()+b':'+value
    if isinstance(value, list): return b'l'+b''.join(map(encode,value))+b'e'
    if isinstance(value, dict): return b'd'+b''.join(encode(k)+encode(value[k]) for k in sorted(value))+b'e'
    raise TorrentError('Invalid torrent field')


def component(raw):
    if not isinstance(raw, bytes): raise TorrentError('Invalid torrent filename')
    try: text = raw.decode('utf-8')
    except UnicodeError: raise TorrentError('Torrent filenames must be UTF-8') from None
    if not text or text in ('.','..') or len(text) > 180 or re.search(r'[\\/:\x00-\x1f]', text): raise TorrentError('Unsafe torrent filename')
    return text


def inspect_torrent(data):
    metadata = decode(data)
    info = metadata.get(b'info')
    if not isinstance(info, dict) or b'pieces' not in info: raise TorrentError('Only v1 and hybrid BitTorrent files are supported')
    if info.get(b'private'): raise TorrentError('Private tracker torrents are not supported by the DHT downloader')
    piece_length = info.get(b'piece length')
    if not isinstance(piece_length, int) or not 0 < piece_length <= 16 * 1024 * 1024: raise TorrentError('Unsupported torrent piece size')
    root = component(info.get(b'name'))
    if info.get(b'name.utf-8', info.get(b'name')) != info.get(b'name') or b'symlink path' in info or b'l' in info.get(b'attr', b''):
        raise TorrentError('Unsupported alternate torrent name or link')
    files = info.get(b'files')
    if files is None: files = [{b'path': [], b'length': info.get(b'length')}]
    if not isinstance(files, list) or not 0 < len(files) <= 2000: raise TorrentError('Torrent has too many files')
    entries = []; seen = set()
    for index, item in enumerate(files, 1):
        if not isinstance(item, dict) or b'symlink path' in item or b'l' in item.get(b'attr', b''): raise TorrentError('Torrent links are not allowed')
        parts = item.get(b'path')
        if not isinstance(parts, list) or item.get(b'path.utf-8', parts) != parts: raise TorrentError('Invalid or alternate torrent path')
        name = str(Path(root, *(component(p) for p in parts)))
        length = item.get(b'length')
        if not isinstance(length, int) or length < 0 or name in seen: raise TorrentError('Invalid torrent file size/path')
        seen.add(name)
        entries.append({'index': index, 'name': name, 'size': length})
    total = sum(entry['size'] for entry in entries)
    pieces = info[b'pieces']
    if not isinstance(pieces, bytes) or len(pieces) != ((total+piece_length-1)//piece_length)*20: raise TorrentError('Invalid torrent piece hashes')
    # Retain exactly the canonical info dictionary, hence the same infohash; drop web seeds and trackers.
    clean = encode({b'info': info})
    return clean, entries, hashlib.sha1(encode(info)).hexdigest()


def normalize_magnet(value):
    parsed = urlsplit(value)
    if parsed.scheme != 'magnet': raise TorrentError('Expected a magnet link')
    for xt in parse_qs(parsed.query).get('xt', []):
        match = re.fullmatch(r'urn:btih:([0-9a-fA-F]{40}|[a-zA-Z2-7]{32})', xt)
        if match:
            token = match[1]
            digest = token.lower() if len(token) == 40 else base64.b32decode(token.upper()).hex()
            return 'magnet:?xt=urn:btih:' + digest, digest
    raise TorrentError('Only BitTorrent v1/hybrid magnet links are supported')


async def run_aria(source, folder, progress, *, metadata=False, index=None, expected=None, limit=MAX_DOWNLOAD_BYTES):
    tool = shutil.which('aria2c')
    if not tool: raise TorrentError('Install aria2 to use torrent downloads')
    folder = Path(folder).resolve(); folder.mkdir(parents=True, exist_ok=True)
    args = [tool, '--no-conf=true', '--enable-rpc=false', '--seed-time=0', '--seed-ratio=0',
            '--enable-dht=true', '--enable-dht6=false', '--enable-peer-exchange=true',
            '--bt-exclude-tracker=*', '--follow-torrent=false', '--file-allocation=none',
            '--allow-overwrite=false', '--auto-file-renaming=false', '--continue=true',
            '--max-overall-upload-limit=64K', '--bt-stop-timeout=120', '--summary-interval=0',
            '--console-log-level=error', '--download-result=hide', '--bt-max-peers=30',
            '--dht-file-path='+str(folder/'dht.dat'), '--dir='+str(folder),
            '--disk-cache=0', '--bt-remove-unselected-file=true']
    if metadata: args += ['--bt-metadata-only=true', '--bt-save-metadata=true']
    if index is not None: args += ['--select-file='+str(index)]
    args.append(str(source))
    process = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.DEVNULL,
                                                   stderr=asyncio.subprocess.DEVNULL)
    try:
        async with asyncio.timeout(180 if metadata else 24*3600):
            while process.returncode is None:
                stats = [p.stat() for p in folder.rglob('*') if p.is_file()]
                size = sum(stat.st_size for stat in stats)
                if size > limit + 32*1024*1024: raise TorrentError('Torrent exceeded the local disk limit')
                allocated = sum(stat.st_blocks * 512 for stat in stats)
                await progress(min(allocated, expected or allocated), expected, 0)
                try: await asyncio.wait_for(process.wait(), 1)
                except TimeoutError: pass
        if process.returncode: raise TorrentError('Torrent download failed or has no reachable peers; try again later')
    finally:
        if process.returncode is None:
            process.terminate()
            try: await asyncio.wait_for(process.wait(), 5)
            except TimeoutError:
                process.kill(); await process.wait()


async def magnet_metadata(magnet, folder):
    magnet, digest = normalize_magnet(magnet)
    async def ignore(*args): pass
    await run_aria(magnet, folder, ignore, metadata=True, limit=MAX_TORRENT_BYTES)
    path = Path(folder)/(digest+'.torrent')
    if not path.is_file(): raise TorrentError('No torrent metadata returned')
    data = path.read_bytes()
    clean, entries, actual = inspect_torrent(data)
    if actual != digest: raise TorrentError('Torrent metadata does not match the magnet')
    path.write_bytes(clean)
    return path, entries


async def download_torrent(metadata_path, index, folder, progress):
    clean, entries, _ = inspect_torrent(Path(metadata_path).read_bytes())
    entry = next((e for e in entries if e['index'] == index), None)
    if not entry or not 0 < entry['size'] <= MAX_DOWNLOAD_BYTES: raise TorrentError('Selected torrent file exceeds 10 GB or is empty')
    # Metadata path is task-owned and was sanitized on import and is rechecked here.
    Path(metadata_path).write_bytes(clean)
    try:
        await run_aria(Path(metadata_path).resolve(), folder, progress, index=index, expected=entry['size'])
        path = Path(folder)/entry['name']
        if not path.is_file() or path.is_symlink() or path.stat().st_size != entry['size']: raise TorrentError('Selected torrent file is incomplete')
        return path
    except BaseException:
        shutil.rmtree(folder, ignore_errors=True)
        raise
