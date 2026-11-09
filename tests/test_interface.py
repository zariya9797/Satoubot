import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from telegram import InlineKeyboardButton as Button, InlineKeyboardMarkup as Markup
from telegram.error import BadRequest, Forbidden

from satoubot.app import Bot, admin_session, latest_command
from satoubot.discovery import Discovery
from satoubot.store import Store


def setup_bot(tmp_path):
    bot = Bot(Store(tmp_path/'db'), tmp_path, 42, MagicMock())
    message = MagicMock()
    panel = MagicMock()
    panel.edit_text = AsyncMock()
    panel.reply_text = AsyncMock(return_value=panel)
    panel.delete = AsyncMock()
    panel.edit_reply_markup = AsyncMock()
    message.reply_text = AsyncMock(return_value=panel)
    notice = MagicMock(delete=AsyncMock())
    async def reply(text, **kwargs):
        return notice if text == 'Opening menu…' else panel
    message.reply_text.side_effect = reply
    context = SimpleNamespace(user_data={}, args=[], bot=MagicMock())
    context.bot.send_message = AsyncMock(return_value=panel)
    update = SimpleNamespace(message=message, effective_user=SimpleNamespace(id=42),
                             effective_chat=SimpleNamespace(type='private'), callback_query=None)
    return bot, message, panel, context, update


def callback(update, panel, data):
    update.callback_query = SimpleNamespace(data=data, message=panel, answer=AsyncMock())


@pytest.mark.asyncio
async def test_navigation_replaces_panel_and_back_restores_prompt(tmp_path):
    bot, message, panel, context, update = setup_bot(tmp_path)
    await bot.main_menu(message)
    callback(update, panel, 'menu:addons')
    await admin_session(bot.callback)(update, context)
    assert not context.user_data.get('manifest_pending')
    callback(update, panel, 'menu:addmanifest')
    await admin_session(bot.callback)(update, context)
    assert context.user_data['manifest_pending']
    callback(update, panel, 'menu:library')
    await admin_session(bot.callback)(update, context)
    assert 'manifest_pending' not in context.user_data
    callback(update, panel, 'menu:back')
    await admin_session(bot.callback)(update, context)
    assert context.user_data['manifest_pending']
    assert 'search_pending' not in context.user_data
    assert 'manifest URL' in bot.panel().text
    assert panel.reply_text.await_count == 4


@pytest.mark.asyncio
async def test_pending_check_follows_navigation_and_duplicate_click_does_not_restart(tmp_path):
    bot, message, panel, context, update = setup_bot(tmp_path)
    await bot.main_menu(message)
    started, release = asyncio.Event(), asyncio.Event()
    async def work():
        started.set()
        await release.wait()
    factory = MagicMock(side_effect=work)
    await bot.start_panel_task('file', 'Checking Example.mkv', factory, context)
    await started.wait()
    callback(update, panel, 'menu:library')
    await admin_session(bot.callback)(update, context)
    text = panel.reply_text.await_args.args[0]
    assert 'published library' in text and 'Checking Example.mkv' in text
    await bot.start_panel_task('file', 'Checking Example.mkv', factory, context)
    factory.assert_called_once()
    release.set()
    await asyncio.gather(*bot.discovery_tasks)
    assert 'In progress' not in panel.edit_text.await_args.args[0]
    assert 'published library' in panel.edit_text.await_args.args[0]
    assert panel.reply_text.await_count == 1


@pytest.mark.asyncio
async def test_search_completion_attaches_results_without_replacing_new_menu(tmp_path, monkeypatch):
    bot, message, panel, context, update = setup_bot(tmp_path)
    Discovery(bot.store).register('https://catalog.test/manifest.json', {'name':'test', 'catalogs':[]})
    started, release = asyncio.Event(), asyncio.Event()
    async def search(self, query):
        started.set()
        await release.wait()
        return [{'id':'tt1','name':'Example','year':'2024'}], []
    monkeypatch.setattr(Discovery, 'search', search)
    await bot.begin_search(message, 'Example')
    await started.wait()
    await bot.main_menu(message)
    release.set()
    await asyncio.gather(*bot.discovery_tasks)
    call = panel.edit_text.await_args
    assert 'Main menu' in call.args[0]
    result = next(b for row in call.kwargs['reply_markup'].inline_keyboard for b in row if b.text == 'Results: Example')
    callback(update, panel, result.callback_data)
    await admin_session(bot.callback)(update, context)
    assert bot.panel().markup.inline_keyboard[0][0].text == 'Example (2024)'
    assert panel.reply_text.await_count == 1


@pytest.mark.asyncio
async def test_search_completion_loads_buttons_into_current_search_panel(tmp_path, monkeypatch):
    bot, message, panel, context, update = setup_bot(tmp_path)
    Discovery(bot.store).register('https://catalog.test/manifest.json', {'name':'test', 'catalogs':[]})
    monkeypatch.setattr(Discovery, 'search', AsyncMock(return_value=([{'id':'tt1','name':'Example'}], [])))
    await bot.begin_search(message, 'Example')
    await asyncio.gather(*bot.discovery_tasks)
    assert panel.edit_text.await_args.kwargs['reply_markup'].inline_keyboard[0][0].callback_data.startswith('movie:')
    assert 'In progress' not in panel.edit_text.await_args.args[0]


@pytest.mark.asyncio
async def test_idle_cleanup_deletes_only_temporary_messages_and_not_active_panel(tmp_path):
    bot, message, panel, context, update = setup_bot(tmp_path)
    await bot.main_menu(message)
    context.bot.send_message.return_value = MagicMock(delete=AsyncMock())
    durable = await context.bot.send_message(42, 'Download done')
    old_notice = MagicMock(delete=AsyncMock())
    bot.panel().retired.append(old_notice)
    now = bot.panel().updated
    await bot.cleanup_panels(now+299)
    panel.delete.assert_not_awaited()
    bot.panel().pending['file'] = 'Checking source'
    await bot.cleanup_panels(now+600)
    panel.delete.assert_not_awaited()
    bot.panel().pending.clear()
    await bot.cleanup_panels(now+600)
    panel.delete.assert_awaited_once()
    old_notice.delete.assert_awaited_once()
    durable.delete.assert_not_awaited()
    assert bot.panel().message is None
    await bot.main_menu(message)
    assert message.reply_text.await_count == 2


@pytest.mark.asyncio
async def test_cleanup_deletion_denied_removes_controls_without_crashing(tmp_path):
    bot, message, panel, context, update = setup_bot(tmp_path)
    await bot.main_menu(message)
    panel.delete.side_effect = Forbidden('Cannot delete')
    await bot.cleanup_panels(bot.panel().updated+301)
    panel.edit_reply_markup.assert_awaited_once_with(reply_markup=None)
    assert len(bot.panel().retired) == 1


@pytest.mark.asyncio
async def test_new_menu_is_sent_before_previous_is_deleted(tmp_path):
    bot, message, old, context, update = setup_bot(tmp_path)
    await bot.main_menu(message)
    replacement = MagicMock(edit_text=AsyncMock(), delete=AsyncMock())
    async def send(text, **kwargs):
        old.delete.assert_not_awaited()
        return replacement
    message.reply_text.side_effect = send
    await bot.main_menu(message)
    old.delete.assert_awaited_once()
    assert bot.panel().message is replacement


@pytest.mark.asyncio
async def test_two_admins_have_independent_panels_and_inactivity(tmp_path):
    bot, message, panel, context, update = setup_bot(tmp_path)
    bot.store.db.execute('INSERT INTO admins VALUES (1234)')
    bot.store.db.commit()
    await admin_session(bot.start)(update, context)
    first = bot.panel()
    other_panel = MagicMock(edit_text=AsyncMock(), delete=AsyncMock())
    other_notice = MagicMock(delete=AsyncMock())
    async def other_reply(text, **kwargs):
        return other_notice if text == 'Opening menu…' else other_panel
    other_message = MagicMock(reply_text=AsyncMock(side_effect=other_reply))
    other = SimpleNamespace(message=other_message, effective_user=SimpleNamespace(id=1234), effective_chat=SimpleNamespace(type='private'))
    await admin_session(bot.start)(other, SimpleNamespace(user_data={}, args=[]))
    assert bot.panels[1234] is not first and bot.panels[1234].message is other_panel
    await bot.cleanup_panels(first.updated+299)
    panel.delete.assert_not_awaited()
    other_panel.delete.assert_not_awaited()


@pytest.mark.asyncio
async def test_idle_prompt_expires_but_running_operation_survives(tmp_path):
    bot, message, panel, context, update = setup_bot(tmp_path)
    context.user_data['manifest_pending'] = True
    bot.panel().updated = time.monotonic() - 301
    bot.touch_panel(context)
    assert 'manifest_pending' not in context.user_data
    context.user_data['manifest_pending'] = True
    bot.panel().updated = time.monotonic() - 301
    bot.panel().pending['movie'] = 'Finding files'
    bot.touch_panel(context)
    assert context.user_data['manifest_pending']


@pytest.mark.asyncio
async def test_migration_entry_saved_pagination_and_pending_list_avoid_message_fanout(tmp_path):
    bot, message, panel, context, update = setup_bot(tmp_path)
    await bot.main_menu(message)
    assert any(b.text == 'Migration' for row in message.reply_text.await_args.kwargs['reply_markup'].inline_keyboard for b in row)
    callback(update, panel, 'menu:migration')
    await admin_session(bot.callback)(update, context)
    assert 'Migration' in bot.panel().text
    for i in range(12):
        key = bot.store.create(f'https://file.test/{i}', 'Example', f'{i}.mkv', 100)
        bot.store.update(key, state='oversize')
    await bot.saved(update, context)
    rows = bot.panel().markup.inline_keyboard
    assert len(rows) == 10 and rows[-2][0].text == 'More'
    for i in range(4):
        key = bot.store.create(f'https://review.test/{i}', 'Example', f'{i}.mkv', 100)
        bot.store.update(key, state='review')
    bot.preview = AsyncMock()
    await bot.pending(update, context)
    bot.preview.assert_not_awaited()
    assert len(bot.panel().markup.inline_keyboard) == 5
    assert message.reply_text.await_count == 3


@pytest.mark.asyncio
async def test_file_check_starts_while_partial_provider_results_are_loading(tmp_path):
    bot, message, panel, context, update = setup_bot(tmp_path)
    await bot.main_menu(message)
    started, release = asyncio.Event(), asyncio.Event()
    async def provider():
        started.set()
        await release.wait()
    await bot.start_panel_task('movie', 'Finding files', provider, context)
    await started.wait()
    key = bot.store.cache('stream', {'url':'https://file.test/movie.mkv', 'movie':{'name':'Example'}, 'filename':'movie.mkv', 'size':100})
    bot.prepare_choice = AsyncMock()
    callback(update, panel, f'file:{key}')
    await admin_session(bot.callback)(update, context)
    await asyncio.sleep(0)
    bot.prepare_choice.assert_awaited_once()
    assert 'movie' in bot.panel().pending
    release.set()
    await asyncio.gather(*bot.discovery_tasks)
    assert not bot.panel().pending


@pytest.mark.asyncio
async def test_cancelled_background_check_clears_pending_footer(tmp_path):
    bot, message, panel, context, update = setup_bot(tmp_path)
    await bot.main_menu(message)
    started = asyncio.Event()
    async def check():
        started.set()
        await asyncio.Event().wait()
    await bot.start_panel_task('file', 'Checking file', check, context)
    await started.wait()
    task = next(iter(bot.discovery_tasks))
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert not bot.panel().pending
    assert 'In progress' not in panel.edit_text.await_args.args[0]


@pytest.mark.asyncio
async def test_latest_search_replaces_earlier_search_and_preserves_transfer(tmp_path, monkeypatch):
    bot, message, panel, context, update = setup_bot(tmp_path)
    Discovery(bot.store).register('https://catalog.test/manifest.json', {'name':'test', 'catalogs':[]})
    first_started = asyncio.Event()
    cancelled = asyncio.Event()
    transfer_release = asyncio.Event()
    async def transfer():
        await transfer_release.wait()
    bot.spawn(transfer())
    transfer_task = next(iter(bot.tasks))
    async def search(self, query):
        if query == 'First':
            first_started.set()
            try: await asyncio.Event().wait()
            finally: cancelled.set()
        return [{'id':'second', 'name':query}], []
    monkeypatch.setattr(Discovery, 'search', search)
    await bot.begin_search(message, 'First')
    await first_started.wait()
    await bot.begin_search(message, 'Second')
    await asyncio.gather(*bot.discovery_tasks)
    assert cancelled.is_set()
    assert bot.panel().markup.inline_keyboard[0][0].text.startswith('Second')
    assert not transfer_task.done()
    transfer_release.set()
    await transfer_task


@pytest.mark.asyncio
async def test_latest_command_interrupts_blocked_handler_and_only_its_admin(tmp_path):
    bot, message, panel, context, update = setup_bot(tmp_path)
    bot.store.db.execute('INSERT INTO admins VALUES (1234)'); bot.store.db.commit()
    started = asyncio.Event()
    stopped = asyncio.Event()
    other_release = asyncio.Event()
    async def slow(update, context):
        if update.effective_user.id == 1234:
            await other_release.wait()
            return
        started.set()
        try: await asyncio.Event().wait()
        finally: stopped.set()
    async def fast(update, context):
        await bot.show_panel(update.message, 'Latest command')
    bot.slow = slow; bot.fast = fast
    # Use bound methods as real command handlers do.
    from types import MethodType
    bot.slow = MethodType(lambda self, update, context: slow(update, context), bot)
    bot.fast = MethodType(lambda self, update, context: fast(update, context), bot)
    other = SimpleNamespace(message=message, effective_user=SimpleNamespace(id=1234), effective_chat=SimpleNamespace(type='private'))
    other_task = asyncio.create_task(latest_command(bot.slow)(other, context))
    first = asyncio.create_task(latest_command(bot.slow)(update, context))
    await started.wait()
    await latest_command(bot.fast)(update, context)
    assert stopped.is_set() and first.cancelled()
    assert not other_task.done() and bot.panel().text == 'Latest command'
    other_release.set(); await other_task


@pytest.mark.asyncio
async def test_start_deletes_temporary_opening_notice(tmp_path):
    bot, message, panel, context, update = setup_bot(tmp_path)
    notice = await message.reply_text('Opening menu…')
    await admin_session(bot.start)(update, context)
    notice.delete.assert_awaited_once()
    assert bot.panel().message is panel


@pytest.mark.asyncio
async def test_new_command_clears_old_prompt_but_text_can_answer_prompt(tmp_path):
    bot, message, panel, context, update = setup_bot(tmp_path)
    context.user_data['manifest_pending'] = True
    await latest_command(bot.start)(update, context)
    assert 'manifest_pending' not in context.user_data
    context.user_data['catalogue_search_pending'] = True
    message.text = 'One Piece'
    bot.begin_search = AsyncMock()
    await latest_command(bot.text, clear_prompt=False)(update, context)
    bot.begin_search.assert_awaited_once_with(message, 'One Piece')


@pytest.mark.asyncio
async def test_latest_command_cancels_only_own_admins_background_lookup(tmp_path):
    bot, message, panel, context, update = setup_bot(tmp_path)
    bot.store.db.execute('INSERT INTO admins VALUES (1234)'); bot.store.db.commit()
    first_started = asyncio.Event()
    second_started = asyncio.Event()
    other_release = asyncio.Event()
    async def first_check():
        first_started.set()
        await asyncio.Event().wait()
    async def second_check():
        second_started.set()
        await other_release.wait()
    await bot.start_panel_task('movie', 'First lookup', first_check, context)
    await first_started.wait()
    other = SimpleNamespace(message=message, effective_user=SimpleNamespace(id=1234), effective_chat=SimpleNamespace(type='private'), callback_query=None)
    async def start_other(update, context):
        await bot.start_panel_task('search', 'Other lookup', second_check, context)
    await admin_session(start_other)(other, context)
    await second_started.wait()
    await latest_command(bot.start)(update, context)
    assert not bot.panels[42].pending
    assert 'search' in bot.panels[1234].pending
    other_release.set()
    await asyncio.gather(*bot.discovery_tasks)


@pytest.mark.asyncio
async def test_stop_cancels_pending_search_and_transfer(tmp_path):
    bot, message, panel, context, update = setup_bot(tmp_path)
    lookup_started = asyncio.Event()
    lookup_cancelled = asyncio.Event()
    transfer_release = asyncio.Event()
    async def lookup():
        lookup_started.set()
        try: await asyncio.Event().wait()
        finally: lookup_cancelled.set()
    async def transfer():
        await transfer_release.wait()
    bot.spawn(transfer())
    transfer_task = next(iter(bot.tasks))
    await bot.start_panel_task('search', 'Searching', lookup, context)
    await lookup_started.wait()
    await latest_command(bot.stop)(update, context)
    assert lookup_cancelled.is_set() and not bot.panel().pending
    assert 'Stopped active work' in bot.panel().text
    assert transfer_task.cancelled() and not bot.transfers_enabled()


@pytest.mark.asyncio
async def test_stop_cancels_active_handler_and_reports_idle_on_repeat(tmp_path):
    bot, message, panel, context, update = setup_bot(tmp_path)
    from types import MethodType
    started, cancelled = asyncio.Event(), asyncio.Event()
    async def slow(self, update, context):
        started.set()
        try: await asyncio.Event().wait()
        finally: cancelled.set()
    bot.slow = MethodType(slow, bot)
    running = asyncio.create_task(latest_command(bot.slow)(update, context))
    await started.wait()
    await latest_command(bot.stop)(update, context)
    assert running.cancelled() and cancelled.is_set()
    assert 'Stopped active work' in bot.panel().text
    await latest_command(bot.stop)(update, context)
    assert 'Stopped active work' in bot.panel().text


@pytest.mark.asyncio
async def test_stop_cancels_another_admins_lookup(tmp_path):
    bot, message, panel, context, update = setup_bot(tmp_path)
    bot.store.db.execute('INSERT INTO admins VALUES (1234)'); bot.store.db.commit()
    started, release = asyncio.Event(), asyncio.Event()
    async def lookup():
        started.set(); await release.wait()
    other = SimpleNamespace(message=message, effective_user=SimpleNamespace(id=1234), effective_chat=SimpleNamespace(type='private'), callback_query=None)
    async def start_other(update, context):
        await bot.start_panel_task('search', 'Other lookup', lookup, context)
    await admin_session(start_other)(other, context)
    await started.wait()
    await latest_command(bot.stop)(update, context)
    assert 'Stopped active work' in bot.panel().text
    assert 'search' not in bot.panels[1234].pending


class ChatMessage:
    def __init__(self, chat, number, text='', markup=None, *, photo=None, caption=None):
        self.chat = chat
        self.chat_id = 42
        self.message_id = number
        self.text, self.reply_markup = text, markup
        self.caption = caption
        self.photo = photo or []
        self.deleted = False
        self.delete = AsyncMock(side_effect=self._delete)
        self.edit_reply_markup = AsyncMock(side_effect=self._markup)
        self.edit_text = AsyncMock(side_effect=self._edit)
        self.edit_caption = AsyncMock(side_effect=self._caption)
    async def _delete(self): self.deleted = True
    async def _markup(self, reply_markup=None): self.reply_markup = reply_markup
    async def _edit(self, text, reply_markup=None): self.text = text; self.reply_markup = reply_markup
    async def _caption(self, caption, reply_markup=None): self.caption = caption; self.reply_markup = reply_markup
    async def reply_text(self, text, reply_markup=None, **kwargs):
        assert kwargs.get('do_quote', False) is False
        return await self.chat.send_message(42, text, reply_markup=reply_markup)


class Chat:
    def __init__(self): self.messages = []
    async def send_message(self, chat_id, text, reply_markup=None, **kwargs):
        message = ChatMessage(self, len(self.messages)+1, text, reply_markup)
        self.messages.append(message)
        return message
    async def send_photo(self, chat_id, photo, caption, reply_markup=None):
        message = ChatMessage(self, len(self.messages)+1, markup=reply_markup,
                              photo=[SimpleNamespace(file_id='photo1')], caption=caption)
        self.messages.append(message)
        return message


@pytest.mark.asyncio
async def test_commands_buttons_and_back_create_new_messages_without_touching_transfer(tmp_path):
    bot = Bot(Store(tmp_path/'db'),tmp_path,42,MagicMock())
    chat = Chat()
    user_message = ChatMessage(chat, 0)
    progress = await chat.send_message(42, 'Downloading 50%')
    release=asyncio.Event()
    bot.spawn(release.wait())
    context=SimpleNamespace(user_data={}, args=[], bot=chat)
    update=SimpleNamespace(message=user_message,effective_user=SimpleNamespace(id=42),effective_chat=SimpleNamespace(type='private'),callback_query=None)
    try:
        await latest_command(bot.start)(update,context)
        first = bot.panel().message
        update.callback_query=SimpleNamespace(data='menu:addons',message=first,answer=AsyncMock())
        await admin_session(bot.callback)(update,context)
        second=bot.panel().message
        assert first.deleted and second.message_id>first.message_id
        update.callback_query=SimpleNamespace(data='menu:addmanifest',message=second,answer=AsyncMock())
        await admin_session(bot.callback)(update,context)
        third=bot.panel().message
        assert second.deleted and 'manifest URL' in third.text
        update.callback_query=SimpleNamespace(data='menu:back',message=third,answer=AsyncMock())
        await admin_session(bot.callback)(update,context)
        fourth=bot.panel().message
        assert third.deleted and fourth.message_id>third.message_id and 'Add-ons' in fourth.text
        update.callback_query=None
        context.args=['One Piece']
        await latest_command(bot.online_search)(update,context)
        assert fourth.deleted and 'Add or enable' in bot.panel().message.text
        assert not progress.deleted and not next(iter(bot.tasks)).done()
    finally:
        release.set(); await asyncio.gather(*bot.tasks)


@pytest.mark.asyncio
async def test_failed_deletion_disables_old_controls_and_keeps_new_menu(tmp_path):
    bot=Bot(Store(tmp_path/'db'),tmp_path,42,MagicMock())
    chat=Chat(); user=ChatMessage(chat,0)
    await bot.main_menu(user)
    old=bot.panel().message
    old.delete.side_effect=Forbidden('cannot delete')
    await bot.main_menu(user)
    assert bot.panel().message is not old
    old.edit_reply_markup.assert_awaited_once_with(reply_markup=None)


@pytest.mark.asyncio
async def test_old_file_check_cannot_replace_new_navigation_screen(tmp_path, monkeypatch):
    bot=Bot(Store(tmp_path/'db'),tmp_path,42,MagicMock())
    chat=Chat(); user=ChatMessage(chat,0)
    context=SimpleNamespace(user_data={},bot=chat)
    await bot.main_menu(user)
    started,release=asyncio.Event(),asyncio.Event()
    async def probe(*args):
        started.set();await release.wait()
        return {'status':'ready','size':10,'filename':'file.mkv','content_type':'video/x-matroska'}
    monkeypatch.setattr('satoubot.catalogue.probe',probe)
    monkeypatch.setattr('satoubot.catalogue.inspect_remote_media',AsyncMock(side_effect=ValueError()))
    stream={'url':'https://example.test/file.mkv','file_type':'mkv','movie':{'id':'tt1','name':'Film'}}
    task=asyncio.create_task(bot.prepare_choice(context,stream))
    await started.wait()
    await bot.show_panel(user,'New screen')
    newest=bot.panel().message
    release.set();await task
    assert bot.panel().message is newest and not newest.deleted and newest.text=='New screen'


@pytest.mark.asyncio
async def test_manifest_url_is_registered_without_download_probe(tmp_path,monkeypatch):
    bot, message, panel, context, update = setup_bot(tmp_path)
    url='https://catalogue.test/manifest.json?key=value'
    message.text=url
    monkeypatch.setattr(Discovery,'add',AsyncMock(return_value=('My catalogue','addon')))
    bot.handle_source=AsyncMock()
    await latest_command(bot.text,clear_prompt=False)(update,context)
    Discovery.add.assert_awaited_once_with(url)
    bot.handle_source.assert_not_awaited()
    assert 'Saved My catalogue' in bot.panel().text


@pytest.mark.asyncio
async def test_photo_card_pagination_sends_new_card_and_keeps_work(tmp_path):
    bot=Bot(Store(tmp_path/'db'),tmp_path,42,MagicMock())
    chat=Chat(); old=await chat.send_photo(42,photo='photo1',caption='Film')
    bot.panel().card=old
    selection=bot.store.cache('pages',[])
    update=SimpleNamespace(callback_query=SimpleNamespace(data=f'page:{selection*1000}',message=old))
    await bot.catalogue_callback(update,SimpleNamespace(bot=chat,user_data={}))
    new=bot.panel().card
    assert new is not old and old.deleted and new.caption=='Film'
    assert new.photo and not new.deleted


@pytest.mark.asyncio
async def test_file_back_restores_photo_card_then_search_results(tmp_path):
    bot=Bot(Store(tmp_path/'db'),tmp_path,42,MagicMock())
    chat=Chat();user=ChatMessage(chat,0)
    context=SimpleNamespace(user_data={},bot=chat)
    update=SimpleNamespace(message=user,effective_user=SimpleNamespace(id=42),effective_chat=SimpleNamespace(type='private'),callback_query=None)
    await bot.show_panel(user,'Search results',Markup([[Button('Film',callback_data='movie:1')]]))
    await bot.show_panel(user,'Finding files')
    searching=bot.panel().message
    card=await chat.send_photo(42,'photo1','Film file options',reply_markup=Markup([[Button('Back',callback_data='menu:back')]]))
    bot.panel().message=None;bot.panel().card=card
    await searching.delete()
    await bot.show_panel(card,'File confirmation')
    confirmation=bot.panel().message
    assert card.deleted
    update.callback_query=SimpleNamespace(data='menu:back',message=confirmation,answer=AsyncMock())
    await admin_session(bot.callback)(update,context)
    restored=bot.panel().card
    assert confirmation.deleted and restored.caption=='Film file options'
    update.callback_query=SimpleNamespace(data='menu:back',message=restored,answer=AsyncMock())
    await admin_session(bot.callback)(update,context)
    assert restored.deleted and bot.panel().message.text=='Search results'


@pytest.mark.asyncio
async def test_manifest_document_with_url_caption_is_imported(tmp_path):
    bot,message,panel,context,update=setup_bot(tmp_path)
    message.caption='https://catalogue.test/manifest.json'
    message.document=SimpleNamespace(file_size=100, get_file=AsyncMock(return_value=SimpleNamespace(
        download_as_bytearray=AsyncMock(return_value=b'{"name":"My catalogue","catalogs":[]}'))))
    await bot.manifest_document(update,context)
    assert Discovery(bot.store).manifests()[0][0]=='https://catalogue.test/manifest.json'
    assert 'Saved My catalogue' in bot.panel().text


@pytest.mark.asyncio
async def test_real_file_selection_confirmation_back_restores_movie(tmp_path, monkeypatch):
    bot=Bot(Store(tmp_path/'db'),tmp_path,42,MagicMock())
    chat=Chat();user=ChatMessage(chat,0)
    context=SimpleNamespace(user_data={},bot=chat)
    update=SimpleNamespace(message=user,effective_user=SimpleNamespace(id=42),effective_chat=SimpleNamespace(type='private'),callback_query=None)
    await bot.show_panel(user,'Search results')
    await bot.show_panel(user,'Finding files')
    searching=bot.panel().message
    card=await chat.send_photo(42,'photo1','Movie options',reply_markup=Markup([[Button('Back',callback_data='menu:back')]]))
    bot.panel().message=None;bot.panel().card=card
    await searching.delete()
    key=bot.store.cache('stream',{'url':'https://example.test/file.mkv','movie':{'id':'tt1','name':'Film'},'filename':'file.mkv','file_type':'mkv'})
    monkeypatch.setattr('satoubot.catalogue.probe',AsyncMock(return_value={'status':'ready','size':100,'filename':'file.mkv','content_type':'video/x-matroska'}))
    monkeypatch.setattr('satoubot.catalogue.inspect_remote_media',AsyncMock(side_effect=ValueError()))
    update.callback_query=SimpleNamespace(data=f'file:{key}',message=card,answer=AsyncMock())
    await admin_session(bot.callback)(update,context)
    await asyncio.gather(*bot.discovery_tasks)
    confirmation=bot.panel().message
    assert 'Download this file?' in confirmation.text
    update.callback_query=SimpleNamespace(data='menu:back',message=confirmation,answer=AsyncMock())
    await admin_session(bot.callback)(update,context)
    assert bot.panel().card.caption=='Movie options'
    assert confirmation.deleted


@pytest.mark.asyncio
async def test_back_to_link_keeps_preview_disabled(tmp_path):
    bot,message,panel,context,update=setup_bot(tmp_path)
    await bot.show_panel(message,'https://example.test/file?private=1',disable_preview=True)
    await bot.show_panel(message,'Another screen')
    await bot.panel_back(message,context)
    assert message.reply_text.await_args.kwargs['disable_web_page_preview'] is True


@pytest.mark.asyncio
async def test_convert_button_replaces_menu_without_cancelling_conversion(tmp_path):
    bot=Bot(Store(tmp_path/'db'),tmp_path,42,MagicMock())
    chat=Chat();user=ChatMessage(chat,0)
    context=SimpleNamespace(user_data={},bot=chat)
    update=SimpleNamespace(message=user,effective_user=SimpleNamespace(id=42),effective_chat=SimpleNamespace(type='private'),callback_query=None)
    job=bot.store.create('https://file.test/file','Film','Film.ts',100)
    bot.store.update(job,state='local_only',path=str(tmp_path/'Film.ts'))
    await bot.show_panel(user,'Convert format')
    menu=bot.panel().message
    started,release=asyncio.Event(),asyncio.Event()
    async def convert(*args): started.set();await release.wait()
    bot.convert_job=convert
    update.callback_query=SimpleNamespace(data=f'convert:{job}:mkv',message=menu,answer=AsyncMock())
    try:
        await admin_session(bot.callback)(update,context)
        await started.wait()
        assert menu.deleted and 'Conversion started' in bot.panel().message.text
        assert not next(iter(bot.tasks)).done()
    finally:
        release.set();await asyncio.gather(*bot.tasks)
