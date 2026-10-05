"""One-off: self-host the Google Fonts used by the web UI (IBM Plex Sans KR, IBM Plex Mono, Instrument Serif).

Run once:  python fetch_fonts.py
Downloads every woff2 slice listed by the Google Fonts CSS API into web/fonts/ and writes web/css/fonts.css
pointing at them, so the UI needs no third-party requests (CSP: font-src 'self'). Re-run to refresh.
"""
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests

ROOT = Path(__file__).parent
FONT_DIR, CSS_OUT = ROOT / "web" / "fonts", ROOT / "web" / "css" / "fonts.css"
CSS_URL = ("https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500&family=IBM+Plex+Sans+KR:wght@400;500;600"
           "&family=Instrument+Serif:ital@0;1&display=swap")
# a modern UA makes the API return woff2 slices (with unicode-range) rather than a single ttf
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
GSTATIC = re.compile(r"url\((https://fonts\.gstatic\.com/[^)]+\.woff2)\)")


def main() -> int:
    css = requests.get(CSS_URL, headers={"User-Agent": UA}, timeout=30)
    css.raise_for_status()
    text, jobs = css.text, {}
    for m in re.finditer(r"font-family: '([^']+)';.*?src: " + GSTATIC.pattern, text, re.S):
        jobs[m.group(2)] = re.sub(r"\W+", "", m.group(1)).lower() + "-" + m.group(2).rsplit("/", 1)[-1]
    FONT_DIR.mkdir(parents=True, exist_ok=True)

    def get(item):
        url, name = item
        dest = FONT_DIR / name
        if not dest.exists():
            r = requests.get(url, headers={"User-Agent": UA}, timeout=60)
            r.raise_for_status()
            dest.write_bytes(r.content)

    with ThreadPoolExecutor(8) as ex:
        list(ex.map(get, jobs.items()))
    out = GSTATIC.sub(lambda m: f"url(../fonts/{jobs[m.group(1)]})", text)
    CSS_OUT.write_text(out, encoding="utf-8")
    total = sum(f.stat().st_size for f in FONT_DIR.glob("*.woff2"))
    print(f"{len(jobs)} font files ({total / 1e6:.1f} MB) -> {FONT_DIR}; css -> {CSS_OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
