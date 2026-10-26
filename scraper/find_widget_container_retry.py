"""
Like find_widget_container.py, but re-fetches (up to N times) until the widget links appear.
    python find_widget_container_retry.py <url> [text] [tries]
"""
import sys
import time

from bs4 import BeautifulSoup

import content_freshness_httpV1 as cf

url = sys.argv[1]
needle = sys.argv[2] if len(sys.argv) > 2 else "Read more"
tries = int(sys.argv[3]) if len(sys.argv) > 3 else 15


def chains_for(html):
    soup = BeautifulSoup(html, "html.parser")
    seen, total = {}, 0
    for a in soup.find_all("a"):
        if needle.lower() not in a.get_text(" ", strip=True).lower():
            continue
        total += 1
        chain, node = [], a
        for _ in range(7):
            node = node.parent
            if node is None or node.name == "[document]":
                break
            cls = ".".join(node.get("class", []))
            chain.append(f"{node.name}{'#' + node['id'] if node.get('id') else ''}{'.' + cls if cls else ''}")
        key = " < ".join(chain)
        seen[key] = seen.get(key, 0) + 1
    return total, seen


for i in range(1, tries + 1):
    html, status, err = cf.fetch_html(url)
    if err or not html:
        print(f"try {i}: fetch failed {status} {err}")
        continue
    total, seen = chains_for(html)
    print(f"try {i}: '{needle}' links = {total}")
    if total:
        for k, n in sorted(seen.items(), key=lambda x: -x[1]):
            print(f"  x{n}: {k}\n")
        break
    time.sleep(1)
else:
    print("Widget never appeared in this many fetches.")