import hashlib
import sqlite3
from pathlib import Path


class Store:
    def __init__(self, path: Path):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS admins (user_id INTEGER PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS source_links (id INTEGER PRIMARY KEY AUTOINCREMENT, fingerprint TEXT UNIQUE NOT NULL, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS manifests (url TEXT PRIMARY KEY, kind TEXT NOT NULL, data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS discovery_cache (id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, payload TEXT NOT NULL, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
            DELETE FROM discovery_cache WHERE created_at < datetime('now', '-1 hour');
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS jobs (
                id INTEGER PRIMARY KEY, source TEXT NOT NULL, title TEXT NOT NULL,
                filename TEXT NOT NULL, size INTEGER NOT NULL, path TEXT,
                thumbnail TEXT, state TEXT NOT NULL, target TEXT,
                telegram_message_id INTEGER, error TEXT, approval_token TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS jobs_source ON jobs(source);
        ''')
        # Retire the removed built-in API on existing installations as well.
        for row in self.db.execute("SELECT url FROM manifests WHERE kind='animepahe'").fetchall():
            key = 'enabled:' + hashlib.sha256(row['url'].encode()).hexdigest()
            self.db.execute('DELETE FROM settings WHERE key=?', (key,))
        self.db.execute("DELETE FROM manifests WHERE kind='animepahe'")
        self.db.execute("DELETE FROM settings WHERE key='animepahe_default_base'")
        self.db.execute("""DELETE FROM discovery_cache
            WHERE json_extract(payload, '$.animepahe_session') IS NOT NULL
               OR json_extract(payload, '$.movie.animepahe_session') IS NOT NULL""")
        self.db.execute("DELETE FROM source_links WHERE json_extract(payload, '$.movie.animepahe_session') IS NOT NULL")
        self.db.commit()

    def setting(self, key, default=None):
        row = self.db.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
        return row['value'] if row else default

    def set_setting(self, key, value):
        self.db.execute('INSERT OR REPLACE INTO settings VALUES (?,?)', (key, str(value)))
        self.db.commit()

    def create(self, source, title, filename, size):
        cursor = self.db.execute('INSERT INTO jobs(source,title,filename,size,state) VALUES (?,?,?,?,?)',
                                 (source,title,filename,size,'ready'))
        self.db.commit()
        return cursor.lastrowid

    def get(self, job_id):
        return self.db.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()

    def update(self, job_id, **changes):
        allowed = {'title','filename','size','path','thumbnail','state','target','telegram_message_id','error','approval_token'}
        if not changes or not set(changes).issubset(allowed):
            raise ValueError('invalid update')
        fields = ','.join(f'{key}=?' for key in changes)
        self.db.execute(f'UPDATE jobs SET {fields} WHERE id=?', (*changes.values(), job_id))
        self.db.commit()

    def find_source(self, source):
        return self.db.execute("SELECT * FROM jobs WHERE source=? AND state NOT IN ('failed','cancelled') ORDER BY id DESC LIMIT 1", (source,)).fetchone()

    def search(self, query):
        return self.db.execute("SELECT * FROM jobs WHERE state='published' AND title LIKE ? ORDER BY title LIMIT 20", ('%' + query + '%',)).fetchall()

    def cache(self, kind, payload):
        import json
        if kind == 'stream':
            import hashlib
            identity = payload.get('identity') or payload['url']
            fingerprint = hashlib.sha256(identity.encode()).hexdigest()
            row = self.db.execute("SELECT id,payload FROM source_links WHERE fingerprint=? OR json_extract(payload, '$.url')=? ORDER BY (fingerprint=?) DESC LIMIT 1",(fingerprint,payload['url'],fingerprint)).fetchone()
            if row:
                old=json.loads(row['payload'])
                payload['mirrors']=[*old.get('mirrors',[]), {'url':old['url'],'headers':old.get('headers',{})}]
                payload['alternatives']=list(dict.fromkeys([*old.get('alternatives',[]),old['url'],*payload.get('alternatives',[])]))
                self.db.execute('UPDATE source_links SET fingerprint=?,payload=? WHERE id=?',(fingerprint,json.dumps(payload),row['id']))
                self.db.commit()
                return row['id']
            cursor=self.db.execute('INSERT INTO source_links(fingerprint,payload) VALUES (?,?)',(fingerprint,json.dumps(payload)))
            self.db.commit()
            return cursor.lastrowid
        cursor = self.db.execute('INSERT INTO discovery_cache(kind,payload) VALUES (?,?)', (kind, json.dumps(payload)))
        self.db.commit()
        return cursor.lastrowid

    def cached(self, key, kind):
        import json
        if kind == 'stream':
            row=self.db.execute('SELECT payload FROM source_links WHERE id=?',(key,)).fetchone()
            return json.loads(row['payload']) if row else None
        row = self.db.execute("SELECT payload FROM discovery_cache WHERE id=? AND kind=? AND created_at > datetime('now', '-1 hour')", (key, kind)).fetchone()
        return json.loads(row['payload']) if row else None
