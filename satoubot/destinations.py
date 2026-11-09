from telegram import InlineKeyboardButton as Button, InlineKeyboardMarkup as Markup
from telegram.error import TelegramError
from telethon.utils import get_peer_id
from .menu import navigation


class DestinationFunctions:
    async def destination_menu(self, context, job_id=0):
        rows=[]
        try:
            dialogs=await self.uploader.get_dialogs()
            for dialog in dialogs:
                entity=dialog.entity
                if not getattr(entity,'creator',False): continue
                chat_id=get_peer_id(entity)
                try:
                    member=await context.bot.get_chat_member(chat_id,context.bot.id)
                    if member.status!='administrator': continue
                    if getattr(entity,'broadcast',False) and not member.can_post_messages: continue
                except Exception:
                    continue
                key=self.store.cache('destination',{'target':chat_id,'job':job_id,'title':dialog.name})
                rows.append([Button(dialog.name[:80],callback_data=f'dest:{key}')])
        except Exception:
            await self.show_panel(None, 'Could not list your channels. Check the user session and try again.', context=context, reply_markup=Markup([navigation()]));return
        rows.append(navigation())
        await self.show_panel(None, 'Select a channel or group you own. Only chats where the bot is an administrator are listed. Add the bot as admin and retry if yours is missing.',reply_markup=Markup(rows), context=context)

    async def select_destination(self, update, context):
        q=update.callback_query
        if q.data=='destcancel':
            return await self.main_menu(q.message)
        if q.data.startswith('choose_dest:'):
            try: job_id=int(q.data.split(':')[1])
            except ValueError:return
            await self.destination_menu(context,job_id);return
        try: key=int(q.data.split(':')[1])
        except ValueError:return
        destination=self.store.cached(key,'destination')
        if not destination:
            await q.message.reply_text('Selection expired. Open the channel menu again.');return
        try:
            member=await context.bot.get_chat_member(destination['target'],context.bot.id)
        except TelegramError:
            await q.message.reply_text('Could not check the bot’s access to this chat. Check its administrator access and try again.')
            return
        if member.status!='administrator':
            await q.message.reply_text('Make the bot an administrator before selecting this chat.');return
        self.store.set_setting('target',destination['target'])
        await self.show_panel(q.message, 'Destination selected: '+destination['title'])
        job_id=destination['job']
        if job_id and self.store.get(job_id) and self.store.get(job_id)['state']=='review':
            await self.preview(context,job_id)
