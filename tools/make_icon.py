"""Podcasts icon in the Wizard launcher style (same recipe as wizard-app-index tools/make_icons.py).

uv run -q --with resvg-py --with pillow python3 tools/make_icon.py
Writes icon.png (512 px), icon.svg and runtipi/metadata/logo.jpg.
"""
import io
from pathlib import Path

import resvg_py
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent


def stroke(w=22):
    return f'fill="none" stroke="#fff" stroke-width="{w}" stroke-linecap="round" stroke-linejoin="round"'


COLORS = ['#f472b6', '#9333ea']
GLYPH = f'''
  <rect x="214" y="118" width="84" height="164" rx="42" fill="#fff"/>
  <path d="M174 238 a82 82 0 0 0 164 0" {stroke(22)}/>
  <path d="M256 322 V380 M212 386 H300" {stroke(22)}/>
  <path d="M146 168 a150 150 0 0 0 0 150 M366 168 a150 150 0 0 1 0 150" {stroke(18)} opacity=".85"/>'''


def svg():
    a, b = COLORS
    return f'''<svg xmlns="http://www.w3.org/2000/svg" width="512" height="512" viewBox="0 0 512 512">
  <defs>
    <linearGradient id="g" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="{a}"/><stop offset="1" stop-color="{b}"/></linearGradient>
    <radialGradient id="glow" cx=".3" cy=".2" r=".9"><stop offset="0" stop-color="#fff" stop-opacity=".28"/><stop offset=".6" stop-color="#fff" stop-opacity="0"/></radialGradient>
  </defs>
  <rect width="512" height="512" rx="112" fill="url(#g)"/>
  <rect width="512" height="512" rx="112" fill="url(#glow)"/>
  <rect x="3" y="3" width="506" height="506" rx="109" fill="none" stroke="#fff" stroke-opacity=".18" stroke-width="6"/>
  {GLYPH}
</svg>'''


if __name__ == '__main__':
    png = bytes(resvg_py.svg_to_bytes(svg_string=svg(), width=512, height=512))
    (ROOT / 'icon.png').write_bytes(png)
    (ROOT / 'icon.svg').write_text(svg())
    img = Image.open(io.BytesIO(png)).convert('RGBA')
    bg = Image.new('RGB', img.size, (5, 3, 12))
    bg.paste(img, mask=img.split()[3])
    bg.save(ROOT / 'runtipi' / 'metadata' / 'logo.jpg', 'JPEG', quality=92)
    print('icon.png', len(png), 'bytes')
