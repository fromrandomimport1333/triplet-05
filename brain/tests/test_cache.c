/* Decision-cache key construction and invalidation. */
#include "cb_test.h"

static void test_bucket(void)
{
    static const double edges[3] = { 0.25, 0.5, 0.75 };
    int32_t b;

    CB_CASE("buckets are half-open and the mapping is total");
    CB_CHECK_OK(cb_cache_bucket(edges, 3, -100.0, &b));
    CB_CHECK_EQ_INT(b, 0);
    CB_CHECK_OK(cb_cache_bucket(edges, 3, 0.0, &b));
    CB_CHECK_EQ_INT(b, 0);
    CB_CHECK_OK(cb_cache_bucket(edges, 3, 0.2499, &b));
    CB_CHECK_EQ_INT(b, 0);
    CB_CHECK_OK(cb_cache_bucket(edges, 3, 0.25, &b));
    CB_CHECK_EQ_INT(b, 1);
    CB_CHECK_OK(cb_cache_bucket(edges, 3, 0.5, &b));
    CB_CHECK_EQ_INT(b, 2);
    CB_CHECK_OK(cb_cache_bucket(edges, 3, 0.75, &b));
    CB_CHECK_EQ_INT(b, 3);
    CB_CHECK_OK(cb_cache_bucket(edges, 3, 1e300, &b));
    CB_CHECK_EQ_INT(b, 3);

    CB_CASE("one edge gives two buckets");
    CB_CHECK_OK(cb_cache_bucket(edges, 1, 0.1, &b));
    CB_CHECK_EQ_INT(b, 0);
    CB_CHECK_OK(cb_cache_bucket(edges, 1, 0.9, &b));
    CB_CHECK_EQ_INT(b, 1);

    CB_CASE("bucket argument domains");
    CB_CHECK_STATUS(cb_cache_bucket(edges, 0, 0.5, &b), CB_ERR_DOMAIN);
    CB_CHECK_STATUS(cb_cache_bucket(NULL, 3, 0.5, &b), CB_ERR_NULL);
    CB_CHECK_STATUS(cb_cache_bucket(edges, 3, 0.5, NULL), CB_ERR_NULL);
    CB_CHECK_STATUS(cb_cache_bucket(edges, 3, (double)NAN, &b),
                    CB_ERR_NOT_FINITE);
    CB_CHECK_STATUS(cb_cache_bucket(edges, CB_CACHE_MAX_BUCKET_EDGES + 1,
                                             0.5, &b), CB_ERR_CAPACITY);
    {
        static const double unsorted[3] = { 0.5, 0.25, 0.75 };
        static const double equal[2] = { 0.5, 0.5 };
        static const double nonfinite[2] = { 0.5, HUGE_VAL };
        CB_CHECK_STATUS(cb_cache_bucket(unsorted, 3, 0.1, &b), CB_ERR_DOMAIN);
        CB_CHECK_STATUS(cb_cache_bucket(equal, 2, 0.1, &b), CB_ERR_DOMAIN);
        CB_CHECK_STATUS(cb_cache_bucket(nonfinite, 2, 0.1, &b),
                        CB_ERR_NOT_FINITE);
    }
}

static void test_key(void)
{
    cb_cache_key_t a, b;
    int32_t eq;

    CB_CASE("the same four fields give the same key and hash");
    CB_CHECK_OK(cb_cache_key_make(1, 7, 2, 99u, &a));
    CB_CHECK_OK(cb_cache_key_make(1, 7, 2, 99u, &b));
    CB_CHECK_TRUE(a.hash == b.hash);
    CB_CHECK_OK(cb_cache_key_equal(&a, &b, &eq));
    CB_CHECK_EQ_INT(eq, 1);

    CB_CASE("changing any one field changes the key");
    CB_CHECK_OK(cb_cache_key_make(2, 7, 2, 99u, &b));
    CB_CHECK_TRUE(a.hash != b.hash);
    CB_CHECK_OK(cb_cache_key_equal(&a, &b, &eq));
    CB_CHECK_EQ_INT(eq, 0);
    CB_CHECK_OK(cb_cache_key_make(1, 8, 2, 99u, &b));
    CB_CHECK_TRUE(a.hash != b.hash);
    CB_CHECK_OK(cb_cache_key_make(1, 7, 3, 99u, &b));
    CB_CHECK_TRUE(a.hash != b.hash);
    CB_CHECK_OK(cb_cache_key_make(1, 7, 2, 100u, &b));
    CB_CHECK_TRUE(a.hash != b.hash);
    CB_CHECK_OK(cb_cache_key_equal(&a, &b, &eq));
    CB_CHECK_EQ_INT(eq, 0);

    CB_CASE("the hash is stable across runs and platforms");
    CB_CHECK_OK(cb_cache_key_make(0, 0, 0, 0u, &a));
    /* Fixed value for FNV-1a 64 over 32 zero bytes. If this ever changes,
     * every cache key in a running deployment changes with it. */
    CB_CHECK_TRUE(a.hash == 901300984310592933ULL);

    CB_CASE("negative identifiers are rejected");
    CB_CHECK_STATUS(cb_cache_key_make(-1, 0, 0, 0u, &a), CB_ERR_DOMAIN);
    CB_CHECK_STATUS(cb_cache_key_make(0, -1, 0, 0u, &a), CB_ERR_DOMAIN);
    CB_CHECK_STATUS(cb_cache_key_make(0, 0, -1, 0u, &a), CB_ERR_DOMAIN);
    CB_CHECK_STATUS(cb_cache_key_make(0, 0, 0, 0u, NULL), CB_ERR_NULL);
    CB_CHECK_STATUS(cb_cache_key_equal(NULL, &b, &eq), CB_ERR_NULL);
}

static void test_validity(void)
{
    cb_cache_key_t k;
    int32_t valid;
    uint32_t reason;

    CB_CHECK_OK(cb_cache_key_make(1, 2, 3, 10u, &k));

    CB_CASE("a fresh key with a matching state version is valid");
    CB_CHECK_OK(cb_cache_key_valid(&k, 10u, 1000u, 1500u, 2000u, &valid, &reason));
    CB_CHECK_EQ_INT(valid, 1);
    CB_CHECK_EQ_INT(reason, CB_CACHE_VALID);

    CB_CASE("any partition state change invalidates immediately");
    CB_CHECK_OK(cb_cache_key_valid(&k, 11u, 1000u, 1000u, 2000u, &valid, &reason));
    CB_CHECK_EQ_INT(valid, 0);
    CB_CHECK_TRUE((reason & CB_CACHE_STALE_STATE) != 0u);

    CB_CASE("the TTL boundary is exclusive: age == ttl is expired");
    CB_CHECK_OK(cb_cache_key_valid(&k, 10u, 1000u, 2999u, 2000u, &valid, &reason));
    CB_CHECK_EQ_INT(valid, 1);
    CB_CHECK_OK(cb_cache_key_valid(&k, 10u, 1000u, 3000u, 2000u, &valid, &reason));
    CB_CHECK_EQ_INT(valid, 0);
    CB_CHECK_TRUE((reason & CB_CACHE_EXPIRED) != 0u);

    CB_CASE("a zero TTL means nothing is ever reused");
    CB_CHECK_OK(cb_cache_key_valid(&k, 10u, 1000u, 1000u, 0u, &valid, &reason));
    CB_CHECK_EQ_INT(valid, 0);

    CB_CASE("both reasons can apply at once");
    CB_CHECK_OK(cb_cache_key_valid(&k, 99u, 1000u, 9000u, 2000u, &valid, &reason));
    CB_CHECK_EQ_INT(reason, CB_CACHE_STALE_STATE | CB_CACHE_EXPIRED);

    CB_CASE("a backwards clock cannot extend an entry's life");
    CB_CHECK_OK(cb_cache_key_valid(&k, 10u, 5000u, 1000u, 2000u, &valid, &reason));
    CB_CHECK_EQ_INT(valid, 1); /* age clamps to 0, not to a wrapped huge value */
    CB_CHECK_OK(cb_cache_key_valid(&k, 10u, 5000u, 1000u, 0u, &valid, &reason));
    CB_CHECK_EQ_INT(valid, 0);

    CB_CASE("validity outputs are optional");
    CB_CHECK_OK(cb_cache_key_valid(&k, 10u, 0u, 0u, 1u, NULL, NULL));
    CB_CHECK_STATUS(cb_cache_key_valid(NULL, 10u, 0u, 0u, 1u, &valid, &reason),
                    CB_ERR_NULL);
}

int main(void)
{
    test_bucket();
    test_key();
    test_validity();
    return cb_t_report("test_cache");
}
