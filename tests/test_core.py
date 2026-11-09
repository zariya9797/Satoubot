from types import SimpleNamespace
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
import pytest

from satoubot.store import Store
from satoubot.transfer import MAX_BYTES, MAX_DOWNLOAD_BYTES, download, safe_name
from satoubot.app import Bot


def test_store_duplicate_and_published_search(tmp_path):
    store = Store(tmp_path / 'db')
    job = store.create('https://test/file', 'Example', 'example.mp4', 23)
    assert store.find_source('https://test/file')['id'] == job
    assert not store.search('Example')
    store.update(job, state='published', telegram_message_id=42)
    assert store.search('Example')[0]['telegram_message_id'] == 42


def test_safe_name_blocks_path_traversal():
    assert '/' not in safe_name('../../test')
    assert '\\' not in safe_name('..\\test')


@pytest.mark.asyncio
async def test_reject_oversize_without_request(tmp_path):
    with pytest.raises(ValueError):
        await download('https://example.org/file', tmp_path / 'file', MAX_DOWNLOAD_BYTES + 1, AsyncMock())
    assert not list(tmp_path.iterdir())


@pytest.mark.asyncio
async def test_stale_approval_never_uploads(tmp_path):
    store = Store(tmp_path / 'db')
    job = store.create('https://test/file', 'Example', 'example.mp4', 23)
    store.set_setting('target', '-100123')
    store.update(job, state='review', target='-100123', approval_token='newtoken')
    bot = Bot(store, tmp_path, 42, MagicMock())
    q = MagicMock(); q.data = f'upload:{job}:oldtoken'; q.answer = AsyncMock(); q.message.reply_text = AsyncMock()
    update = MagicMock(); update.effective_user.id = 42; update.effective_chat.type = 'private'; update.callback_query = q
    await bot.callback(update, MagicMock())
    assert store.get(job)['state'] == 'review'
    assert not bot.tasks
    q.message.reply_text.assert_awaited_once()


@pytest.mark.asyncio
async def test_unauthorized_approval_is_ignored(tmp_path):
    store = Store(tmp_path / 'db')
    job = store.create('https://test/file', 'Example', 'example.mp4', 23)
    store.update(job, state='review', approval_token='token', target='-100123')
    bot = Bot(store, tmp_path, 42, MagicMock())
    update = MagicMock(); update.effective_user.id = 43; update.effective_chat.type = 'private'
    update.callback_query.data = f'upload:{job}:token'
    await bot.callback(update, MagicMock())
    assert store.get(job)['state'] == 'review'
    assert not bot.tasks


@pytest.mark.asyncio
async def test_parallel_download_and_range_validation(tmp_path, monkeypatch):
    monkeypatch.setattr('satoubot.public_transport.public_address', AsyncMock(return_value='127.0.0.1'))
    import asyncio
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    data = bytes(range(256)) * 16385
    requests = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_HEAD(self):
            self.send_response(200)
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Accept-Ranges', 'bytes')
            self.send_header('ETag', '"test-version"')
            self.end_headers()
        def do_GET(self):
            start, end = map(int, self.headers['Range'].removeprefix('bytes=').split('-'))
            requests.append((start, end, self.headers.get('If-Range')))
            self.send_response(206)
            self.send_header('Content-Range', f'bytes {start}-{end}/{len(data)}')
            self.send_header('Content-Length', str(end-start+1))
            self.end_headers()
            self.wfile.write(data[start:end+1])
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    try:
        path = tmp_path/'out'
        size = await download(f'http://127.0.0.1:{server.server_port}/file', path, len(data), AsyncMock())
        assert size == len(data)
        assert path.read_bytes() == data
        assert len(requests) == 4
        assert all(validator == '"test-version"' for _, _, validator in requests)
    finally:
        await asyncio.to_thread(server.shutdown)
        server.server_close()
        thread.join()


@pytest.mark.asyncio
async def test_approved_upload_uses_selected_target_and_marks_published(tmp_path):
    store = Store(tmp_path / 'db')
    job = store.create('https://test/file', 'Example', 'example.mp4', 23)
    file = tmp_path/'example.mp4'; file.write_bytes(b'example')
    store.set_setting('target', '-100123')
    store.update(job, state='review', target='-100123', approval_token='correct', path=str(file))
    uploader = MagicMock(); uploader.send_file = AsyncMock(return_value=MagicMock(id=99))
    bot = Bot(store, tmp_path, 42, uploader)
    context = MagicMock(); context.bot.get_chat_member = AsyncMock(return_value=SimpleNamespace(status='administrator',can_post_messages=True)); context.bot.send_message = AsyncMock(return_value=MagicMock(edit_text=AsyncMock()))
    q = MagicMock(); q.data = f'upload:{job}:correct'; q.answer = AsyncMock(); q.edit_message_reply_markup = AsyncMock()
    update = MagicMock(); update.effective_user.id = 42; update.effective_chat.type = 'private'; update.callback_query = q
    await bot.callback(update, context)
    import asyncio
    await asyncio.gather(*bot.tasks)
    assert store.get(job)['state'] == 'published'
    assert store.get(job)['telegram_message_id'] == 99
    assert uploader.send_file.await_args.args[:2] == (-100123, str(file))


@pytest.mark.asyncio
async def test_upload_failure_is_not_automatically_retried(tmp_path):
    store = Store(tmp_path / 'db')
    job = store.create('https://test/file', 'Example', 'example.mp4', 23)
    store.update(job, state='uploading', target='-100123', path=str(tmp_path/'file'))
    uploader = MagicMock(); uploader.send_file = AsyncMock(side_effect=ConnectionError())
    bot = Bot(store, tmp_path, 42, uploader)
    context = MagicMock(); context.bot.get_chat_member = AsyncMock(return_value=SimpleNamespace(status='administrator',can_post_messages=True)); context.bot.send_message = AsyncMock(return_value=MagicMock(edit_text=AsyncMock()))
    await bot.perform_upload(context, job)
    assert store.get(job)['state'] == 'upload_unknown'
    uploader.send_file.assert_awaited_once()


@pytest.mark.asyncio
async def test_thumbnail_preview_requires_new_approval_and_is_uploaded(tmp_path):
    from PIL import Image
    import asyncio

    store = Store(tmp_path / 'db')
    job = store.create('https://test/file', 'Example', 'example.mp4', 23)
    file = tmp_path / 'example.mp4'; file.write_bytes(b'example')
    store.set_setting('target', '-100123')
    store.update(job, state='review', target='-100123', approval_token='old', path=str(file))
    uploader = MagicMock(); uploader.send_file = AsyncMock(return_value=MagicMock(id=99))
    bot = Bot(store, tmp_path, 42, uploader)
    context = MagicMock(); context.bot.get_chat_member = AsyncMock(return_value=SimpleNamespace(status='administrator',can_post_messages=True)); context.user_data = {}
    context.bot.send_photo = AsyncMock(); context.bot.send_message = AsyncMock(return_value=MagicMock(edit_text=AsyncMock()))
    q = MagicMock(); q.data = f'thumb:{job}:old'; q.answer = AsyncMock(); q.message.reply_text = AsyncMock()
    update = MagicMock(); update.effective_user.id = 42; update.effective_chat.type = 'private'; update.callback_query = q
    await bot.callback(update, context)

    async def save_photo(custom_path):
        Image.new('RGB', (800, 600), 'red').save(custom_path, 'JPEG')
    remote = MagicMock(); remote.download_to_drive = AsyncMock(side_effect=save_photo)
    photo = MagicMock(); photo.file_size = 1000; photo.get_file = AsyncMock(return_value=remote)
    update.message.photo = [photo]; update.message.reply_text = AsyncMock()
    await bot.thumbnail_photo(update, context)
    row = store.get(job)
    assert row['approval_token'] != 'old'
    with Image.open(row['thumbnail']) as thumb:
        assert thumb.format == 'JPEG' and max(thumb.size) <= 320
    assert Path(row['thumbnail']).stat().st_size < 20_000
    context.bot.send_photo.assert_awaited_once()

    q.data = f'upload:{job}:old'
    await bot.callback(update, context)
    assert store.get(job)['state'] == 'review' and not bot.tasks
    q.data = f'upload:{job}:{row["approval_token"]}'; q.edit_message_reply_markup = AsyncMock()
    await bot.callback(update, context)
    await asyncio.gather(*bot.tasks)
    assert store.get(job)['state'] == 'published'
    assert uploader.send_file.await_args.kwargs['thumb'] == row['thumbnail']


@pytest.mark.asyncio
async def test_thumbnail_does_not_change_job_if_approval_happens_during_download(tmp_path):
    from PIL import Image

    store = Store(tmp_path / 'db')
    job = store.create('https://test/file', 'Example', 'example.mp4', 23)
    store.update(job, state='review', approval_token='old')
    bot = Bot(store, tmp_path, 42, MagicMock())
    context = MagicMock(); context.bot.get_chat_member = AsyncMock(return_value=SimpleNamespace(status='administrator',can_post_messages=True)); context.user_data = {'thumbnail_request': (job, 'old')}
    async def save_photo(custom_path):
        Image.new('RGB', (100, 100)).save(custom_path, 'JPEG')
        store.update(job, state='uploading', approval_token='')
    remote = MagicMock(); remote.download_to_drive = AsyncMock(side_effect=save_photo)
    photo = MagicMock(); photo.file_size = 1000; photo.get_file = AsyncMock(return_value=remote)
    update = MagicMock(); update.effective_user.id = 42; update.effective_chat.type = 'private'
    update.message.photo = [photo]; update.message.reply_text = AsyncMock()
    await bot.thumbnail_photo(update, context)
    assert store.get(job)['thumbnail'] is None
    assert list((tmp_path / str(job)).iterdir()) == []
    update.message.reply_text.assert_awaited_once()


@pytest.mark.asyncio
async def test_transfer_switch_blocks_callbacks_and_survives_restart(tmp_path):
    store = Store(tmp_path / 'db')
    job = store.create('https://test/file', 'Example', 'example.mp4', 23)
    store.set_setting('target', '-100123')
    store.update(job, state='review', target='-100123', approval_token='token')
    bot = Bot(store, tmp_path, 42, MagicMock())
    update = MagicMock(); update.effective_user.id = 42; update.effective_chat.type = 'private'
    update.message.text = 'Stop transfers'; update.message.reply_text = AsyncMock()
    await bot.text(update, MagicMock())
    assert not Bot(Store(tmp_path / 'db'), tmp_path, 42, MagicMock()).transfers_enabled()
    q = MagicMock(); q.data = f'upload:{job}:token'; q.answer = AsyncMock(); q.message.reply_text = AsyncMock()
    update.callback_query = q
    await bot.callback(update, MagicMock())
    assert store.get(job)['state'] == 'review'
    assert store.get(job)['approval_token'] == 'token'
    assert not bot.tasks
    assert 'off' in q.message.reply_text.await_args.args[0]
    update.message.text = 'Library search'
    await bot.text(update, MagicMock())
    assert '/find' in update.message.reply_text.await_args.args[0]
    update.message.text = 'Start transfers'
    await bot.text(update, MagicMock())
    assert bot.transfers_enabled()


@pytest.mark.asyncio
async def test_busy_transfer_asks_to_wait_and_preserves_approval(tmp_path):
    import asyncio
    store = Store(tmp_path / 'db')
    job = store.create('https://test/file', 'Example', 'example.mp4', 23)
    store.set_setting('target', '-100123')
    store.update(job, state='review', target='-100123', approval_token='token')
    bot = Bot(store, tmp_path, 42, MagicMock())
    release = asyncio.Event()
    bot.spawn(release.wait(), kind='upload')
    bot.spawn(release.wait(), kind='upload')
    q = MagicMock(); q.data = f'upload:{job}:token'; q.answer = AsyncMock(); q.message.reply_text = AsyncMock()
    update = MagicMock(); update.effective_user.id = 42; update.effective_chat.type = 'private'; update.callback_query = q
    await bot.callback(update, MagicMock())
    assert 'wait' in q.message.reply_text.await_args.args[0].lower()
    assert store.get(job)['state'] == 'review'
    assert store.get(job)['approval_token'] == 'token'
    update.message.text = 'Stop transfers'; update.message.reply_text = AsyncMock()
    await bot.text(update, MagicMock())
    assert 'finish' in update.message.reply_text.await_args.args[0]
    release.set()
    await asyncio.gather(*bot.tasks)


@pytest.mark.asyncio
async def test_notification_failure_does_not_mark_successful_upload_unknown(tmp_path):
    store = Store(tmp_path / 'db')
    job = store.create('https://test/file', 'Example', 'example.mp4', 23)
    store.update(job, state='uploading', target='-100123', path=str(tmp_path/'file'))
    uploader = MagicMock(); uploader.send_file = AsyncMock(return_value=MagicMock(id=99))
    bot = Bot(store, tmp_path, 42, uploader)
    context = MagicMock(); context.bot.get_chat_member = AsyncMock(return_value=SimpleNamespace(status='administrator',can_post_messages=True)); context.bot.send_message = AsyncMock(side_effect=[MagicMock(), ConnectionError()])
    await bot.perform_upload(context, job)
    assert store.get(job)['state'] == 'published'
    assert store.get(job)['telegram_message_id'] == 99


@pytest.mark.asyncio
async def test_menu_search_works_while_transfers_are_off(tmp_path):
    store = Store(tmp_path / 'db')
    job = store.create('https://test/file', 'Example', 'example.mp4', 23)
    store.update(job, state='published', telegram_message_id=42)
    store.set_setting('transfers_enabled', '0')
    bot = Bot(store, tmp_path, 42, MagicMock())
    context = MagicMock(); context.bot.get_chat_member = AsyncMock(return_value=SimpleNamespace(status='administrator',can_post_messages=True)); context.user_data = {}
    update = MagicMock(); update.effective_user.id = 42; update.effective_chat.type = 'private'
    update.message.reply_text = AsyncMock(); update.message.text = 'Library search'
    await bot.text(update, context)
    update.message.text = 'Example'
    await bot.text(update, context)
    assert 'Example' in update.message.reply_text.await_args.args[0]


@pytest.mark.asyncio
async def test_download_button_respects_transfer_switch(tmp_path):
    store = Store(tmp_path / 'db')
    job = store.create('https://test/file', 'Example', 'example.mp4', 23)
    store.set_setting('transfers_enabled', '0')
    bot = Bot(store, tmp_path, 42, MagicMock())
    q = MagicMock(); q.data = f'dl:{job}'; q.answer = AsyncMock(); q.message.reply_text = AsyncMock()
    update = MagicMock(); update.effective_user.id = 42; update.effective_chat.type = 'private'; update.callback_query = q
    await bot.callback(update, MagicMock())
    assert store.get(job)['state'] == 'ready'
    assert not bot.tasks
    assert 'off' in q.message.reply_text.await_args.args[0]
