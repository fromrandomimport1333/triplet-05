/* Expected-cost decision rule and action ladder. */
#include "cb_test.h"

static void test_two_action_rule(void)
{
    cb_cost_table_t t;
    cb_decision_result_t r;

    CB_CASE("defaults put the indifference threshold at 1/21");
    CB_CHECK_OK(cb_cost_table_init(&t));
    CB_CHECK_OK(cb_decision_expected_cost(&t, 0.0, &r));
    CB_CHECK_NEAR(r.indifference_threshold, 1.0 / 21.0, 1e-15);

    CB_CASE("below the threshold the rule holds, above it acts");
    CB_CHECK_OK(cb_decision_expected_cost(&t, 0.04, &r));
    CB_CHECK_EQ_INT(r.action, CB_ACTION_HOLD);
    CB_CHECK_OK(cb_decision_expected_cost(&t, 0.06, &r));
    CB_CHECK_EQ_INT(r.action, CB_ACTION_ACT);

    CB_CASE("expected costs are the textbook values");
    CB_CHECK_OK(cb_decision_expected_cost(&t, 0.25, &r));
    CB_CHECK_NEAR(r.expected_cost_act, 0.75, 1e-15);
    CB_CHECK_NEAR(r.expected_cost_hold, 5.0, 1e-15);
    CB_CHECK_NEAR(r.margin, 4.25, 1e-15);

    CB_CASE("an exact tie holds, deterministically");
    CB_CHECK_OK(cb_decision_expected_cost(&t, 1.0 / 21.0, &r));
    CB_CHECK_NEAR(r.expected_cost_act, r.expected_cost_hold, 1e-15);
    CB_CHECK_EQ_INT(r.action, CB_ACTION_HOLD);

    CB_CASE("posterior boundaries 0 and 1 are legal");
    CB_CHECK_OK(cb_decision_expected_cost(&t, 0.0, &r));
    CB_CHECK_EQ_INT(r.action, CB_ACTION_HOLD);
    CB_CHECK_NEAR(r.expected_cost_act, 1.0, 0.0);
    CB_CHECK_OK(cb_decision_expected_cost(&t, 1.0, &r));
    CB_CHECK_EQ_INT(r.action, CB_ACTION_ACT);
    CB_CHECK_NEAR(r.expected_cost_hold, 20.0, 0.0);

    CB_CASE("a posterior outside [0,1] is a domain error");
    CB_CHECK_STATUS(cb_decision_expected_cost(&t, -0.001, &r), CB_ERR_DOMAIN);
    CB_CHECK_STATUS(cb_decision_expected_cost(&t, 1.001, &r), CB_ERR_DOMAIN);
    CB_CHECK_STATUS(cb_decision_expected_cost(&t, (double)NAN, &r), CB_ERR_NOT_FINITE);
    CB_CHECK_STATUS(cb_decision_expected_cost(NULL, 0.5, &r), CB_ERR_NULL);
    CB_CHECK_STATUS(cb_decision_expected_cost(&t, 0.5, NULL), CB_ERR_NULL);

    CB_CASE("non-finite costs are rejected");
    CB_CHECK_OK(cb_cost_table_init(&t));
    t.cost_hold_when_true = HUGE_VAL;
    CB_CHECK_STATUS(cb_decision_expected_cost(&t, 0.5, &r), CB_ERR_NOT_FINITE);

    CB_CASE("a dominated action still decides, with a sentinel threshold");
    CB_CHECK_OK(cb_cost_table_init(&t));
    t.cost_act_when_false = 0.0;
    t.cost_hold_when_true = 0.0;
    t.cost_act_when_true = 0.0;
    t.cost_hold_when_false = 0.0;
    CB_CHECK_OK(cb_decision_expected_cost(&t, 0.5, &r));
    CB_CHECK_EQ_INT(r.action, CB_ACTION_HOLD);
    CB_CHECK_NEAR(r.indifference_threshold, -1.0, 0.0);

    CB_CASE("negative costs read as benefits and still decide");
    CB_CHECK_OK(cb_cost_table_init(&t));
    t.cost_act_when_true = -5.0;
    CB_CHECK_OK(cb_decision_expected_cost(&t, 0.5, &r));
    CB_CHECK_NEAR(r.expected_cost_act, -2.0, 1e-15);
    CB_CHECK_EQ_INT(r.action, CB_ACTION_ACT);

    CB_CASE("the rule is monotone in the posterior");
    {
        int i;
        int switched = 0;
        int last = CB_ACTION_HOLD;
        CB_CHECK_OK(cb_cost_table_init(&t));
        for (i = 0; i <= 1000; ++i) {
            double p = (double)i / 1000.0;
            CB_CHECK_OK(cb_decision_expected_cost(&t, p, &r));
            if (r.action != last) {
                switched++;
                last = r.action;
            }
        }
        CB_CHECK_EQ_INT(switched, 1);
        CB_CHECK_EQ_INT(last, CB_ACTION_ACT);
    }
}

static void test_ladder(void)
{
    cb_action_cost_t rungs[4];
    size_t index;
    double ec;

    CB_CASE("the ladder picks the cheapest rung");
    rungs[0].action_id = 0; rungs[0].cost_if_true = 20.0; rungs[0].cost_if_false = 0.0;
    rungs[1].action_id = 1; rungs[1].cost_if_true = 5.0;  rungs[1].cost_if_false = 1.0;
    rungs[2].action_id = 2; rungs[2].cost_if_true = 0.0;  rungs[2].cost_if_false = 6.0;
    CB_CHECK_OK(cb_decision_ladder(rungs, 3, 0.0, &index, &ec));
    CB_CHECK_EQ_INT(index, 0);
    CB_CHECK_NEAR(ec, 0.0, 0.0);
    CB_CHECK_OK(cb_decision_ladder(rungs, 3, 0.5, &index, &ec));
    CB_CHECK_EQ_INT(index, 1);
    CB_CHECK_NEAR(ec, 3.0, 1e-15);
    CB_CHECK_OK(cb_decision_ladder(rungs, 3, 1.0, &index, &ec));
    CB_CHECK_EQ_INT(index, 2);
    CB_CHECK_NEAR(ec, 0.0, 0.0);

    CB_CASE("ties go to the lowest index, so order the ladder conservatively");
    rungs[0].cost_if_true = 1.0; rungs[0].cost_if_false = 1.0;
    rungs[1].cost_if_true = 1.0; rungs[1].cost_if_false = 1.0;
    rungs[2].cost_if_true = 1.0; rungs[2].cost_if_false = 1.0;
    CB_CHECK_OK(cb_decision_ladder(rungs, 3, 0.37, &index, &ec));
    CB_CHECK_EQ_INT(index, 0);

    CB_CASE("a single rung is always chosen");
    CB_CHECK_OK(cb_decision_ladder(rungs, 1, 0.9, &index, &ec));
    CB_CHECK_EQ_INT(index, 0);

    CB_CASE("ladder argument domains");
    CB_CHECK_STATUS(cb_decision_ladder(rungs, 0, 0.5, &index, &ec), CB_ERR_DOMAIN);
    CB_CHECK_STATUS(cb_decision_ladder(NULL, 3, 0.5, &index, &ec), CB_ERR_NULL);
    CB_CHECK_STATUS(cb_decision_ladder(rungs, 3, -0.1, &index, &ec), CB_ERR_DOMAIN);
    CB_CHECK_STATUS(cb_decision_ladder(rungs, 3, 1.1, &index, &ec), CB_ERR_DOMAIN);
    CB_CHECK_STATUS(cb_decision_ladder(rungs, CB_DECISION_MAX_ACTIONS + 1, 0.5,
                                       &index, &ec), CB_ERR_CAPACITY);
    rungs[1].cost_if_false = (double)NAN;
    CB_CHECK_STATUS(cb_decision_ladder(rungs, 3, 0.5, &index, &ec), CB_ERR_NOT_FINITE);
    rungs[1].cost_if_false = 1.0;
    CB_CHECK_OK(cb_decision_ladder(rungs, 3, 0.5, NULL, NULL));
}

int main(void)
{
    test_two_action_rule();
    test_ladder();
    return cb_t_report("test_decision");
}
