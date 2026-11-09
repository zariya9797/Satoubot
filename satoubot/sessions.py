from contextlib import contextmanager
import fcntl

from telethon import TelegramClient


@contextmanager
def single_instance(data_dir):
    """Hold an OS lock before either SQLite database is opened."""
    with (data_dir / 'bot.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit(
                'SatouBot is already running with this DATA_DIR. '
                'Stop satoubot.service or the other bot process before starting it again.'
            ) from None
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


class SessionManager:
    """Own the user session; the Telegram application owns the bot session."""

    def __init__(self, data_dir, api_id, api_hash):
        self.user = TelegramClient(str(data_dir / 'user'), api_id, api_hash)

    async def start(self):
        await self.user.start()
        await self.user.get_dialogs()

    async def stop(self):
        await self.user.disconnect()
