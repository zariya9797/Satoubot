import asyncio
import json
import shutil
import subprocess
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from satoubot.app import Bot
from satoubot.hls import HLSError, download_hls, inspect_hls
from satoubot.store import Store


@pytest.fixture
def network(monkeypatch):
    handlers={}
    requests=[]
    def handle(req):
        requests.append(req)
        return handlers[req.url.path](req)
    monkeypatch.setattr('satoubot.hls.PublicTransport', lambda: httpx.MockTransport(handle))
    return handlers,requests


def resource(data, status=200, headers=None):
    return lambda req: httpx.Response(status,headers=headers,content=data)


@pytest.mark.asyncio
async def test_master_fmp4_download_and_does_not_leak_headers_to_other_host(tmp_path,network):
    handlers,requests=network
    handlers['/master']=resource(b'#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=100,RESOLUTION=1280x720\nhttps://cdn.test/film\n')
    handlers['/film']=resource(b'#EXTM3U\n#EXT-X-MAP:URI="init"\n#EXTINF:2,\nseg\n#EXT-X-ENDLIST\n')
    handlers['/init']=resource(b'\x00\x00\x00\x08ftyp')
    handlers['/seg']=resource(b'\x00\x00\x00\x08styp')
    info=await inspect_hls('https://source.test/master',{'Authorization':'secret','Cookie':'session=x'})
    assert info['format']=='mp4'
    result=tmp_path/'movie.mp4'; progress=AsyncMock()
    size=await download_hls('https://source.test/master',result,'mp4',progress,headers={'Authorization':'secret','Cookie':'session=x'})
    assert size == 16 and result.read_bytes().endswith(b'styp')
    assert all('authorization' not in r.headers and 'cookie' not in r.headers for r in requests if r.url.host=='cdn.test')
    assert progress.await_args.args[1] is None


@pytest.mark.asyncio
@pytest.mark.parametrize('playlist',[
    b'#EXTM3U\n#EXTINF:1,\na.ts\n',
    b'#EXTM3U\n#EXT-X-KEY:METHOD=AES-128,URI="key"\n#EXTINF:1,\na.ts\n#EXT-X-ENDLIST\n',
    b'#EXTM3U\n#EXT-X-BYTERANGE:10@0\n#EXTINF:1,\na.ts\n#EXT-X-ENDLIST\n',
    b'#EXTM3U\n#EXT-X-MEDIA:TYPE=AUDIO,URI="audio"\n#EXT-X-STREAM-INF:BANDWIDTH=1\nvideo\n',
])
async def test_unsupported_playlists_rejected(tmp_path,network,playlist):
    handlers,_=network;handlers['/playlist']=resource(playlist)
    with pytest.raises(HLSError): await inspect_hls('https://source.test/playlist')


@pytest.mark.asyncio
async def test_size_limit_and_bad_segment_remove_partial_file(tmp_path,network):
    handlers,_=network
    handlers['/playlist']=resource(b'#EXTM3U\n#EXTINF:1,\na\n#EXT-X-ENDLIST\n')
    handlers['/a']=resource(b'G123456789')
    path=tmp_path/'file.ts'
    with pytest.raises(HLSError,match='limit'):
        await download_hls('https://source.test/playlist',path,'ts',AsyncMock(),limit=5)
    assert not path.exists() and not path.with_suffix('.ts.part').exists()
    handlers['/a']=resource(b'<html>problem</html>')
    with pytest.raises(HLSError,match='format'):
        await download_hls('https://source.test/playlist',path,'ts',AsyncMock())
    assert not path.exists() and not path.with_suffix('.ts.part').exists()


@pytest.mark.asyncio
async def test_redirect_to_private_address_is_rejected(tmp_path,monkeypatch):
    reached=[]
    class Handler(SimpleHTTPRequestHandler):
        def log_message(self,*args): pass
        def do_GET(self):
            reached.append(self.path)
            self.send_response(302);self.send_header('Location',f'http://127.0.0.1:{self.server.server_port}/private');self.end_headers()
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    async def resolve(host):
        if host != 'public.test': raise ValueError('private')
        return '127.0.0.1'
    monkeypatch.setattr('satoubot.public_transport.public_address',resolve)
    try:
        with pytest.raises(ValueError): await inspect_hls(f'http://public.test:{server.server_port}/start')
        assert reached == ['/start']
    finally:
        await asyncio.to_thread(server.shutdown);server.server_close();thread.join()


@pytest.mark.asyncio
async def test_large_hls_is_saved_locally_and_never_uploaded(tmp_path,monkeypatch):
    store=Store(tmp_path/'db');job=store.create('https://example.test/video.m3u8','Movie','Movie.mp4',0)
    store.update(job,state='downloading');store.set_setting(f'job:{job}:hls','mp4')
    uploader=MagicMock();uploader.send_file=AsyncMock()
    bot=Bot(store,tmp_path,42,uploader)
    bot.preview=AsyncMock()
    message=MagicMock(edit_text=AsyncMock())
    context=SimpleNamespace(bot=SimpleNamespace(send_message=AsyncMock(return_value=message)))
    monkeypatch.setattr('satoubot.download.probe',AsyncMock(return_value={'status':'manual','hls_candidate':True}))
    async def fake_download(url,path,fmt,progress,**kwargs):
        with path.open('wb') as f: f.truncate(2_000_000_001)
        return 2_000_000_001
    monkeypatch.setattr('satoubot.download.download_hls',fake_download)
    monkeypatch.setattr('satoubot.preferences.inspect_media', AsyncMock(return_value={'streams':[]}))
    await bot.perform_download(context,job)
    assert store.get(job)['state']=='local_only' and store.get(job)['size']==2_000_000_001
    bot.preview.assert_not_awaited()
    await bot.perform_upload(context,job)
    uploader.send_file.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.skipif(not shutil.which('ffmpeg') or not shutil.which('ffprobe'), reason='ffmpeg and ffprobe are optional')
@pytest.mark.parametrize('kind', ['mpegts','fmp4'])
async def test_real_generated_hls_output_is_playable(tmp_path,monkeypatch,kind):
    source=tmp_path/'source';source.mkdir()
    cmd=['ffmpeg','-hide_banner','-loglevel','error','-f','lavfi','-i','testsrc2=size=128x72:rate=10','-t','2','-c:v','mpeg4','-g','10','-f','hls','-hls_time','1','-hls_playlist_type','vod']
    if kind=='fmp4': cmd += ['-hls_segment_type','fmp4']
    subprocess.run([*cmd,str(source/'index.m3u8')],check=True,capture_output=True)
    class Handler(SimpleHTTPRequestHandler):
        def __init__(self,*args,**kwargs): super().__init__(*args,directory=str(source),**kwargs)
        def log_message(self,*args): pass
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    async def resolve(host):
        if host != 'public.test': raise ValueError('private')
        return '127.0.0.1'
    monkeypatch.setattr('satoubot.public_transport.public_address',resolve)
    try:
        fmt='mp4' if kind=='fmp4' else 'ts';out=tmp_path/f'output.{fmt}'
        await download_hls(f'http://public.test:{server.server_port}/index.m3u8',out,fmt,AsyncMock())
        result=subprocess.run(['ffprobe','-v','error','-show_entries','format=duration','-of','json',str(out)],check=True,capture_output=True,text=True)
        assert float(json.loads(result.stdout)['format']['duration']) > 1
    finally:
        await asyncio.to_thread(server.shutdown);server.server_close();thread.join()


@pytest.mark.asyncio
async def test_hls_search_confirmation_and_download_gate(tmp_path,monkeypatch):
    bot=Bot(Store(tmp_path/'db'),tmp_path,42,MagicMock())
    stream={'url':'https://source.test/file.m3u8','movie':{'id':'tt1','name':'Movie','type':'movie'},'quality':'720p'}
    context=SimpleNamespace(bot=SimpleNamespace(send_message=AsyncMock()))
    monkeypatch.setattr('satoubot.catalogue.probe',AsyncMock(return_value={'status':'manual','hls_candidate':True}))
    monkeypatch.setattr('satoubot.catalogue.inspect_hls',AsyncMock(return_value={'format':'mp4'}))
    await bot.prepare_choice(context,stream)
    sent=context.bot.send_message.await_args
    assert 'unknown' in sent.args[1] and '10 GB' in sent.args[1] and '2 GB' in sent.args[1]
    buttons=sent.kwargs['reply_markup'].inline_keyboard[0]
    assert [b.text for b in buttons]==['Transfer','Download only','Give link','No']
    key=int(buttons[0].callback_data.split(':')[1])
    choice=bot.store.cached(key,'confirmation')
    bot.set_transfers(False);bot.perform_download=AsyncMock()
    await bot.download_choice(context,choice)
    bot.perform_download.assert_not_awaited()
    bot.set_transfers(True)
    await bot.download_choice(context,choice)
    bot.perform_download.assert_awaited_once()
    job=bot.store.db.execute('SELECT * FROM jobs').fetchone()
    assert job['filename'].endswith('.mp4') and job['size']==0
    assert bot.store.setting(f'job:{job["id"]}:hls')=='mp4'


@pytest.mark.asyncio
async def test_small_hls_offers_existing_upload_preview(tmp_path,monkeypatch):
    store=Store(tmp_path/'db');job=store.create('https://source.test/a.m3u8','Film','Film.ts',0)
    store.update(job,state='downloading');store.set_setting(f'job:{job}:hls','ts')
    bot=Bot(store,tmp_path,42,MagicMock());bot.preview=AsyncMock()
    context=SimpleNamespace(bot=SimpleNamespace(send_message=AsyncMock(return_value=MagicMock(edit_text=AsyncMock()))))
    monkeypatch.setattr('satoubot.download.probe',AsyncMock(return_value={'status':'manual','hls_candidate':True}))
    async def download(url,path,fmt,progress,**kwargs): path.write_bytes(b'Gtest');return 5
    monkeypatch.setattr('satoubot.download.download_hls',download)
    await bot.perform_download(context,job)
    assert store.get(job)['state']=='review' and store.get(job)['size']==5
    bot.preview.assert_awaited_once()


@pytest.mark.asyncio
async def test_direct_hls_offers_transfer_and_manual_link(tmp_path,monkeypatch):
    bot=Bot(Store(tmp_path/'db'),tmp_path,42,MagicMock())
    monkeypatch.setattr('satoubot.download.probe',AsyncMock(return_value={'status':'manual','hls_candidate':True}))
    monkeypatch.setattr('satoubot.download.inspect_hls',AsyncMock(return_value={'format':'ts'}))
    update=SimpleNamespace(message=SimpleNamespace(reply_text=AsyncMock()))
    await bot.handle_source(update,'https://source.test/film.m3u8')
    args=update.message.reply_text.await_args
    assert 'unknown' in args.args[0]
    assert [b.text for b in args.kwargs['reply_markup'].inline_keyboard[0]]==['Transfer','Download only','Give link','No']


@pytest.mark.asyncio
async def test_direct_media_above_2gb_is_approved_and_saved_locally(tmp_path,monkeypatch):
    size=3_000_000_000
    store=Store(tmp_path/'db');bot=Bot(store,tmp_path,42,MagicMock());bot.preview=AsyncMock()
    info={'status':'ready','size':size,'filename':'large.mkv','content_type':'video/x-matroska'}
    monkeypatch.setattr('satoubot.download.probe',AsyncMock(return_value=info))
    update=SimpleNamespace(message=SimpleNamespace(reply_text=AsyncMock()))
    await bot.handle_source(update,'https://source.test/large.mkv')
    args=update.message.reply_text.await_args
    assert 'saved locally' in args.args[0] and args.kwargs['reply_markup'].inline_keyboard[0][0].text=='Transfer'
    job=store.find_source('https://source.test/large.mkv')
    store.update(job['id'],state='downloading')
    async def download(url,path,expected,progress,**kwargs):
        assert expected == size
        with path.open('wb') as f: f.truncate(size)
    monkeypatch.setattr('satoubot.download.download',download)
    context=SimpleNamespace(bot=SimpleNamespace(send_message=AsyncMock(return_value=MagicMock(edit_text=AsyncMock()))))
    monkeypatch.setattr('satoubot.preferences.inspect_media', AsyncMock(return_value={'streams':[]}))
    await bot.perform_download(context,job['id'])
    assert store.get(job['id'])['state']=='local_only'
    assert store.get(job['id'])['size']==size
    bot.preview.assert_not_awaited()


@pytest.mark.asyncio
async def test_download_accepts_verified_size_above_upload_limit_but_rejects_short_body(tmp_path,monkeypatch):
    from satoubot.transfer import download,MAX_BYTES
    calls=[]
    def response(req): calls.append(req);return httpx.Response(200,content=b'abc')
    monkeypatch.setattr('satoubot.transfer.PublicTransport',lambda: httpx.MockTransport(response))
    with pytest.raises(ValueError,match='does not match'):
        await download('https://source.test/large.mkv',tmp_path/'large.mkv',MAX_BYTES+1,AsyncMock(),connections=1)
    assert len(calls)==1 and not (tmp_path/'large.mkv').exists()


@pytest.mark.asyncio
async def test_upload_rechecks_actual_file_size_before_sending(tmp_path):
    file=tmp_path/'large.mp4'
    with file.open('wb') as f: f.truncate(2_000_000_001)
    store=Store(tmp_path/'db');job=store.create('https://source.test/film','Film','large.mp4',100)
    store.update(job,state='uploading',target='-100123',path=str(file))
    uploader=MagicMock();uploader.send_file=AsyncMock()
    bot=Bot(store,tmp_path,42,uploader)
    context=SimpleNamespace(bot=SimpleNamespace(send_message=AsyncMock()))
    await bot.perform_upload(context,job)
    uploader.send_file.assert_not_awaited()
    assert store.get(job)['state']=='local_only' and store.get(job)['size']==2_000_000_001
