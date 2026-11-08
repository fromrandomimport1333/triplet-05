#define _POSIX_C_SOURCE 200809L
/*
 * Benchmark driver for the hot-path functions: this code runs once per
 * incoming reading, so fusion and the change detector matter most.
 *
 * Each benchmark warms up, then times a fixed number of iterations at a
 * realistic call shape (not a single trivial call), and reports iterations
 * per second and nanoseconds per call. Numbers are printed, not asserted:
 * this is a measurement tool, not a pass/fail gate, because absolute
 * throughput is hardware-dependent. docs/testing.md quotes a run's output.
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <math.h>
#include <time.h>

#include "citybrain/citybrain.h"

typedef struct { uint64_t s; } rng_t;
static double rng_uniform(rng_t *r)
{
    r->s = r->s * 6364136223846793005ULL + 1442695040888963407ULL;
    return (double)((r->s >> 11) & 0x1FFFFFFFFFFFFFULL) / 9007199254740992.0;
}
static double rng_normal(rng_t *r)
{
    double u1 = rng_uniform(r), u2 = rng_uniform(r);
    if (u1 < 1e-12) { u1 = 1e-12; }
    return sqrt(-2.0 * log(u1)) * cos(6.283185307179586 * u2);
}

static double now_seconds(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (double)ts.tv_sec + (double)ts.tv_nsec * 1e-9;
}

static void report(const char *name, long iterations, double seconds, long items_per_call)
{
    double per_call_ns = (seconds / (double)iterations) * 1e9;
    double calls_per_sec = (double)iterations / seconds;
    printf("%-42s %10ld calls  %8.2f s  %10.1f ns/call  %12.0f calls/s",
           name, iterations, seconds, per_call_ns, calls_per_sec);
    if (items_per_call > 1) {
        printf("  (%ld items/call, %6.1f ns/item)",
               items_per_call, per_call_ns / (double)items_per_call);
    }
    printf("\n");
}

/* -------------------------------------------------------------- fusion -- */
static void bench_fusion(long iterations, int n_items)
{
    cb_fusion_config_t cfg;
    cb_fusion_result_t res;
    cb_evidence_t *items = malloc(sizeof(cb_evidence_t) * (size_t)n_items);
    double *scratch = malloc(sizeof(double) * (size_t)n_items);
    rng_t r; r.s = 1u;
    long i;
    double t0, t1;
    char label[64];

    cb_fusion_config_init(&cfg);
    for (i = 0; i < n_items; ++i) {
        items[i].likelihood_ratio = exp(rng_normal(&r));
        items[i].weight = rng_uniform(&r);
        items[i].group_id = (int32_t)(i % (n_items / 4 + 1));
    }

    /* warm up */
    for (i = 0; i < 1000; ++i) {
        cb_fusion_posterior(&cfg, items, (size_t)n_items, scratch, (size_t)n_items, &res);
    }

    t0 = now_seconds();
    for (i = 0; i < iterations; ++i) {
        cb_fusion_posterior(&cfg, items, (size_t)n_items, scratch, (size_t)n_items, &res);
    }
    t1 = now_seconds();

    snprintf(label, sizeof label, "fusion: %d items, grouped", n_items);
    report(label, iterations, t1 - t0, n_items);
    free(items);
    free(scratch);
}

/* ------------------------------------------------------------- detector -- */
static void bench_detector(long iterations)
{
    cb_detector_config_t cfg;
    cb_detector_state_t st;
    cb_detector_output_t out;
    rng_t r; r.s = 2u;
    long i;
    double t0, t1;

    cb_detector_config_init(&cfg);
    cb_detector_init(&st, &cfg, 0.0, 1.0);

    for (i = 0; i < 1000; ++i) {
        cb_detector_update(&st, &cfg, rng_normal(&r), &out);
    }

    t0 = now_seconds();
    for (i = 0; i < iterations; ++i) {
        cb_detector_update(&st, &cfg, rng_normal(&r), &out);
    }
    t1 = now_seconds();
    report("detector: one sample (EWMA+CUSUM)", iterations, t1 - t0, 1);
}

/* ---------------------------------------------------------------- graph -- */
static void bench_graph_adjust_prior(long iterations)
{
    cb_graph_config_t cfg;
    cb_graph_t g;
    int32_t active[4] = { 0, 1, 2, 3 };
    double adjusted, mult;
    long i;
    double t0, t1;

    cb_graph_config_init(&cfg);
    cfg.n_nodes = 4;
    cb_graph_init(&g, &cfg);
    cb_example_load_graph(&g);

    for (i = 0; i < 1000; ++i) {
        cb_graph_adjust_prior(&g, 3, active, 3, 0.02, &adjusted, &mult);
    }

    t0 = now_seconds();
    for (i = 0; i < iterations; ++i) {
        cb_graph_adjust_prior(&g, 3, active, 3, 0.02, &adjusted, &mult);
    }
    t1 = now_seconds();
    report("graph: adjust_prior, 3 active categories", iterations, t1 - t0, 1);
}

/* -------------------------------------------------------------- decision -- */
static void bench_decision(long iterations)
{
    cb_cost_table_t t;
    cb_decision_result_t res;
    rng_t r; r.s = 3u;
    long i;
    double t0, t1;

    cb_cost_table_init(&t);

    for (i = 0; i < 1000; ++i) {
        cb_decision_expected_cost(&t, rng_uniform(&r), &res);
    }

    t0 = now_seconds();
    for (i = 0; i < iterations; ++i) {
        cb_decision_expected_cost(&t, rng_uniform(&r), &res);
    }
    t1 = now_seconds();
    report("decision: expected-cost rule", iterations, t1 - t0, 1);
}

/* ------------------------------------------------------------ assignment -- */
static void bench_assignment(long iterations, int n)
{
    cb_assign_params_t p;
    double *m = malloc(sizeof(double) * (size_t)(n * n));
    int32_t *out = malloc(sizeof(int32_t) * (size_t)n);
    size_t need;
    unsigned char *scratch;
    rng_t r; r.s = 4u;
    long i, k;
    double t0, t1;
    char label[64];

    for (k = 0; k < n * n; ++k) { m[k] = rng_uniform(&r) * 100.0; }
    cb_assign_params_init(&p);
    p.matrix = m; p.n_rows = n; p.n_cols = n;
    cb_assign_scratch_bytes(n, n, &need);
    scratch = malloc(need);

    for (i = 0; i < 100; ++i) {
        cb_assign_solve(&p, out, NULL, scratch, need);
    }

    t0 = now_seconds();
    for (i = 0; i < iterations; ++i) {
        cb_assign_solve(&p, out, NULL, scratch, need);
    }
    t1 = now_seconds();
    snprintf(label, sizeof label, "assignment: %dx%d Hungarian solve", n, n);
    report(label, iterations, t1 - t0, 1);
    free(m); free(out); free(scratch);
}

int main(void)
{
    const char *version;
    cb_version_string(&version);
    printf("citybrain core benchmark, version %s\n", version);
    printf("Each row: realistic call shape, warmed up, timed over many calls.\n\n");

    bench_fusion(200000, 1);
    bench_fusion(200000, 8);
    bench_fusion(50000, 64);
    bench_detector(2000000);
    bench_graph_adjust_prior(500000);
    bench_decision(2000000);
    bench_assignment(20000, 4);
    bench_assignment(2000, 16);
    bench_assignment(50, 64);

    return 0;
}
