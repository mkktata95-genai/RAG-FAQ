"""
Prints the HTML ancestor chain (tag#id.class) around every 'Read more' link on a page,
to identify the related-content / news widget container.
Place next to content_freshness_httpV1.py. Run:
    python find_widget_container.py <url> [text]
"""
import sys

from bs4 import BeautifulSoup

import content_freshness_httpV1 as cf

url = sys.argv[1]
needle = sys.argv[2] if len(sys.argv) > 2 else "Read more"

html, status, err = cf.fetch_html(url)
if err or not html:
    sys.exit(f"fetch failed: {status} {err}")

soup = BeautifulSoup(html, "html.parser")
seen = {}
for a in soup.find_all("a"):
    if needle.lower() not in a.get_text(" ", strip=True).lower():
        continue
    chain, node = [], a
    for _ in range(7):
        node = node.parent
        if node is None or node.name in ("[document]",):
            break
        cls = ".".join(node.get("class", []))
        chain.append(f"{node.name}{'#' + node['id'] if node.get('id') else ''}{'.' + cls if cls else ''}")
    key = " < ".join(chain)
    seen[key] = seen.get(key, 0) + 1

print(f"{url}\n'{needle}' links found: {sum(seen.values())}\n")
for k, n in sorted(seen.items(), key=lambda x: -x[1]):
    print(f"x{n}: {k}\n")