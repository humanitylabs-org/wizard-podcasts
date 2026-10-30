import math
import os
import sqlite3
import struct
import sys
import time
import wave
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / 'app'))
import podcasts as P  # noqa: E402  # pyright: ignore[reportMissingImports]

P.init()
now = int(time.time())
audio = P.DATA / 'episodes' / '1' / '101.wav'
audio.parent.mkdir(parents=True, exist_ok=True)
with wave.open(str(audio), 'wb') as out:
    out.setnchannels(1)
    out.setsampwidth(2)
    out.setframerate(8000)
    frames = (struct.pack('<h', int(math.sin(i / 8000 * math.pi * 440) * 1200)) for i in range(16000))
    out.writeframes(b''.join(frames))

with P.connect() as db:
    db.execute('DELETE FROM episodes')
    db.execute('DELETE FROM shows')
    db.execute(
        'INSERT INTO shows(id, feed_url, title, author, added_at, last_checked) VALUES(?,?,?,?,?,?)',
        (1, 'https://example.com/feed.xml', 'The Tim Ferriss Show', 'Tim Ferriss', now, now),
    )
    db.execute(
        '''INSERT INTO episodes(
             id, show_id, guid, title, published, duration, enclosure_url, mime, length,
             notes, first_seen, inbox, download, file, file_size, downloaded_at
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
        (
            101, 1, 'fixture-101', 'A real episode to play', now - 120, 2,
            'https://example.com/episode.wav', 'audio/wav', audio.stat().st_size,
            '<p>Episode notes.</p>', now, 1, 'done', 'episodes/1/101.wav', audio.stat().st_size, now,
        ),
    )
print(P.DB_PATH)
