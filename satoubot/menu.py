from types import SimpleNamespace
from telegram import InlineKeyboardButton as Button, InlineKeyboardMarkup as Markup


def navigation():
    return [Button('Back', callback_data='menu:back'), Button('Cancel', callback_data='menu:cancel'), Button('Main menu', callback_data='menu:main')]


class MenuFunctions:
    def clear_prompt(self, context):
        for key in ('manifest_pending', 'uploaded_manifest', 'catalogue_search_pending', 'search_pending', 'admin_add_pending', 'thumbnail_request'):
            context.user_data.pop(key, None)

    async def main_menu(self, message):
        busy = sum(info['kind'] == 'download' for _, info in self.active_transfers()) >= 2
        state = 'on' if self.transfers_enabled() else 'off'
        rows = [[Button('Download' + (' (busy)' if busy else ''), callback_data='menu:download'), Button('Upload approvals', callback_data='menu:pending')],
                [Button('Add-ons', callback_data='menu:addons'), Button('Saved files / links', callback_data='menu:saved')],
                [Button('Library search', callback_data='menu:library'), Button('My channels', callback_data='menu:channels')],
                [Button('Active transfers', callback_data='menu:active'), Button('Admin management', callback_data='menu:admins')],
                [Button('Daily Uploads', callback_data='menu:daily'), Button('Migration', callback_data='menu:migration')],
                [Button('Source history', callback_data='menu:sourcehistory'), Button('Settings', callback_data='menu:settings')],
                [Button('Stop transfers' if self.transfers_enabled() else 'Start transfers', callback_data='menu:toggle')]]
        await self.show_panel(message, f'Main menu · Transfers {state}\nSend a movie title to search catalogues.', reply_markup=Markup(rows))

    async def admin_menu(self, message, user_id):
        if user_id != self.owner_id:
            await self.show_panel(message, 'Only the owner can manage admins.', reply_markup=Markup([navigation()])); return
        ids = [row[0] for row in self.store.db.execute('SELECT user_id FROM admins ORDER BY user_id')]
        rows = [[Button(f'Remove {uid}', callback_data=f'adminremove:{uid}')] for uid in ids]
        rows += [[Button('Add admin', callback_data='menu:addadmin')], navigation()]
        await self.show_panel(message, f'Owner: {self.owner_id}\nAdmins: ' + (', '.join(map(str, ids)) or 'none'), reply_markup=Markup(rows))

    async def active_menu(self, message):
        active = self.active_transfers()
        rows = [[Button('Refresh', callback_data='menu:active')]]
        if active or self.lookup_tasks(): rows.append([Button('Cancel tasks', callback_data='menu:canceltransfer')])
        rows.append(navigation())
        lines = [info['label'] for _, info in active]
        await self.show_panel(message, 'Active transfers\n' + ('\n'.join(lines) if lines else 'No active transfers.'), reply_markup=Markup(rows))

    async def menu_callback(self, update, context):
        q=update.callback_query; action=q.data.split(':',1)[1]
        if q.data.startswith('adminremove:'):
            if update.effective_user.id != self.owner_id:
                await self.show_panel(q.message, 'Only the owner can manage admins.');return
            try: uid=int(action)
            except ValueError:return
            self.store.db.execute('DELETE FROM admins WHERE user_id=?',(uid,));self.store.db.commit()
            await self.admin_menu(q.message, update.effective_user.id);return
        if action.startswith('review:'):
            try: job_id = int(action.split(':')[1])
            except ValueError: return
            return await self.preview(context, job_id)
        if action.startswith('savedpage:'):
            try: page = int(action.split(':')[1])
            except ValueError: return
            proxy = SimpleNamespace(message=q.message, effective_user=update.effective_user, effective_chat=update.effective_chat)
            return await self.saved(proxy, context, page)
        if action.startswith('savedjob:'):
            try: job = self.store.get(int(action.split(':')[1]))
            except ValueError: return
            if job and job['state'] == 'local_only':
                rows = [[Button('Convert format', callback_data=f'convertmenu:{job["id"]}')]]
                if job['size'] <= 2_000_000_000: rows.append([Button('Review Telegram upload', callback_data=f'localreview:{job["id"]}')])
                rows.append(navigation())
                await self.show_panel(q.message, f'{job["filename"]} · {job["size"]/1_000_000:.1f} MB\nSaved locally: {job["path"]}', reply_markup=Markup(rows)); return
            if job and job['state'] in ('oversize', 'reported', 'link_only'):
                await q.message.reply_text(f'{job["filename"]} · {job["size"]/1_000_000:.1f} MB\n{job["source"]}', disable_web_page_preview=True)
            return
        if action == 'back':
            self.clear_prompt(context)
            return await self.panel_back(q.message, context)
        if action.startswith('results:'):
            try: key = int(action.split(':')[1])
            except ValueError: return
            result = self.store.cached(key, 'search_panel')
            if not result:
                return await self.show_panel(q.message, 'Results expired. Send a movie title to search again.', reply_markup=Markup([navigation()]))
            rows = [[Button(label, callback_data=data)] for label, data in result['buttons']]
            rows.append(navigation())
            self.panel().results.pop(key, None)
            return await self.show_panel(q.message, result['text'], reply_markup=Markup(rows))
        if action == 'cancel':
            self.discard_current_confirmation()
            self.clear_prompt(context)
            self.panel().prompt.clear()
            return await self.cancel_menu(q.message)
        if action == 'main':
            self.clear_prompt(context)
            await self.main_menu(q.message);return
        if action in ('sourcehistory', 'sourcehistorytoggle'):
            if update.effective_user.id != self.owner_id:
                return await self.show_panel(q.message, 'Only the owner can manage source history.')
            from .source_history import init_history
            init_history(self.store)
            if action == 'sourcehistorytoggle':
                enabled = self.store.setting('idle_source_checks', '1') == '1'
                self.store.set_setting('idle_source_checks', '0' if enabled else '1')
            enabled = self.store.setting('idle_source_checks', '1') == '1'
            history = self.store.db.execute('SELECT host,ready,manual,temporary,unavailable FROM source_history ORDER BY updated DESC LIMIT 15').fetchall()
            lines = [f'{row["host"]}: {row["ready"]} ready, {row["manual"]} manual, {row["temporary"]} temporary, {row["unavailable"]} unavailable' for row in history]
            rows = [[Button('Stop idle checks' if enabled else 'Start idle checks', callback_data='menu:sourcehistorytoggle')], navigation()]
            return await self.show_panel(q.message, 'Source history · idle checks ' + ('on' if enabled else 'off') + '\n' + ('\n'.join(lines) or 'No checks recorded yet.'), reply_markup=Markup(rows))
        if action == 'catalogues': return await self.catalogues_menu(q.message)
        if action.startswith('cataloguespage:'):
            try: page = int(action.split(':')[1])
            except ValueError: return
            return await self.catalogues_menu(q.message, page)
        if action == 'settings': return await self.settings_menu(q.message)
        if action == 'daily': return await self.daily_menu(q.message, context)
        if action == 'active': return await self.active_menu(q.message)
        if action == 'canceltransfer': return await self.cancel_menu(q.message)
        if action == 'admins': return await self.admin_menu(q.message, update.effective_user.id)
        if action == 'addadmin':
            if update.effective_user.id != self.owner_id:
                await self.show_panel(q.message, 'Only the owner can manage admins.');return
            self.clear_prompt(context);context.user_data['admin_add_pending']=True
            await self.show_panel(q.message, 'Send the numeric Telegram user ID.',reply_markup=Markup([navigation()]));return
        if action == 'toggle':
            self.set_transfers(not self.transfers_enabled())
            return await self.main_menu(q.message)
        self.clear_prompt(context)
        proxy=SimpleNamespace(message=q.message,effective_user=update.effective_user,effective_chat=update.effective_chat)
        if action == 'download':
            text='Send a direct HTTP(S) media link, magnet link, or .torrent document.'
            await self.show_panel(q.message, text,reply_markup=Markup([navigation()]));return
        if action == 'library':
            context.user_data['search_pending']=True
            await self.show_panel(q.message, 'Send a title to search your published library.',reply_markup=Markup([navigation()]));return
        if action == 'addmanifest': return await self.add_manifest_prompt(proxy,context)
        if action == 'migration': return await self.migration_menu(proxy,context)
        if action == 'addons': return await self.addons(proxy,context)
        if action == 'saved': return await self.saved(proxy,context)
        if action == 'pending': return await self.pending(proxy,context)
        if action == 'channels': return await self.destination_menu(context)
