"""Temporary menu panels. Durable movie and transfer messages never enter this registry."""
import asyncio
import time
from dataclasses import dataclass, field

from telegram import InlineKeyboardButton as Button, InlineKeyboardMarkup as Markup
from telegram.error import BadRequest, TelegramError
from .task_context import transfer_id


@dataclass
class Panel:
    message: object = None
    card: object = None
    preview: object = None
    generation: int = 0
    disable_preview: bool = False
    text: str = ''
    markup: object = None
    updated: float = field(default_factory=time.monotonic)
    pending: dict = field(default_factory=dict)
    tasks: dict = field(default_factory=dict)
    task_daily: dict = field(default_factory=dict)
    results: dict = field(default_factory=dict)
    retired: list = field(default_factory=list)
    history: list = field(default_factory=list)
    prompt: dict = field(default_factory=dict)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class InterfaceFunctions:
    def panel(self):
        if not hasattr(self, 'panels'):
            self.panels = {}
        return self.panels.setdefault(self.admin_id, Panel())

    def touch_panel(self, context):
        self.last_user_activity = time.monotonic()
        task = getattr(self, 'idle_check_task', None)
        if task and not task.done(): task.cancel()
        panel = self.panel()
        if time.monotonic() - panel.updated >= 300 and not panel.pending:
            self.clear_prompt(context)
        panel.updated = time.monotonic()
        keys = ('manifest_pending', 'uploaded_manifest', 'catalogue_search_pending',
                'search_pending', 'admin_add_pending', 'thumbnail_request')
        panel.prompt = {key: context.user_data[key] for key in keys if key in context.user_data}

    async def show_panel(self, message, text, reply_markup=None, *, context=None, remember=True, expected_generation=None, disable_preview=False):
        if reply_markup is None:
            from .menu import navigation
            reply_markup = Markup([navigation()])
        panel = self.panel()
        async with panel.lock:
            if expected_generation is not None and panel.generation != expected_generation:
                return False
            if remember and panel.card is not None:
                card = message if self.same_message(panel.card, message) else panel.card
                photos = getattr(card, 'photo', None) or []
                snapshot = {'card': True, 'photo': photos[-1].file_id if photos else None,
                            'text': card.caption if photos else card.text}
                panel.history.append((snapshot, card.reply_markup, panel.prompt.copy(), False))
                panel.history = panel.history[-10:]
            elif remember and panel.text and (panel.text, panel.markup) != (text, reply_markup):
                panel.history.append((panel.text, panel.markup, panel.prompt.copy(), panel.disable_preview))
                panel.history = panel.history[-10:]
            panel.text, panel.markup, panel.disable_preview = text, reply_markup, disable_preview
            await self._render_panel(panel, message, context, fresh=True)

    async def panel_back(self, message, context):
        panel = self.panel()
        if not panel.history:
            return await self.main_menu(message)
        text, markup, prompt, disable_preview = panel.history.pop()
        context.user_data.update(prompt)
        if isinstance(text, dict) and text.get('card'):
            async with panel.lock:
                if text['photo']:
                    card = await context.bot.send_photo(self.admin_id, photo=text['photo'], caption=text['text'], reply_markup=markup)
                else:
                    card = await context.bot.send_message(self.admin_id, text['text'], reply_markup=markup)
                old_message, old_card, old_preview = panel.message, panel.card, panel.preview
                panel.message, panel.card, panel.preview = None, card, None
                panel.generation += 1
                if old_message is not card: await self._retire_message(panel, old_message)
                if old_card is not old_message and old_card is not card: await self._retire_message(panel, old_card)
                if old_preview is not old_message and old_preview is not old_card and old_preview is not card: await self._retire_message(panel, old_preview)
            return
        await self.show_panel(message, text, markup, remember=False, disable_preview=disable_preview)

    async def panel_status(self, context, key, label=None):
        panel = self.panel()
        ident = transfer_id.get()
        if key == 'transfer' and ident is not None: key = f'transfer:{ident}'
        async with panel.lock:
            if label:
                panel.pending[key] = label
            else:
                panel.pending.pop(key, None)
            if panel.message is not None:
                try:
                    await self._render_panel(panel, context=context)
                except TelegramError:
                    pass

    @staticmethod
    def same_message(left, right):
        if left is right: return left is not None
        if left is None or right is None: return False
        left_id, right_id = getattr(left, 'message_id', None), getattr(right, 'message_id', None)
        left_chat, right_chat = getattr(left, 'chat_id', None), getattr(right, 'chat_id', None)
        return (isinstance(left_id, int) and isinstance(right_id, int)
                and isinstance(left_chat, int) and isinstance(right_chat, int)
                and (left_chat, left_id) == (right_chat, right_id))

    async def _retire_message(self, panel, message):
        if message is None: return
        try:
            await message.delete()
        except TelegramError:
            try: await message.edit_reply_markup(reply_markup=None)
            except TelegramError: pass
            panel.retired.append(message)
            panel.retired = panel.retired[-10:]

    async def _render_panel(self, panel, message=None, context=None, fresh=False):
        text = panel.text
        if panel.pending:
            text += '\n\nIn progress:\n' + '\n'.join('• ' + value for value in panel.pending.values())
        rows = list(panel.markup.inline_keyboard) if panel.markup else []
        for key, title in panel.results.items():
            rows.append([Button(title, callback_data=f'menu:results:{key}')])
        if panel.pending and not any(b.callback_data == 'menu:main' for row in rows for b in row):
            rows.append([Button('Main menu', callback_data='menu:main')])
        markup = Markup(rows) if rows else None
        preview_options = {'disable_web_page_preview': True} if panel.disable_preview else {}
        if panel.message is not None and not fresh:
            try:
                await panel.message.edit_text(text, reply_markup=markup, **preview_options)
                return
            except BadRequest as exc:
                if 'message is not modified' in str(exc).lower():
                    return
            except TelegramError:
                # Keep the old reference for a later best-effort cleanup.
                pass
            panel.retired.append(panel.message)
            panel.message = None
        previous = panel.message
        card = panel.card
        preview = panel.preview
        if message is not None:
            new_message = await message.reply_text(text, reply_markup=markup, do_quote=False, **preview_options)
        elif context is not None:
            new_message = await context.bot.send_message(self.admin_id, text, reply_markup=markup, **preview_options)
        else:
            return
        panel.message, panel.card, panel.preview = new_message, None, None
        if fresh: panel.generation += 1
        if previous is not new_message: await self._retire_message(panel, previous)
        if card is not previous and card is not new_message: await self._retire_message(panel, card)
        if preview is not previous and preview is not card and preview is not new_message: await self._retire_message(panel, preview)

    async def register_preview(self, message):
        panel = self.panel()
        async with panel.lock:
            old = (panel.message, panel.card, panel.preview)
            if panel.message is not None and panel.text:
                panel.history.append((panel.text, panel.markup, panel.prompt.copy(), panel.disable_preview))
                panel.history = panel.history[-10:]
            panel.message, panel.card, panel.preview = None, None, message
            panel.generation += 1
            for previous in old:
                if previous is not None and previous is not message:
                    await self._retire_message(panel, previous)

    async def panel_task(self, key, factory, context):
        """Expose work beneath whichever menu is currently open, without blocking navigation."""
        panel = self.panel()
        try:
            if key == 'movie':
                # Preserve one provider lookup across admins while menus and link checks run.
                if not hasattr(self, 'provider_lookup_lock'):
                    self.provider_lookup_lock = asyncio.Lock()
                async with self.provider_lookup_lock:
                    await factory()
            else:
                await factory()
        finally:
            async with panel.lock:
                panel.tasks.pop(key, None)
                panel.task_daily.pop(key, None)
                panel.pending.pop(key, None)
                if panel.message is not None:
                    try:
                        await self._render_panel(panel, context=context)
                    except TelegramError:
                        pass

    async def start_panel_task(self, key, label, factory, context, *, daily_identity=None):
        panel = self.panel()
        if key in panel.pending:
            async with panel.lock:
                await self._render_panel(panel, context=context)
            return
        panel.pending[key] = label
        if daily_identity: panel.task_daily[key] = daily_identity
        if not panel.text:
            panel.text = 'Choose another action while this request finishes.'
        try:
            async with panel.lock:
                await self._render_panel(panel, context=context)
        except BaseException:
            panel.pending.pop(key, None)
            panel.task_daily.pop(key, None)
            raise
        panel.tasks[key] = self.launch_discovery(self.panel_task(key, factory, context))

    async def cancel_panel_tasks(self):
        """Cancel this admin's temporary lookups, leaving transfer tasks untouched."""
        panel = self.panel()
        tasks = [task for task in panel.tasks.values() if not task.done()]
        for task in tasks: task.cancel()
        if tasks: await asyncio.gather(*tasks, return_exceptions=True)
        for key, task in list(panel.tasks.items()):
            if task.done():
                panel.tasks.pop(key, None)
                panel.task_daily.pop(key, None)
                panel.pending.pop(key, None)
        return len(tasks)

    async def cleanup_panels(self, now=None):
        now = time.monotonic() if now is None else now
        for panel in list(getattr(self, 'panels', {}).values()):
            async with panel.lock:
                if now - panel.updated < 300 or panel.pending:
                    continue
                remaining = []
                for message in panel.retired + ([panel.message] if panel.message is not None else []) + ([panel.card] if panel.card is not None else []) + ([panel.preview] if panel.preview is not None else []):
                    try:
                        await message.delete()
                    except TelegramError:
                        # Telegram can deny deletion; at least deactivate the old menu.
                        try:
                            await message.edit_reply_markup(reply_markup=None)
                        except TelegramError:
                            pass
                        remaining.append(message)
                panel.message = None
                panel.card = None
                panel.preview = None
                panel.generation += 1
                panel.retired = remaining[-2:]
                panel.text, panel.markup = '', None
                panel.results.clear()
                panel.history.clear()

    async def panel_cleaner(self):
        while True:
            await asyncio.sleep(15)
            await self.cleanup_panels()
