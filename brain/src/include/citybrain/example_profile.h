/*
 * Sample data for the correlation graph -- not part of the generic
 * library. One worked category set and edge list for demos and docs.
 * Nothing else in this library refers to it.
 */
#ifndef CITYBRAIN_EXAMPLE_PROFILE_H
#define CITYBRAIN_EXAMPLE_PROFILE_H

#include "citybrain/common.h"
#include "citybrain/graph.h"

#ifdef __cplusplus
extern "C" {
#endif

typedef enum cb_example_category {
    CB_EXAMPLE_FLOOD    = 0,
    CB_EXAMPLE_FIRE     = 1,
    CB_EXAMPLE_ACCIDENT = 2,
    CB_EXAMPLE_JAM      = 3,
    CB_EXAMPLE_CATEGORY_COUNT = 4
} cb_example_category_t;

/*
 * Loads a worked edge set on an already-initialised graph with at least
 * 4 nodes: ACCIDENT->JAM x3, FLOOD->ACCIDENT x1.5, FLOOD->JAM x1.3,
 * FIRE->JAM x1.5, FLOOD<->FIRE x0.5. Illustrative numbers, not calibrated
 * values.
 */
CB_API cb_status_t cb_example_load_graph(cb_graph_t *graph);

#ifdef __cplusplus
} /* extern "C" */
#endif

#endif /* CITYBRAIN_EXAMPLE_PROFILE_H */
