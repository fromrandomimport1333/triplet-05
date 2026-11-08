/* Tabular MDP policy table (stretch goal). */
#include "cb_test.h"

static void test_two_state(void)
{
    /* Two states (quiet, active), two actions (hold, act); acting from active
     * resolves it, acting from quiet wastes a resource. The hand-obvious optimal
     * policy is what the solver is checked against. */
    double transition[2 * 2 * 2];
    double reward[2 * 2];
    double scratch[4];
    int32_t policy[2];
    double value[2];
    int32_t iters = 0;
    cb_mdp_problem_t prob;
    size_t need;

    CB_CASE("a hand-checkable two-state problem gives the obvious policy");
    /* state 0, action 0 (hold): stay quiet */
    transition[(0 * 2 + 0) * 2 + 0] = 0.9;
    transition[(0 * 2 + 0) * 2 + 1] = 0.1;
    /* state 0, action 1 (act): also stays quiet, but wastes a resource */
    transition[(0 * 2 + 1) * 2 + 0] = 0.9;
    transition[(0 * 2 + 1) * 2 + 1] = 0.1;
    /* state 1, action 0 (hold): the condition persists */
    transition[(1 * 2 + 0) * 2 + 0] = 0.1;
    transition[(1 * 2 + 0) * 2 + 1] = 0.9;
    /* state 1, action 1 (act): the condition resolves */
    transition[(1 * 2 + 1) * 2 + 0] = 0.95;
    transition[(1 * 2 + 1) * 2 + 1] = 0.05;

    reward[0 * 2 + 0] =  0.0;  /* quiet, hold */
    reward[0 * 2 + 1] = -1.0;  /* quiet, act: wasted */
    reward[1 * 2 + 0] = -5.0;  /* active, hold: the cost continues */
    reward[1 * 2 + 1] = -1.0;  /* active, act: cost of the resource */

    CB_CHECK_OK(cb_mdp_problem_init(&prob));
    prob.transition = transition;
    prob.reward = reward;
    prob.n_states = 2;
    prob.n_actions = 2;

    CB_CHECK_OK(cb_mdp_scratch_doubles(2, &need));
    CB_CHECK_EQ_INT(need, 4);
    CB_CHECK_OK(cb_mdp_solve(&prob, policy, value, scratch, 4, &iters));
    CB_CHECK_EQ_INT(policy[0], 0);
    CB_CHECK_EQ_INT(policy[1], 1);
    CB_CHECK_TRUE(iters > 0);
    CB_CHECK_TRUE(value[0] > value[1]);
    printf("  two-state MDP converged in %d iterations, V = [%.4f, %.4f]\n",
           iters, value[0], value[1]);

    CB_CASE("the solution satisfies the Bellman equation");
    {
        int s, a, s2;
        for (s = 0; s < 2; ++s) {
            double best = -1e300;
            for (a = 0; a < 2; ++a) {
                double q = reward[s * 2 + a];
                for (s2 = 0; s2 < 2; ++s2) {
                    q += prob.discount * transition[(s * 2 + a) * 2 + s2] * value[s2];
                }
                if (q > best) { best = q; }
            }
            CB_CHECK_NEAR(value[s], best, 1e-8);
        }
    }

    CB_CASE("outputs are optional");
    CB_CHECK_OK(cb_mdp_solve(&prob, NULL, NULL, scratch, 4, NULL));

    CB_CASE("a tight iteration limit reports NO_CONVERGE but still writes a policy");
    prob.max_iterations = 1;
    prob.tolerance = 1e-14;
    CB_CHECK_STATUS(cb_mdp_solve(&prob, policy, value, scratch, 4, &iters),
                    CB_ERR_NO_CONVERGE);
    CB_CHECK_EQ_INT(iters, 1);
    CB_CHECK_TRUE(policy[0] == 0 || policy[0] == 1);
}

static void test_mdp_domains(void)
{
    double transition[2 * 1 * 2];
    double reward[2 * 1];
    double scratch[4];
    cb_mdp_problem_t prob;
    int32_t policy[2];

    CB_CHECK_OK(cb_mdp_problem_init(&prob));
    transition[0] = 1.0; transition[1] = 0.0;
    transition[2] = 0.0; transition[3] = 1.0;
    reward[0] = 1.0; reward[1] = 2.0;
    prob.transition = transition;
    prob.reward = reward;
    prob.n_states = 2;
    prob.n_actions = 1;

    CB_CASE("a single-action problem is legal");
    CB_CHECK_OK(cb_mdp_solve(&prob, policy, NULL, scratch, 4, NULL));
    CB_CHECK_EQ_INT(policy[0], 0);

    CB_CASE("a transition row that does not sum to one is rejected");
    transition[1] = 0.5;
    CB_CHECK_STATUS(cb_mdp_solve(&prob, policy, NULL, scratch, 4, NULL),
                    CB_ERR_DOMAIN);
    transition[1] = 0.0;

    CB_CASE("a negative transition probability is rejected");
    transition[0] = 1.5; transition[1] = -0.5;
    CB_CHECK_STATUS(cb_mdp_solve(&prob, policy, NULL, scratch, 4, NULL),
                    CB_ERR_DOMAIN);
    transition[0] = 1.0; transition[1] = 0.0;

    CB_CASE("non-finite model entries are rejected");
    reward[0] = (double)NAN;
    CB_CHECK_STATUS(cb_mdp_solve(&prob, policy, NULL, scratch, 4, NULL),
                    CB_ERR_NOT_FINITE);
    reward[0] = 1.0;

    CB_CASE("parameter domains");
    prob.discount = 1.0;
    CB_CHECK_STATUS(cb_mdp_solve(&prob, policy, NULL, scratch, 4, NULL),
                    CB_ERR_DOMAIN);
    prob.discount = -0.1;
    CB_CHECK_STATUS(cb_mdp_solve(&prob, policy, NULL, scratch, 4, NULL),
                    CB_ERR_DOMAIN);
    prob.discount = 0.9;
    prob.tolerance = 0.0;
    CB_CHECK_STATUS(cb_mdp_solve(&prob, policy, NULL, scratch, 4, NULL),
                    CB_ERR_DOMAIN);
    prob.tolerance = 1e-10;
    prob.max_iterations = 0;
    CB_CHECK_STATUS(cb_mdp_solve(&prob, policy, NULL, scratch, 4, NULL),
                    CB_ERR_DOMAIN);
    prob.max_iterations = 100;
    prob.n_states = 0;
    CB_CHECK_STATUS(cb_mdp_solve(&prob, policy, NULL, scratch, 4, NULL),
                    CB_ERR_DOMAIN);
    prob.n_states = CB_MDP_MAX_STATES + 1;
    CB_CHECK_STATUS(cb_mdp_solve(&prob, policy, NULL, scratch, 4, NULL),
                    CB_ERR_CAPACITY);
    prob.n_states = 2;
    CB_CHECK_STATUS(cb_mdp_solve(&prob, policy, NULL, scratch, 3, NULL),
                    CB_ERR_CAPACITY);
    CB_CHECK_STATUS(cb_mdp_solve(&prob, policy, NULL, NULL, 4, NULL), CB_ERR_NULL);
    CB_CHECK_STATUS(cb_mdp_solve(NULL, policy, NULL, scratch, 4, NULL), CB_ERR_NULL);
    prob.transition = NULL;
    CB_CHECK_STATUS(cb_mdp_solve(&prob, policy, NULL, scratch, 4, NULL), CB_ERR_NULL);

    CB_CASE("scratch sizing domains");
    {
        size_t need;
        CB_CHECK_STATUS(cb_mdp_scratch_doubles(0, &need), CB_ERR_DOMAIN);
        CB_CHECK_STATUS(cb_mdp_scratch_doubles(CB_MDP_MAX_STATES + 1, &need),
                        CB_ERR_CAPACITY);
        CB_CHECK_STATUS(cb_mdp_scratch_doubles(4, NULL), CB_ERR_NULL);
    }
}

static void test_state_index(void)
{
    static const int32_t dims[3] = { 4, 4, 8 };
    int32_t idx, count;
    int32_t coords[3], back[3];

    CB_CASE("mixed-radix index and decode are inverses over the whole space");
    CB_CHECK_OK(cb_mdp_state_count(dims, 3, &count));
    CB_CHECK_EQ_INT(count, 4 * 4 * 8);
    for (coords[0] = 0; coords[0] < dims[0]; ++coords[0]) {
        for (coords[1] = 0; coords[1] < dims[1]; ++coords[1]) {
            for (coords[2] = 0; coords[2] < dims[2]; ++coords[2]) {
                CB_CHECK_OK(cb_mdp_state_index(dims, coords, 3, &idx));
                CB_CHECK_TRUE(idx >= 0 && idx < count);
                CB_CHECK_OK(cb_mdp_state_decode(dims, 3, idx, back));
                CB_CHECK_EQ_INT(back[0], coords[0]);
                CB_CHECK_EQ_INT(back[1], coords[1]);
                CB_CHECK_EQ_INT(back[2], coords[2]);
            }
        }
    }

    CB_CASE("the layout is row-major with the last dimension fastest");
    coords[0] = 0; coords[1] = 0; coords[2] = 1;
    CB_CHECK_OK(cb_mdp_state_index(dims, coords, 3, &idx));
    CB_CHECK_EQ_INT(idx, 1);
    coords[0] = 0; coords[1] = 1; coords[2] = 0;
    CB_CHECK_OK(cb_mdp_state_index(dims, coords, 3, &idx));
    CB_CHECK_EQ_INT(idx, 8);
    coords[0] = 1; coords[1] = 0; coords[2] = 0;
    CB_CHECK_OK(cb_mdp_state_index(dims, coords, 3, &idx));
    CB_CHECK_EQ_INT(idx, 32);

    CB_CASE("a single dimension degenerates to the identity");
    {
        static const int32_t one[1] = { 5 };
        coords[0] = 3;
        CB_CHECK_OK(cb_mdp_state_index(one, coords, 1, &idx));
        CB_CHECK_EQ_INT(idx, 3);
    }

    CB_CASE("index argument domains");
    coords[0] = 0; coords[1] = 0; coords[2] = 0;
    CB_CHECK_STATUS(cb_mdp_state_index(dims, coords, 0, &idx), CB_ERR_DOMAIN);
    CB_CHECK_STATUS(cb_mdp_state_index(dims, coords, CB_MDP_MAX_DIMS + 1, &idx),
                    CB_ERR_DOMAIN);
    CB_CHECK_STATUS(cb_mdp_state_index(NULL, coords, 3, &idx), CB_ERR_NULL);
    CB_CHECK_STATUS(cb_mdp_state_index(dims, NULL, 3, &idx), CB_ERR_NULL);
    CB_CHECK_STATUS(cb_mdp_state_index(dims, coords, 3, NULL), CB_ERR_NULL);
    coords[2] = 8;
    CB_CHECK_STATUS(cb_mdp_state_index(dims, coords, 3, &idx), CB_ERR_DOMAIN);
    coords[2] = -1;
    CB_CHECK_STATUS(cb_mdp_state_index(dims, coords, 3, &idx), CB_ERR_DOMAIN);
    {
        static const int32_t zero[2] = { 0, 3 };
        coords[0] = 0; coords[1] = 0;
        CB_CHECK_STATUS(cb_mdp_state_index(zero, coords, 2, &idx), CB_ERR_DOMAIN);
    }
    {
        static const int32_t huge[3] = { 100000, 100000, 100000 };
        CB_CHECK_STATUS(cb_mdp_state_count(huge, 3, &idx), CB_ERR_CAPACITY);
    }
    CB_CHECK_STATUS(cb_mdp_state_decode(dims, 3, -1, back), CB_ERR_DOMAIN);
    CB_CHECK_STATUS(cb_mdp_state_decode(dims, 3, count, back), CB_ERR_DOMAIN);
    CB_CHECK_STATUS(cb_mdp_state_decode(dims, 3, 0, NULL), CB_ERR_NULL);
}

int main(void)
{
    test_two_state();
    test_mdp_domains();
    test_state_index();
    return cb_t_report("test_mdp");
}
