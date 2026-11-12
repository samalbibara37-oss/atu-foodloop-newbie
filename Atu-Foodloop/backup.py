"""Create a consistent SQLite backup while the application is running."""
from pathlib import Path
from datetime import datetime
import os,sqlite3
root=Path(__file__).resolve().parent
source=Path(os.environ.get('FOODLOOP_DB',root/'instance'/'foodloop.sqlite3'))
if not source.is_file():raise SystemExit('База не найдена. Сначала запустите приложение.')
target=root/'instance'/'backups'/f'foodloop-{datetime.now():%Y%m%d-%H%M%S}.sqlite3'
target.parent.mkdir(parents=True,exist_ok=True)
with sqlite3.connect(source) as src,sqlite3.connect(target) as dst:src.backup(dst)
print('Резервная копия:',target)
