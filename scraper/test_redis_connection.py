"""
test_redis_connection.py
Quick connectivity test for Azure Managed Redis from VDI.
Uses Entra ID (DefaultAzureCredential) auth - no access keys, per policy.
"""

import redis
from azure.identity import DefaultAzureCredential

REDIS_HOST = "<your-redis-name>.<region>.redis.azure.net"  # from Azure portal
REDIS_PORT = 10000  # Azure Managed Redis uses 10000, not 6379
SCOPE = "https://redis.azure.com/.default"

def get_token():
    cred = DefaultAzureCredential()
    token = cred.get_token(SCOPE)
    return token.token

def main():
    username = "<object-id-of-your-managed-identity-or-user>"  # required for AAD auth on Azure Redis
    password = get_token()

    r = redis.Redis(
        host=REDIS_HOST,
        port=REDIS_PORT,
        username=username,
        password=password,
        ssl=True,
        decode_responses=True,
    )

    # Basic ops
    r.set("digiassist:test", "connection_ok", ex=60)
    print("SET ok")
    print("GET:", r.get("digiassist:test"))
    print("PING:", r.ping())

if __name__ == "__main__":
    main()