import time
from unittest.mock import AsyncMock, MagicMock

import pytest
from satoubot.app import Bot
from satoubot.discovery import Discovery
from satoubot.file_info import probe
from satoubot.source_history import record_check, reliability
from satoubot.store import Store


def setup(tmp_path):
    bot = Bot(Store(tmp_path/'db'), tmp_path, 42, MagicMock())
    bot.last_user_activity -= 400
    bot.last_idle_check -= 1000
    manifest = 'https://provider.test/manifest.json'
    Discovery(bot.store).register(manifest, {'name':'Source', 'catalogs':[]})
    return bot, {'url':'https://media.test/movie.mkv?token=secret', 'source_manifest':manifest, 'headers':{}}


@pytest.mark.asyncio
async def test_idle_check_runs_only_for_enabled_saved_source_and_obeys_cooldown(tmp_path, monkeypatch):
    bot, stream = setup(tmp_path)
    bot.store.cache('stream',stream.copy())
    check=AsyncMock(return_value={'status':'ready', 'size':123})
    monkeypatch.setattr('satoubot.source_history.probe',check)
    await bot.idle_source_check()
    check.assert_awaited_once()
    bot.last_idle_check -= 1000
    await bot.idle_source_check()
    check.assert_awaited_once()
    row=bot.store.db.execute('SELECT * FROM source_history').fetchone()
    assert row['host']=='media.test' and row['ready']==1
    assert 'secret' not in str(dict(row))
    bot.store.db.execute('DELETE FROM source_rechecks');bot.store.db.commit()
    Discovery(bot.store).toggle(stream['source_manifest'])
    bot.last_idle_check -= 1000
    await bot.idle_source_check()
    check.assert_awaited_once()


@pytest.mark.asyncio
async def test_idle_checks_wait_for_activity_and_can_be_disabled(tmp_path, monkeypatch):
    bot, stream=setup(tmp_path);bot.store.cache('stream',stream.copy())
    check=AsyncMock();monkeypatch.setattr('satoubot.source_history.probe',check)
    bot.last_user_activity=time.monotonic();await bot.idle_source_check()
    bot.last_user_activity-=400;bot.store.set_setting('idle_source_checks','0');await bot.idle_source_check()
    check.assert_not_awaited()


def test_reliability_uses_checks_and_resets_stale_data(tmp_path):
    bot,stream=setup(tmp_path)
    for _ in range(3): record_check(bot.store,stream,{'status':'ready','size':100})
    assert reliability(bot.store,stream)==1
    record_check(bot.store,stream,{'status':'manual'})
    assert reliability(bot.store,stream)==.75
    bot.store.db.execute('UPDATE source_history SET updated=0');bot.store.db.commit()
    assert reliability(bot.store,stream)==0


def test_recheck_respects_long_retry_after_across_restart(tmp_path):
    bot,stream=setup(tmp_path)
    record_check(bot.store,stream,{'status':'temporary','retry_after':'172800'})
    reopened=Store(tmp_path/'db')
    assert reopened.db.execute('SELECT next_check FROM source_rechecks').fetchone()[0] > time.time()+170000


@pytest.mark.asyncio
async def test_retry_after_applies_to_other_links_on_same_host(tmp_path,monkeypatch):
    bot,stream=setup(tmp_path)
    record_check(bot.store,stream,{'status':'temporary','retry_after':'172800'})
    another=dict(stream,url='https://media.test/another.mkv')
    bot.store.cache('stream',another)
    check=AsyncMock();monkeypatch.setattr('satoubot.source_history.probe',check)
    await bot.idle_source_check()
    check.assert_not_awaited()


@pytest.mark.asyncio
async def test_user_activity_cancels_idle_probe(tmp_path,monkeypatch):
    import asyncio
    from types import SimpleNamespace
    bot,stream=setup(tmp_path);bot.store.cache('stream',stream)
    started=asyncio.Event()
    async def check(*args):
        started.set()
        await asyncio.Event().wait()
    monkeypatch.setattr('satoubot.source_history.probe',check)
    bot.idle_check_task=asyncio.create_task(bot.idle_source_check())
    await started.wait()
    bot.touch_panel(SimpleNamespace(user_data={}))
    with pytest.raises(asyncio.CancelledError): await bot.idle_check_task
    assert bot.store.db.execute('SELECT count(*) FROM source_history').fetchone()[0]==0


def test_ready_links_use_history_but_still_come_before_manual_links(tmp_path):
    bot, stream=setup(tmp_path)
    good=dict(stream,url='https://good.test/a',check_status='ready',size=100,file_type='mkv',quality='good')
    unknown=dict(stream,url='https://unknown.test/a',check_status='ready',size=100,file_type='mkv',quality='unknown')
    manual=dict(stream,url='https://good.test/b',check_status='manual',size=None,file_type='mkv',quality='manual')
    for _ in range(3): record_check(bot.store,good,{'status':'ready','size':100})
    keys=[bot.store.cache('stream',item) for item in (unknown,manual,good)]
    buttons=bot.file_page(1,keys,0).inline_keyboard
    assert [row[0].callback_data for row in buttons[:3]] == [f'file:{keys[2]}',f'file:{keys[0]}',f'file:{keys[1]}']


@pytest.mark.asyncio
async def test_idle_checks_follow_explicit_provider_override(tmp_path, monkeypatch):
    bot, stream = setup(tmp_path)
    url=stream['source_manifest']
    service=Discovery(bot.store)
    service.register(url, {'scrapers':[{'filename':'source.js','enabled':False}]})
    stream['source_provider']='source.js'
    bot.store.cache('stream',stream)
    check=AsyncMock(return_value={'status':'ready','size':100})
    monkeypatch.setattr('satoubot.source_history.probe',check)
    await bot.idle_source_check()
    check.assert_not_awaited()
    service.toggle(url,'source.js')
    bot.last_idle_check-=1000
    await bot.idle_source_check()
    check.assert_awaited_once()
