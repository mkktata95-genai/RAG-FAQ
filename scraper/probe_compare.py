# probe_compare.py — run from RAG\ : python scraper\probe_compare.py
import asyncio, aiohttp
import content_freshness_httpV1 as cf
URL = "https://www.royallondon.com/pensions/investment-options/investment-governance"

async def main():
    async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=False),
                                     headers={"User-Agent": "RLG-Aria-ContentFreshness/1.0"}) as s:
        probe = []
        for _ in range(10):
            try: probe.append((await cf._probe_url(s, URL))[0])
            except Exception as e: probe.append(type(e).__name__)
            await asyncio.sleep(1)
    print("freshness probe :", probe)
    get = []
    for _ in range(10):
        _, st, err = cf.fetch_html(URL)
        get.append(st if not err else f"err:{st}")
    print("fetch_html (GET):", get)

asyncio.run(main())