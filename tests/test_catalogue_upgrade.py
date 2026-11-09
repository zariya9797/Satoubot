import hashlib

from satoubot.discovery import Discovery
from satoubot.store import Store


def test_upgrade_removes_retired_api_and_preserves_catalogues_and_downloads(tmp_path):
    path = tmp_path / 'db'
    store = Store(path)
    service = Discovery(store)
    api = 'http://127.0.0.1:3000/'
    catalogue = 'https://catalogue.test/manifest.json'
    service.register(catalogue, {'name': 'Catalogue', 'catalogs': []})
    service.toggle(catalogue)
    store.db.execute('INSERT INTO manifests VALUES (?,?,?)', (api, 'animepahe', '{}'))
    api_switch = 'enabled:' + hashlib.sha256(api.encode()).hexdigest()
    store.set_setting(api_switch, '0')
    store.set_setting('animepahe_default_base', api)
    old_movie = {'id': 'animepahe:old', 'animepahe_session': 'old', 'addon': api}
    old_movie_key = store.cache('movie', old_movie)
    old_series_key = store.cache('series', dict(old_movie, videos=[]))
    old_confirmation = store.cache('confirmation', {'movie': old_movie})
    old_stream = store.cache('stream', {'url': 'https://file.test/old.mp4', 'movie': old_movie})
    movie = {'id': 'tt123', 'addon': catalogue, 'name': 'animepahe_example'}
    movie_key = store.cache('movie', movie)
    stream = {'url': 'https://file.test/kept.mp4', 'movie': movie}
    stream_key = store.cache('stream', stream)
    job = store.create('https://file.test/old.mp4', 'Downloaded', 'old.mp4', 100)
    store.update(job, state='local_only', path='/media/old.mp4')
    store.db.close()

    for _ in range(2):
        store = Store(path)
        assert [(url, kind) for url, kind, _ in Discovery(store).manifests(active=False)] == [(catalogue, 'addon')]
        assert not Discovery(store).enabled(catalogue)
        assert store.setting(api_switch) is None
        assert store.setting('animepahe_default_base') is None
        assert store.cached(old_movie_key, 'movie') is None
        assert store.cached(old_series_key, 'series') is None
        assert store.cached(old_confirmation, 'confirmation') is None
        assert store.cached(old_stream, 'stream') is None
        assert store.cached(movie_key, 'movie') == movie
        assert store.cached(stream_key, 'stream') == stream
        assert store.get(job)['state'] == 'local_only'
        assert store.get(job)['path'] == '/media/old.mp4'
        store.db.close()
