import httpx
import pytest
from unittest.mock import AsyncMock
from satoubot.file_info import probe


@pytest.fixture
def transport(monkeypatch):
    import satoubot.file_info as module
    module._cooldowns.clear()
    original=httpx.AsyncClient
    calls=[]
    handler=[None]
    async def resolve(host):
        if host in ('localhost','127.0.0.1'): raise ValueError('private')
        return '8.8.8.8'
    monkeypatch.setattr(module,'public_address',resolve)
    def response(req):
        calls.append(req)
        response=handler[0](req)
        if response.is_stream_consumed:
            return httpx.Response(response.status_code, headers=response.headers, stream=httpx.ByteStream(response.content))
        return response
    monkeypatch.setattr(module.httpx,'AsyncClient',lambda **kw: original(transport=httpx.MockTransport(response),**kw))
    return handler,calls


@pytest.mark.asyncio
async def test_direct_media_head_and_range_metadata(transport):
    handler,calls=transport
    def reply(req):
        if req.method=='HEAD': return httpx.Response(200,headers={'content-length':'4','content-disposition':'attachment; filename="movie.mkv"'})
        return httpx.Response(206,headers={'content-range':'bytes 0-3/4','content-type':'video/x-matroska'},content=b'\x1aE\xdf\xa3')
    handler[0]=reply
    result=await probe('https://public.test/path?private=token')
    assert result['status']=='ready' and result['size']==4 and result['filename']=='movie.mkv'
    assert calls[1].headers['range']=='bytes=0-511'
    assert all(req.url.host=='8.8.8.8' for req in calls)


@pytest.mark.asyncio
async def test_head_unsupported_uses_single_byte_range(transport):
    handler,calls=transport
    handler[0]=lambda req: httpx.Response(405) if req.method=='HEAD' else httpx.Response(206,headers={'content-range':'bytes 0-0/999','content-type':'video/mp4'},content=b'x')
    result=await probe('https://public.test/test.mp4')
    assert result['status']=='ready' and result['size']==999
    assert calls[1].headers['range']=='bytes=0-0'


@pytest.mark.asyncio
async def test_ignored_range_is_closed_without_reading(transport):
    handler,calls=transport
    class Guard(httpx.AsyncByteStream):
        async def __aiter__(self):
            raise AssertionError('Full file must not be read')
            yield b''
    handler[0]=lambda req: httpx.Response(200,headers={'content-length':'999999'},stream=Guard())
    assert (await probe('https://public.test/file'))['status']=='manual'


@pytest.mark.asyncio
async def test_safe_redirect_and_private_redirect(transport):
    handler,calls=transport
    def reply(req):
        if req.url.path=='/start': return httpx.Response(302,headers={'location':'https://public.test/final'})
        if req.method=='HEAD': return httpx.Response(200)
        return httpx.Response(206,headers={'content-range':'bytes 0-0/1','content-type':'video/mp4'},content=b'x')
    handler[0]=reply
    assert (await probe('https://public.test/start'))['status']=='ready'
    handler[0]=lambda req:httpx.Response(302,headers={'location':'http://127.0.0.1/secret'})
    assert (await probe('https://public.test/start'))['status']=='unavailable'


@pytest.mark.asyncio
@pytest.mark.parametrize('code,expected', [(404,'unavailable'),(410,'unavailable'),(429,'temporary'),(503,'temporary')])
async def test_missing_and_temporary(transport,code,expected):
    handler,_=transport;handler[0]=lambda req:httpx.Response(code,headers={'retry-after':'15'})
    result=await probe('https://public.test/file')
    assert result['status']==expected
    if expected=='temporary': assert result['retry_after']=='15'


@pytest.mark.asyncio
@pytest.mark.parametrize('status,content_type,body,reason',[(206,'text/html',b'<html>','Web page'),(403,'application/json',b'{"error":"file_rate_limited_captcha_required"}','Pixeldrain'),(401,'text/plain',b'login','Login'),(403,'text/plain',b'forbidden','Login'),(400,'text/plain',b'error','HTTP 400')])
async def test_blocked_pages_and_pixeldrain(transport,status,content_type,body,reason):
    handler,_=transport
    handler[0]=lambda req: httpx.Response(200) if req.method=='HEAD' else httpx.Response(status,headers={'content-type':content_type,'content-range':f'bytes 0-{len(body)-1}/{len(body)}'},content=body)
    result=await probe('https://public.test/file')
    assert result['status']=='manual' and reason in result['reason']


@pytest.mark.asyncio
async def test_timeout_is_temporary_and_unknown_size_remains_unknown(transport):
    handler,_=transport
    def timeout(req): raise httpx.ReadTimeout('timeout')
    handler[0]=timeout
    assert (await probe('https://public.test/file'))['status']=='temporary'
    handler[0]=lambda req: httpx.Response(405) if req.method=='HEAD' else httpx.Response(206,headers={'content-range':'bytes 0-0/*','content-type':'video/mp4'},content=b'x')
    assert (await probe('https://public.test/file'))['size'] is None


@pytest.mark.asyncio
async def test_retry_after_prevents_early_recheck(transport):
    handler,calls=transport
    handler[0]=lambda req:httpx.Response(429,headers={'retry-after':'60'})
    assert (await probe('https://public.test/file'))['status']=='temporary'
    assert (await probe('https://public.test/file'))['status']=='temporary'
    assert len(calls)==1


@pytest.mark.asyncio
async def test_pixeldrain_error_json_200_is_recognized(transport):
    handler,_=transport
    body=b'{"error":"file_rate_limited_captcha_required"}'
    handler[0]=lambda req: httpx.Response(403) if req.method=='HEAD' else httpx.Response(200,headers={'content-type':'application/json','content-length':str(len(body))},content=body)
    result=await probe('https://public.test/file')
    assert result['status']=='manual' and 'Pixeldrain' in result['reason']


@pytest.mark.asyncio
async def test_download_redirect_to_local_is_rejected(tmp_path, monkeypatch):
    import asyncio
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from satoubot.transfer import download_sequential
    reached=[]
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args): pass
        def do_GET(self):
            reached.append(self.path)
            self.send_response(302)
            self.send_header('Location',f'http://127.0.0.1:{self.server.server_port}/private')
            self.end_headers()
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    async def resolve(host):
        if host != 'public.test': raise ValueError('private')
        return '127.0.0.1'
    monkeypatch.setattr('satoubot.public_transport.public_address',resolve)
    try:
        with pytest.raises(ValueError):
            await download_sequential(f'http://public.test:{server.server_port}/start',tmp_path/'file',1,AsyncMock())
        assert reached == ['/start']
        assert not (tmp_path/'file').exists()
    finally:
        await asyncio.to_thread(server.shutdown);server.server_close();thread.join()


@pytest.mark.asyncio
async def test_streaming_playlist_is_manual(transport):
    handler,_=transport
    handler[0]=lambda req:httpx.Response(200) if req.method=='HEAD' else httpx.Response(206,headers={'content-range':'bytes 0-3/4','content-type':'application/vnd.apple.mpegurl'},content=b'#EXT')
    result=await probe('https://public.test/video.m3u8')
    assert result['status']=='manual' and 'playlist' in result['reason']


@pytest.mark.asyncio
async def test_probe_limits_redirects(transport):
    handler,calls=transport
    handler[0]=lambda req:httpx.Response(302,headers={'location':'https://public.test/loop'})
    assert (await probe('https://public.test/loop'))['status']=='unavailable'
    assert len(calls)==6


@pytest.mark.asyncio
async def test_hls_ignored_range_explains_streaming_is_unsupported(transport):
    handler,_=transport
    handler[0]=lambda req:httpx.Response(200,headers={'content-type':'application/vnd.apple.mpegurl'})
    result=await probe('https://public.test/stream.m3u8')
    assert result['status']=='manual' and 'HLS' in result['reason']
