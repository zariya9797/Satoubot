import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from telegram.error import BadRequest

from satoubot.app import Bot
from satoubot.discovery import Discovery, HDHUB
from satoubot.store import Store


def setup_service(tmp_path, n=1):
    store=Store(tmp_path/'db'); service=Discovery(store)
    service.register(HDHUB, {'name':'HdHub','catalogs':[]})
    service.register('https://repo.test/manifest.json', {'name':'Plugins','scrapers':[
        {'name':str(i),'filename':str(i)+'.js','supportedTypes':['movie']} for i in range(n)]})
    return store,service


@pytest.mark.asyncio
async def test_provider_limit_and_results_arrive_before_slow_sources(tmp_path,monkeypatch):
    store,service=setup_service(tmp_path,8)
    started=asyncio.Event();release=asyncio.Event(); active=0; peak=0; batches=[]
    async def code(url): return url.rsplit('/',1)[-1].encode()
    async def run(code,tmdb):
        nonlocal active,peak
        active+=1;peak=max(active,peak)
        try:
            if code!='0.js': await release.wait()
            return [{'url':'https://file.test/'+code}]
        finally: active-=1
    async def result(batch,error): batches.extend(batch);started.set()
    monkeypatch.setattr('satoubot.discovery.fetch_bytes',code)
    monkeypatch.setattr('satoubot.discovery.run_plugin',run)
    task=asyncio.create_task(service.streams({'id':'tt1','moviedb_id':1},on_result=result))
    try:
        await asyncio.wait_for(started.wait(),1)
        assert batches[0]['url'].endswith('0.js')
        assert not task.done() and peak<=4
        release.set();found,errors=await task
        assert len(found)==8 and not errors and peak==4
    finally:
        task.cancel();await asyncio.gather(task,return_exceptions=True)


@pytest.mark.asyncio
async def test_slow_provider_is_cancelled_without_losing_fast_result(tmp_path,monkeypatch):
    _,service=setup_service(tmp_path,2)
    cancelled=asyncio.Event()
    async def fetch(url): return url.rsplit('/',1)[-1].encode()
    async def run(code,tmdb):
        if code=='0.js': return [{'url':'https://fast.test/file'}]
        try: await asyncio.Event().wait()
        finally: cancelled.set()
    monkeypatch.setattr('satoubot.discovery.PROVIDER_TIMEOUT',.05)
    monkeypatch.setattr('satoubot.discovery.fetch_bytes',fetch)
    monkeypatch.setattr('satoubot.discovery.run_plugin',run)
    found,errors=await service.streams({'id':'tt1','moviedb_id':1})
    assert len(found)==1 and errors==['1'] and cancelled.is_set()


@pytest.mark.asyncio
async def test_manifest_metadata_cache_expiry_and_registration_invalidation(tmp_path,monkeypatch):
    store=Store(tmp_path/'db');service=Discovery(store)
    calls=[]
    async def fetch(url):
        calls.append(url)
        if 'manifest' in url: return {'name':'Catalogue','catalogs':[]}
        return {'meta':{'name':'Example'}}
    monkeypatch.setattr('satoubot.discovery.fetch_json',fetch)
    await service.add('https://catalog.test/manifest.json')
    await service.add('https://catalog.test/manifest.json')
    assert len(calls)==1
    movie={'id':'tt1','addon':'https://catalog.test/manifest.json'}
    await service.details(movie);await service.details(movie)
    assert len(calls)==2
    key='https://catalog.test/meta/movie/tt1.json'
    store._discovery_memo[key]=(0, {'meta':{'name':'old'}})
    await service.details(movie)
    assert len(calls)==3
    service.register('https://catalog.test/manifest.json',{'name':'updated','catalogs':[]})
    await service.details(movie)
    assert len(calls)==4


@pytest.mark.asyncio
async def test_provider_429_and_forbidden_are_not_retried_or_cached(tmp_path,monkeypatch):
    store=Store(tmp_path/'db'); service=Discovery(store)
    service.register(HDHUB, {'name':'HdHub','catalogs':[]})
    service.register('https://repo.test/manifest.json', {'name':'No JS','scrapers':[]})
    service.register('https://stream.test/manifest.json', {'name':'Source','resources':['stream'],'catalogs':[]})
    fetch=AsyncMock(side_effect=[RuntimeError('429'),RuntimeError('403')])
    monkeypatch.setattr('satoubot.discovery.fetch_json',fetch)
    for _ in range(2):
        found,errors=await service.streams({'id':'tt1'})
        assert not found and errors==['Source']
    assert fetch.await_count==2


@pytest.mark.asyncio
async def test_movie_card_shows_partial_checked_result_while_provider_runs(tmp_path,monkeypatch):
    store=Store(tmp_path/'db'); bot=Bot(store,tmp_path,42,MagicMock())
    movie={'id':'tt1','name':'Example','year':'2024'}; key=store.cache('movie',movie)
    release=asyncio.Event();sent=asyncio.Event()
    async def results(self,movie,on_result=None):
        first={'url':'https://file.test/movie','quality':'720p'}
        await on_result([first],None)
        await release.wait()
        return [first], []
    monkeypatch.setattr(Discovery,'details',AsyncMock(return_value=movie))
    monkeypatch.setattr(Discovery,'streams',results)
    monkeypatch.setattr('satoubot.catalogue.probe',AsyncMock(return_value={'status':'ready','size':100,'filename':'movie.mkv','content_type':'video/x-matroska'}))
    context=MagicMock();message=MagicMock();message.edit_text=AsyncMock()
    async def send(*args,**kwargs):
        assert 'Searching other providers' in args[1]
        assert 'mkv' in kwargs['reply_markup'].inline_keyboard[0][0].text
        sent.set();return message
    context.bot.send_message=AsyncMock(side_effect=send)
    task=asyncio.create_task(bot.movie_card(context,key))
    try:
        await asyncio.wait_for(sent.wait(),1)
        assert not task.done()
        release.set(); await task
        assert message.edit_text.await_count>=1
    finally:
        release.set();task.cancel();await asyncio.gather(task,return_exceptions=True)


@pytest.mark.asyncio
async def test_stale_approval_during_permission_check_never_uploads(tmp_path):
    store=Store(tmp_path/'db');job=store.create('https://source.test/file','Example','file.mkv',100)
    store.set_setting('target','-100123');store.update(job,state='review',target='-100123',approval_token='old')
    bot=Bot(store,tmp_path,42,MagicMock());started=asyncio.Event();release=asyncio.Event()
    async def check(*args):
        started.set(); await release.wait()
        return SimpleNamespace(status='administrator',can_post_messages=True)
    ctx=MagicMock();ctx.bot.get_chat_member=check
    q=MagicMock();q.data=f'upload:{job}:old';q.answer=AsyncMock();q.message.reply_text=AsyncMock();q.edit_message_reply_markup=AsyncMock()
    update=MagicMock();update.effective_user.id=42;update.effective_chat.type='private';update.callback_query=q
    task=asyncio.create_task(bot.callback(update,ctx));await started.wait()
    store.update(job,approval_token='new')
    release.set();await task
    assert store.get(job)['state']=='review' and not bot.tasks
    assert 'out of date' in q.message.reply_text.await_args.args[0]


@pytest.mark.asyncio
async def test_repeated_extension_never_appears_in_renamed_file(tmp_path):
    store=Store(tmp_path/'db');job=store.create('https://file.test','Movie','old.mkv',100)
    path=tmp_path/'old.mkv';path.write_bytes(b'x');store.update(job,state='review',path=str(path))
    bot=Bot(store,tmp_path,42,MagicMock());bot.preview=AsyncMock()
    update=MagicMock();update.effective_user.id=42;update.effective_chat.type='private';update.message.reply_text=AsyncMock()
    context=MagicMock();context.args=[str(job),'Movie.mkv.mkv']
    await bot.rename(update,context)
    assert store.get(job)['filename']=='Movie.mkv'


@pytest.mark.asyncio
async def test_cached_metadata_does_not_cache_failed_access(tmp_path,monkeypatch):
    store=Store(tmp_path/'db');service=Discovery(store)
    fetch=AsyncMock(side_effect=[RuntimeError('403'), {'meta':{'name':'Allowed'}}])
    monkeypatch.setattr('satoubot.discovery.fetch_json',fetch)
    movie={'id':'tt1','addon':'https://catalog.test/manifest.json'}
    with pytest.raises(RuntimeError): await service.details(movie)
    assert (await service.details(movie))['name']=='Allowed'
    assert fetch.await_count==2


@pytest.mark.asyncio
async def test_uploading_job_cannot_receive_new_preview(tmp_path):
    store=Store(tmp_path/'db');job=store.create('https://file.test','Movie','file.mkv',100)
    store.update(job,state='uploading',approval_token='')
    bot=Bot(store,tmp_path,42,MagicMock());context=MagicMock()
    context.bot.send_message=AsyncMock();context.bot.send_photo=AsyncMock()
    await bot.preview(context,job)
    assert store.get(job)['approval_token']==''
    context.bot.send_message.assert_not_awaited();context.bot.send_photo.assert_not_awaited()


@pytest.mark.asyncio
async def test_addons_on_same_host_do_not_overlap(tmp_path,monkeypatch):
    store=Store(tmp_path/'db');service=Discovery(store)
    service.register(HDHUB, {'name':'HdHub','catalogs':[]})
    service.register('https://repo.test/manifest.json', {'name':'No JS','scrapers':[]})
    for name in ('a','b'):
        service.register(f'https://same.test/{name}/manifest.json', {'name':name,'resources':['stream'],'catalogs':[]})
    active=0;peak=0
    async def fetch(url):
        nonlocal active,peak
        active+=1;peak=max(peak,active)
        try: await asyncio.sleep(.01);return {'streams':[]}
        finally:active-=1
    monkeypatch.setattr('satoubot.discovery.fetch_json',fetch)
    await service.streams({'id':'tt1'})
    assert peak==1


@pytest.mark.asyncio
async def test_only_installed_sources_are_used_without_automatic_manifest_additions(tmp_path,monkeypatch):
    store=Store(tmp_path/'db');service=Discovery(store)
    service.register('https://repo.test/manifest.json', {'name':'Plugins','scrapers':[{'name':'Ready','filename':'ready.js','supportedTypes':['movie']}]})
    release=asyncio.Event(); first=asyncio.Event()
    async def add(url): await release.wait(); raise RuntimeError('unavailable')
    monkeypatch.setattr(service,'add',add)
    monkeypatch.setattr('satoubot.discovery.fetch_bytes',AsyncMock(return_value=b'code'))
    monkeypatch.setattr('satoubot.discovery.run_plugin',AsyncMock(return_value=[{'url':'https://file.test/media'}]))
    async def on_result(batch,error):
        if batch: first.set()
    task=asyncio.create_task(service.streams({'id':'tt1','moviedb_id':1},on_result=on_result))
    try:
        await asyncio.wait_for(first.wait(),1)
        found,errors=await task
        assert len(found)==1 and not errors
        assert len(service.manifests()) == 1
    finally:
        release.set();task.cancel();await asyncio.gather(task,return_exceptions=True)


@pytest.mark.asyncio
async def test_partial_card_batches_update_while_other_sources_remain(tmp_path,monkeypatch):
    store=Store(tmp_path/'db');bot=Bot(store,tmp_path,42,MagicMock())
    movie={'id':'tt1','name':'Example'}; key=store.cache('movie',movie)
    release=asyncio.Event(); edited=asyncio.Event()
    async def results(self,movie,on_result=None):
        await on_result([{'url':'https://file.test/first','quality':'720p'}],None)
        await asyncio.sleep(.05)
        await on_result([{'url':'https://file.test/second','quality':'1080p'}],None)
        await release.wait()
        return [], []
    monkeypatch.setattr(Discovery,'details',AsyncMock(return_value=movie))
    monkeypatch.setattr(Discovery,'streams',results)
    monkeypatch.setattr('satoubot.catalogue.probe',AsyncMock(return_value={'status':'ready','size':100,'filename':'movie.mkv','content_type':'video/x-matroska'}))
    context=MagicMock();message=MagicMock()
    async def edit(text,**kwargs):
        if 'Available files: 2' in text: edited.set()
    message.edit_text=AsyncMock(side_effect=edit);context.bot.send_message=AsyncMock(return_value=message)
    # Distinct source identities ensure both checked files remain visible.
    async def probe(url,headers):
        return {'status':'ready','size':100,'filename':url.rsplit('/',1)[-1]+'.mkv','content_type':'video/x-matroska'}
    monkeypatch.setattr('satoubot.catalogue.probe',probe)
    task=asyncio.create_task(bot.movie_card(context,key))
    try:
        await asyncio.wait_for(edited.wait(),2)
        assert not task.done()
    finally:
        release.set();await task


@pytest.mark.asyncio
async def test_verified_mkv_survives_metadata_warning(tmp_path,monkeypatch):
    import warnings
    store=Store(tmp_path/'db');bot=Bot(store,tmp_path,42,MagicMock())
    movie={'id':'tt1','name':'Example'}; key=store.cache('movie',movie)
    monkeypatch.setattr(Discovery,'details',AsyncMock(return_value=movie))
    monkeypatch.setattr(Discovery,'streams',AsyncMock(return_value=([{'url':'https://file.test/download','quality':'720p'}],[])))
    async def probe(url,headers):
        warnings.warn('Metadata parser unavailable')
        return {'status':'ready','size':100,'filename':'file.mkv','content_type':'application/octet-stream'}
    monkeypatch.setattr('satoubot.catalogue.probe',probe)
    context=MagicMock();context.bot.send_message=AsyncMock(return_value=MagicMock(edit_text=AsyncMock()))
    with pytest.warns(UserWarning,match='Metadata parser'):
        await bot.movie_card(context,key)
    assert 'mkv' in context.bot.send_message.await_args.kwargs['reply_markup'].inline_keyboard[0][0].text


@pytest.mark.asyncio
@pytest.mark.parametrize('photo', [False, True])
async def test_failed_card_update_does_not_hide_later_downloadable_link(tmp_path, monkeypatch, photo):
    import io
    store = Store(tmp_path/'db'); bot = Bot(store, tmp_path, 42, MagicMock())
    movie = {'id':'tt1', 'name':'Example'}
    if photo: movie['poster'] = 'https://image.test/poster.jpg'
    manual_shown = asyncio.Event()
    streams = [{'url':'https://wrapper.test/file', 'quality':'1080p'},
               {'url':'https://direct.test/file', 'quality':'1080p'}]
    monkeypatch.setattr(Discovery, 'details', AsyncMock(return_value=movie))
    monkeypatch.setattr(Discovery, 'streams', AsyncMock(return_value=(streams, [])))
    monkeypatch.setattr('satoubot.catalogue.landscape', AsyncMock(return_value=io.BytesIO(b'jpeg')))
    async def check(url, headers):
        if 'wrapper.test' in url:
            return {'status':'manual', 'reason':'Provider page'}
        await manual_shown.wait()
        return {'status':'ready', 'size':3_799_555_945, 'filename':'movie.mkv', 'content_type':'video/x-matroska'}
    monkeypatch.setattr('satoubot.catalogue.probe', check)
    old_card = SimpleNamespace(
        edit_text=AsyncMock(side_effect=BadRequest('Message to edit not found')),
        edit_caption=AsyncMock(side_effect=BadRequest('Message to edit not found')),
        delete=AsyncMock())
    replacement = SimpleNamespace(edit_text=AsyncMock(), delete=AsyncMock())
    sent = []
    async def send(*args, **kwargs):
        sent.append(kwargs['reply_markup'])
        manual_shown.set()
        return old_card if len(sent) == 1 else replacement
    context = SimpleNamespace(bot=SimpleNamespace(send_message=AsyncMock(side_effect=send),
                                                  send_photo=AsyncMock(side_effect=send)))
    await asyncio.wait_for(bot.movie_card(context, store.cache('movie', movie)), 2)
    assert sent[0].inline_keyboard[0][0].text.startswith('Manual ')
    assert len(sent) == 2
    button = sent[-1].inline_keyboard[0][0]
    assert button.text.startswith('DW ') and 'mkv' in button.text
    assert 'direct.test' in button.text
    stream = store.cached(int(button.callback_data.split(':')[1]), 'stream')
    assert stream['url'] == 'https://direct.test/file'
    assert bot.panel().card is replacement
    old_card.delete.assert_awaited_once()
