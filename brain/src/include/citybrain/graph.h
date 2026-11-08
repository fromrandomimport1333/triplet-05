/*
 * A small weighted directed graph over categories, with bounded odds
 * multipliers on each edge.
 *
 * Each edge raises or lowers the odds of its target category when its
 * source is active. Every stored multiplier, and every multiplier reached
 * by composing edges along a path, is clipped to [m_min, m_max]. The clip
 * is applied to the composed product, not just to each factor, so no chain
 * of lowering edges, however long, can push the result below the floor --
 * this is what stops a spoofed category from fully suppressing a real one.
 *
 * Nodes are plain integer indices with no attached meaning. A worked
 * example category set lives in example_profile.h and is sample data only.
 */
#ifndef CITYBRAIN_GRAPH_H
#define CITYBRAIN_GRAPH_H

#include "citybrain/common.h"

#ifdef __cplusplus
extern "C" {
#endif

#define CB_GRAPH_MAX_NODES 8

typedef enum cb_edge_kind {
    CB_EDGE_NONE   = 0,
    CB_EDGE_RAISES = 1, /* multiplier >= 1 */
    CB_EDGE_LOWERS = 2  /* multiplier <= 1 */
} cb_edge_kind_t;

typedef struct cb_graph_config {
    double  m_min;   /* floor, in (0, 1] */
    double  m_max;   /* ceiling, >= 1 */
    int32_t n_nodes; /* in [1, CB_GRAPH_MAX_NODES] */
} cb_graph_config_t;

typedef struct cb_graph {
    cb_graph_config_t cfg;
    double  multiplier[CB_GRAPH_MAX_NODES][CB_GRAPH_MAX_NODES];
    uint8_t kind[CB_GRAPH_MAX_NODES][CB_GRAPH_MAX_NODES];
    uint32_t magic;
} cb_graph_t;

/* Default: m_min=0.5, m_max=3.0, n_nodes=4. */
CB_API cb_status_t cb_graph_config_init(cb_graph_config_t *cfg);

/* Empty graph: every multiplier 1.0, every edge kind NONE. */
CB_API cb_status_t cb_graph_init(cb_graph_t *graph, const cb_graph_config_t *cfg);

/* Set one directed edge; multiplier is clipped into [m_min, m_max] before
 * storage. Errors: CB_ERR_DOMAIN for a self-loop, an out-of-range node, or
 * a kind/multiplier mismatch (RAISES needs >= 1, LOWERS needs <= 1). */
CB_API cb_status_t cb_graph_set_edge(cb_graph_t *graph,
                                     int32_t from, int32_t to,
                                     cb_edge_kind_t kind,
                                     double multiplier);

CB_API cb_status_t cb_graph_get_edge(const cb_graph_t *graph,
                                     int32_t from, int32_t to,
                                     cb_edge_kind_t *out_kind,
                                     double *out_multiplier);

CB_API cb_status_t cb_graph_clear_edge(cb_graph_t *graph, int32_t from, int32_t to);

/* Compose the multipliers along a path of node indices and clip the
 * product to [m_min, m_max]. A two-node path is one edge; a missing edge
 * contributes 1.0. */
CB_API cb_status_t cb_graph_path_multiplier(const cb_graph_t *graph,
                                            const int32_t *path,
                                            size_t path_len,
                                            double *out_multiplier);

/* Min/max composed multiplier over all simple paths between two nodes.
 * CB_ERR_NOT_FOUND if no path exists. */
CB_API cb_status_t cb_graph_extremal_multiplier(const cb_graph_t *graph,
                                                int32_t from, int32_t to,
                                                double *out_min,
                                                double *out_max);

/* Apply the graph to a prior for target given a set of active categories:
 * multiplies the direct edges from each distinct active category to
 * target, clips to [m_min, m_max], and applies that to the prior's odds.
 * Order of active does not matter. */
CB_API cb_status_t cb_graph_adjust_prior(const cb_graph_t *graph,
                                         int32_t target,
                                         const int32_t *active,
                                         size_t n_active,
                                         double prior,
                                         double *out_adjusted_prior,
                                         double *out_multiplier);

/* True if active contains two distinct categories with no edge between
 * them in either direction. A structural check only -- it says nothing
 * about whether the data is trustworthy. */
CB_API cb_status_t cb_graph_has_unrelated_pair(const cb_graph_t *graph,
                                               const int32_t *active,
                                               size_t n_active,
                                               int32_t *out_unrelated);

#ifdef __cplusplus
} /* extern "C" */
#endif

#endif /* CITYBRAIN_GRAPH_H */
