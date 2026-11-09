import asyncio
import hashlib
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import re
from email.message import Message
from urllib.parse import unquote, urljoin, urlparse
import httpx
from .plugin_network import public_address
from .transfer import safe_name


_cooldowns = {}


def _temporary(url, headers):
    retry = headers.get('retry-after')
    if retry:
        try:
            delay = int(retry) if retry.isdigit() else max(0, (parsedate_to_datetime(retry) - datetime.now(timezone.utc)).total_seconds())
            _cooldowns[hashlib.sha256(url.encode()).digest()] = time.monotonic() + max(0, delay)
        except (TypeError, ValueError, OverflowError):
            retry = None
    return {'status':'temporary','reason':'Source is temporarily unavailable','retry_after':retry}


async def probe(url, headers=None):
    """Verify an HTTP media source using bounded requests to public IPs."""
    key=hashlib.sha256(url.encode()).digest()
    remaining=_cooldowns.get(key,0)-time.monotonic()
    if remaining>0:
        return {'status':'temporary','reason':'Source requested a wait before retrying','retry_after':str(int(remaining)+1)+' seconds'}
    _cooldowns.pop(key,None)
    clean={str(k):str(v) for k,v in (headers or {}).items() if k.lower() not in ('host','range','accept-encoding','connection','content-length','transfer-encoding','proxy-authorization')}
    head_headers={}; head_status=0
    try:
        async with httpx.AsyncClient(follow_redirects=False, trust_env=False, timeout=10) as client:
            async def request(method, initial, extra):
                current=initial
                for hop in range(6):
                    parsed=httpx.URL(current)
                    if parsed.scheme not in ('http','https') or not parsed.host or parsed.userinfo: raise ValueError('Invalid or unsafe URL')
                    ip=await public_address(parsed.host)
                    target=parsed.copy_with(host=ip)
                    request_headers={**clean,**extra,'Accept-Encoding':'identity','Host':parsed.netloc.decode('ascii')}
                    req=client.build_request(method,target,headers=request_headers,extensions={'sni_hostname':parsed.host})
                    response=await client.send(req,stream=True)
                    if response.status_code in (301,302,303,307,308) and response.headers.get('location'):
                        location=urljoin(str(parsed),response.headers['location'])
                        await response.aclose()
                        if hop == 5: raise ValueError('Too many redirects')
                        next_url=httpx.URL(location)
                        if (parsed.scheme,parsed.host,parsed.port)!=(next_url.scheme,next_url.host,next_url.port):
                            for key in list(clean):
                                if key.lower() in ('authorization','cookie'): clean.pop(key)
                        current=location;continue
                    return response,str(parsed)
                raise ValueError('Too many redirects')
            response,final=await request('HEAD',url,{})
            head_status=response.status_code;head_headers=dict(response.headers)
            await response.aclose()
            if head_status in (404,410): return {'status':'unavailable','reason':'File not found'}
            if head_status==429 or (head_status>=500 and head_status!=501):
                return _temporary(url,head_headers)
            # Even a successful HEAD is followed by a small byte-range check.
            end=0 if head_status in (405,501) else 511
            response,final=await request('GET',final,{'Range':f'bytes=0-{end}'})
            status=response.status_code; response_headers=dict(response.headers); sample=b''
            if status==200:
                content_type=response_headers.get('content-type','').lower()
                length=response_headers.get('content-length','')
                if 'application/json' in content_type and length.isdigit() and int(length)<=512:
                    body=b''
                    async for chunk in response.aiter_raw(chunk_size=512):
                        body+=chunk[:512-len(body)]
                        if len(body)>=512: break
                    await response.aclose()
                    if b'file_rate_limited_captcha_required' in body.lower():
                        return {'status':'manual','reason':'Pixeldrain CAPTCHA required; manual download required'}
                await response.aclose()
                if 'text/html' in content_type:
                    reason = 'Web page; manual download required'
                elif 'mpegurl' in content_type or urlparse(final).path.lower().endswith('.m3u8'):
                    reason = 'HLS streaming playlist; download is not supported yet'
                else:
                    reason = 'Source ignored the small range request; manual download required'
                return {'status':'manual','reason':reason, 'hls_candidate': reason.startswith('HLS streaming')}
            async for chunk in response.aiter_raw(chunk_size=512):
                sample+=chunk[:512-len(sample)]
                if len(sample)>=512:break
            await response.aclose()
            lower=sample.lower()
            if b'file_rate_limited_captcha_required' in lower:
                return {'status':'manual','reason':'Pixeldrain CAPTCHA required; manual download required'}
            if status in (401,403): return {'status':'manual','reason':'Login, access restriction or hotlink block; manual download required'}
            if status in (404,410): return {'status':'unavailable','reason':'File not found'}
            if status==429 or status>=500: return _temporary(url,response_headers)
            if status!=206: return {'status':'manual','reason':'Source did not provide a valid media range'}
            content_type=(response_headers.get('content-type') or head_headers.get('content-type') or '').lower()
            if 'mpegurl' in content_type or urlparse(final).path.lower().endswith('.m3u8'):
                return {'status':'manual','reason':'HLS streaming playlist', 'hls_candidate':True}
            if 'dash+xml' in content_type or urlparse(final).path.lower().endswith('.mpd'):
                return {'status':'manual','reason':'Streaming playlist; manual download required'}
            if 'text/html' in content_type or 'text/plain' in content_type or 'application/json' in content_type or lower.lstrip().startswith((b'<!doctype html',b'<html',b'{')) or (end == 0 and lower == b'<') or b'captcha' in lower:
                return {'status':'manual','reason':'Web page, provider error or CAPTCHA; manual download required'}
            match=re.fullmatch(r'bytes 0-(\d+)/(\d+|\*)',response_headers.get('content-range',''), re.I)
            if not match or int(match.group(1))>end or len(sample)!=int(match.group(1))+1:
                return {'status':'manual','reason':'Invalid or incomplete media range'}
            size=int(match.group(2)) if match.group(2)!='*' else None
            if size is not None and int(match.group(1)) >= size:
                return {'status':'manual','reason':'Invalid media range size'}
            message=Message();message['content-disposition']=response_headers.get('content-disposition') or head_headers.get('content-disposition','')
            name=message.get_filename() or unquote(urlparse(final).path.rsplit('/',1)[-1])
            return {'status':'ready','reason':'Media bytes verified','size':size,'filename':safe_name(name or 'media'),'content_type':content_type}
    except (httpx.TimeoutException, httpx.NetworkError, asyncio.TimeoutError):
        return {'status':'temporary','reason':'Connection timed out or failed'}
    except ValueError:
        return {'status':'unavailable','reason':'Unsafe or invalid source URL'}
    except Exception:
        return {'status':'temporary','reason':'Link check failed'}


def clean_title(movie, stream):
    title=str(movie.get('name','Movie'))
    if movie.get('season') is not None:
        title += f' S{int(movie["season"]):02d}E{int(movie["episode"]):02d}'
    year=str(movie.get('year') or movie.get('releaseInfo') or '')[:4]
    details=' '.join(str(stream.get(k) or '') for k in ('quality','title','name','filename','language','description'))
    details=re.sub(r'[._-]+',' ',details)
    quality=re.search(r'(?i)\b(2160|1080|720|480|360)p\b|\b4k\b',details)
    languages=[]
    for pattern,label in [(r'\b(hindi|hin)\b','Hindi'),(r'\b(english|eng)\b','English'),(r'\b(tamil|tam)\b','Tamil'),(r'\b(telugu|tel)\b','Telugu'),(r'\b(malayalam|mal)\b','Malayalam')]:
        if re.search(pattern,details,re.I):languages.append(label)
    codec=re.search(r'\b(HEVC|x265|x264|H264|H265|AVC)\b',details,re.I)
    text=' '.join([title,year,quality.group(0).upper() if quality else '',codec.group(0).upper() if codec else '',*languages])
    return safe_name(re.sub(r'[._]+',' ',text)).strip()
