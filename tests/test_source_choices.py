import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from satoubot.app import Bot
from satoubot.store import Store
from satoubot.transfer import MAX_BYTES

URL = 'https://source.test/file.mkv?token=synthetic'


@pytest.fixture
def bot(tmp_path):
    return Bot(Store(tmp_path / 'db'), tmp_path, 42, MagicMock())


@pytest.mark.asyncio
@pytest.mark.parametrize('result', [
    {'status':'unavailable', 'reason':'Unsafe or invalid source URL'},
    {'status':'temporary', 'reason':'Timed out', 'retry_after':'15'},
    {'status':'manual', 'reason':'Provider page'},
    {'status':'ready', 'size':None},
    ValueError('synthetic error'),
])
async def test_catalogue_failure_returns_original_link(bot, monkeypatch, result):
    probe = AsyncMock(side_effect=result) if isinstance(result, Exception) else AsyncMock(return_value=result)
    monkeypatch.setattr('satoubot.catalogue.probe', probe)
    context = SimpleNamespace(bot=SimpleNamespace(send_message=AsyncMock()))
    await bot.prepare_choice(context, {'url':URL, 'movie':{'name':'Example'}})
    message = context.bot.send_message.await_args
    assert URL in message.args[1]
    assert message.kwargs['disable_web_page_preview'] is True
    assert bot.store.db.execute('SELECT count(*) FROM jobs').fetchone()[0] == 0


@pytest.mark.asyncio
async def test_pasted_unavailable_source_offers_original_link(bot, monkeypatch):
    monkeypatch.setattr('satoubot.download.probe', AsyncMock(return_value={'status':'unavailable', 'reason':'Unsafe or invalid source URL'}))
    bot.show_panel = AsyncMock()
    update = MagicMock(); update.effective_user.id = 42; update.effective_chat.type = 'private'
    await bot.handle_source(update, URL)
    panel = bot.show_panel.await_args
    assert 'Give link' in panel.args[1]
    button = panel.kwargs['reply_markup'].inline_keyboard[0][0]
    assert button.text == 'Give link'
    update.callback_query.data = button.callback_data
    update.callback_query.answer = AsyncMock()
    await bot.callback(update, MagicMock())
    assert bot.show_panel.await_args.args[1] == URL
    assert bot.show_panel.await_args.kwargs['disable_preview'] is True
    assert bot.store.db.execute('SELECT count(*) FROM jobs').fetchone()[0] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize('size,expected', [
    (MAX_BYTES, ['Transfer', 'Download only', 'Give link', 'No']),
    (MAX_BYTES + 1, ['Download only', 'Give link', 'No']),
])
@pytest.mark.parametrize('source', ['direct', 'catalogue'])
async def test_confirm_choices_follow_verified_file_size(bot, monkeypatch, size, expected, source):
    info = {'status':'ready', 'size':size, 'filename':'file.mkv', 'content_type':'video/x-matroska'}
    monkeypatch.setattr(f'satoubot.{"download" if source == "direct" else "catalogue"}.probe', AsyncMock(return_value=info))
    monkeypatch.setattr(f'satoubot.{"download" if source == "direct" else "catalogue"}.inspect_remote_media', AsyncMock(side_effect=ValueError()))
    bot.show_panel = AsyncMock()
    if source == 'direct':
        await bot.handle_source(SimpleNamespace(message=MagicMock()), URL)
    else:
        context = SimpleNamespace(bot=SimpleNamespace(send_message=AsyncMock()))
        await bot.prepare_choice(context, {'url':URL, 'movie':{'id':'tt1', 'name':'Example'}})
    buttons = bot.show_panel.await_args.kwargs['reply_markup'].inline_keyboard[0]
    assert [button.text for button in buttons] == expected
    assert any(button.callback_data.startswith('dllocal:' if source == 'direct' else 'yeslocal:') for button in buttons)
    assert not bot.tasks


@pytest.mark.asyncio
async def test_failed_download_includes_original_link(bot, monkeypatch):
    job = bot.store.create(URL, 'Example', 'file.mkv', 100)
    bot.store.update(job, state='downloading')
    monkeypatch.setattr('satoubot.download.probe', AsyncMock(return_value={'status':'unavailable', 'reason':'Source is unavailable'}))
    download = AsyncMock(); monkeypatch.setattr('satoubot.download.download', download)
    context = SimpleNamespace(bot=SimpleNamespace(send_message=AsyncMock(return_value=MagicMock(edit_text=AsyncMock()))))
    await bot.perform_download(context, job)
    message = context.bot.send_message.await_args
    assert URL in message.args[1] and message.kwargs['disable_web_page_preview'] is True
    assert bot.store.get(job)['state'] == 'failed'
    download.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('source', ['direct', 'catalogue'])
async def test_large_local_button_saves_without_upload(bot, monkeypatch, source):
    size = MAX_BYTES + 1
    info = {'status':'ready', 'size':size, 'filename':'file.mkv', 'content_type':'video/x-matroska'}
    monkeypatch.setattr('satoubot.download.probe', AsyncMock(return_value=info))
    monkeypatch.setattr('satoubot.catalogue.probe', AsyncMock(return_value=info))
    monkeypatch.setattr('satoubot.download.inspect_remote_media', AsyncMock(side_effect=ValueError()))
    monkeypatch.setattr('satoubot.catalogue.inspect_remote_media', AsyncMock(side_effect=ValueError()))
    monkeypatch.setattr('satoubot.preferences.inspect_media', AsyncMock(return_value={'streams':[]}))
    async def download(url, path, expected_size, progress, **kwargs):
        assert url == URL and expected_size == size
        with path.open('wb') as output: output.truncate(size)
    monkeypatch.setattr('satoubot.download.download', download)
    bot.preview = AsyncMock()
    bot.show_panel = AsyncMock()
    context = SimpleNamespace(bot=SimpleNamespace(send_message=AsyncMock(return_value=MagicMock(edit_text=AsyncMock()))))
    update = MagicMock(); update.effective_user.id = 42; update.effective_chat.type = 'private'
    if source == 'direct':
        await bot.handle_source(update, URL)
    else:
        await bot.prepare_choice(context, {'url':URL, 'movie':{'id':'tt1', 'name':'Example'}})
    buttons = bot.show_panel.await_args.kwargs['reply_markup'].inline_keyboard[0]
    local_button = next(button for button in buttons if button.text == 'Download only')
    update.callback_query.data = local_button.callback_data
    update.callback_query.answer = AsyncMock()
    await bot.callback(update, context)
    await asyncio.gather(*bot.tasks)
    job = bot.store.find_source(URL)
    assert job['state'] == 'local_only' and job['size'] == size
    assert Path(job['path']).is_file()
    bot.preview.assert_not_awaited()
