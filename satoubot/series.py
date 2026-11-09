"""Catalogue episode navigation; file choices stay scoped to a single episode."""
from telegram import InlineKeyboardButton as Button, InlineKeyboardMarkup as Markup
from .menu import navigation
from .discovery import Discovery


def ordered_episodes(movie):
    episodes, seen = [], set()
    for video in movie.get('videos', []):
        if not isinstance(video, dict): continue
        season, episode = video.get('season'), video.get('episode')
        if isinstance(season, bool) or isinstance(episode, bool): continue
        try:
            if str(int(season)) != str(season) or str(int(episode)) != str(episode): continue
            season, episode = int(season), int(episode)
        except (TypeError, ValueError): continue
        if season < 0 or episode < 1 or (season, episode) in seen: continue
        seen.add((season, episode))
        episodes.append(dict(video, season=season, episode=episode))
    return sorted(episodes, key=lambda v: (v['season'], v['episode']))


class SeriesFunctions:
    async def series_menu(self, context, movie, season=None, page=0):
        videos = ordered_episodes(movie)
        key = self.store.cache('series', movie)
        if season is None:
            choices = [(f'Season {s}' if s else 'Specials', f'season:{key}:{s}:0') for s in sorted({v['season'] for v in videos})]
            title = f'{movie["name"]}\nChoose a season.'
            prefix = 'seasons'
        else:
            choices = []
            base = {k: v for k, v in movie.items() if k != 'videos'}
            for video in videos:
                if video['season'] != season: continue
                selected = dict(base, season=season, episode=video['episode'], embedded_streams=video.get('streams') or [], video_id=video.get('id') or f'{movie["id"]}:{season}:{video["episode"]}')
                item = self.store.cache('movie', selected)
                label = f'S{season:02d}E{video["episode"]:02d}' + f' · {video.get("title") or video.get("name") or "Episode"}'
                choices.append((label[:100], f'movie:{item}'))
            title = f'{movie["name"]} · ' + f'Season {season}' + '\nChoose an episode; files are checked for that episode only.'
            prefix = f'season:{key}:{season}'
        page = max(0, min(page, max(0, (len(choices)-1)//8)))
        rows = [[Button(label, callback_data=data)] for label, data in choices[page*8:page*8+8]]
        paging = []
        route = f'seasons:{key}' if season is None else prefix
        if page: paging.append(Button('Previous', callback_data=f'{route}:{page-1}'))
        if (page+1)*8 < len(choices): paging.append(Button('More', callback_data=f'{route}:{page+1}'))
        if paging: rows.append(paging)
        if not videos: title += '\nThis catalogue did not supply season/episode metadata. Try another enabled catalogue.'
        rows.append(navigation())
        await self.show_panel(None, title, reply_markup=Markup(rows), context=context)

    async def series_callback(self, update, context):
        parts = update.callback_query.data.split(':')
        try:
            movie = self.store.cached(int(parts[1]), 'series')
            season = int(parts[2]) if parts[0] == 'season' else None
            page = int(parts[3] if season is not None else parts[2])
        except (ValueError, IndexError): return
        if not movie:
            return await self.show_panel(update.callback_query.message, 'Episodes expired. Search the title again.')
        await self.series_menu(context, movie, season, page)
