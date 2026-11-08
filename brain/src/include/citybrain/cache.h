/*
 * Decision-cache key construction and invalidation rules. The cache
 * storage itself belongs to the caller; this is just the pure-function
 * rule for building a key and knowing when it's stale, so every component
 * that touches the cache agrees on it without reimplementing it.
 *
 * A key is (category, partition, bucket, state_version). It is valid while
 * the partition's state version is unchanged and the entry is younger than
 * its TTL.
 */
#ifndef CITYBRAIN_CACHE_H
#define CITYBRAIN_CACHE_H

#include "citybrain/common.h"

#ifdef __cplusplus
extern "C" {
#endif

#define CB_CACHE_MAX_BUCKET_EDGES ((size_t)64)

#define CB_CACHE_VALID       0x00u
#define CB_CACHE_STALE_STATE 0x01u
#define CB_CACHE_EXPIRED     0x02u

typedef struct cb_cache_key {
    int32_t  category_id;
    int32_t  partition_id;
    int32_t  bucket_id;
    uint64_t state_version;
    uint64_t hash; /* FNV-1a 64 over the four fields; not a security primitive */
} cb_cache_key_t;

/* Map a continuous value to a bucket index using strictly increasing
 * edges; n edges give n+1 half-open buckets. Errors: CB_ERR_DOMAIN if
 * edges is empty or not strictly increasing. */
CB_API cb_status_t cb_cache_bucket(const double *edges, size_t n_edges,
                                   double value, int32_t *out_bucket);

/* Build a key from its four fields. Deterministic across runs and
 * platforms. */
CB_API cb_status_t cb_cache_key_make(int32_t category_id, int32_t partition_id,
                                     int32_t bucket_id, uint64_t state_version,
                                     cb_cache_key_t *out_key);

/* Field-wise equality, not hash equality -- a hash collision is never
 * treated as a match. */
CB_API cb_status_t cb_cache_key_equal(const cb_cache_key_t *a,
                                      const cb_cache_key_t *b,
                                      int32_t *out_equal);

/* A key is valid only if state_version still matches and age < ttl_ms. A
 * clock that steps backwards cannot extend an entry's life. */
CB_API cb_status_t cb_cache_key_valid(const cb_cache_key_t *key,
                                      uint64_t current_state_version,
                                      uint64_t created_ms,
                                      uint64_t now_ms,
                                      uint64_t ttl_ms,
                                      int32_t *out_valid,
                                      uint32_t *out_reason);

#ifdef __cplusplus
} /* extern "C" */
#endif

#endif /* CITYBRAIN_CACHE_H */
