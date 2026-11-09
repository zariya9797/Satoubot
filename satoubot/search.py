from telegram import InlineKeyboardButton, InlineKeyboardMarkup

class SearchFunctions:
    async def saved(self, update, context, page=0):
        if not self.authorized(update): return
        import json
        from .menu import navigation
        rows = self.store.db.execute("SELECT * FROM jobs WHERE state IN ('oversize','reported','link_only','local_only') ORDER BY id DESC LIMIT 20").fetchall()
        sources = self.store.db.execute('SELECT id,payload FROM source_links ORDER BY id DESC LIMIT 20').fetchall()
        choices = []
        for row in rows:
            choices.append((f'#{row["id"]} {row["filename"]} · {row["size"]/1_000_000:.1f} MB', f'menu:savedjob:{row["id"]}'))
        for source in sources:
            item = json.loads(source['payload'])
            size = item.get('size')
            size_text = f'{size/1_000_000:.1f} MB' if isinstance(size, (int, float)) else 'size unknown'
            choices.append((f'{item.get("movie", {}).get("name", "File")} | {item.get("quality") or "quality unknown"} | {size_text}', f'file:{source["id"]}'))
        page = max(0, min(page, max(0, (len(choices)-1)//8)))
        buttons = [[InlineKeyboardButton(label[:100], callback_data=data)] for label, data in choices[page*8:page*8+8]]
        paging = []
        if page: paging.append(InlineKeyboardButton('Previous', callback_data=f'menu:savedpage:{page-1}'))
        if (page+1)*8 < len(choices): paging.append(InlineKeyboardButton('More', callback_data=f'menu:savedpage:{page+1}'))
        if paging: buttons.append(paging)
        buttons.append(navigation())
        await self.show_panel(update.message, f'Saved files / links · Page {page+1}' if choices else 'No saved files or links.', reply_markup=InlineKeyboardMarkup(buttons))

    async def find(self, update, context):
        if not self.authorized(update): return
        query = ' '.join(context.args).strip()
        await self.search_query(update, query)

    async def search_query(self, update, query):
        if not query:
            await self.show_panel(update.message, 'Use /find <title>.'); return
        rows = self.store.search(query)
        text = '\n'.join(f'#{r["id"]} {r["title"]} · {r["size"]/1_000_000:.1f} MB · message {r["telegram_message_id"]}' for r in rows)
        await self.show_panel(update.message, text or 'No published matches found.')
