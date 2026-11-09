"""Interactive cancellation for lookups and active transfer tasks."""
import asyncio
from telegram import InlineKeyboardButton as Button, InlineKeyboardMarkup as Markup
from .menu import navigation


class TaskControlFunctions:
    def lookup_tasks(self, *, all_admins=False):
        panels = getattr(self, 'panels', {})
        owners = panels.items() if all_admins else [(self.admin_id, self.panel())]
        return [(owner, key, task, panel) for owner, panel in owners
                for key, task in panel.tasks.items() if not task.done()]

    def is_daily_transfer(self, info):
        if info.get('daily_identity'): return True
        if not info.get('job_id'): return False
        self.daily_init()
        return bool(self.store.db.execute("SELECT 1 FROM daily_titles WHERE job_id=? AND state='transferring'", (info['job_id'],)).fetchone())

    def discard_current_confirmation(self):
        """Invalidate an unstarted selection when its Cancel button is used."""
        panel = self.panel()
        if not panel.markup: return
        for row in panel.markup.inline_keyboard:
            for button in row:
                data = button.callback_data or ''
                parts = data.split(':')
                try: ident = int(parts[1])
                except (IndexError, ValueError): continue
                if parts[0] in ('dl', 'dllocal'):
                    job = self.store.get(ident)
                    if job and job['state'] == 'ready': self.store.update(ident, state='cancelled')
                elif parts[0] in ('yes', 'yeslocal'):
                    self.store.db.execute("DELETE FROM discovery_cache WHERE kind='confirmation' AND id=?", (ident,))
        self.store.db.commit()

    async def cancel_menu(self, message, *, daily_only=False, notice=None):
        rows = []
        for task, info in self.active_transfers():
            if daily_only and not self.is_daily_transfer(info): continue
            rows.append([Button(('Cancel ' + info['label'])[:100], callback_data=f'cancelwork:transfer:{info["id"]}')])
        for owner, key, task, panel in self.lookup_tasks():
            if daily_only and not panel.task_daily.get(key): continue
            ref = self.store.cache('cancel_lookup', {'owner':owner, 'key':key, 'task':id(task)})
            rows.append([Button(('Cancel ' + panel.pending.get(key, key))[:100], callback_data=f'cancelwork:lookup:{ref}')])
        if not daily_only:
            task = getattr(self, 'command_tasks', {}).get(self.admin_id)
            if task and task is not asyncio.current_task() and not task.done():
                ref = self.store.cache('cancel_command', {'owner':self.admin_id, 'task':id(task)})
                rows.append([Button('Cancel current command', callback_data=f'cancelwork:command:{ref}')])
        if daily_only or self.store.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='daily_titles'").fetchone():
            self.daily_init()
            current = self.store.db.execute("SELECT identity FROM daily_titles WHERE state='offered' LIMIT 1").fetchone()
            if current:
                ref = self.store.cache('daily_title', {'identity': current['identity']})
                rows.append([Button('Cancel current daily release', callback_data=f'cancelwork:daily:{ref}')])
        if rows:
            rows.append([Button('Cancel all daily tasks' if daily_only else 'Cancel all active tasks', callback_data='daily:cancelall' if daily_only else 'cancelwork:all')])
            text = 'Choose one task to cancel, or cancel all active tasks. Cancelling an upload may leave a copy in Telegram.'
        else:
            text = 'No active tasks. Your current input was cancelled.'
        if notice: text = notice + '\n\n' + text
        rows.append([Button('Back', callback_data='menu:back'), Button('Main menu', callback_data='menu:main')])
        await self.show_panel(message, text, reply_markup=Markup(rows))

    def mark_daily_cancelled(self, identity=None, job_id=None):
        self.daily_init()
        row = self.store.db.execute('SELECT * FROM daily_titles WHERE identity=? OR (job_id IS NOT NULL AND job_id=?)',
                                    (identity, job_id)).fetchone()
        if not row or row['state'] not in ('offered', 'transferring'): return
        job = self.store.get(row['job_id']) if row['job_id'] else None
        state = 'needs_attention' if job and job['state'] in ('uploading', 'upload_unknown', 'published') else 'skipped'
        self.store.db.execute('UPDATE daily_titles SET state=? WHERE identity=?', (state,row['identity']))
        self.store.db.commit()
        self.store.set_setting('daily_next_check','0')

    async def cancel_work(self, *, transfer=None, lookup=None, command=None, all_work=False):
        targets = []
        if all_work:
            for task in getattr(self, 'command_tasks', {}).values():
                if task is not asyncio.current_task() and not task.done(): targets.append((task, {}))
            self.store.set_setting('daily_enabled','0')
            self.daily_init()
            self.store.db.execute("UPDATE daily_titles SET state='skipped' WHERE state='offered'")
            self.store.db.commit()
            targets += [(task, info) for task, info in self.active_transfers()]
            targets += [(task, {'daily_identity':panel.task_daily.get(key)}) for _, key, task, panel in self.lookup_tasks(all_admins=True)]
            for task in list(getattr(self, 'discovery_tasks', ())):
                if not task.done() and all(task is not t for t, _ in targets): targets.append((task, {}))
        elif transfer is not None:
            entry = self.task_info.get(transfer)
            if entry and not entry[0].done(): targets.append(entry)
        elif command is not None:
            ref = self.store.cached(command, 'cancel_command')
            if ref and ref.get('owner') == self.admin_id:
                task = getattr(self, 'command_tasks', {}).get(self.admin_id)
                if task and not task.done() and id(task) == ref['task']:
                    targets.append((task, {}))
        elif lookup is not None:
            ref = self.store.cached(lookup, 'cancel_lookup')
            if ref and ref.get('owner') == self.admin_id:
                panel = getattr(self, 'panels', {}).get(ref['owner'])
                task = panel.tasks.get(ref['key']) if panel else None
                if task and not task.done() and id(task) == ref['task']:
                    targets.append((task, {'daily_identity':panel.task_daily.get(ref['key'])}))
        unique = {task:info for task,info in targets if task is not asyncio.current_task()}
        for task in unique: task.cancel()
        if unique: await asyncio.gather(*unique, return_exceptions=True)
        for info in unique.values():
            if info.get('daily_identity') or info.get('job_id'):
                self.mark_daily_cancelled(info.get('daily_identity'), info.get('job_id'))
        return len(unique)

    async def cancel_daily_all(self):
        self.store.set_setting('daily_enabled', '0')
        self.daily_init()
        self.store.db.execute("UPDATE daily_titles SET state='skipped' WHERE state='offered'")
        self.store.db.commit()
        transfers = [(task, info) for task, info in self.active_transfers() if self.is_daily_transfer(info)]
        lookups = [(task, {'daily_identity':panel.task_daily.get(key)}) for _, key, task, panel in self.lookup_tasks(all_admins=True) if panel.task_daily.get(key)]
        unique = {task:info for task, info in transfers + lookups}
        for task in unique: task.cancel()
        if unique: await asyncio.gather(*unique, return_exceptions=True)
        for info in unique.values(): self.mark_daily_cancelled(info.get('daily_identity'), info.get('job_id'))
        self.store.db.execute("UPDATE daily_titles SET state='skipped' WHERE state='offered'")
        self.store.db.commit()
        return len(unique)

    async def cancel_daily_scan(self, context):
        task = getattr(self, 'daily_task', None)
        if task and not task.done() and task is not asyncio.current_task():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            self.daily_task = asyncio.create_task(self.daily_loop(context.bot))

    async def cancel_callback(self, update, context):
        parts = update.callback_query.data.split(':')
        upload_cancelled = False
        if len(parts) == 2 and parts[1] == 'all':
            upload_cancelled = any(info['kind'] == 'upload' for _, info in self.active_transfers())
            count = await self.cancel_work(all_work=True)
            await self.cancel_daily_scan(context)
            text = f'Cancelled {count} active task(s). Daily Uploads are stopped.'
        else:
            try: ident=int(parts[2]); kind=parts[1]
            except (ValueError, IndexError): return
            if kind == 'transfer':
                entry = self.task_info.get(ident)
                upload_cancelled = bool(entry and entry[1]['kind'] == 'upload')
                count = await self.cancel_work(transfer=ident)
            elif kind == 'lookup': count = await self.cancel_work(lookup=ident)
            elif kind == 'command': count = await self.cancel_work(command=ident)
            elif kind == 'daily':
                entry = self.store.cached(ident, 'daily_title')
                identity = entry.get('identity') if entry else None
                if not identity: return
                cursor = self.store.db.execute("UPDATE daily_titles SET state='skipped' WHERE identity=? AND state='offered'", (identity,))
                self.store.db.commit()
                if not cursor.rowcount:
                    return await self.cancel_menu(update.callback_query.message, notice='That daily release has already changed.')
                self.store.set_setting('daily_next_check', '0')
                lookups = [(task, panel) for _, key, task, panel in self.lookup_tasks(all_admins=True)
                           if panel.task_daily.get(key) == identity]
                for task, _ in lookups: task.cancel()
                if lookups: await asyncio.gather(*(task for task, _ in lookups), return_exceptions=True)
                count = 1
            else: return
            text = 'Task cancelled.' if count else 'That task has already finished.'
        if upload_cancelled: text += '\nThe upload may already be in Telegram. Check the destination before retrying.'
        await self.cancel_menu(update.callback_query.message, notice=text)
