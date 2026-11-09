import re
from unittest.mock import AsyncMock

import httpx
import pytest

from satoubot.transfer import download


class BytesStream(httpx.AsyncByteStream):
    def __init__(self, data):
        self.data = data

    async def __aiter__(self):
        yield self.data


@pytest.mark.asyncio
@pytest.mark.parametrize('head_status',[401,403,405,501])
async def test_get_only_signed_large_file_downloads_with_verified_ranges(tmp_path,monkeypatch,head_status):
    data=b'abcd'*(1024*1024+1); requests=[]
    def handler(request):
        requests.append(request)
        if request.method=='HEAD': return httpx.Response(head_status,request=request)
        range_header=request.headers.get('range')
        if range_header:
            match=re.fullmatch(r'bytes=(\d+)-(\d+)',range_header)
            start,end=map(int,match.groups())
            return httpx.Response(206,headers={'Content-Range':f'bytes {start}-{end}/{len(data)}','ETag':'"stable"'},stream=BytesStream(data[start:end+1]),request=request)
        return httpx.Response(200,headers={'ETag':'"stable"'},stream=BytesStream(data),request=request)
    monkeypatch.setattr('satoubot.transfer.PublicTransport',lambda:httpx.MockTransport(handler))
    path=tmp_path/'video.mkv';url='https://storage.example/file?X-Amz-Signature=synthetic'
    assert await download(url,path,len(data),AsyncMock())==len(data)
    assert path.read_bytes()==data
    assert requests[0].method=='HEAD'
    gets=[r for r in requests if r.method=='GET']
    assert len(gets)==5 and gets[0].headers['range']=='bytes=0-0'
    assert all(r.headers['if-range']=='"stable"' for r in gets[1:])
    assert all(str(r.url)==url for r in requests)


@pytest.mark.asyncio
async def test_get_only_without_validator_uses_bounded_single_connection(tmp_path,monkeypatch):
    data=b'x'*(4*1024*1024+1);requests=[]
    def handler(request):
        requests.append(request)
        if request.method=='HEAD': return httpx.Response(403,request=request)
        if request.headers.get('range'): return httpx.Response(206,headers={'Content-Range':f'bytes 0-0/{len(data)}'},stream=BytesStream(b'x'),request=request)
        return httpx.Response(200,stream=BytesStream(data),request=request)
    monkeypatch.setattr('satoubot.transfer.PublicTransport',lambda:httpx.MockTransport(handler))
    path=tmp_path/'video'
    assert await download('https://public.test/file',path,len(data),AsyncMock())==len(data)
    assert path.read_bytes()==data and len(requests)==3
    assert requests[-1].headers.get('range') is None


@pytest.mark.asyncio
@pytest.mark.parametrize('status,content_range,body',[
    (200,None,b'x'),(206,'bytes 0-0/123',b'x'),(206,'bytes 0-0/4194305',b''),
    (206,'bytes 0-1/4194305',b'xx'),(403,None,b''),
])
async def test_get_only_rejects_bad_sample_without_full_download(tmp_path,monkeypatch,status,content_range,body):
    requests=[]
    def handler(request):
        requests.append(request)
        if request.method=='HEAD': return httpx.Response(403,request=request)
        headers={'Content-Range':content_range} if content_range else {}
        return httpx.Response(status,headers=headers,stream=BytesStream(body),request=request)
    monkeypatch.setattr('satoubot.transfer.PublicTransport',lambda:httpx.MockTransport(handler))
    output=tmp_path/'video'
    with pytest.raises((ValueError,httpx.HTTPStatusError)):
        await download('https://public.test/file',output,4194305,AsyncMock())
    assert not output.exists() and not (tmp_path/'video.part').exists()
    assert len(requests)==2 and requests[1].headers['range']=='bytes=0-0'


@pytest.mark.asyncio
@pytest.mark.parametrize('status',[429,500,503])
async def test_head_rate_limit_or_server_error_is_not_bypassed(tmp_path,monkeypatch,status):
    requests=[]
    def handler(request):
        requests.append(request)
        return httpx.Response(status,request=request)
    monkeypatch.setattr('satoubot.transfer.PublicTransport',lambda:httpx.MockTransport(handler))
    with pytest.raises(httpx.HTTPStatusError):
        await download('https://public.test/file',tmp_path/'video',4194305,AsyncMock())
    assert len(requests)==1 and requests[0].method=='HEAD'
