"""
Semantic Cache with Redis/Valkey backend.
Falls back to in-memory if Redis unavailable.

# ─────────────────────────────────────────────────────────────
# TODO: PRODUCTION READINESS
# Before go-live replace/configure the following:
#
# Redis Backend:
#      Current  → local Redis/Valkey or in-memory fallback
#                 (in-memory resets on every restart,
#                  not suitable for production)
#      Replace  → Azure Cache for Redis
#                 Update REDIS_URL in .env:
#                 REDIS_URL=rediss://:<password>@<your-cache>.redis.cache.windows.net:6380
#                 Note: rediss:// (double s) = SSL required for Azure
#
# Cache Persistence:
#      Current  → pickle serialization (fine for dev)
#      Consider → JSON serialization for better portability
#                 and easier debugging in production
#
# Cache Size:
#      Current  → CACHE_MAX_SIZE=1000 (in-memory limit)
#      Review   → Azure Cache for Redis has its own memory
#                 limits based on SKU — confirm with RLG team
#                 Basic C0 = 250MB, Standard C1 = 1GB etc.
#
# Cache TTL:
#      Current  → CACHE_TTL=86400 (24 hours)
#      Review   → Confirm TTL with RLG content team
#                 FAQ content changes infrequently but
#                 cache should be cleared after re-indexing
#
# Monitoring:
#      Add      → Azure Cache for Redis has built-in metrics
#                 in Azure Portal (hits, misses, memory usage)
#                 Connect to Azure Monitor for alerting
# ─────────────────────────────────────────────────────────────
"""

import os
import time
import math
import pickle
import threading
from typing import Optional
import structlog
from dotenv import load_dotenv

load_dotenv()
log = structlog.get_logger()

# ── Config ────────────────────────────────────────────────────
# TODO: PRODUCTION → Set REDIS_URL to Azure Cache for Redis:
# REDIS_URL=rediss://:<password>@<your-cache>.redis.cache.windows.net:6380
REDIS_URL       = os.getenv("REDIS_URL", "redis://localhost:6379")
CACHE_TTL       = int(os.getenv("CACHE_TTL", "86400"))
CACHE_THRESHOLD = float(os.getenv("CACHE_THRESHOLD", "0.90"))
CACHE_MAX_SIZE  = int(os.getenv("CACHE_MAX_SIZE", "1000"))


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """Calculate cosine similarity between two vectors."""
    dot    = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


class RedisSemanticCache:
    """
    Semantic cache backed by Redis/Valkey.
    Persists across server restarts.
    Falls back to in-memory if Redis unavailable.
    Uses connection pool for stability.

    TODO: PRODUCTION → Ensure Redis URL points to
    Azure Cache for Redis before go-live.
    """

    def __init__(
        self,
        threshold: float = CACHE_THRESHOLD,
        ttl: int = CACHE_TTL,
        max_size: int = CACHE_MAX_SIZE,
    ):
        self.threshold = threshold
        self.ttl       = ttl
        self.max_size  = max_size
        self._lock     = threading.Lock()
        self._redis    = None
        self._fallback: dict = {}
        self._using_redis    = False

        self._connect_redis()

    def _connect_redis(self):
        """
        Connect to Redis/Valkey with connection pool.
        TODO: PRODUCTION → Verify SSL connection works with
        Azure Cache for Redis (rediss:// URL with port 6380).
        """
        try:
            import redis
            pool = redis.ConnectionPool.from_url(
                REDIS_URL,
                decode_responses=False,
                socket_connect_timeout=3,
                socket_timeout=3,
                max_connections=10,
                retry_on_timeout=True,
            )
            client = redis.Redis(connection_pool=pool)
            client.ping()
            self._redis       = client
            self._using_redis = True
            log.info(
                "redis_connected",
                url=REDIS_URL,
                backend="valkey/redis",
            )
        except Exception as e:
            log.warning(
                "redis_unavailable",
                error=str(e),
                fallback="in-memory cache",
            )
            self._using_redis = False

    def _ensure_connected(self):
        """Reconnect if Redis connection dropped."""
        if self._using_redis and self._redis:
            try:
                self._redis.ping()
            except Exception:
                log.warning("redis_reconnecting")
                self._connect_redis()

    @property
    def size(self) -> int:
        """Number of entries in cache."""
        if self._using_redis and self._redis:
            try:
                keys = self._redis.keys("rlg:cache:*")
                return len(keys)
            except Exception:
                pass
        return len(self._fallback)

    def get(self, embedding: list[float]) -> Optional[object]:
        """Find semantically similar cached response."""
        self._ensure_connected()
        if self._using_redis and self._redis:
            return self._get_redis(embedding)
        return self._get_memory(embedding)

    def _get_redis(
        self, embedding: list[float]
    ) -> Optional[object]:
        """Get from Redis with semantic similarity search."""
        try:
            keys = self._redis.keys("rlg:cache:*")
            if not keys:
                return None

            best_score    = 0.0
            best_response = None

            for key in keys:
                try:
                    data = self._redis.get(key)
                    if not data:
                        continue
                    entry = pickle.loads(data)
                    score = cosine_similarity(
                        embedding, entry["embedding"]
                    )
                    if score > best_score:
                        best_score    = score
                        best_response = entry["response"]
                except Exception:
                    continue

            if best_score >= self.threshold:
                log.info(
                    "cache_hit",
                    similarity=round(best_score, 4),
                    backend="redis",
                )
                return best_response

            log.info(
                "cache_miss",
                best_similarity=round(best_score, 4),
                backend="redis",
            )
            return None

        except Exception as e:
            log.warning("redis_get_error", error=str(e))
            return self._get_memory(embedding)

    def _get_memory(
        self, embedding: list[float]
    ) -> Optional[object]:
        """
        Get from in-memory fallback cache.
        TODO: PRODUCTION → This fallback should not be
        the primary cache in production. Ensure Redis
        is properly configured before go-live.
        """
        with self._lock:
            now          = time.time()
            best_score   = 0.0
            best_response = None
            expired_keys = []

            for key, entry in self._fallback.items():
                if now - entry["timestamp"] > self.ttl:
                    expired_keys.append(key)
                    continue
                score = cosine_similarity(
                    embedding, entry["embedding"]
                )
                if score > best_score:
                    best_score    = score
                    best_response = entry["response"]

            for key in expired_keys:
                del self._fallback[key]

            if best_score >= self.threshold:
                log.info(
                    "cache_hit",
                    similarity=round(best_score, 4),
                    backend="memory",
                )
                return best_response

            log.info(
                "cache_miss",
                best_similarity=round(best_score, 4),
                backend="memory",
            )
            return None

    def set(
        self,
        query: str,
        embedding: list[float],
        response: object,
    ):
        """Store response in cache."""
        self._ensure_connected()
        if self._using_redis and self._redis:
            self._set_redis(query, embedding, response)
        else:
            self._set_memory(query, embedding, response)

    def _set_redis(
        self,
        query: str,
        embedding: list[float],
        response: object,
    ):
        """Store in Redis with TTL."""
        try:
            keys = self._redis.keys("rlg:cache:*")
            if len(keys) >= self.max_size:
                oldest_key  = keys[0]
                oldest_time = float("inf")
                for key in keys:
                    data = self._redis.get(key)
                    if data:
                        try:
                            entry = pickle.loads(data)
                            ts    = entry.get(
                                "timestamp", float("inf")
                            )
                            if ts < oldest_time:
                                oldest_time = ts
                                oldest_key  = key
                        except Exception:
                            pass
                self._redis.delete(oldest_key)

            key   = f"rlg:cache:{hash(query)}"
            entry = {
                "query":     query,
                "embedding": embedding,
                "response":  response,
                "timestamp": time.time(),
            }
            self._redis.set(
                key,
                pickle.dumps(entry),
                ex=self.ttl,
            )
            size = len(self._redis.keys("rlg:cache:*"))
            log.info(
                "cache_set",
                query=query[:50],
                cache_size=size,
                backend="redis",
            )
        except Exception as e:
            log.warning("redis_set_error", error=str(e))
            self._set_memory(query, embedding, response)

    def _set_memory(
        self,
        query: str,
        embedding: list[float],
        response: object,
    ):
        """
        Store in in-memory fallback.
        TODO: PRODUCTION → Not suitable as primary cache.
        Data lost on every server restart.
        """
        with self._lock:
            if len(self._fallback) >= self.max_size:
                oldest = min(
                    self._fallback.items(),
                    key=lambda x: x[1]["timestamp"],
                )
                del self._fallback[oldest[0]]

            key = str(hash(query))
            self._fallback[key] = {
                "query":     query,
                "embedding": embedding,
                "response":  response,
                "timestamp": time.time(),
            }
            log.info(
                "cache_set",
                query=query[:50],
                cache_size=len(self._fallback),
                backend="memory",
            )

    def clear(self):
        """
        Clear all cache entries.
        Call this after re-indexing Azure AI Search
        to ensure stale responses are removed.
        """
        if self._using_redis and self._redis:
            try:
                keys = self._redis.keys("rlg:cache:*")
                if keys:
                    self._redis.delete(*keys)
                log.info("cache_cleared", backend="redis")
            except Exception as e:
                log.warning("redis_clear_error", error=str(e))
        self._fallback.clear()
        log.info("cache_cleared", backend="memory")

    def get_stats(self) -> dict:
        """Get cache statistics."""
        return {
            "size":      self.size,
            "threshold": self.threshold,
            "ttl":       self.ttl,
            "max_size":  self.max_size,
            "backend":   "redis" if self._using_redis else "memory",
            "redis_url": REDIS_URL if self._using_redis else None,
        }


# ── Singleton ─────────────────────────────────────────────────
_cache: Optional[RedisSemanticCache] = None


def get_cache() -> RedisSemanticCache:
    """Get or create singleton cache instance."""
    global _cache
    if _cache is None:
        _cache = RedisSemanticCache()
        log.info(
            "cache_initialized",
            backend=(
                "redis" if _cache._using_redis else "memory"
            ),
            threshold=_cache.threshold,
            ttl=_cache.ttl,
        )
    return _cache


# ── Backward Compatibility ────────────────────────────────────
class SemanticCache(RedisSemanticCache):
    """Alias for backward compatibility with tests."""
    pass