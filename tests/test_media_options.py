import asyncio
import hashlib
import io
import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from satoubot.app import Bot, current_admin
from satoubot.discovery import Discovery, stream_records
from satoubot.media import MediaError, convert_media, inspect_media, inspect_remote_media, reported_tracks, track_summary
from satoubot.store import Store
from satoubot.torrent import TorrentError, encode, inspect_torrent, normalize_magnet, run_aria, download_torrent


def torrent_data(files=None, extra=None):
    content = b'synthetic media bytes'
    info = {b'name':b'movie.mkv', b'piece length':16384, b'length':len(content), b'pieces':hashlib.sha1(content).digest()}
    if files:
        info.pop(b'length'); info[b'files'] = files
        total = sum(f[b'length'] for f in files)
        info[b'pieces'] = b'0'*(((total+16383)//16384)*20)
    info.update(extra or {})
    return encode({b'info':info, b'url-list':[b'http://127.0.0.1/private'], b'announce':b'http://127.0.0.1/tracker'})


@pytest.fixture
def bot_context(tmp_path):
    bot = Bot(Store(tmp_path/'db'), tmp_path, 42, MagicMock())
    context = SimpleNamespace(bot=SimpleNamespace(send_message=AsyncMock()), user_data={})
    bot.preview = AsyncMock()
    bot.show_panel = AsyncMock()
    return bot, context


def test_torrent_metadata_strips_webseeds_trackers_preserves_hash_and_selection():
    data = torrent_data([{b'path':[b'a.mkv'], b'length':10}, {b'path':[b'b.srt'], b'length':5}])
    from satoubot.torrent import decode
    clean, entries, digest = inspect_torrent(data)
    assert list(decode(clean)) == [b'info']
    assert digest == hashlib.sha1(encode(decode(data)[b'info'])).hexdigest()
    assert entries == [{'index':1,'name':'movie.mkv/a.mkv','size':10}, {'index':2,'name':'movie.mkv/b.srt','size':5}]


@pytest.mark.parametrize('changes', [{b'name':b'../escape'}, {b'name':b'/absolute'}, {b'name':b'C:\\escape'},
    {b'private':1}, {b'piece length':0}, {b'pieces':b'bad'}, {b'files':[{b'path':[b'..',b'bad'], b'length':1}]},
    {b'files':[{b'path':[b'link'], b'length':1, b'attr':b'l'}]}])
def test_torrent_unsafe_metadata_rejected(changes):
    with pytest.raises(TorrentError): inspect_torrent(torrent_data(extra=changes))


@pytest.mark.parametrize('data',[b'', b'd4:info', b'd4:infoi01ee', b'd4:infoi-0ee', b'd4:infoleejunk', b'd1:bi1e1:ai2ee'])
def test_invalid_bencode_rejected(data):
    with pytest.raises(TorrentError): inspect_torrent(data)


def test_magnet_ignores_private_webseeds_and_trackers_and_rejects_bad_hash():
    magnet, digest = normalize_magnet('magnet:?xt=urn:btih:'+'ab'*20+'&tr=http://127.0.0.1/tracker&ws=http://localhost/file')
    assert magnet == 'magnet:?xt=urn:btih:'+'ab'*20 and digest == 'ab'*20
    with pytest.raises(TorrentError): normalize_magnet('magnet:?xt=urn:btih:../bad')
    assert stream_records([{'infoHash':'ab'*20, 'title':'Movie'}], 'addon')[0]['url'] == magnet


@pytest.mark.asyncio
async def test_preferences_persist_per_admin_and_job_is_snapshot(bot_context):
    bot, context = bot_context
    context.user_data = {}
    update = SimpleNamespace(callback_query=SimpleNamespace(data='pref:output_format:mkv',message=MagicMock()))
    await bot.preference_callback(update, context)
    job = bot.store.create('https://source.test/file','File','File.ts',10)
    bot.save_transfer_options(job)
    assert bot.transfer_options(job)['output_format']=='mkv'
    bot.store.set_setting('admin:42:output_format','mp4')
    assert bot.transfer_options(job)['output_format']=='mkv'
    token = current_admin.set(43)
    try: assert bot.transfer_options()['output_format']=='original'
    finally: current_admin.reset(token)
    reopened = Store(bot.media_dir/'db')
    assert reopened.setting('admin:42:output_format')=='mp4'


@pytest.mark.asyncio
async def test_local_only_keeps_bytes_without_upload_or_destination(bot_context, monkeypatch):
    bot, context = bot_context
    path = bot.media_dir/'file.mkv'; path.write_bytes(b'file')
    job = bot.store.create('https://source.test/file','File',path.name,4)
    bot.save_transfer_options(job, {'download_mode':'local'})
    monkeypatch.setattr('satoubot.preferences.inspect_media', AsyncMock(return_value={'streams':[
        {'codec_type':'audio','codec_name':'aac','tags':{'language':'eng'}},
        {'codec_type':'subtitle','codec_name':'subrip','tags':{'language':'hin'}}]}))
    await bot.finish_local(context, job, path)
    assert bot.store.get(job)['state']=='local_only'
    bot.preview.assert_not_awaited()
    text = context.bot.send_message.await_args.args[1]
    assert 'eng' in text and 'hin' in text and 'No upload started' in text
    assert path.read_bytes()==b'file'


@pytest.mark.asyncio
async def test_conversion_failure_keeps_original_and_reviews_it(bot_context, monkeypatch):
    bot, context = bot_context
    path = bot.media_dir/'file.ts'; path.write_bytes(b'file')
    job = bot.store.create('https://source.test/file','File',path.name,4)
    bot.save_transfer_options(job, {'output_format':'mkv'})
    monkeypatch.setattr('satoubot.preferences.convert_media', AsyncMock(side_effect=MediaError('Incompatible format')))
    monkeypatch.setattr('satoubot.preferences.inspect_media', AsyncMock(side_effect=MediaError('Not media')))
    await bot.finish_local(context, job, path)
    assert bot.store.get(job)['path']==str(path) and path.read_bytes()==b'file'
    assert 'original retained' in context.bot.send_message.await_args.args[1]
    bot.preview.assert_awaited_once()


@pytest.mark.asyncio
async def test_torrent_file_confirmation_gates_and_only_selected_file_is_launched(bot_context):
    bot, context = bot_context
    metadata = bot.media_dir/'input.torrent';metadata.write_bytes(torrent_data())
    _, entries, _ = inspect_torrent(metadata.read_bytes())
    key=bot.store.cache('torrent', {'path':str(metadata),'entries':entries,'admin':42})
    bot.perform_torrent = AsyncMock()
    q = SimpleNamespace(data=f'torrentpick:{key}:1', message=SimpleNamespace(reply_text=AsyncMock()),edit_message_reply_markup=AsyncMock())
    update=SimpleNamespace(callback_query=q)
    await bot.torrent_callback(update,context)
    assert not bot.tasks and bot.store.db.execute('select count(*) from jobs').fetchone()[0]==0
    bot.store.set_setting('transfers_enabled','0')
    q.data=f'torrentlocal:{key}:1'
    await bot.torrent_callback(update,context)
    assert not bot.tasks and bot.store.cached(key,'torrent')
    bot.store.set_setting('transfers_enabled','1')
    await bot.torrent_callback(update,context)
    await asyncio.gather(*bot.tasks)
    assert bot.perform_torrent.await_count==1
    job=bot.store.db.execute('select * from jobs').fetchone()
    assert bot.transfer_options(job['id'])['download_mode']=='local'
    await bot.torrent_callback(update,context)
    assert bot.perform_torrent.await_count==1


@pytest.mark.asyncio
async def test_torrent_selection_does_not_allow_other_admin(bot_context):
    bot,context=bot_context
    key=bot.store.cache('torrent',{'admin':43,'entries':[]})
    message=SimpleNamespace(reply_text=AsyncMock())
    await bot.torrent_callback(SimpleNamespace(callback_query=SimpleNamespace(data=f'torrentlocal:{key}:1',message=message)),context)
    assert not bot.tasks
    assert 'expired' in message.reply_text.await_args.args[0]


@pytest.mark.asyncio
async def test_aria_cancellation_terminates_its_process(tmp_path, monkeypatch):
    started=asyncio.Event(); stopped=asyncio.Event(); args_seen=[]
    class Process:
        returncode=None
        async def wait(self): await stopped.wait(); return self.returncode
        def terminate(self): self.returncode=-15; stopped.set()
    process=Process()
    async def spawn(*args,**kwargs): args_seen.extend(args);started.set();return process
    monkeypatch.setattr('satoubot.torrent.shutil.which',lambda _: '/usr/bin/aria2c')
    monkeypatch.setattr('satoubot.torrent.asyncio.create_subprocess_exec',spawn)
    task=asyncio.create_task(run_aria('magnet:?xt=urn:btih:'+'ab'*20,tmp_path,AsyncMock()))
    await started.wait();task.cancel()
    with pytest.raises(asyncio.CancelledError): await task
    assert stopped.is_set() and '--enable-rpc=false' in args_seen and '--seed-time=0' in args_seen
    assert '--bt-exclude-tracker=*' in args_seen


@pytest.mark.asyncio
async def test_torrent_failure_removes_only_task_partial_folder(tmp_path, monkeypatch):
    metadata=tmp_path/'input.torrent';metadata.write_bytes(torrent_data())
    folder=tmp_path/'partial';folder.mkdir();(folder/'movie.mkv').write_bytes(b'partial')
    other=tmp_path/'other';other.write_bytes(b'keep')
    monkeypatch.setattr('satoubot.torrent.run_aria',AsyncMock(side_effect=TorrentError('failed')))
    with pytest.raises(TorrentError): await download_torrent(metadata,1,folder,AsyncMock())
    assert not folder.exists() and other.read_bytes()==b'keep'


@pytest.mark.asyncio
async def test_track_sample_ignores_non_range_response_without_reading_body(tmp_path, monkeypatch):
    called=AsyncMock()
    monkeypatch.setattr('satoubot.media.inspect_media',called)
    monkeypatch.setattr('satoubot.public_transport.PublicTransport',lambda:httpx.MockTransport(lambda req:httpx.Response(200,content=b'not a range')))
    with pytest.raises(MediaError): await inspect_remote_media('https://public.test/file',10)
    called.assert_not_awaited()


@pytest.mark.asyncio
async def test_track_sample_bounded_and_strips_credentials_on_redirect(monkeypatch):
    seen=[]
    def response(req):
        seen.append(req)
        if req.url.host=='source.test': return httpx.Response(302,headers={'Location':'https://cdn.test/file'})
        return httpx.Response(206,headers={'Content-Range':'bytes 0-9/10'},content=b'0123456789')
    monkeypatch.setattr('satoubot.public_transport.PublicTransport',lambda:httpx.MockTransport(response))
    async def inspect(path):
        assert Path(path).read_bytes()==b'0123456789'
        return {'streams':[]}
    monkeypatch.setattr('satoubot.media.inspect_media',inspect)
    await inspect_remote_media('https://source.test/file',10,{'Authorization':'secret','Cookie':'private'})
    assert seen[-1].headers['Range']=='bytes=0-9'
    assert 'authorization' not in seen[-1].headers and 'cookie' not in seen[-1].headers


@pytest.mark.asyncio
async def test_removed_source_migration_preserves_addons_jobs_and_local_settings(tmp_path):
    path=tmp_path/'db'; store=Store(path)
    store.db.execute("insert into manifests values ('http://localhost:3002/','animepahe','{}')")
    Discovery(store).register('https://own.test/manifest.json',{'name':'Own','catalogs':[]})
    job=store.create('https://source.test/file','Title','Title.mkv',10)
    store.set_setting('admin:42:output_format','mkv')
    store.db.commit();store.db.close()
    store=Store(path)
    assert len(Discovery(store).manifests())==1 and store.get(job)
    assert store.setting('admin:42:output_format')=='mkv'


@pytest.mark.asyncio
async def test_posters_and_catalogue_provenance_are_used(bot_context, monkeypatch):
    bot,context=bot_context
    movie={'id':'tt1','name':'Film','addon':'https://own.test/manifest.json','poster':'https://own.test/poster','background':'https://own.test/scene'}
    Discovery(bot.store).register(movie['addon'],{'name':'My own catalogue','catalogs':[]})
    monkeypatch.setattr(Discovery,'details',AsyncMock(return_value=movie))
    monkeypatch.setattr(Discovery,'streams',AsyncMock(return_value=([],[])))
    artwork=AsyncMock(return_value=io.BytesIO(b'image'))
    monkeypatch.setattr('satoubot.catalogue.landscape',artwork)
    context.bot.send_photo=AsyncMock()
    await bot.movie_card(context,bot.store.cache('movie',movie))
    artwork.assert_awaited_once_with(movie['poster'])
    assert 'My own catalogue · poster' in context.bot.send_photo.await_args.kwargs['caption']


@pytest.mark.asyncio
async def test_real_ts_mkv_tracks_and_compatible_mp4_conversion(tmp_path):
    assert shutil.which('ffmpeg') and shutil.which('ffprobe'), 'Install documented ffmpeg prerequisite'
    subtitle=tmp_path/'sub.srt';subtitle.write_text('1\n00:00:00,000 --> 00:00:01,000\nSynthetic subtitle\n')
    source=tmp_path/'original.mkv'
    subprocess.run(['ffmpeg','-nostdin','-v','error','-f','lavfi','-i','color=c=blue:s=160x90:r=10:d=1',
                    '-f','lavfi','-i','sine=frequency=440:duration=1','-i',str(subtitle),'-map','0','-map','1','-map','2',
                    '-c:v','libx264','-threads','1','-c:a','aac','-c:s','srt',
                    '-metadata:s:a:0','language=eng','-metadata:s:s:0','language=hin',str(source)],check=True)
    before=hashlib.sha256(source.read_bytes()).hexdigest()
    converted=await convert_media(source,'mp4-h264')
    info=await inspect_media(converted)
    assert 'eng' in track_summary(info) and 'hin' in track_summary(info)
    assert any(s['codec_name']=='mov_text' for s in info['streams'])
    assert hashlib.sha256(source.read_bytes()).hexdigest()==before
    ts=tmp_path/'hls.ts'
    subprocess.run(['ffmpeg','-v','error','-i',str(source),'-map','0:v','-map','0:a','-c','copy','-f','mpegts',str(ts)],check=True)
    mkv=await convert_media(ts,'mkv')
    assert {'audio','video'} <= {s['codec_type'] for s in (await inspect_media(mkv))['streams']}
    assert ts.exists() and mkv.stat().st_size>0
    with pytest.raises(MediaError,match='already exists'): await convert_media(ts,'mkv')


@pytest.mark.asyncio
async def test_invalid_local_conversion_and_size_limit_preserve_original(tmp_path):
    path=tmp_path/'broken.ts';path.write_bytes(b'not media')
    with pytest.raises(MediaError): await convert_media(path,'mkv')
    assert path.read_bytes()==b'not media' and not list(tmp_path.glob('*part*'))
    assert 'not supplied' in reported_tracks({})


@pytest.mark.asyncio
async def test_real_aria2_download_from_synthetic_local_peer(tmp_path, monkeypatch):
    """No public trackers/peers: a local fixture tracker points at one local seed."""
    import socket
    import struct
    import threading
    import time
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    assert shutil.which('aria2c'), 'Install documented aria2 prerequisite'
    content = b'X' * 65536
    skipped = b'Y' * 16384
    all_bytes = skipped + content
    piece_length = 16384
    info = {b'name':b'fixture', b'files':[{b'path':[b'skipped.bin'],b'length':len(skipped)}, {b'path':[b'fixture.bin'],b'length':len(content)}], b'piece length':piece_length,
            b'pieces':b''.join(hashlib.sha1(all_bytes[i:i+piece_length]).digest() for i in range(0,len(all_bytes),piece_length))}
    metadata=tmp_path/'fixture.torrent';metadata.write_bytes(encode({b'info':info}))
    seed_dir=tmp_path/'seed';seed_dir.mkdir();(seed_dir/'fixture').mkdir()
    (seed_dir/'fixture'/'fixture.bin').write_bytes(content)
    (seed_dir/'fixture'/'skipped.bin').write_bytes(skipped)
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0));seed_port=sock.getsockname()[1]
    class Tracker(BaseHTTPRequestHandler):
        def log_message(self,*args): pass
        def do_GET(self):
            data=encode({b'interval':1,b'peers':socket.inet_aton('127.0.0.1')+struct.pack('!H',seed_port)})
            self.send_response(200);self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)
    tracker=ThreadingHTTPServer(('127.0.0.1',0),Tracker)
    threading.Thread(target=tracker.serve_forever,daemon=True).start()
    address=f'http://127.0.0.1:{tracker.server_port}/announce'
    actual_spawn=asyncio.create_subprocess_exec
    seed=await actual_spawn('aria2c','--no-conf=true','--enable-rpc=false','--enable-dht=false',
        '--enable-peer-exchange=false','--disable-ipv6=true','--bt-seed-unverified=true','--seed-time=1',
        '--seed-ratio=100','--listen-port='+str(seed_port),'--bt-tracker='+address,'--dir='+str(seed_dir),str(metadata),
        stdout=asyncio.subprocess.DEVNULL,stderr=asyncio.subprocess.DEVNULL)
    try:
        for _ in range(100):
            try:
                reader,writer=await asyncio.open_connection('127.0.0.1',seed_port)
                writer.close();await writer.wait_closed();break
            except OSError: await asyncio.sleep(.05)
        else: pytest.fail('Local aria2 seed did not listen')
        async def local_only_spawn(*args, **kwargs):
            # Test-only override. Production never accepts tracker flags from chat input.
            return await actual_spawn(*args[:-1], '--enable-dht=false','--enable-peer-exchange=false',
                '--disable-ipv6=true','--bt-exclude-tracker=','--bt-tracker='+address,
                '--bt-tracker-connect-timeout=2','--bt-tracker-timeout=3',args[-1],**kwargs)
        monkeypatch.setattr('satoubot.torrent.asyncio.create_subprocess_exec',local_only_spawn)
        path=await asyncio.wait_for(download_torrent(metadata,2,tmp_path/'download',AsyncMock()),25)
        assert path.read_bytes()==content
        assert not (tmp_path/'download'/'fixture'/'skipped.bin').exists()
    finally:
        if seed.returncode is None:
            seed.terminate();await seed.wait()
        tracker.shutdown();tracker.server_close()


@pytest.mark.asyncio
async def test_track_sample_cannot_request_private_destinations():
    with pytest.raises(ValueError,match='not public'):
        await inspect_remote_media('http://127.0.0.1/private', 10)


@pytest.mark.asyncio
async def test_actual_hls_download_then_local_mkv_and_track_review(tmp_path, monkeypatch):
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    source = tmp_path/'source';source.mkdir()
    subprocess.run(['ffmpeg','-nostdin','-v','error','-f','lavfi','-i','color=c=blue:s=160x90:r=10:d=1',
                    '-f','lavfi','-i','sine=frequency=440:duration=1','-map','0','-map','1',
                    '-c:v','libx264','-threads','1','-c:a','aac','-f','hls','-hls_time','1','-hls_playlist_type','vod',
                    str(source/'index.m3u8')],check=True)
    from http.server import SimpleHTTPRequestHandler
    class Handler(SimpleHTTPRequestHandler):
        def __init__(self,*args,**kwargs): super().__init__(*args,directory=str(source),**kwargs)
        def log_message(self,*args): pass
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    threading.Thread(target=server.serve_forever,daemon=True).start()
    async def resolve(host):
        if host!='fixture.test': raise ValueError('private')
        return '127.0.0.1'
    monkeypatch.setattr('satoubot.public_transport.public_address',resolve)
    from satoubot.hls import download_hls
    try:
        ts=tmp_path/'download.ts'
        await download_hls(f'http://fixture.test:{server.server_port}/index.m3u8',ts,'ts',AsyncMock())
        mkv=await convert_media(ts,'mkv')
        tracks=await inspect_media(mkv)
        assert {'audio','video'} <= {s['codec_type'] for s in tracks['streams']}
        assert float(tracks['format']['duration']) > .5
        assert ts.exists()
    finally:
        await asyncio.to_thread(server.shutdown);server.server_close()


@pytest.mark.asyncio
async def test_oversized_conversion_removes_partial_output_and_keeps_original(tmp_path):
    source=tmp_path/'file.mkv'
    subprocess.run(['ffmpeg','-nostdin','-v','error','-f','lavfi','-i','testsrc2=size=128x72:rate=10:duration=1',
                    '-c:v','mpeg4',str(source)],check=True)
    before=source.read_bytes()
    with pytest.raises(MediaError): await convert_media(source,'mkv',limit=500)
    assert source.read_bytes()==before and not list(tmp_path.glob('*.converted.*'))


@pytest.mark.asyncio
async def test_gui_cancel_stops_metadata_lookup_and_preserves_transfer(bot_context):
    bot,context=bot_context
    started=asyncio.Event()
    async def lookup(): started.set();await asyncio.Event().wait()
    await bot.start_panel_task('torrent','Metadata',lookup,context)
    await started.wait()
    transfer=asyncio.create_task(asyncio.Event().wait());bot.tasks.add(transfer)
    bot.main_menu=AsyncMock()
    try:
        await bot.menu_callback(SimpleNamespace(callback_query=SimpleNamespace(data='menu:cancel',message=MagicMock())),context)
        assert 'torrent' in bot.panel().tasks and not transfer.done()
    finally:
        transfer.cancel();await asyncio.gather(transfer,return_exceptions=True)


@pytest.mark.asyncio
async def test_local_playlist_demuxer_is_not_allowed(tmp_path):
    other=tmp_path/'secret.wav'
    subprocess.run(['ffmpeg','-nostdin','-v','error','-f','lavfi','-i','sine=duration=1',str(other)],check=True)
    playlist=tmp_path/'disguised.mkv'
    playlist.write_text('ffconcat version 1.0\nfile secret.wav\n')
    with pytest.raises(MediaError): await inspect_media(playlist)


@pytest.mark.asyncio
async def test_download_only_catalogue_confirmation_preserves_gate_and_settings_snapshot(bot_context, monkeypatch):
    bot,context=bot_context
    bot.store.set_setting('admin:42:output_format','mkv')
    stream={'url':'https://file.test/file.ts','movie':{'name':'Movie'},'file_type':'ts','transfer_options':bot.transfer_options()}
    key=bot.store.cache('confirmation',stream)
    bot.store.set_setting('admin:42:output_format','mp4')
    bot.download_choice=AsyncMock()
    q=SimpleNamespace(data=f'yeslocal:{key}',message=SimpleNamespace(reply_text=AsyncMock()), edit_message_reply_markup=AsyncMock())
    await bot.catalogue_callback(SimpleNamespace(callback_query=q),context)
    await asyncio.gather(*bot.tasks)
    accepted=bot.download_choice.await_args.args[1]
    assert accepted['transfer_options']=={'output_format':'mkv','download_mode':'local'}
    assert bot.store.cached(key,'confirmation') is None


@pytest.mark.asyncio
async def test_long_track_preview_respects_telegram_photo_caption_limit(bot_context):
    bot,context=bot_context
    del bot.preview
    path=bot.media_dir/'file.mkv';path.write_bytes(b'file')
    thumb=bot.media_dir/'poster.jpg';thumb.write_bytes(b'jpg')
    job=bot.store.create('https://source.test/file','Movie',path.name,4)
    bot.store.update(job,path=str(path),thumbnail=str(thumb),state='review')
    streams=[{'codec_type':'audio','codec_name':'aac','tags':{'language':'eng','title':'Long track title '*6}} for _ in range(15)]
    streams += [{'codec_type':'subtitle','codec_name':'subrip','tags':{'language':'hin','title':'Subtitle '*15}} for _ in range(15)]
    bot.store.set_setting(f'job:{job}:media',json.dumps({'streams':streams}))
    context.bot.send_photo=AsyncMock()
    await bot.preview(context,job)
    assert len(context.bot.send_photo.await_args.kwargs['caption'])<=1024
    assert 'Audio:' in context.bot.send_message.await_args.args[1]
    assert 'Approve upload' in context.bot.send_message.await_args.kwargs['reply_markup'].inline_keyboard[0][0].text


@pytest.mark.parametrize('changes',[
    {b'name.utf-8':b'../escape'},
    {b'files':[{b'path':[b'safe'],b'path.utf-8':[b'..',b'escape'],b'length':1}]},
    {b'attr':b'l', b'symlink path':[b'..',b'escape']},
])
def test_alternate_torrent_paths_cannot_escape_validation(changes):
    with pytest.raises(TorrentError): inspect_torrent(torrent_data(extra=changes))


def test_interrupted_conversion_retains_completed_local_file(bot_context):
    from satoubot.preferences import recover_local_jobs
    bot,_=bot_context
    path=bot.media_dir/'movie.ts';path.write_bytes(b'local media')
    job=bot.store.create('https://file.test/file','Movie','movie.ts',0)
    bot.store.update(job,state='converting',path=str(path),approval_token='old')
    recover_local_jobs(bot.store)
    row=bot.store.get(job)
    assert row['state']=='local_only' and row['size']==len(b'local media')
    assert not row['approval_token'] and path.read_bytes()==b'local media'
