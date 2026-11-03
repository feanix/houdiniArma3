"""Fetch a SideFX docs page and print it as plain text (stdlib only).

Usage: python fetch_doc.py <path under https://www.sidefx.com/docs/houdini/>
Example: python fetch_doc.py licensing/limited_commercial.html
"""
import html
import io
import re
import sys
import urllib.request

BASE = "https://www.sidefx.com/docs/houdini/"


def main():
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    url = BASE + sys.argv[1].lstrip("/")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    raw = urllib.request.urlopen(req, timeout=30).read().decode("utf-8", "replace")
    raw = re.sub(r"(?is)<(script|style|nav|header|footer)\b.*?</\1>", "", raw)
    raw = re.sub(r"(?i)<br\s*/?>|</(p|div|li|h\d|tr|pre)>", "\n", raw)
    text = html.unescape(re.sub(r"<[^>]+>", "", raw))
    text = re.sub(r"\n\s*\n+", "\n\n", text)
    print("SOURCE:", url)
    print(text.strip())


if __name__ == "__main__":
    main()
