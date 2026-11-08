/*
 * Expected-cost decision rule and action ladder. Given a posterior
 * probability and a cost table, picks the cheaper action deterministically
 * (ties go to the passive action), so the same input always gives the
 * same output and a decision can be replayed from a log.
 */
#ifndef CITYBRAIN_DECISION_H
#define CITYBRAIN_DECISION_H

#include "citybrain/common.h"

#ifdef __cplusplus
extern "C" {
#endif

#define CB_DECISION_MAX_ACTIONS ((size_t)4096)

typedef enum cb_action {
    CB_ACTION_HOLD = 0,
    CB_ACTION_ACT  = 1
} cb_action_t;

typedef struct cb_cost_table {
    double cost_act_when_false;
    double cost_hold_when_true;
    double cost_act_when_true;
    double cost_hold_when_false;
} cb_cost_table_t;

typedef struct cb_decision_result {
    int32_t action;
    double  expected_cost_act;
    double  expected_cost_hold;
    double  indifference_threshold; /* -1.0 if one action always dominates */
    double  margin;
} cb_decision_result_t;

/* Default: act_when_false=1.0, hold_when_true=20.0, the other two 0.0
 * (indifference threshold 1/21). */
CB_API cb_status_t cb_cost_table_init(cb_cost_table_t *table);

/* Pick ACT or HOLD by expected cost. Errors: CB_ERR_DOMAIN if posterior is
 * outside [0, 1]. */
CB_API cb_status_t cb_decision_expected_cost(const cb_cost_table_t *table,
                                             double posterior,
                                             cb_decision_result_t *out);

typedef struct cb_action_cost {
    int32_t action_id;
    double  cost_if_true;
    double  cost_if_false;
} cb_action_cost_t;

/* Same idea for more than two actions. Ties go to the lowest index, so
 * list actions most-passive-first. Errors: CB_ERR_DOMAIN for n_actions==0
 * or posterior outside [0,1]; CB_ERR_CAPACITY above 4096 actions. */
CB_API cb_status_t cb_decision_ladder(const cb_action_cost_t *actions,
                                      size_t n_actions,
                                      double posterior,
                                      size_t *out_index,
                                      double *out_expected_cost);

#ifdef __cplusplus
} /* extern "C" */
#endif

#endif /* CITYBRAIN_DECISION_H */
