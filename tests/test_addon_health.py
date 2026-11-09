import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from satoubot.addon_health import get_status, set_status, label
from satoubot.app import Bot
from satoubot.discovery import Discovery, PRESETS
from satoubot.store import Store


@pytest.mark.asyncio
async def test_provider_default_disabled_can_be_enabled_and_persists(tmp_path, monkeypatch):
    path=tmp_path/'db'; store=Store(path); service=Discovery(store)
    url='https://repo.test/manifest.json'
    service.register(url,{'scrapers':[{'name':'A','filename':'a.js','enabled':False,'supportedTypes':['movie']}]})
    fetch=AsyncMock(return_value=b'code'); run=AsyncMock(return_value=[{'url':'https://media.test/file'}])
    monkeypatch.setattr('satoubot.discovery.fetch_bytes',fetch)
    monkeypatch.setattr('satoubot.discovery.run_plugin',run)
    movie={'id':'tt1','moviedb_id':123}
    assert not service.enabled(url,'a.js')
    assert await service.streams(movie)==([],[])
    fetch.assert_not_awaited()
    service.toggle(url,'a.js')
    service=Discovery(Store(path))
    assert service.enabled(url,'a.js')
    found,errors=await service.streams(movie)
    assert len(found)==1 and not errors
    service.toggle(url)
    fetch.reset_mock()
    assert await service.streams(movie)==([],[])
    fetch.assert_not_awaited()
    service.toggle(url)
    assert service.enabled(url,'a.js')
    service.toggle(url,'a.js')
    assert not service.enabled(url,'a.js')


@pytest.mark.asyncio
async def test_manifest_reachability_and_refresh_preserve_switches(tmp_path):
    state={'code':200,'body':b'{"name":"Updated", "scrapers":[{"filename":"new.js"}]}'}
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args): pass
        def do_GET(self):
            self.send_response(state['code']);self.send_header('Content-Length',str(len(state['body'])));self.end_headers();self.wfile.write(state['body'])
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    threading.Thread(target=server.serve_forever,daemon=True).start()
    try:
        url=f'http://127.0.0.1:{server.server_port}/manifest.json'
        store=Store(tmp_path/'db');service=Discovery(store)
        service.register(url,{'name':'Old','scrapers':[{'filename':'old.js'}]})
        service.toggle(url)
        assert await service.check_manifest(url)=='ok'
        assert not service.enabled(url) and service.manifests(active=False)[0][2]['name']=='Old'
        assert await service.check_manifest(url,refresh=True)=='ok'
        assert not service.enabled(url) and service.manifests(active=False)[0][2]['name']=='Updated'
        state['body']=b'<html>blocked</html>'
        assert await service.check_manifest(url,refresh=True)=='invalid'
        assert service.manifests(active=False)[0][2]['name']=='Updated'
        assert label(store,url,active=False)[0:2]==('🔴','OFF')
        state['code']=503
        assert await service.check_manifest(url)=='error'
        assert 'unavailable' in label(store,url,active=False)[2]
    finally:
        await asyncio.to_thread(server.shutdown);server.server_close()


@pytest.mark.asyncio
async def test_manifest_check_timeout_and_no_secret_in_health_storage(tmp_path,monkeypatch):
    store=Store(tmp_path/'db');service=Discovery(store)
    url='https://example.test/private-token/manifest.json'
    service.register(url,{'catalogs':[]})
    monkeypatch.setattr('satoubot.discovery.fetch_json',AsyncMock(side_effect=TimeoutError()))
    assert await service.check_manifest(url)=='error'
    rows=store.db.execute("select key,value from settings where key like 'addon_status:%'").fetchall()
    assert rows and all('private-token' not in row['key']+row['value'] for row in rows)
    assert get_status(store,url,'manifest')=='error'


def test_status_stales_and_distinguishes_empty_from_unreachable(tmp_path):
    store=Store(tmp_path/'db');url='https://example.test/manifest.json'
    set_status(store,url,'manifest','ok');set_status(store,url,'search','empty')
    icon,mode,text=label(store,url,active=True)
    assert icon=='🔴' and mode=='ON' and 'no results' in text and 'unavailable' not in text
    set_status(store,url,'manifest','error')
    assert label(store,url,active=False)[0:2]==('🔴','OFF')
    assert get_status(store,url,'manifest',now=10**20)=='unknown'


@pytest.mark.asyncio
async def test_search_results_and_errors_are_tracked_per_source(tmp_path,monkeypatch):
    store=Store(tmp_path/'db'); service=Discovery(store)
    urls=['https://a.test/manifest.json','https://b.test/manifest.json','https://c.test/manifest.json']
    for url in urls: service.register(url,{'name':url,'catalogs':[{'id':'find','type':'movie','extra':[{'name':'search'}]}]})
    async def fetch(url):
        if 'a.test' in url: return {'metas':[]}
        if 'b.test' in url: raise TimeoutError()
        return {'metas':[{'id':'tt1','name':'Film'}]}
    monkeypatch.setattr('satoubot.discovery.fetch_json',fetch)
    found, errors=await service.search('Film')
    assert len(found)==1 and errors==[urls[1]]
    assert [get_status(store,url,'search') for url in urls]==['empty','error','ok']


@pytest.mark.asyncio
async def test_stream_results_are_tracked_even_if_duplicate(tmp_path,monkeypatch):
    store=Store(tmp_path/'db');service=Discovery(store);url='https://repo.test/manifest.json'
    service.register(url,{'scrapers':[{'filename':f'{n}.js','supportedTypes':['movie']} for n in range(3)]})
    monkeypatch.setattr('satoubot.discovery.fetch_bytes',AsyncMock(return_value=b'code'))
    results=iter([[{'url':'https://media.test/file'}],[],RuntimeError()])
    async def run(*args):
        value=next(results)
        if isinstance(value,Exception): raise value
        return value
    monkeypatch.setattr('satoubot.discovery.run_plugin',run)
    found,errors=await service.streams({'id':'tt1','moviedb_id':123})
    assert len(found)==1 and errors
    assert [get_status(store,url,'stream',f'{n}.js') for n in range(3)]==['ok','empty','error']


def fake_ui(tmp_path):
    store=Store(tmp_path/'db'); bot=Bot(store,tmp_path,42,MagicMock())
    panel=MagicMock(edit_text=AsyncMock(),delete=AsyncMock(),edit_reply_markup=AsyncMock())
    message=MagicMock(reply_text=AsyncMock(return_value=panel))
    context=SimpleNamespace(user_data={},bot=SimpleNamespace(send_message=AsyncMock(return_value=panel)))
    update=SimpleNamespace(message=message,effective_user=SimpleNamespace(id=42),effective_chat=SimpleNamespace(type='private'),callback_query=None)
    return bot,message,panel,context,update


@pytest.mark.asyncio
async def test_only_two_presets_are_explicit_and_failures_show_red(tmp_path,monkeypatch):
    assert set(PRESETS)=={'yoru','hdhub'}
    bot,message,panel,context,update=fake_ui(tmp_path)
    await bot.addons(update,context)
    buttons=[b for row in message.reply_text.await_args.kwargs['reply_markup'].inline_keyboard for b in row]
    assert {b.callback_data for b in buttons if b.callback_data.startswith('addonpreset:')}=={'addonpreset:yoru','addonpreset:hdhub'}
    assert not bot.store.db.execute('select * from manifests').fetchall()
    monkeypatch.setattr('satoubot.discovery.fetch_json',AsyncMock(side_effect=TimeoutError()))
    update.callback_query=SimpleNamespace(data='addonpreset:yoru',message=message)
    await bot.addon_callback(update,context)
    await asyncio.gather(*bot.discovery_tasks)
    assert get_status(bot.store,PRESETS['yoru'][1],'manifest')=='error'
    await bot.addons(update,context)
    assert any('🔴 Add Yoru' in b.text for row in message.reply_text.await_args.kwargs['reply_markup'].inline_keyboard for b in row)
    assert not bot.store.db.execute('select * from manifests').fetchall()


@pytest.mark.asyncio
async def test_addons_auto_check_only_enabled_and_not_hijack_new_menu(tmp_path,monkeypatch):
    bot,message,panel,context,update=fake_ui(tmp_path);service=Discovery(bot.store)
    on='https://on.test/manifest.json';off='https://off.test/manifest.json'
    for url in (on,off): service.register(url,{'name':url,'catalogs':[]})
    service.toggle(off)
    started,release=asyncio.Event(),asyncio.Event();calls=[]
    async def check(self,url,refresh=False):
        calls.append(url);started.set();await release.wait();set_status(self.store,url,'manifest','error');return 'error'
    monkeypatch.setattr(Discovery,'check_manifest',check)
    await bot.addons(update,context);await started.wait()
    await bot.show_panel(message,'New screen')
    release.set();await asyncio.gather(*bot.discovery_tasks)
    assert calls==[on] and bot.panel().text=='New screen'
    assert not service.enabled(off) and get_status(bot.store,on,'manifest')=='error'


@pytest.mark.asyncio
async def test_check_all_can_explicitly_check_disabled_without_enabling(tmp_path,monkeypatch):
    bot,message,panel,context,update=fake_ui(tmp_path);service=Discovery(bot.store)
    url='https://off.test/manifest.json';service.register(url,{'name':'Disabled','catalogs':[]});service.toggle(url)
    check=AsyncMock(return_value='error');monkeypatch.setattr(service.__class__,'check_manifest',check)
    await bot.addons(update,context)
    rows=message.reply_text.await_args.kwargs['reply_markup'].inline_keyboard
    callback=next(b.callback_data for row in rows for b in row if b.text=='Check manifests on this page')
    update.callback_query=SimpleNamespace(data=callback,message=message)
    await bot.addon_callback(update,context)
    await asyncio.gather(*bot.discovery_tasks)
    check.assert_awaited_once_with(url,refresh=False)
    assert not service.enabled(url)


@pytest.mark.asyncio
async def test_refresh_rejects_manifest_kind_change_and_keeps_original(tmp_path,monkeypatch):
    store=Store(tmp_path/'db');service=Discovery(store);url='https://addon.test/manifest.json'
    service.register(url,{'name':'Catalogue','catalogs':[]})
    monkeypatch.setattr('satoubot.discovery.fetch_json',AsyncMock(return_value={'name':'Other','catalogs':[],'scrapers':[]}))
    assert await service.check_manifest(url,refresh=True)=='invalid'
    entry=service.manifests(active=False)[0]
    assert entry[1]=='addon' and entry[2]['name']=='Catalogue'


@pytest.mark.asyncio
async def test_manifest_check_completes_updates_only_current_menu(tmp_path,monkeypatch):
    bot,message,panel,context,update=fake_ui(tmp_path);service=Discovery(bot.store)
    url='https://on.test/manifest.json';service.register(url,{'name':'Catalog','catalogs':[]})
    monkeypatch.setattr('satoubot.discovery.fetch_json',AsyncMock(side_effect=TimeoutError()))
    await bot.addons(update,context)
    await asyncio.gather(*bot.discovery_tasks)
    assert '🔴 ON · Catalog' in panel.edit_text.await_args.kwargs['reply_markup'].inline_keyboard[0][0].text
    assert 'manifest unavailable' in panel.edit_text.await_args.kwargs['reply_markup'].inline_keyboard[0][0].text
    assert bot.panel().generation==1


@pytest.mark.asyncio
async def test_manifest_refresh_invalidates_memo_and_keeps_explicit_provider_choice(tmp_path,monkeypatch):
    store=Store(tmp_path/'db');service=Discovery(store);url='https://repo.test/manifest.json'
    service.register(url,{'scrapers':[{'filename':'p.js','enabled':False}]})
    service.toggle(url,'p.js')
    store._discovery_memo={'https://repo.test/file':(10**20,{'old':1})}
    monkeypatch.setattr('satoubot.discovery.fetch_json',AsyncMock(return_value={'scrapers':[{'filename':'p.js','enabled':False}]}))
    assert await service.check_manifest(url,refresh=True)=='ok'
    assert service.enabled(url,'p.js')
    assert not store._discovery_memo


@pytest.mark.asyncio
async def test_provider_ui_indicates_parent_off_and_explicit_child_selection(tmp_path):
    bot,message,panel,context,update=fake_ui(tmp_path);service=Discovery(bot.store)
    url='https://repo.test/manifest.json';service.register(url,{'scrapers':[{'name':'Child','filename':'p.js','enabled':False}]})
    service.toggle(url)
    set_status(bot.store,url,'manifest','error')
    await bot.addons(update,context,repository=url)
    row=message.reply_text.await_args.kwargs['reply_markup'].inline_keyboard[0][0]
    assert '🔴 OFF (repository OFF)' in row.text
    update.callback_query=SimpleNamespace(data=row.callback_data,message=message)
    await bot.addon_callback(update,context)
    row=message.reply_text.await_args.kwargs['reply_markup'].inline_keyboard[0][0]
    assert '🔴 ON (repository OFF)' in row.text and service.enabled(url,'p.js') and not service.enabled(url)


@pytest.mark.asyncio
async def test_automatic_check_updates_red_after_user_toggles_same_list(tmp_path,monkeypatch):
    bot,message,panel,context,update=fake_ui(tmp_path);service=Discovery(bot.store)
    url='https://repo.test/manifest.json';service.register(url,{'name':'Repo','catalogs':[]})
    started,release=asyncio.Event(),asyncio.Event()
    async def check(self,url,refresh=False):
        started.set();await release.wait();set_status(self.store,url,'manifest','error');return 'error'
    monkeypatch.setattr(Discovery,'check_manifest',check)
    await bot.addons(update,context);await started.wait()
    rows=message.reply_text.await_args.kwargs['reply_markup'].inline_keyboard
    update.callback_query=SimpleNamespace(data=rows[0][0].callback_data,message=message)
    await bot.addon_callback(update,context)
    assert not service.enabled(url)
    release.set();await asyncio.gather(*bot.discovery_tasks)
    row=panel.edit_text.await_args.kwargs['reply_markup'].inline_keyboard[0][0]
    assert '🔴 OFF · Repo' in row.text and 'manifest unavailable' in row.text


@pytest.mark.asyncio
async def test_adding_preset_is_explicit_and_does_not_install_other_sources(tmp_path,monkeypatch):
    bot,message,panel,context,update=fake_ui(tmp_path)
    fetch=AsyncMock(return_value={'name':'Yoru','scrapers':[{'filename':'p.js','supportedTypes':['movie']}]})
    monkeypatch.setattr('satoubot.discovery.fetch_json',fetch)
    await bot.addons(update,context)
    fetch.assert_not_awaited()
    update.callback_query=SimpleNamespace(data='addonpreset:yoru',message=message)
    await bot.addon_callback(update,context)
    await asyncio.gather(*bot.discovery_tasks)
    entries=Discovery(bot.store).manifests(active=False)
    assert len(entries)==1 and entries[0][0]==PRESETS['yoru'][1] and entries[0][1]=='providers'
    assert get_status(bot.store,PRESETS['yoru'][1],'manifest')=='ok'
    assert bot.store.db.execute('select count(*) from manifests').fetchone()[0]==1


@pytest.mark.asyncio
async def test_untouched_off_source_is_never_automatically_checked(tmp_path,monkeypatch):
    bot,message,panel,context,update=fake_ui(tmp_path);service=Discovery(bot.store)
    url='https://off.test/manifest.json';service.register(url,{'name':'off','catalogs':[]});service.toggle(url)
    check=AsyncMock();monkeypatch.setattr(Discovery,'check_manifest',check)
    await bot.addons(update,context)
    assert not getattr(bot,'discovery_tasks',set())
    check.assert_not_awaited()


@pytest.mark.asyncio
async def test_check_all_limits_concurrency_and_leaves_sources_disabled(tmp_path,monkeypatch):
    bot,message,panel,context,update=fake_ui(tmp_path);service=Discovery(bot.store)
    for number in range(8):
        url=f'https://{number}.test/manifest.json'
        service.register(url,{'name':str(number),'catalogs':[]})
        service.toggle(url)
    active=peak=0
    async def check(self,url,refresh=False):
        nonlocal active,peak
        active+=1;peak=max(peak,active)
        try:
            await asyncio.sleep(.01)
            set_status(self.store,url,'manifest','ok')
        finally: active-=1
    monkeypatch.setattr(Discovery,'check_manifest',check)
    await bot.addons(update,context)
    rows=message.reply_text.await_args.kwargs['reply_markup'].inline_keyboard
    data=next(b.callback_data for row in rows for b in row if b.text=='Check manifests on this page')
    update.callback_query=SimpleNamespace(data=data,message=message)
    await bot.addon_callback(update,context)
    await asyncio.gather(*bot.discovery_tasks)
    assert peak==4 and all(not service.enabled(url) for url,_,_ in service.manifests(active=False))
    assert all(get_status(bot.store,url,'manifest')=='ok' for url,_,_ in service.manifests(active=False))
