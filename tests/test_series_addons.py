from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
import pytest
from satoubot.discovery import Discovery, run_plugin
from satoubot.store import Store
from satoubot.series import ordered_episodes
from satoubot.file_info import clean_title
from satoubot.app import Bot


@pytest.mark.asyncio
async def test_toggles_persist_exclude_search_and_do_not_bootstrap(tmp_path, monkeypatch):
    path = tmp_path/'db'
    service = Discovery(Store(path))
    url = 'https://catalog.test/manifest.json'
    service.register(url, {'name':'Catalogue', 'catalogs':[{'id':'tv','type':'series','extra':[{'name':'search'}]}], 'resources':['stream']})
    service.toggle(url)
    service = Discovery(Store(path))
    fetch = AsyncMock()
    monkeypatch.setattr('satoubot.discovery.fetch_json', fetch)
    assert not service.enabled(url) and not service.manifests()
    assert len(service.manifests(active=False)) == 1
    assert await service.search('Example') == ([], [])
    assert await service.streams({'id':'tt1', 'type':'series', 'season':1, 'episode':2}) == ([], [])
    fetch.assert_not_awaited()
    service.toggle(url)
    assert service.enabled(url)


@pytest.mark.asyncio
async def test_series_catalog_metadata_and_episode_stream_endpoints(tmp_path, monkeypatch):
    service = Discovery(Store(tmp_path/'db'))
    url = 'https://catalog.test/manifest.json'
    service.register(url, {'name':'Series', 'types':['series'], 'resources':['stream', 'meta'], 'catalogs':[{'type':'series','id':'anime','extra':[{'name':'search'}]}]})
    service.store.set_setting('source_selection_explicit', '1')
    calls = []
    async def fetch(url):
        calls.append(url)
        if '/catalog/' in url: return {'metas':[{'id':'tt1','name':'Example'}]}
        if '/meta/' in url: return {'meta':{'videos':[{'id':'tt1:2:10','season':2,'episode':10}]}}
        return {'streams':[{'url':'https://file.test/s02e10.mkv'}]}
    monkeypatch.setattr('satoubot.discovery.fetch_json', fetch)
    movies, errors = await service.search('Example')
    assert movies[0]['type'] == 'series' and not errors
    movie = await service.details(movies[0])
    movie.update(season=2, episode=10, video_id='tt1:2:10')
    streams, errors = await service.streams(movie)
    assert len(streams) == 1 and not errors
    assert calls == ['https://catalog.test/catalog/series/anime/search=Example.json', 'https://catalog.test/meta/series/tt1.json', 'https://catalog.test/stream/series/tt1%3A2%3A10.json']


@pytest.mark.asyncio
async def test_provider_receives_tv_numbers_and_disabled_children_are_skipped(tmp_path, monkeypatch):
    service = Discovery(Store(tmp_path/'db'))
    url = 'https://repo.test/manifest.json'
    service.register(url, {'scrapers':[{'name':str(i),'filename':f'{i}.js','supportedTypes':['tv']} for i in range(3)]})
    service.toggle(url, '1.js')
    monkeypatch.setattr('satoubot.discovery.fetch_bytes', AsyncMock(return_value=b'code'))
    run = AsyncMock(return_value=[{'url':'https://file.test/episode.mkv'}])
    monkeypatch.setattr('satoubot.discovery.run_plugin', run)
    streams, errors = await service.streams({'id':'tt1','type':'series','moviedb_id':123,'season':2,'episode':10})
    assert run.await_count == 2 and not errors
    assert all(call.args == ('code', '123', 'tv', 2, 10) for call in run.await_args_list)
    assert len(streams) == 1


def test_numeric_episode_order_specials_invalid_metadata_and_filename():
    movie = {'name':'Example','year':'2024','videos':[
        {'season':2,'episode':10},{'season':1,'episode':10},{'season':1,'episode':2},
        {'season':0,'episode':1},{'season':1,'episode':2},{'season':'bad','episode':1}, {'season':True,'episode':1}]}
    assert [(v['season'],v['episode']) for v in ordered_episodes(movie)] == [(0,1),(1,2),(1,10),(2,10)]
    assert 'S02E10' in clean_title(dict(movie, season=2, episode=10), {'filename':'Example.mkv'})


@pytest.mark.asyncio
async def test_season_picker_uses_episode_identity_and_numeric_order(tmp_path):
    bot = Bot(Store(tmp_path/'db'),tmp_path,42,MagicMock())
    context = SimpleNamespace(bot=SimpleNamespace(send_message=AsyncMock()))
    movie = {'id':'tt1','type':'series','name':'Example','videos':[{'id':'special-10','season':1,'episode':10},{'id':'special-2','season':1,'episode':2}]}
    await bot.series_menu(context, movie, 1)
    rows = context.bot.send_message.await_args.kwargs['reply_markup'].inline_keyboard
    assert rows[0][0].text.startswith('S01E02') and rows[1][0].text.startswith('S01E10')
    selected = bot.store.cached(int(rows[0][0].callback_data.split(':')[1]), 'movie')
    assert selected['id'] == 'tt1' and selected['video_id'] == 'special-2' and selected['episode'] == 2


@pytest.mark.asyncio
async def test_runtime_passes_tv_arguments_in_isolated_worker():
    code = 'module.exports.getStreams=async(id,type,season,episode)=>[{url:[id,type,season,episode].join("/")}];'
    assert await run_plugin(code, '123', 'tv', 2, 10) == [{'url':'123/tv/2/10'}]


@pytest.mark.asyncio
async def test_addon_list_toggles_then_renders_and_has_explicit_import_button(tmp_path):
    bot = Bot(Store(tmp_path/'db'),tmp_path,42,MagicMock())
    service = Discovery(bot.store)
    service.register('https://repo.test/manifest.json', {'name':'Repository','scrapers':[{'name':'TV source','filename':'tv.js','supportedTypes':['tv']}]})
    message = MagicMock(reply_text=AsyncMock(return_value=MagicMock(edit_text=AsyncMock())))
    update = SimpleNamespace(message=message,effective_user=SimpleNamespace(id=42),effective_chat=SimpleNamespace(type='private'))
    context = SimpleNamespace(user_data={'manifest_pending':True})
    await bot.addons(update, context)
    rows = message.reply_text.await_args.kwargs['reply_markup'].inline_keyboard
    assert 'ON · Repository' in rows[0][0].text
    assert 'manifest_pending' not in context.user_data
    assert any(b.callback_data == 'menu:addmanifest' for row in rows for b in row)
    update.callback_query = SimpleNamespace(message=message, data=rows[0][0].callback_data)
    await bot.addon_callback(update, context)
    assert not service.enabled('https://repo.test/manifest.json')
    assert 'OFF · Repository' in message.reply_text.await_args.kwargs['reply_markup'].inline_keyboard[0][0].text


@pytest.mark.asyncio
async def test_series_dispatch_never_fetches_streams_before_episode_selected(tmp_path, monkeypatch):
    bot = Bot(Store(tmp_path/'db'),tmp_path,42,MagicMock())
    key = bot.store.cache('movie', {'id':'anime:1','type':'anime','name':'Example','addon':'https://catalog.test/manifest.json'})
    monkeypatch.setattr(Discovery, 'details', AsyncMock(return_value={'id':'anime:1','type':'anime','name':'Example','videos':[{'season':1,'episode':1,'id':'anime:1:1'}]}))
    bot.series_menu = AsyncMock()
    bot.movie_card = AsyncMock()
    await bot.safe_movie_card(MagicMock(), key)
    bot.series_menu.assert_awaited_once()
    bot.movie_card.assert_not_awaited()
