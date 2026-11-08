#include "citybrain/cache.h"
#include "internal.h"

/* FNV-1a 64: short, no tables, same result in any language. Not a security primitive. */
static const uint64_t CB_FNV_OFFSET = 14695981039346656037ULL;
static const uint64_t CB_FNV_PRIME  = 1099511628211ULL;

static uint64_t fnv_mix_u64(uint64_t hash, uint64_t value)
{
    int i;
    for (i = 0; i < 8; ++i) {
        uint8_t byte = (uint8_t)((value >> (i * 8)) & 0xFFu);
        hash ^= (uint64_t)byte;
        hash *= CB_FNV_PRIME;
    }
    return hash;
}

cb_status_t cb_cache_bucket(const double *edges, size_t n_edges,
                            double value, int32_t *out_bucket)
{
    size_t i;

    CB_REQUIRE(edges != NULL && out_bucket != NULL, CB_ERR_NULL);
    CB_REQUIRE(n_edges > 0, CB_ERR_DOMAIN);
    CB_REQUIRE(n_edges <= CB_CACHE_MAX_BUCKET_EDGES, CB_ERR_CAPACITY);
    CB_REQUIRE(cb_finite(value), CB_ERR_NOT_FINITE);
    CB_CHECK(cb_finite_array(edges, n_edges));
    for (i = 1; i < n_edges; ++i) {
        CB_REQUIRE(edges[i] > edges[i - 1], CB_ERR_DOMAIN);
    }

    /* Half-open buckets [edges[i-1], edges[i]); the mapping is total. */
    for (i = 0; i < n_edges; ++i) {
        if (value < edges[i]) {
            *out_bucket = (int32_t)i;
            return CB_OK;
        }
    }
    *out_bucket = (int32_t)n_edges;
    return CB_OK;
}

cb_status_t cb_cache_key_make(int32_t category_id, int32_t partition_id,
                              int32_t bucket_id, uint64_t state_version,
                              cb_cache_key_t *out_key)
{
    uint64_t h;

    CB_REQUIRE(out_key != NULL, CB_ERR_NULL);
    CB_REQUIRE(category_id >= 0 && partition_id >= 0 && bucket_id >= 0,
               CB_ERR_DOMAIN);

    h = CB_FNV_OFFSET;
    h = fnv_mix_u64(h, (uint64_t)(uint32_t)category_id);
    h = fnv_mix_u64(h, (uint64_t)(uint32_t)partition_id);
    h = fnv_mix_u64(h, (uint64_t)(uint32_t)bucket_id);
    h = fnv_mix_u64(h, state_version);

    out_key->category_id   = category_id;
    out_key->partition_id  = partition_id;
    out_key->bucket_id     = bucket_id;
    out_key->state_version = state_version;
    out_key->hash          = h;
    return CB_OK;
}

cb_status_t cb_cache_key_equal(const cb_cache_key_t *a, const cb_cache_key_t *b,
                               int32_t *out_equal)
{
    CB_REQUIRE(a != NULL && b != NULL && out_equal != NULL, CB_ERR_NULL);
    /* Field-wise, never hash-wise: a collision must not read as a hit. */
    *out_equal = (a->category_id == b->category_id &&
                  a->partition_id == b->partition_id &&
                  a->bucket_id == b->bucket_id &&
                  a->state_version == b->state_version) ? 1 : 0;
    return CB_OK;
}

cb_status_t cb_cache_key_valid(const cb_cache_key_t *key,
                               uint64_t current_state_version,
                               uint64_t created_ms, uint64_t now_ms, uint64_t ttl_ms,
                               int32_t *out_valid, uint32_t *out_reason)
{
    uint32_t reason = CB_CACHE_VALID;
    uint64_t age;

    CB_REQUIRE(key != NULL, CB_ERR_NULL);

    if (key->state_version != current_state_version) {
        reason |= CB_CACHE_STALE_STATE;
    }
    /* A clock that steps backwards reads as age zero, not a wrapped huge value. */
    age = (now_ms > created_ms) ? (now_ms - created_ms) : 0u;
    if (age >= ttl_ms) {
        reason |= CB_CACHE_EXPIRED;
    }

    if (out_reason != NULL) { *out_reason = reason; }
    if (out_valid != NULL)  { *out_valid = (reason == CB_CACHE_VALID) ? 1 : 0; }
    return CB_OK;
}
