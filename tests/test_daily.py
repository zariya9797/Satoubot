import asyncio
import json
from datetime import datetime, timezone, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
import pytest
from satoubot.app import Bot
from satoubot.store import Store
from satoubot.discovery import Discovery


def setup(tmp_path):
    bot=Bot(Store(tmp_path/'db'),tmp_path,42,MagicMock());bot.daily_init()
    context=SimpleNamespace(bot=SimpleNamespace(send_message=AsyncMock(return_value=MagicMock(edit_text=AsyncMock(),delete=AsyncMock()))))
    bot.daily_menu=AsyncMock();bot.safe_movie_card=AsyncMock()
    return bot,context


@pytest.mark.asyncio
async def test_daily_off_no_search_and_explicit_start_selects_one_without_download(tmp_path):
    bot,context=setup(tmp_path)
    bot.recent_releases=AsyncMock(return_value=[{'id':'tt1','name':'New film','type':'movie'}])
    bot.set_transfers(False);await bot.daily_tick(context);bot.recent_releases.assert_not_awaited()
    bot.set_transfers(True);await bot.daily_tick(context)
    await asyncio.gather(*bot.discovery_tasks)
    bot.safe_movie_card.assert_awaited_once()
    assert not bot.tasks
    await bot.daily_tick(context)
    assert bot.recent_releases.await_count==1
    assert bot.store.db.execute("SELECT state FROM daily_titles").fetchone()[0]=='offered'


@pytest.mark.asyncio
async def test_quota_persists_and_stops_without_more_offers(tmp_path):
    bot,context=setup(tmp_path)
    for n in range(4):
        bot.store.db.execute("INSERT INTO daily_titles VALUES (?,?,'published',NULL,?)",(str(n),'{}',bot.daily_day()))
    bot.store.db.commit();bot.set_transfers(True)
    bot=Bot(Store(tmp_path/'db'),tmp_path,42,MagicMock())
    bot.recent_releases=AsyncMock()
    await bot.daily_tick(context)
    assert not bot.transfers_enabled() and bot.daily_count()==4
    bot.recent_releases.assert_not_awaited()
    assert 'quota' in context.bot.send_message.await_args.args[1]


@pytest.mark.asyncio
async def test_skip_is_not_offered_again_and_cancelled_job_needs_attention(tmp_path):
    bot,context=setup(tmp_path);bot.set_transfers(True)
    movie={'id':'tt1','type':'movie','name':'Film'}
    bot.store.db.execute("INSERT INTO daily_titles VALUES ('movie:tt1',?,'skipped',NULL,NULL)",(json.dumps(movie),))
    job=bot.store.create('https://file.test','Film','film.mkv',100);bot.store.update(job,state='upload_unknown')
    bot.store.db.execute("INSERT INTO daily_titles VALUES ('other','{}','transferring',?,NULL)",(job,));bot.store.db.commit()
    bot.recent_releases=AsyncMock(return_value=[movie])
    await bot.daily_tick(context)
    bot.safe_movie_card.assert_not_awaited()
    assert bot.store.db.execute("SELECT state FROM daily_titles WHERE identity='other'").fetchone()[0]=='needs_attention'


@pytest.mark.asyncio
async def test_recent_releases_filter_future_old_and_required_catalogues(tmp_path,monkeypatch):
    bot,context=setup(tmp_path)
    d=Discovery(bot.store);d.register('https://catalog.test/manifest.json',{'catalogs':[{'type':'movie','id':'new'},{'type':'movie','id':'search','extra':[{'name':'search','isRequired':True}]}]})
    today=datetime.now(timezone.utc).date()
    fetch=AsyncMock(return_value={'metas':[{'id':str(n),'name':str(n),'released':date.isoformat()} for n,date in enumerate([today-timedelta(days=1),today+timedelta(days=1),today-timedelta(days=200)])]})
    monkeypatch.setattr('satoubot.discovery.fetch_json',fetch)
    results=await bot.recent_releases()
    assert [m['id'] for m in results]==['0']
    fetch.assert_awaited_once()


@pytest.mark.asyncio
async def test_transfer_and_link_modes_keep_confirmation_and_off_gate(tmp_path,monkeypatch):
    bot,context=setup(tmp_path);bot.set_transfers(False)
    stream={'url':'https://file.test/film.mkv','movie':{'id':'tt1','name':'Film'},'file_type':'mkv'}
    monkeypatch.setattr('satoubot.catalogue.probe',AsyncMock(return_value={'status':'ready','size':100,'filename':'film.mkv','content_type':'video/x-matroska'}))
    await bot.prepare_choice(context,stream)
    buttons=context.bot.send_message.await_args.kwargs['reply_markup'].inline_keyboard[0]
    assert [b.text for b in buttons]==['Transfer','Download only','Give link','No']
    q=SimpleNamespace(data=buttons[2].callback_data,message=MagicMock(reply_text=AsyncMock()))
    await bot.catalogue_callback(SimpleNamespace(callback_query=q),context)
    assert q.message.reply_text.await_args.args[0]==stream['url'] and not bot.tasks
    assert q.message.reply_text.await_args.kwargs['disable_web_page_preview'] is True
    q.data=buttons[0].callback_data
    await bot.catalogue_callback(SimpleNamespace(callback_query=q),context)
    assert not bot.tasks and 'off' in q.message.reply_text.await_args.args[0]


@pytest.mark.asyncio
async def test_daily_cards_only_offer_verified_files_strictly_below_two_gb(tmp_path,monkeypatch):
    bot,context=setup(tmp_path)
    movie={'id':'tt1','name':'New film','type':'movie','daily_identity':'movie:tt1'}
    key=bot.store.cache('movie',movie)
    monkeypatch.setattr(Discovery,'details',AsyncMock(return_value=movie))
    monkeypatch.setattr(Discovery,'streams',AsyncMock(return_value=([{'url':f'https://file.test/{n}'} for n in range(4)],[])))
    async def probe(url,headers):
        n=int(url.rsplit('/',1)[1])
        return {'status':'ready' if n!=3 else 'manual','size':[100,2_000_000_000,None,100][n],'filename':'film.mkv','content_type':'video/x-matroska'}
    monkeypatch.setattr('satoubot.catalogue.probe',probe)
    await bot.movie_card(context,key)
    row=bot.store.db.execute("SELECT id FROM discovery_cache WHERE kind='pages'").fetchone()
    ids=bot.store.cached(row[0],'pages')
    assert len(ids)==1 and bot.store.cached(ids[0],'stream')['size']==100


@pytest.mark.asyncio
async def test_daily_quantity_bounds_and_owner_only(tmp_path):
    bot,context=setup(tmp_path)
    q=SimpleNamespace(data='daily:quantity:10',message=MagicMock(reply_text=AsyncMock()))
    owner=SimpleNamespace(effective_user=SimpleNamespace(id=42),callback_query=q)
    await bot.daily_callback(owner,context)
    assert bot.store.setting('daily_quantity')=='10'
    q.data='daily:quantity:11';await bot.daily_callback(owner,context)
    assert bot.store.setting('daily_quantity')=='10'
    bot.show_panel=AsyncMock();q.data='daily:quantity:4'
    await bot.daily_callback(SimpleNamespace(effective_user=SimpleNamespace(id=99),callback_query=q),context)
    assert bot.store.setting('daily_quantity')=='10'


@pytest.mark.asyncio
async def test_switching_off_during_link_recheck_does_not_create_download(tmp_path,monkeypatch):
    bot,context=setup(tmp_path);bot.set_transfers(True)
    async def probe(*args):
        bot.set_transfers(False)
        return {'status':'ready','size':100,'filename':'film.mkv','content_type':'video/x-matroska'}
    monkeypatch.setattr('satoubot.catalogue.probe',probe)
    bot.perform_download=AsyncMock()
    await bot.download_choice(context,{'url':'https://file.test/new','file_type':'mkv','movie':{'id':'tt1','name':'Film'}})
    bot.perform_download.assert_not_awaited()
    assert bot.store.db.execute('SELECT count(*) FROM jobs').fetchone()[0]==0


@pytest.mark.asyncio
async def test_title_year_duplicates_across_catalogues_and_unicode_titles(tmp_path):
    from satoubot.daily import daily_title_alias
    bot,context=setup(tmp_path);bot.set_transfers(True)
    first={'id':'tt1','type':'movie','name':'最新电影','year':'2026'}
    bot.store.set_setting(daily_title_alias(first),'movie:tt1')
    duplicate=dict(first,id='other-id')
    next_movie=dict(first,id='tt2',name='另一部电影')
    assert daily_title_alias(first)!=daily_title_alias(next_movie)
    bot.recent_releases=AsyncMock(return_value=[duplicate,next_movie])
    await bot.daily_tick(context);await asyncio.gather(*bot.discovery_tasks)
    offered=bot.store.db.execute("SELECT identity FROM daily_titles WHERE state='offered'").fetchone()
    assert offered[0]=='movie:tt2'


@pytest.mark.asyncio
async def test_only_successful_daily_uploads_count_and_next_release_follows(tmp_path):
    bot,context=setup(tmp_path);bot.set_transfers(True)
    job=bot.store.create('https://file.test/1','Film','film.mkv',100)
    bot.store.update(job,state='uploading',target='-100123',path=str(tmp_path/'film.mkv'))
    bot.store.db.execute("INSERT INTO daily_titles VALUES ('movie:tt1','{}','transferring',?,NULL)",(job,));bot.store.db.commit()
    bot.uploader.send_file=AsyncMock(return_value=SimpleNamespace(id=9))
    assert bot.daily_count()==0
    await bot.perform_upload(context,job)
    assert bot.daily_count()==1
    # Manual uploads do not consume a daily slot.
    manual=bot.store.create('https://file.test/2','Manual','manual.mkv',100)
    bot.store.update(manual,state='uploading',target='-100123',path=str(tmp_path/'manual.mkv'))
    await bot.perform_upload(context,manual)
    assert bot.daily_count()==1
    bot.recent_releases=AsyncMock(return_value=[{'id':'tt2','type':'movie','name':'Next film'}])
    await bot.daily_tick(context);await asyncio.gather(*bot.discovery_tasks)
    assert bot.store.db.execute("SELECT identity FROM daily_titles WHERE state='offered'").fetchone()[0]=='movie:tt2'


@pytest.mark.asyncio
async def test_temporary_direct_source_still_offers_manual_link(tmp_path,monkeypatch):
    bot,context=setup(tmp_path)
    monkeypatch.setattr('satoubot.download.probe',AsyncMock(return_value={'status':'temporary','reason':'Rate limited','retry_after':60}))
    update=SimpleNamespace(message=SimpleNamespace(reply_text=AsyncMock()))
    await bot.handle_source(update,'https://file.test/film.mkv')
    buttons=update.message.reply_text.await_args.kwargs['reply_markup'].inline_keyboard[0]
    assert [b.text for b in buttons]==['Retry check','Give link']
    assert bot.store.cached(int(buttons[1].callback_data.split(':')[1]),'manual_link')['url']=='https://file.test/film.mkv'
