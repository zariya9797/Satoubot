import asyncio
import os
import re
import time
from pathlib import Path
from urllib.parse import unquote, urlparse

import httpx
from .public_transport import PublicTransport

MAX_BYTES = 2_000_000_000  # Telegram upload limit
MAX_DOWNLOAD_BYTES = 10_000_000_000


def safe_name(value: str) -> str:
    value = re.sub(r'[\\/:*?"<>|\x00-\x1f]', '_', value).strip(' .')
    return value[:180] or 'media'


def filename_from_url(url):
    return safe_name(unquote(Path(urlparse(url).path).name) or 'media')


def readable_speed(size, elapsed):
    speed = size / max(elapsed, .001)
    return speed


async def inspect_url(url, headers=None):
    if urlparse(url).scheme not in ('http', 'https'):
        raise ValueError('Only HTTP and HTTPS URLs are supported here.')
    async with httpx.AsyncClient(transport=PublicTransport(), follow_redirects=True, max_redirects=5, timeout=30, trust_env=False) as client:
        response = await client.head(url, headers={**(headers or {}), 'Accept-Encoding': 'identity'})
        response.raise_for_status()
        length = response.headers.get('content-length')
        if length is None or not length.isdigit() or int(length) <= 0:
            raise ValueError('The source did not provide a reliable size. This download was not started.')
        return int(length), response.headers.get('content-type', '')


class RangeIgnored(ValueError):
    pass


async def download_sequential(url, destination: Path, expected_size: int, progress, headers=None, *, validator=None, validator_header=None):
    """Stream with a hard limit even if the server sends more than its advertised size."""
    if expected_size > MAX_DOWNLOAD_BYTES or expected_size <= 0:
        raise ValueError('File exceeds the 10 GB local limit or has an unknown size.')
    temporary = destination.with_suffix(destination.suffix + '.part')
    total = 0
    started = time.monotonic()
    try:
        async with httpx.AsyncClient(transport=PublicTransport(), follow_redirects=True, max_redirects=5, timeout=httpx.Timeout(60, connect=30), trust_env=False) as client:
            request_headers = {**(headers or {}), 'Accept-Encoding': 'identity'}
            if validator:
                request_headers['If-Match' if validator_header == 'etag' else 'If-Unmodified-Since'] = validator
            async with client.stream('GET', url, headers=request_headers) as response:
                response.raise_for_status()
                if validator and (response.status_code != 200 or response.headers.get(validator_header) != validator):
                    raise ValueError('Source changed before the single connection retry.')
                with temporary.open('wb') as output:
                    async for chunk in response.aiter_bytes(256 * 1024):
                        total += len(chunk)
                        if total > MAX_DOWNLOAD_BYTES or total > expected_size:
                            raise ValueError('Download exceeded its reported size or the 10 GB local limit.')
                        output.write(chunk)
                        now = time.monotonic()
                        await progress(total, expected_size, readable_speed(total, now - started))
        if total != expected_size:
            raise ValueError('The downloaded size does not match the advertised size.')
        os.replace(temporary, destination)
        return total
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


async def download(url, destination: Path, expected_size: int, progress, connections=4, headers=None):
    """Use verified byte ranges when the server supplies a stable validator."""
    if expected_size > MAX_DOWNLOAD_BYTES or expected_size <= 0:
        raise ValueError('File exceeds the 10 GB local limit or has an unknown size.')
    if expected_size < 4 * 1024 * 1024 or connections < 2:
        return await download_sequential(url, destination, expected_size, progress, headers=headers)
    headers = {**(headers or {}), 'Accept-Encoding': 'identity'}
    async with httpx.AsyncClient(transport=PublicTransport(), follow_redirects=True, max_redirects=5, timeout=httpx.Timeout(60, connect=30), trust_env=False) as client:
        head = await client.head(url, headers=headers)
        if head.status_code in (401, 403, 405, 501):
            # A signed GET URL may deliberately forbid HEAD. Verify an exact,
            # bounded range instead; retain the same public transport and limits.
            async with client.stream('GET', url, headers={**headers, 'Range': 'bytes=0-0', 'Accept-Encoding': 'identity'}) as sample:
                sample.raise_for_status()
                if (sample.status_code != 206 or sample.headers.get('content-range') != f'bytes 0-0/{expected_size}'
                        or sample.headers.get('content-encoding', 'identity').lower() != 'identity'):
                    raise ValueError('Source did not provide the expected byte range and size.')
                count = 0
                async for chunk in sample.aiter_raw(chunk_size=1):
                    count += len(chunk)
                    if count > 1: break
                if count != 1: raise ValueError('Source sent an incomplete byte range.')
                validator_header = 'etag' if sample.headers.get('etag') else 'last-modified'
                validator = sample.headers.get(validator_header)
        else:
            head.raise_for_status()
            validator_header = 'etag' if head.headers.get('etag') else 'last-modified'
            validator = head.headers.get(validator_header)
            if head.headers.get('content-length') != str(expected_size) or head.headers.get('accept-ranges', '').lower() != 'bytes':
                return await download_sequential(url, destination, expected_size, progress, headers=headers)
        if not validator or validator.startswith('W/'):
            return await download_sequential(url, destination, expected_size, progress, headers=headers)
        temporary = destination.with_suffix(destination.suffix + '.part')
        count = min(max(1, connections), 8, expected_size)
        total = 0
        started = time.monotonic()
        try:
            with temporary.open('wb') as output:
                output.truncate(expected_size)
            async def part(index):
                nonlocal total
                start = expected_size * index // count
                end = expected_size * (index + 1) // count - 1
                request_headers = {**headers, 'Range': f'bytes={start}-{end}', 'If-Range': validator,
                                   'Accept-Encoding': 'identity'}
                async with client.stream('GET', url, headers=request_headers) as response:
                    if response.status_code == 200 and response.headers.get(validator_header) == validator:
                        raise RangeIgnored('Source ignored byte ranges without changing its validator.')
                    if response.status_code != 206 or response.headers.get('content-range') != f'bytes {start}-{end}/{expected_size}':
                        raise ValueError('Source did not honor the requested byte range.')
                    written = 0
                    with temporary.open('r+b') as output:
                        output.seek(start)
                        async for chunk in response.aiter_bytes(256 * 1024):
                            written += len(chunk)
                            if written > end - start + 1:
                                raise ValueError('Source exceeded its byte range.')
                            output.write(chunk)
                            total += len(chunk)
                            now = time.monotonic()
                            await progress(total, expected_size, readable_speed(total, now-started))
                    if written != end - start + 1:
                        raise ValueError('Source sent an incomplete byte range.')
            async with asyncio.TaskGroup() as group:
                for index in range(count):
                    group.create_task(part(index))
            os.replace(temporary, destination)
            return total
        except BaseException as exc:
            temporary.unlink(missing_ok=True)
            def leaves(error):
                if isinstance(error, BaseExceptionGroup):
                    return [leaf for child in error.exceptions for leaf in leaves(child)]
                return [error]
            if isinstance(exc, ExceptionGroup) and all(isinstance(item, RangeIgnored) for item in leaves(exc)):
                await progress(0, expected_size, 0)
                return await download_sequential(url, destination, expected_size, progress, headers=headers,
                                                 validator=validator, validator_header=validator_header)
            raise
