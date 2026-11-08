/*
 * EXAMPLE PROFILE -- sample data, not part of the generic library.
 * See include/citybrain/example_profile.h.
 */
#include "citybrain/example_profile.h"
#include "internal.h"

cb_status_t cb_example_load_graph(cb_graph_t *graph)
{
    cb_edge_kind_t kind;
    double m;

    CB_REQUIRE(graph != NULL, CB_ERR_NULL);
    /* Probe one in-range edge so an uninitialised or undersized graph is
     * reported before any edge is written. */
    CB_CHECK(cb_graph_get_edge(graph, 0, 1, &kind, &m));
    CB_REQUIRE(graph->cfg.n_nodes >= (int32_t)CB_EXAMPLE_CATEGORY_COUNT,
               CB_ERR_DOMAIN);

    CB_CHECK(cb_graph_set_edge(graph, CB_EXAMPLE_ACCIDENT, CB_EXAMPLE_JAM,
                               CB_EDGE_RAISES, 3.0));
    CB_CHECK(cb_graph_set_edge(graph, CB_EXAMPLE_FLOOD, CB_EXAMPLE_ACCIDENT,
                               CB_EDGE_RAISES, 1.5));
    CB_CHECK(cb_graph_set_edge(graph, CB_EXAMPLE_FLOOD, CB_EXAMPLE_JAM,
                               CB_EDGE_RAISES, 1.3));
    CB_CHECK(cb_graph_set_edge(graph, CB_EXAMPLE_FIRE, CB_EXAMPLE_JAM,
                               CB_EDGE_RAISES, 1.5));
    CB_CHECK(cb_graph_set_edge(graph, CB_EXAMPLE_FLOOD, CB_EXAMPLE_FIRE,
                               CB_EDGE_LOWERS, 0.5));
    CB_CHECK(cb_graph_set_edge(graph, CB_EXAMPLE_FIRE, CB_EXAMPLE_FLOOD,
                               CB_EDGE_LOWERS, 0.5));
    return CB_OK;
}
