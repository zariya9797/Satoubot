import asyncio
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import AsyncMock

import pytest

from satoubot.transfer import download


@pytest.mark.asyncio
@pytest.mark.parametrize('changed',[False,True])
async def test_range_ignored_falls_back_only_with_matching_validator(tmp_path,monkeypatch,changed):
    data=b'0123456789abcdef'*(262144+1)
    calls=[]
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args): pass
        def do_HEAD(self):
            self.send_response(200);self.send_header('Content-Length',str(len(data)))
            self.send_header('Accept-Ranges','bytes');self.send_header('ETag','"stable"');self.end_headers()
        def do_GET(self):
            calls.append((self.headers.get('Range'),self.headers.get('If-Match')))
            self.send_response(200);self.send_header('Content-Length',str(len(data)))
            self.send_header('ETag','"different"' if changed and not self.headers.get('Range') else '"stable"')
            self.end_headers()
            try: self.wfile.write(data)
            except (BrokenPipeError,ConnectionResetError): pass
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    threading.Thread(target=server.serve_forever,daemon=True).start()
    async def resolve(host):
        if host!='public.test': raise ValueError('unsafe')
        return '127.0.0.1'
    monkeypatch.setattr('satoubot.public_transport.public_address',resolve)
    output=tmp_path/'output.mkv'
    try:
        url=f'http://public.test:{server.server_port}/file'
        if changed:
            with pytest.raises(ValueError,match='Source changed'):
                await download(url,output,len(data),AsyncMock())
            assert not output.exists() and not (tmp_path/'output.mkv.part').exists()
        else:
            assert await download(url,output,len(data),AsyncMock())==len(data)
            assert output.read_bytes()==data
        assert any(not byte_range and validator=='"stable"' for byte_range,validator in calls)
    finally:
        await asyncio.to_thread(server.shutdown);server.server_close()


def test_download_diagnostics_do_not_include_sensitive_url():
    import httpx
    from satoubot.download import download_error_reason
    request=httpx.Request('GET','https://example.test/private?token=supersecret')
    response=httpx.Response(403,request=request)
    error=httpx.HTTPStatusError('private?token=supersecret',request=request,response=response)
    reason=download_error_reason(ExceptionGroup('transfer',[error]))
    assert reason=='Source returned HTTP 403' and 'supersecret' not in reason


@pytest.mark.asyncio
async def test_parallel_ignored_range_with_changed_validator_never_retries(tmp_path,monkeypatch):
    data=b'x'*(4*1024*1024+1); calls=[]
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args): pass
        def do_HEAD(self):
            self.send_response(200);self.send_header('Content-Length',str(len(data)));self.send_header('Accept-Ranges','bytes');self.send_header('ETag','"original"');self.end_headers()
        def do_GET(self):
            calls.append(self.headers.get('Range'))
            self.send_response(200);self.send_header('Content-Length',str(len(data)));self.send_header('ETag','"new-version"');self.end_headers()
            try: self.wfile.write(data)
            except (BrokenPipeError,ConnectionResetError): pass
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    threading.Thread(target=server.serve_forever,daemon=True).start()
    monkeypatch.setattr('satoubot.public_transport.public_address',AsyncMock(return_value='127.0.0.1'))
    output=tmp_path/'out'
    try:
        with pytest.raises(ExceptionGroup):
            await download(f'http://public.test:{server.server_port}/file',output,len(data),AsyncMock())
        assert calls and all(calls) and not output.exists() and not (tmp_path/'out.part').exists()
    finally:
        await asyncio.to_thread(server.shutdown);server.server_close()
