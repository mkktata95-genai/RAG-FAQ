import os
from dotenv import load_dotenv
from azure.identity import DefaultAzureCredential
from azure.search.documents import SearchClient

load_dotenv()

c = SearchClient(
    os.getenv("AZURE_SEARCH_ENDPOINT"),
    "rlg-faq-index-v4",
    DefaultAzureCredential(),
)

r = list(c.search(
    "*",
    filter='search.ismatch(\'"/fund-changes"\', \'source_url\')',
    select=["source_url", "indexed_at", "index_run_id"],
    top=5,
))
print(f"fund-changes hits: {len(r)}")
for x in r:
    print(f"  {x['source_url'][:80]}  | {x.get('indexed_at','')[:19]}")

total = c.search("*", include_total_count=True, top=1)
print(f"\nTotal docs in rlg-faq-index-v4: {total.get_count()}")