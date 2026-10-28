import os, re
from azure.storage.blob import BlobServiceClient

c = BlobServiceClient.from_connection_string("UseDevelopmentStorage=true").get_container_client("output")
names = sorted(b.name for b in c.list_blobs(name_starts_with="scraped-data/") if not b.name.endswith("_failures.json"))
print("blobs:", names)
n = names[-1]
t = c.download_blob(n).readall()
os.makedirs("check", exist_ok=True)
open("check/" + n.split("/")[-1], "wb").write(t)
print(n, len(t))
print(set(re.findall(r'"scraper_version": ?"([^"]+)"', t.decode())))