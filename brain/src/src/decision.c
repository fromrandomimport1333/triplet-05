#include "citybrain/decision.h"
#include "internal.h"

cb_status_t cb_cost_table_init(cb_cost_table_t *table)
{
    CB_REQUIRE(table != NULL, CB_ERR_NULL);
    /* Illustrative: a missed true case costs twenty false positives. */
    table->cost_act_when_false  = 1.0;
    table->cost_hold_when_true  = 20.0;
    table->cost_act_when_true   = 0.0;
    table->cost_hold_when_false = 0.0;
    return CB_OK;
}

cb_status_t cb_decision_expected_cost(const cb_cost_table_t *table,
                                      double posterior,
                                      cb_decision_result_t *out)
{
    double ec_act, ec_hold, denom;

    CB_REQUIRE(table != NULL && out != NULL, CB_ERR_NULL);
    CB_REQUIRE(cb_finite(table->cost_act_when_false) &&
               cb_finite(table->cost_hold_when_true) &&
               cb_finite(table->cost_act_when_true) &&
               cb_finite(table->cost_hold_when_false) &&
               cb_finite(posterior), CB_ERR_NOT_FINITE);
    CB_REQUIRE(posterior >= 0.0 && posterior <= 1.0, CB_ERR_DOMAIN);

    ec_act  = posterior * table->cost_act_when_true
            + (1.0 - posterior) * table->cost_act_when_false;
    ec_hold = posterior * table->cost_hold_when_true
            + (1.0 - posterior) * table->cost_hold_when_false;

    out->expected_cost_act  = ec_act;
    out->expected_cost_hold = ec_hold;
    out->margin             = ec_hold - ec_act;
    /* Strict inequality: an exact tie holds, the passive action wins. */
    out->action = (ec_act < ec_hold) ? (int32_t)CB_ACTION_ACT
                                     : (int32_t)CB_ACTION_HOLD;

    denom = (table->cost_hold_when_true - table->cost_hold_when_false)
          + (table->cost_act_when_false - table->cost_act_when_true);
    if (denom > 0.0 && cb_finite(denom)) {
        double t = (table->cost_act_when_false - table->cost_hold_when_false) / denom;
        out->indifference_threshold = cb_finite(t) ? t : -1.0;
    } else {
        /* One action dominates everywhere; -1.0 is a sentinel, not a probability. */
        out->indifference_threshold = -1.0;
    }
    return CB_OK;
}

cb_status_t cb_decision_ladder(const cb_action_cost_t *actions, size_t n_actions,
                               double posterior,
                               size_t *out_index, double *out_expected_cost)
{
    size_t i, best = 0;
    double best_cost = 0.0;

    CB_REQUIRE(actions != NULL, CB_ERR_NULL);
    CB_REQUIRE(cb_finite(posterior), CB_ERR_NOT_FINITE);
    CB_REQUIRE(posterior >= 0.0 && posterior <= 1.0, CB_ERR_DOMAIN);
    CB_REQUIRE(n_actions > 0, CB_ERR_DOMAIN);
    CB_REQUIRE(n_actions <= CB_DECISION_MAX_ACTIONS, CB_ERR_CAPACITY);

    for (i = 0; i < n_actions; ++i) {
        CB_REQUIRE(cb_finite(actions[i].cost_if_true) &&
                   cb_finite(actions[i].cost_if_false), CB_ERR_NOT_FINITE);
    }
    for (i = 0; i < n_actions; ++i) {
        double ec = posterior * actions[i].cost_if_true
                  + (1.0 - posterior) * actions[i].cost_if_false;
        /* Strict less-than: lowest index wins ties, so order the ladder passive-first. */
        if (i == 0 || ec < best_cost) {
            best_cost = ec;
            best = i;
        }
    }
    if (out_index != NULL)         { *out_index = best; }
    if (out_expected_cost != NULL) { *out_expected_cost = best_cost; }
    return CB_OK;
}
