import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from telegram.error import Forbidden

from satoubot.app import Bot, admin_session, latest_command
from satoubot.store import Store


def setup(tmp_path):
    bot=Bot(Store(tmp_path/'db'),tmp_path,42,MagicMock())
    message=MagicMock(reply_text=AsyncMock(return_value=MagicMock(edit_text=AsyncMock(),delete=AsyncMock())))
    context=SimpleNamespace(user_data={},args=[],bot=SimpleNamespace(send_message=AsyncMock()))
    update=SimpleNamespace(message=message,effective_user=SimpleNamespace(id=42),effective_chat=SimpleNamespace(type='private'),callback_query=None)
    return bot,message,context,update


@pytest.mark.asyncio
async def test_two_downloads_and_two_uploads_have_independent_limits(tmp_path):
    bot,message,context,update=setup(tmp_path)
    release=asyncio.Event()
    tasks=[bot.spawn(release.wait(),kind=kind,label=f'{kind} {n}') for kind in ('download','upload') for n in range(2)]
    try:
        assert len(bot.active_transfers())==4
        assert not await bot.transfer_gate(message,'download')
        assert not await bot.transfer_gate(message,'upload')
        tasks[0].cancel();await asyncio.gather(tasks[0],return_exceptions=True)
        assert await bot.transfer_gate(message,'download')
        assert not await bot.transfer_gate(message,'upload')
    finally:
        release.set();await asyncio.gather(*tasks,return_exceptions=True)


@pytest.mark.asyncio
async def test_cancel_menu_one_then_all_and_stop_flag(tmp_path):
    bot,message,context,update=setup(tmp_path)
    release=asyncio.Event()
    first=bot.spawn(release.wait(),kind='download',label='Download 1')
    second=bot.spawn(release.wait(),kind='upload',label='Upload 2')
    await bot.cancel_command(update,context)
    rows=message.reply_text.await_args.kwargs['reply_markup'].inline_keyboard
    assert any('Cancel all' in b.text for row in rows for b in row)
    action=next(b.callback_data for row in rows for b in row if b.text=='Cancel Download 1')
    update.callback_query=SimpleNamespace(data=action,message=message,answer=AsyncMock())
    await bot.callback(update,context)
    assert first.cancelled() and not second.done()
    update.callback_query=SimpleNamespace(data='cancelwork:all',message=message,answer=AsyncMock())
    await bot.callback(update,context)
    assert second.cancelled() and bot.transfers_enabled() and bot.store.setting('daily_enabled')=='0'


@pytest.mark.asyncio
async def test_stop_cancels_transfers_and_lookups_and_start_resumes_only_transfers(tmp_path):
    bot,message,context,update=setup(tmp_path)
    release=asyncio.Event();transfer=bot.spawn(release.wait(),kind='download')
    started=asyncio.Event()
    async def lookup(): started.set();await release.wait()
    await bot.start_panel_task('search','Search',lookup,context)
    await started.wait()
    await latest_command(bot.stop)(update,context)
    assert transfer.cancelled() and not bot.panel().tasks and not bot.transfers_enabled()
    assert bot.store.setting('daily_enabled')=='0'
    await latest_command(bot.start)(update,context)
    assert bot.transfers_enabled() and bot.store.setting('daily_enabled')=='0'


@pytest.mark.asyncio
async def test_cancel_daily_download_skips_release_but_upload_unknown_needs_attention(tmp_path):
    bot,message,context,update=setup(tmp_path);bot.daily_init()
    bot.set_transfers(True)
    started=asyncio.Event()
    async def active_download(job):
        try: started.set();await asyncio.Event().wait()
        finally: bot.store.update(job,state='cancelled')
    job=bot.store.create('https://file.test','Film','f.mkv',10)
    bot.store.update(job,state='downloading')
    bot.store.db.execute("insert into daily_titles values ('movie:tt1','{}','transferring',?,NULL)",(job,));bot.store.db.commit()
    task=bot.spawn(active_download(job),kind='download',job_id=job,daily_identity='movie:tt1')
    await started.wait();ident=next(iter(bot.task_info))
    assert await bot.cancel_work(transfer=ident)==1 and task.cancelled()
    assert bot.store.db.execute("select state from daily_titles where identity='movie:tt1'").fetchone()[0]=='skipped'
    assert bot.store.setting('daily_enabled')=='1' and bot.store.setting('daily_next_check')=='0'
    started.clear()
    async def uncertain_upload(job):
        try: started.set();await asyncio.Event().wait()
        finally: bot.store.update(job,state='upload_unknown')
    job2=bot.store.create('https://file.test/2','Film2','g.mkv',10)
    bot.store.update(job2,state='uploading')
    bot.store.db.execute("insert into daily_titles values ('movie:tt2','{}','transferring',?,NULL)",(job2,));bot.store.db.commit()
    task2=bot.spawn(uncertain_upload(job2),kind='upload',job_id=job2)
    await started.wait();ident=next(iter(bot.task_info))
    await bot.cancel_work(transfer=ident)
    assert task2.cancelled()
    assert bot.store.db.execute("select state from daily_titles where identity='movie:tt2'").fetchone()[0]=='needs_attention'


@pytest.mark.asyncio
async def test_cancel_before_task_starts_does_not_leave_job_stuck(tmp_path):
    bot,message,context,update=setup(tmp_path)
    job=bot.store.create('https://file.test','Film','film.mkv',10)
    bot.store.update(job,state='uploading')
    task=bot.spawn(asyncio.sleep(20),kind='upload',job_id=job)
    task.cancel();await asyncio.gather(task,return_exceptions=True)
    assert bot.store.get(job)['state']=='upload_unknown'


@pytest.mark.asyncio
async def test_startup_notice_goes_to_owner_and_failure_does_not_abort(tmp_path):
    bot,message,context,update=setup(tmp_path)
    transport=SimpleNamespace(send_message=AsyncMock())
    await bot.announce_startup(transport)
    transport.send_message.assert_awaited_once_with(42,"I'm Back")
    transport.send_message.side_effect=Forbidden('owner has not started chat')
    await bot.announce_startup(transport)


@pytest.mark.asyncio
async def test_per_task_progress_does_not_hide_other_transfer(tmp_path):
    bot,message,context,update=setup(tmp_path)
    first_ready=asyncio.Event();second_ready=asyncio.Event();release=asyncio.Event()
    async def work(label, ready):
        try:
            await bot.panel_status(context,'transfer',label);ready.set();await release.wait()
        finally:
            await bot.panel_status(context,'transfer')
    first=bot.spawn(work('Download 1',first_ready),kind='download')
    second=bot.spawn(work('Upload 2',second_ready),kind='upload')
    await first_ready.wait();await second_ready.wait()
    assert set(bot.panel().pending.values())=={'Download 1','Upload 2'}
    first.cancel();await asyncio.gather(first,return_exceptions=True)
    assert list(bot.panel().pending.values())==['Upload 2']
    release.set();await second
    assert not bot.panel().pending


@pytest.mark.asyncio
async def test_cancel_current_daily_lookup_allows_next_title(tmp_path):
    bot,message,context,update=setup(tmp_path);bot.daily_init();bot.set_transfers(True)
    bot.store.db.execute("insert into daily_titles values ('movie:old','{}','offered',NULL,NULL)");bot.store.db.commit()
    started=asyncio.Event()
    async def work(): started.set();await asyncio.Event().wait()
    await bot.start_panel_task('movie','Daily release',work,context,daily_identity='movie:old')
    await started.wait()
    await bot.cancel_command(update,context)
    rows=message.reply_text.await_args.kwargs['reply_markup'].inline_keyboard
    ref=next(b.callback_data for row in rows for b in row if b.text=='Cancel Daily release')
    update.callback_query=SimpleNamespace(data=ref,message=message,answer=AsyncMock())
    await bot.callback(update,context)
    assert bot.store.db.execute("select state from daily_titles where identity='movie:old'").fetchone()[0]=='skipped'
    bot.recent_releases=AsyncMock(return_value=[{'id':'next','type':'movie','name':'Next'}])
    bot.safe_movie_card=AsyncMock()
    await bot.daily_tick(context)
    await asyncio.gather(*bot.discovery_tasks)
    assert bot.store.db.execute("select state from daily_titles where identity='movie:next'").fetchone()[0]=='offered'


@pytest.mark.asyncio
async def test_stale_menu_button_cannot_change_settings(tmp_path):
    bot,message,context,update=setup(tmp_path)
    current=SimpleNamespace(message_id=12,chat_id=42)
    stale=SimpleNamespace(message_id=11,chat_id=42)
    bot.panel().message=current
    query=SimpleNamespace(data='pref:output_format:mkv',message=stale,answer=AsyncMock())
    update.callback_query=query
    await bot.callback(update,context)
    assert bot.transfer_options()['output_format']=='original'
    assert query.answer.await_args.kwargs['show_alert'] is True


@pytest.mark.asyncio
async def test_old_review_conversion_token_is_rejected(tmp_path):
    bot,message,context,update=setup(tmp_path)
    job=bot.store.create('https://file.test/a','Film','film.ts',10)
    bot.store.update(job,state='review',approval_token='new-token',path=str(tmp_path/'film.ts'))
    q=SimpleNamespace(data=f'convertmenu:{job}:old-token',message=message,answer=AsyncMock())
    update.callback_query=q
    bot.conversion_menu=AsyncMock()
    await bot.callback(update,context)
    bot.conversion_menu.assert_not_awaited()
    assert 'out of date' in message.reply_text.await_args.args[0]


@pytest.mark.asyncio
async def test_duplicate_source_claim_is_rechecked_after_concurrent_probe(tmp_path,monkeypatch):
    bot,message,context,update=setup(tmp_path)
    entered=0;ready=asyncio.Event()
    async def probe(*args):
        nonlocal entered
        entered+=1
        if entered==2: ready.set()
        await ready.wait()
        return {'status':'ready','size':10,'filename':'film.mkv','content_type':'video/x-matroska'}
    monkeypatch.setattr('satoubot.catalogue.probe',probe)
    bot.perform_download=AsyncMock()
    first={'url':'https://file.test/file','file_type':'mkv','movie':{'id':'a','name':'First'}}
    second={'url':'https://file.test/file','file_type':'mkv','movie':{'id':'b','name':'Second'}}
    await asyncio.gather(bot.download_choice(context,first),bot.download_choice(context,second))
    assert bot.store.db.execute('select count(*) from jobs').fetchone()[0]==1
    bot.perform_download.assert_awaited_once()


@pytest.mark.asyncio
async def test_real_callback_admission_limits_downloads_and_uploads_independently(tmp_path):
    bot,message,context,update=setup(tmp_path)
    bot.show_panel=AsyncMock()
    release=asyncio.Event();started=asyncio.Event();counts={'download':0,'upload':0}
    async def download(*args):
        counts['download']+=1
        if counts['download']==2 and counts['upload']==2: started.set()
        await release.wait()
    async def upload(*args):
        counts['upload']+=1
        if counts['download']==2 and counts['upload']==2: started.set()
        await release.wait()
    bot.perform_download=download;bot.perform_upload=upload
    bot.store.set_setting('target','-100123')
    context.bot.get_chat_member=AsyncMock(return_value=SimpleNamespace(status='administrator',can_post_messages=True))
    context.bot.id=777
    jobs=[]
    for n in range(3): jobs.append(bot.store.create(f'https://file.test/{n}',f'Film {n}',f'{n}.mkv',10))
    uploads=[]
    for n in range(3):
        job=bot.store.create(f'https://upload.test/{n}',f'Film {n}',f'u{n}.mkv',10)
        bot.store.update(job,state='review',target='-100123',approval_token='approved')
        uploads.append(job)
    def query(data): return SimpleNamespace(data=data,message=message,answer=AsyncMock(),edit_message_reply_markup=AsyncMock())
    try:
        for job in jobs[:2]:
            update.callback_query=query(f'dl:{job}');await bot.callback(update,context)
        for job in uploads[:2]:
            update.callback_query=query(f'upload:{job}:approved');await bot.callback(update,context)
        await asyncio.wait_for(started.wait(),1)
        update.callback_query=query(f'dl:{jobs[2]}');await bot.callback(update,context)
        update.callback_query=query(f'upload:{uploads[2]}:approved');await bot.callback(update,context)
        assert bot.store.get(jobs[2])['state']=='ready'
        assert bot.store.get(uploads[2])['state']=='review'
        assert counts=={'download':2,'upload':2}
    finally:
        release.set();await asyncio.gather(*bot.tasks)


@pytest.mark.asyncio
async def test_daily_cancel_all_preserves_unrelated_transfer(tmp_path):
    bot,message,context,update=setup(tmp_path);bot.daily_init();bot.set_transfers(True)
    started=asyncio.Event();release=asyncio.Event()
    async def daily_task(job):
        try: started.set();await release.wait()
        finally: bot.store.update(job,state='cancelled')
    job=bot.store.create('https://file.test/daily','Daily','d.mkv',10)
    bot.store.update(job,state='downloading')
    bot.store.db.execute("insert into daily_titles values ('movie:daily','{}','transferring',?,NULL)",(job,));bot.store.db.commit()
    daily=bot.spawn(daily_task(job),kind='download',job_id=job)
    other=bot.spawn(release.wait(),kind='upload',label='Manual upload')
    await started.wait()
    try:
        assert await bot.cancel_daily_all()==1
        assert daily.cancelled() and not other.done()
        assert bot.store.setting('daily_enabled')=='0' and bot.transfers_enabled()
        assert bot.store.db.execute("select state from daily_titles where identity='movie:daily'").fetchone()[0]=='skipped'
    finally:
        release.set();await asyncio.gather(other,return_exceptions=True)


@pytest.mark.asyncio
async def test_cancel_discards_unstarted_direct_confirmation(tmp_path):
    from telegram import InlineKeyboardButton as Button, InlineKeyboardMarkup as Markup
    bot,message,context,update=setup(tmp_path)
    job=bot.store.create('https://file.test/video','Film','video.mkv',10)
    await bot.show_panel(message,'Download?',reply_markup=Markup([[Button('Transfer',callback_data=f'dl:{job}')]]))
    update.callback_query=SimpleNamespace(data='menu:cancel',message=message,answer=AsyncMock())
    await bot.callback(update,context)
    assert bot.store.get(job)['state']=='cancelled' and not bot.tasks
    assert bot.store.find_source('https://file.test/video') is None


@pytest.mark.asyncio
async def test_daily_menu_offers_distinct_cancellation_choices(tmp_path):
    bot,message,context,update=setup(tmp_path);bot.daily_init()
    await bot.daily_menu(message,context)
    buttons=[b for row in message.reply_text.await_args.kwargs['reply_markup'].inline_keyboard for b in row]
    assert any(b.text=='Cancel one' and b.callback_data=='daily:cancelone' for b in buttons)
    assert any(b.text=='Cancel all daily' and b.callback_data=='daily:cancelall' for b in buttons)


@pytest.mark.asyncio
async def test_cancel_command_clears_input_without_stopping_transfers(tmp_path):
    bot,message,context,update=setup(tmp_path)
    context.user_data['manifest_pending']=True
    release=asyncio.Event();task=bot.spawn(release.wait(),kind='download',label='Download')
    try:
        await bot.cancel_command(update,context)
        assert 'manifest_pending' not in context.user_data and not task.done()
        assert any(b.text=='Cancel Download' for row in message.reply_text.await_args.kwargs['reply_markup'].inline_keyboard for b in row)
    finally:
        release.set();await task


@pytest.mark.asyncio
async def test_stale_cancel_offer_does_not_skip_inflight_upload(tmp_path):
    bot,message,context,update=setup(tmp_path);bot.daily_init();bot.set_transfers(True)
    job=bot.store.create('https://file.test/daily','Daily','d.mkv',10)
    bot.store.update(job,state='uploading')
    bot.store.db.execute("insert into daily_titles values ('movie:daily','{}','transferring',?,NULL)",(job,));bot.store.db.commit()
    ref=bot.store.cache('daily_title',{'identity':'movie:daily'})
    started=asyncio.Event();release=asyncio.Event()
    async def upload(): started.set();await release.wait()
    task=bot.spawn(upload(),kind='upload',job_id=job)
    await started.wait()
    try:
        update.callback_query=SimpleNamespace(data=f'cancelwork:daily:{ref}',message=message,answer=AsyncMock())
        await bot.callback(update,context)
        assert not task.done()
        assert bot.store.db.execute("select state from daily_titles where identity='movie:daily'").fetchone()[0]=='transferring'
    finally:
        release.set();await task


@pytest.mark.asyncio
async def test_cancel_lists_blocked_command_without_cancelling_transfer(tmp_path):
    from types import MethodType
    bot,message,context,update=setup(tmp_path)
    started=asyncio.Event();release=asyncio.Event()
    async def slow(self, update, context): started.set();await asyncio.Event().wait()
    bot.slow=MethodType(slow,bot)
    command=asyncio.create_task(latest_command(bot.slow)(update,context))
    transfer=bot.spawn(release.wait(),kind='download')
    await started.wait()
    try:
        await bot.cancel_command(update,context)
        rows=message.reply_text.await_args.kwargs['reply_markup'].inline_keyboard
        ref=next(b.callback_data for row in rows for b in row if b.text=='Cancel current command')
        update.callback_query=SimpleNamespace(data=ref,message=message,answer=AsyncMock())
        await bot.callback(update,context)
        assert command.cancelled() and not transfer.done()
    finally:
        release.set();await asyncio.gather(command,transfer,return_exceptions=True)
