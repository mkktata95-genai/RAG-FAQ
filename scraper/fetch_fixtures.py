import sys, re, requests
sys.path.insert(0, 'scraper')
import content_freshness_httpV1 as m

urls = [
    "https://www.royallondon.com/guides-tools/isa-guides/pensions-and-isas-whats-the-difference",
    "https://www.royallondon.com/guides-tools/money-guides/everyday-money/understanding-compound-growth",
    "https://www.royallondon.com/about-us/how-we-are-run/governance-and-leadership-teams/our-board",
    "https://www.royallondon.com/guides-tools/isa-guides/isas-and-tax",
    "https://www.royallondon.com/existing-customers/online-service/royal-london-pensions-since-2004-or-scottish-life",
    "https://www.royallondon.com/guides-tools/isa-guides/what-is-a-stocks-and-shares-isa",
    "https://www.royallondon.com/about-us/our-purpose/social-impact/changemakers/meet-the-changemakers",
    "https://www.royallondon.com/guides-tools/pension-guides/pension-investments/investment-glossary",
    "https://www.royallondon.com/guides-tools/money-guides/everyday-money/why-your-credit-rating-is-important",
]

for url in urls:
    norm = m.normalize_url(url)
    fname = re.sub(r'[^a-zA-Z0-9]+', '_', norm).strip('_') + '.html'
    resp = requests.get(url, timeout=20, headers={"User-Agent": "Mozilla/5.0"})
    path = f"scraper/tests/fixtures/mini/{fname}"
    with open(path, "w", encoding="utf-8") as f:
        f.write(resp.text)
    print(resp.status_code, fname)