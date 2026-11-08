#include "citybrain/graph.h"
#include "citybrain/fusion.h" /* for cb_logit / cb_logistic */
#include "internal.h"

#define CB_GRAPH_MAX_ACTIVE ((size_t)4096)

cb_status_t cb_graph_config_init(cb_graph_config_t *cfg)
{
    CB_REQUIRE(cfg != NULL, CB_ERR_NULL);
    cfg->m_min   = 0.5;  /* illustrative floor: nothing lowers past one half */
    cfg->m_max   = 3.0;  /* illustrative ceiling */
    cfg->n_nodes = 4;    /* illustrative */
    return CB_OK;
}

static cb_status_t graph_config_validate(const cb_graph_config_t *cfg)
{
    CB_REQUIRE(cfg != NULL, CB_ERR_NULL);
    CB_REQUIRE(cb_finite(cfg->m_min) && cb_finite(cfg->m_max), CB_ERR_NOT_FINITE);
    CB_REQUIRE(cfg->m_min > 0.0 && cfg->m_min <= 1.0, CB_ERR_DOMAIN);
    CB_REQUIRE(cfg->m_max >= 1.0, CB_ERR_DOMAIN);
    CB_REQUIRE(cfg->n_nodes >= 1 && cfg->n_nodes <= CB_GRAPH_MAX_NODES, CB_ERR_DOMAIN);
    return CB_OK;
}

cb_status_t cb_graph_init(cb_graph_t *graph, const cb_graph_config_t *cfg)
{
    int32_t i, j;

    CB_REQUIRE(graph != NULL, CB_ERR_NULL);
    CB_CHECK(graph_config_validate(cfg));

    graph->cfg = *cfg;
    for (i = 0; i < CB_GRAPH_MAX_NODES; ++i) {
        for (j = 0; j < CB_GRAPH_MAX_NODES; ++j) {
            graph->multiplier[i][j] = 1.0;
            graph->kind[i][j] = (uint8_t)CB_EDGE_NONE;
        }
    }
    graph->magic = CB_MAGIC_GRAPH;
    return CB_OK;
}

static cb_status_t graph_ready(const cb_graph_t *graph)
{
    CB_REQUIRE(graph != NULL, CB_ERR_NULL);
    CB_REQUIRE(graph->magic == CB_MAGIC_GRAPH, CB_ERR_STATE);
    CB_CHECK(graph_config_validate(&graph->cfg));
    return CB_OK;
}

static int node_ok(const cb_graph_t *graph, int32_t n)
{
    return (n >= 0 && n < graph->cfg.n_nodes) ? 1 : 0;
}

cb_status_t cb_graph_set_edge(cb_graph_t *graph, int32_t from, int32_t to,
                              cb_edge_kind_t kind, double multiplier)
{
    double clipped;

    CB_CHECK(graph_ready(graph));
    CB_REQUIRE(node_ok(graph, from) && node_ok(graph, to), CB_ERR_DOMAIN);
    CB_REQUIRE(from != to, CB_ERR_DOMAIN); /* not evidence for itself */
    CB_REQUIRE(cb_finite(multiplier), CB_ERR_NOT_FINITE);
    CB_REQUIRE(multiplier > 0.0, CB_ERR_DOMAIN);
    CB_REQUIRE(kind == CB_EDGE_RAISES || kind == CB_EDGE_LOWERS, CB_ERR_DOMAIN);
    if (kind == CB_EDGE_RAISES) {
        CB_REQUIRE(multiplier >= 1.0, CB_ERR_DOMAIN);
    } else {
        CB_REQUIRE(multiplier <= 1.0, CB_ERR_DOMAIN);
    }

    clipped = cb_clamp(multiplier, graph->cfg.m_min, graph->cfg.m_max);
    graph->multiplier[from][to] = clipped;
    graph->kind[from][to] = (uint8_t)kind;
    return CB_OK;
}

cb_status_t cb_graph_get_edge(const cb_graph_t *graph, int32_t from, int32_t to,
                              cb_edge_kind_t *out_kind, double *out_multiplier)
{
    CB_CHECK(graph_ready(graph));
    CB_REQUIRE(node_ok(graph, from) && node_ok(graph, to), CB_ERR_DOMAIN);
    if (out_kind != NULL) {
        *out_kind = (cb_edge_kind_t)graph->kind[from][to];
    }
    if (out_multiplier != NULL) {
        *out_multiplier = graph->multiplier[from][to];
    }
    return CB_OK;
}

cb_status_t cb_graph_clear_edge(cb_graph_t *graph, int32_t from, int32_t to)
{
    CB_CHECK(graph_ready(graph));
    CB_REQUIRE(node_ok(graph, from) && node_ok(graph, to), CB_ERR_DOMAIN);
    graph->multiplier[from][to] = 1.0;
    graph->kind[from][to] = (uint8_t)CB_EDGE_NONE;
    return CB_OK;
}

cb_status_t cb_graph_path_multiplier(const cb_graph_t *graph,
                                     const int32_t *path, size_t path_len,
                                     double *out_multiplier)
{
    double product = 1.0;
    size_t i;

    CB_CHECK(graph_ready(graph));
    CB_REQUIRE(out_multiplier != NULL, CB_ERR_NULL);
    CB_REQUIRE(path != NULL || path_len == 0, CB_ERR_NULL);
    CB_REQUIRE(path_len <= (size_t)CB_GRAPH_MAX_NODES, CB_ERR_CAPACITY);

    for (i = 0; i < path_len; ++i) {
        CB_REQUIRE(node_ok(graph, path[i]), CB_ERR_DOMAIN);
    }
    for (i = 0; i + 1 < path_len; ++i) {
        product *= graph->multiplier[path[i]][path[i + 1]];
    }
    /* Clip the whole product, not each factor, so the floor holds for any
     * chain length. */
    *out_multiplier = cb_clamp(product, graph->cfg.m_min, graph->cfg.m_max);
    return CB_OK;
}

/* Depth-first enumeration of simple paths. n_nodes <= 8, so the number of
 * simple paths between two nodes is at most (n_nodes-2)! = 720 and the
 * recursion depth is at most n_nodes. */
static void extremal_walk(const cb_graph_t *graph, int32_t at, int32_t to,
                          double product, uint8_t *visited,
                          double *best_min, double *best_max, int32_t *found)
{
    int32_t next;

    if (at == to) {
        double clipped = cb_clamp(product, graph->cfg.m_min, graph->cfg.m_max);
        if (*found == 0) {
            *best_min = clipped;
            *best_max = clipped;
            *found = 1;
        } else {
            if (clipped < *best_min) { *best_min = clipped; }
            if (clipped > *best_max) { *best_max = clipped; }
        }
        return;
    }
    for (next = 0; next < graph->cfg.n_nodes; ++next) {
        if (visited[next] != 0u) { continue; }
        if (graph->kind[at][next] == (uint8_t)CB_EDGE_NONE) { continue; }
        visited[next] = 1u;
        extremal_walk(graph, next, to, product * graph->multiplier[at][next],
                      visited, best_min, best_max, found);
        visited[next] = 0u;
    }
}

cb_status_t cb_graph_extremal_multiplier(const cb_graph_t *graph,
                                         int32_t from, int32_t to,
                                         double *out_min, double *out_max)
{
    uint8_t visited[CB_GRAPH_MAX_NODES];
    double lo = 1.0, hi = 1.0;
    int32_t found = 0;
    int32_t i;

    CB_CHECK(graph_ready(graph));
    CB_REQUIRE(node_ok(graph, from) && node_ok(graph, to), CB_ERR_DOMAIN);
    CB_REQUIRE(from != to, CB_ERR_DOMAIN);

    for (i = 0; i < CB_GRAPH_MAX_NODES; ++i) { visited[i] = 0u; }
    visited[from] = 1u;
    extremal_walk(graph, from, to, 1.0, visited, &lo, &hi, &found);

    if (out_min != NULL) { *out_min = lo; }
    if (out_max != NULL) { *out_max = hi; }
    return (found != 0) ? CB_OK : CB_ERR_NOT_FOUND;
}

cb_status_t cb_graph_adjust_prior(const cb_graph_t *graph, int32_t target,
                                  const int32_t *active, size_t n_active,
                                  double prior,
                                  double *out_adjusted_prior,
                                  double *out_multiplier)
{
    uint8_t seen[CB_GRAPH_MAX_NODES];
    double product = 1.0;
    double multiplier;
    size_t i;
    int32_t node;

    CB_CHECK(graph_ready(graph));
    CB_REQUIRE(node_ok(graph, target), CB_ERR_DOMAIN);
    CB_REQUIRE(active != NULL || n_active == 0, CB_ERR_NULL);
    CB_REQUIRE(n_active <= CB_GRAPH_MAX_ACTIVE, CB_ERR_CAPACITY);
    CB_REQUIRE(cb_finite(prior), CB_ERR_NOT_FINITE);
    CB_REQUIRE(prior > 0.0 && prior < 1.0, CB_ERR_DOMAIN);

    for (i = 0; i < (size_t)CB_GRAPH_MAX_NODES; ++i) { seen[i] = 0u; }
    for (i = 0; i < n_active; ++i) {
        CB_REQUIRE(node_ok(graph, active[i]), CB_ERR_DOMAIN);
    }
    /* Walk node indices, not the caller's array order, so the result
     * doesn't depend on how active was ordered. */
    for (i = 0; i < n_active; ++i) {
        seen[active[i]] = 1u;
    }
    seen[target] = 0u; /* a category is not evidence for itself */

    for (node = 0; node < graph->cfg.n_nodes; ++node) {
        if (seen[node] == 0u) { continue; }
        product *= graph->multiplier[node][target];
    }
    multiplier = cb_clamp(product, graph->cfg.m_min, graph->cfg.m_max);

    if (out_multiplier != NULL) { *out_multiplier = multiplier; }
    if (out_adjusted_prior != NULL) {
        double log_odds, adjusted;
        CB_CHECK(cb_logit(prior, &log_odds));
        log_odds += log(multiplier);
        CB_CHECK(cb_logistic(log_odds, &adjusted));
        *out_adjusted_prior = adjusted;
    }
    return CB_OK;
}

cb_status_t cb_graph_has_unrelated_pair(const cb_graph_t *graph,
                                       const int32_t *active,
                                       size_t n_active,
                                       int32_t *out_unrelated)
{
    uint8_t seen[CB_GRAPH_MAX_NODES];
    size_t i;
    int32_t a, b;
    int32_t count = 0;

    CB_CHECK(graph_ready(graph));
    CB_REQUIRE(out_unrelated != NULL, CB_ERR_NULL);
    CB_REQUIRE(active != NULL || n_active == 0, CB_ERR_NULL);
    CB_REQUIRE(n_active <= CB_GRAPH_MAX_ACTIVE, CB_ERR_CAPACITY);

    for (i = 0; i < (size_t)CB_GRAPH_MAX_NODES; ++i) { seen[i] = 0u; }
    for (i = 0; i < n_active; ++i) {
        CB_REQUIRE(node_ok(graph, active[i]), CB_ERR_DOMAIN);
        seen[active[i]] = 1u;
    }
    for (a = 0; a < graph->cfg.n_nodes; ++a) {
        if (seen[a] != 0u) { count++; }
    }
    if (count < 2) {
        *out_unrelated = 0;
        return CB_OK;
    }
    for (a = 0; a < graph->cfg.n_nodes; ++a) {
        if (seen[a] == 0u) { continue; }
        for (b = a + 1; b < graph->cfg.n_nodes; ++b) {
            if (seen[b] == 0u) { continue; }
            if (graph->kind[a][b] == (uint8_t)CB_EDGE_NONE &&
                graph->kind[b][a] == (uint8_t)CB_EDGE_NONE) {
                *out_unrelated = 1;
                return CB_OK;
            }
        }
    }
    *out_unrelated = 0;
    return CB_OK;
}
