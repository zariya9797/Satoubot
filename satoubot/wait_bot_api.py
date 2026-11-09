"""Wait for the configured Telegram Bot API TCP endpoint before bot startup."""
import os
import socket
import time
from urllib.parse import urlsplit

from dotenv import load_dotenv


def wait_for_api(timeout=90, interval=1):
    load_dotenv()
    try:
        url = urlsplit(os.getenv('BOT_API_URL', 'http://127.0.0.1:8081').rstrip('/'))
        if url.scheme not in ('http', 'https') or not url.hostname:
            raise ValueError('BOT_API_URL must have an HTTP(S) host')
        host, port = url.hostname, url.port or (443 if url.scheme == 'https' else 80)
    except ValueError:
        print('Invalid BOT_API_URL; check .env', flush=True)
        return False
    deadline = time.monotonic() + timeout
    print(f'Waiting for Telegram Bot API at {host}:{port}', flush=True)
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            print('Telegram Bot API did not accept connections; check satoubot-api.service', flush=True)
            return False
        try:
            with socket.create_connection((host, port), timeout=min(2, remaining)):
                print('Telegram Bot API is accepting connections', flush=True)
                return True
        except OSError:
            time.sleep(min(interval, max(0, deadline - time.monotonic())))


def main():
    raise SystemExit(0 if wait_for_api() else 1)


if __name__ == '__main__':
    main()
