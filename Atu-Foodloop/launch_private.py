"""Start an isolated private database with a password entered in the terminal."""
import getpass
import os
from pathlib import Path
import sqlite3
import subprocess
import sys


def needs_password(path):
    if not path.exists():
        return True
    try:
        with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True) as db:
            return db.execute('SELECT 1 FROM users LIMIT 1').fetchone() is None
    except sqlite3.OperationalError:
        return True


def main():
    root = Path(__file__).resolve().parent
    database = root / 'instance' / 'private.sqlite3'
    env = os.environ.copy()
    if needs_password(database):
        print('Create your admin password. Minimum 12 characters. Input is hidden.')
        while True:
            password = getpass.getpass('New password: ')
            if len(password) < 12:
                print('Use at least 12 characters. Try again.')
                continue
            if password != getpass.getpass('Repeat password: '):
                print('Passwords do not match. Try again.')
                continue
            env['ADMIN_PASSWORD'] = password
            break
    try:
        return subprocess.call([sys.executable, str(root / 'server.py'), '--db',
                                str(database), '--host', '0.0.0.0'], cwd=root, env=env)
    except KeyboardInterrupt:
        return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (KeyboardInterrupt, EOFError):
        print('\nCancelled.')
        sys.exit(0)
