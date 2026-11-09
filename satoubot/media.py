"""Bounded local media inspection and conversion; remote URLs never reach FFmpeg."""
import asyncio
import json
import shutil
from pathlib import Path

from .transfer import MAX_DOWNLOAD_BYTES

LOCAL_FORMATS = 'matroska,webm,mov,mpegts,avi,mpeg,asf,flv,ogg,mp3,aac,flac,wav'

FORMATS = {'original': 'Original', 'mkv': 'MKV (keep codecs)', 'mp4': 'MP4 (keep codecs)',
           'mp4-h264': 'MP4 (H.264/AAC)'}


class MediaError(ValueError):
    pass


async def run_tool(args, timeout, output=None, limit=MAX_DOWNLOAD_BYTES):
    process = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE,
                                                   stderr=asyncio.subprocess.DEVNULL)
    async def monitor():
        while process.returncode is None:
            if output and output.exists() and output.stat().st_size >= limit:
                raise MediaError('Converted file reached the 10 GB local limit')
            await asyncio.sleep(.1)
    watcher = asyncio.create_task(monitor())
    async def read_output():
        data = bytearray()
        while chunk := await process.stdout.read(8192):
            data.extend(chunk)
            if len(data) > 1_000_000: raise MediaError('Media metadata is too large')
        await process.wait()
        return bytes(data)
    communicate = asyncio.create_task(read_output())
    try:
        async with asyncio.timeout(timeout):
            done, _ = await asyncio.wait((watcher, communicate), return_when=asyncio.FIRST_COMPLETED)
            if watcher in done: await watcher
            data = await communicate
        if process.returncode: raise MediaError('Media tool could not read or convert this file; original kept')
        return data
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
        watcher.cancel()
        await asyncio.gather(watcher, communicate, return_exceptions=True)


async def inspect_media(path):
    tool = shutil.which('ffprobe')
    if not tool: raise MediaError('Install ffmpeg (including ffprobe) to inspect tracks')
    path = Path(path).resolve(strict=True)
    data = await run_tool([tool, '-v', 'error', '-protocol_whitelist', 'file,pipe', '-format_whitelist', LOCAL_FORMATS,
                           '-show_entries', 'stream=index,codec_type,codec_name:stream_tags=language,title:format=duration,format_name',
                           '-of', 'json', str(path)], 30)
    if len(data) > 1_000_000: raise MediaError('Media metadata is too large')
    result = json.loads(data)
    return {'streams': result.get('streams', [])[:128], 'format': result.get('format', {})}


def track_summary(info):
    lines = []
    for kind, label in (('audio', 'Audio'), ('subtitle', 'Subtitles')):
        tracks = []
        for stream in info.get('streams', []):
            if stream.get('codec_type') != kind: continue
            tags = stream.get('tags') or {}
            tracks.append(' · '.join(str(v)[:80] for v in
                          (tags.get('language') or 'language unknown', stream.get('codec_name') or 'codec unknown', tags.get('title')) if v))
        lines.append(label + ': ' + (', '.join(tracks)[:700] if tracks else 'none detected'))
    return '\n'.join(lines)


def reported_tracks(stream):
    def labels(value):
        if isinstance(value, str): return value[:180]
        if isinstance(value, dict): value = [value]
        if not isinstance(value, list): return ''
        return ', '.join(str(item.get('lang') or item.get('language') or item.get('name') or 'unspecified')
                         if isinstance(item, dict) else str(item) for item in value[:12])[:180]
    audio = labels(stream.get('audio') or stream.get('languages') or stream.get('language'))
    subtitles = labels(stream.get('subtitles') or stream.get('subs'))
    return f'Audio (add-on): {audio or "not supplied"}\nSubtitles (add-on): {subtitles or "not supplied"}\nTracks are verified from the local file after download.'


async def convert_media(source, selection, limit=MAX_DOWNLOAD_BYTES):
    if selection not in FORMATS or selection == 'original': raise MediaError('Choose a supported conversion')
    tool = shutil.which('ffmpeg')
    if not tool: raise MediaError('Install ffmpeg to convert files')
    source = Path(source).resolve(strict=True)
    suffix = 'mp4' if selection.startswith('mp4') else selection
    # Never overwrite the original, including when it already has the target extension.
    output = source.with_name(source.stem + '.converted.' + suffix)
    temporary = output.with_name(output.stem + '.part.' + suffix)
    if output.exists(): raise MediaError('A converted file already exists; keep or rename it first')
    args = [tool, '-nostdin', '-hide_banner', '-loglevel', 'error', '-n',
            '-protocol_whitelist', 'file,pipe', '-format_whitelist', LOCAL_FORMATS, '-i', str(source), '-map', '0', '-map_metadata', '0']
    if selection == 'mp4-h264':
        args += ['-c:v', 'libx264', '-preset', 'veryfast', '-crf', '20', '-threads', '2',
                 '-c:a', 'aac', '-c:s', 'mov_text']
    else: args += ['-c', 'copy']
    args += ['-fs', str(limit + 1), str(temporary)]
    try:
        await run_tool(args, 6 * 3600, output=temporary, limit=limit)
        if not temporary.exists() or not 0 < temporary.stat().st_size < limit:
            raise MediaError('Converted file is empty or exceeds the local limit')
        await inspect_media(temporary)
        temporary.replace(output)
        return output
    finally:
        temporary.unlink(missing_ok=True)

async def inspect_remote_media(url, size, headers=None):
    """Read at most 8 MiB of verified ranges into a sparse local sample.

    Metadata at either end is often enough for MKV/MP4 tracks. Failure is unknown,
    never a claim that the full file has no audio/subtitles. No remote FFprobe I/O.
    """
    import tempfile
    import httpx
    from .public_transport import PublicTransport
    if not isinstance(size, int) or not 0 < size <= MAX_DOWNLOAD_BYTES:
        raise MediaError('Track information unavailable before download')
    clean = {str(k):str(v) for k,v in (headers or {}).items()
             if str(k).lower() not in ('host','range','accept-encoding','connection','content-length','transfer-encoding','proxy-authorization')}
    sample_bytes = 4 * 1024 * 1024
    ranges = [(0, min(size, sample_bytes)-1)]
    if size > sample_bytes: ranges.append((max(sample_bytes, size-sample_bytes), size-1))
    with tempfile.TemporaryDirectory(prefix='satoubot-tracks-') as folder:
        path = Path(folder)/'sample.media'
        with path.open('wb') as output:
            output.truncate(size)
            async with httpx.AsyncClient(transport=PublicTransport(), follow_redirects=False,
                                         trust_env=False, timeout=10) as client:
                for start,end in ranges:
                    current = url
                    request_headers = dict(clean)
                    for hop in range(6):
                        async with client.stream('GET', current, headers={**request_headers,
                                                 'Range':f'bytes={start}-{end}', 'Accept-Encoding':'identity'}) as response:
                            if response.is_redirect:
                                from urllib.parse import urljoin, urlsplit
                                following = urljoin(current, response.headers.get('location',''))
                                before, after = urlsplit(current), urlsplit(following)
                                if (before.scheme,before.hostname,before.port)!=(after.scheme,after.hostname,after.port):
                                    request_headers = {k:v for k,v in request_headers.items() if k.lower() not in ('cookie','authorization')}
                                current = following
                                continue
                            if response.status_code != 206 or response.headers.get('content-range') != f'bytes {start}-{end}/{size}' or response.headers.get('content-encoding','identity').lower() != 'identity':
                                raise MediaError('Track sample range unavailable')
                            output.seek(start); count = 0
                            async for chunk in response.aiter_bytes(64*1024):
                                count += len(chunk)
                                if count > end-start+1: raise MediaError('Track sample exceeded the requested range')
                                output.write(chunk)
                            if count != end-start+1: raise MediaError('Incomplete track sample')
                            break
                    else: raise MediaError('Too many track sample redirects')
        return await inspect_media(path)
