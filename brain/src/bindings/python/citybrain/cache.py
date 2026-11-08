"""
Pythonic wrapper for citybrain/cache.h: decision-cache key construction and
invalidation rules. The cache itself (storage, eviction) is the caller's;
this is only the pure-function rule for building and invalidating a key.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import NamedTuple, Optional, Sequence

from citybrain._raw import ffi, lib
from citybrain.errors import check

VALID = 0x00
STALE_STATE = 0x01
EXPIRED = 0x02


@dataclass(frozen=True)
class CacheKey:
    category_id: int
    partition_id: int
    bucket_id: int
    state_version: int
    hash: int


def bucket(edges: Sequence[float], value: float) -> int:
    """Map `value` to a half-open bucket index using strictly increasing
    `edges`. n edges produce n+1 buckets; the mapping is total."""
    c_edges = ffi.new("double[]", list(edges))
    out = ffi.new("int32_t *")
    check(lib.cb_cache_bucket(c_edges, len(edges), value, out))
    return out[0]


def make_key(category_id: int, partition_id: int, bucket_id: int, state_version: int) -> CacheKey:
    k = ffi.new("cb_cache_key_t *")
    check(lib.cb_cache_key_make(category_id, partition_id, bucket_id, state_version, k))
    return CacheKey(category_id=k.category_id, partition_id=k.partition_id,
                     bucket_id=k.bucket_id, state_version=k.state_version, hash=k.hash)


def keys_equal(a: CacheKey, b: CacheKey) -> bool:
    """Field-wise equality, never hash-wise, so a hash collision is never
    mistaken for a cache hit."""
    return (a.category_id == b.category_id and a.partition_id == b.partition_id
            and a.bucket_id == b.bucket_id and a.state_version == b.state_version)


class Validity(NamedTuple):
    valid: bool
    reason: int


def key_valid(key: CacheKey, current_state_version: int, created_ms: int,
               now_ms: int, ttl_ms: int) -> Validity:
    """A key is valid only if its state_version matches the partition's
    current version AND its age is strictly less than ttl_ms. A clock that
    steps backwards cannot extend an entry's life (age clamps to 0)."""
    k = ffi.new("cb_cache_key_t *")
    k.category_id = key.category_id
    k.partition_id = key.partition_id
    k.bucket_id = key.bucket_id
    k.state_version = key.state_version
    k.hash = key.hash
    valid_out = ffi.new("int32_t *")
    reason_out = ffi.new("uint32_t *")
    check(lib.cb_cache_key_valid(k, current_state_version, created_ms, now_ms, ttl_ms,
                                  valid_out, reason_out))
    return Validity(valid=bool(valid_out[0]), reason=reason_out[0])
