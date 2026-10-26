# widget_probe.py — place in RAG\scraper\ ; run: python scraper\widget_probe.py <url>
import sys
from bs4 import BeautifulSoup
import content_freshness_httpV1 as cf
html, st, err = cf.fetch_html(sys.argv[1])
soup = BeautifulSoup(html, "html.parser")
total = len(soup.get_text(" ", strip=True))
print("page text chars:", total)
for sel in cf.WIDGET_SELECTORS:
    for el in soup.select(sel):
        t = el.get_text(" ", strip=True)
        par = el.parent
        print(f"\n{sel} -> <{el.name} id={el.get('id')} class={el.get('class')}> chars={len(t)} parent=<{par.name} class={par.get('class')}>")
        print("   h-tags inside:", [h.get_text(strip=True)[:40] for h in el.find_all(['h1','h2','h3'])][:5])
        print("   starts:", t[:150])