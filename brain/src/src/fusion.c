#include "citybrain/fusion.h"
#include "internal.h"

/* Logit / logistic */

cb_status_t cb_logit(double probability, double *out_log_odds)
{
    CB_REQUIRE(out_log_odds != NULL, CB_ERR_NULL);
    CB_REQUIRE(cb_finite(probability), CB_ERR_NOT_FINITE);
    CB_REQUIRE(probability > 0.0 && probability < 1.0, CB_ERR_DOMAIN);
    /* log(p) - log1p(-p) keeps precision near both ends of the interval. */
    *out_log_odds = log(probability) - log1p(-probability);
    return CB_OK;
}

cb_status_t cb_logistic(double log_odds, double *out_probability)
{
    double p;

    CB_REQUIRE(out_probability != NULL, CB_ERR_NULL);
    CB_REQUIRE(cb_finite(log_odds), CB_ERR_NOT_FINITE);

    if (log_odds >= 0.0) {
        p = 1.0 / (1.0 + exp(-log_odds));
    } else {
        double e = exp(log_odds);
        p = e / (1.0 + e);
    }
    *out_probability = p;
    return CB_OK;
}

/* Configuration */

cb_status_t cb_fusion_config_init(cb_fusion_config_t *cfg)
{
    CB_REQUIRE(cfg != NULL, CB_ERR_NULL);
    cfg->lr_cap            = 8.0;   /* illustrative, design brief section 6 */
    cfg->prior             = 0.02;  /* illustrative */
    cfg->posterior_floor   = 0.0;
    cfg->posterior_ceiling = 1.0;
    cfg->group_cap_enabled = 1;
    return CB_OK;
}

static cb_status_t fusion_config_validate(const cb_fusion_config_t *cfg)
{
    CB_REQUIRE(cfg != NULL, CB_ERR_NULL);
    CB_REQUIRE(cb_finite(cfg->lr_cap) && cb_finite(cfg->prior) &&
               cb_finite(cfg->posterior_floor) && cb_finite(cfg->posterior_ceiling),
               CB_ERR_NOT_FINITE);
    CB_REQUIRE(cfg->lr_cap > 1.0, CB_ERR_DOMAIN);
    CB_REQUIRE(cfg->prior > 0.0 && cfg->prior < 1.0, CB_ERR_DOMAIN);
    CB_REQUIRE(cfg->posterior_floor >= 0.0 && cfg->posterior_floor < 1.0, CB_ERR_DOMAIN);
    CB_REQUIRE(cfg->posterior_ceiling > cfg->posterior_floor &&
               cfg->posterior_ceiling <= 1.0, CB_ERR_DOMAIN);
    CB_REQUIRE(cfg->group_cap_enabled == 0 || cfg->group_cap_enabled == 1, CB_ERR_DOMAIN);
    return CB_OK;
}

/* Fusion core, used by both entry points below. */

static cb_status_t fusion_core(const cb_fusion_config_t *cfg,
                               const cb_evidence_t *items,
                               size_t n_items,
                               double *group_scratch,
                               size_t n_groups,
                               int use_groups,
                               cb_fusion_result_t *out)
{
    double ln_cap;
    double prior_log_odds;
    double evidence = 0.0;
    double posterior_log_odds;
    double posterior;
    size_t i;
    int32_t n_items_clamped = 0;
    int32_t n_groups_clamped = 0;
    int32_t n_groups_used = 0;

    CB_REQUIRE(out != NULL, CB_ERR_NULL);
    CB_CHECK(fusion_config_validate(cfg));
    CB_REQUIRE(n_items == 0 || items != NULL, CB_ERR_NULL);
    CB_REQUIRE(n_items <= CB_FUSION_MAX_ITEMS, CB_ERR_CAPACITY);

    if (use_groups) {
        CB_REQUIRE(n_groups > 0, CB_ERR_DOMAIN);
        CB_REQUIRE(n_groups <= CB_FUSION_MAX_GROUPS, CB_ERR_CAPACITY);
        CB_REQUIRE(group_scratch != NULL, CB_ERR_NULL);
    }

    ln_cap = log(cfg->lr_cap);           /* > 0 because lr_cap > 1 */
    CB_CHECK(cb_logit(cfg->prior, &prior_log_odds));

    /* Validate every item before mutating any caller memory, so a rejected
     * call leaves the scratch buffer untouched. */
    for (i = 0; i < n_items; ++i) {
        CB_REQUIRE(cb_finite(items[i].likelihood_ratio) && cb_finite(items[i].weight),
                   CB_ERR_NOT_FINITE);
        CB_REQUIRE(items[i].likelihood_ratio > 0.0, CB_ERR_DOMAIN);
        CB_REQUIRE(items[i].weight >= 0.0 && items[i].weight <= 1.0, CB_ERR_DOMAIN);
        if (use_groups) {
            CB_REQUIRE(items[i].group_id >= 0 &&
                       (size_t)items[i].group_id < n_groups, CB_ERR_DOMAIN);
        }
    }

    if (use_groups) {
        /* NaN marks "group not touched". No valid contribution is NaN, so
         * this distinguishes an untouched group from one summing to zero. */
        for (i = 0; i < n_groups; ++i) {
            group_scratch[i] = (double)NAN;
        }
    }

    for (i = 0; i < n_items; ++i) {
        double raw = log(items[i].likelihood_ratio);
        double capped = cb_clamp(raw, -ln_cap, ln_cap);
        double contribution;

        if (raw > ln_cap || raw < -ln_cap) {
            n_items_clamped++;
        }
        contribution = items[i].weight * capped;

        if (use_groups) {
            size_t g = (size_t)items[i].group_id;
            if (isnan(group_scratch[g])) {
                group_scratch[g] = contribution;
            } else {
                group_scratch[g] += contribution;
            }
        } else {
            /* Each item is its own group. Because weight <= 1 the weighted
             * contribution already satisfies |contribution| <= ln_cap, so the
             * group clip would be a no-op and is skipped. */
            evidence += contribution;
            n_groups_used++;
        }
    }

    if (use_groups) {
        /* Summed in increasing group id so the total is a deterministic
         * function of the inputs, independent of item order across groups. */
        for (i = 0; i < n_groups; ++i) {
            double total;
            if (isnan(group_scratch[i])) {
                continue;
            }
            total = group_scratch[i];
            n_groups_used++;
            if (total > ln_cap || total < -ln_cap) {
                n_groups_clamped++;
            }
            evidence += cb_clamp(total, -ln_cap, ln_cap);
        }
    }

    posterior_log_odds = prior_log_odds + evidence;
    CB_CHECK(cb_logistic(posterior_log_odds, &posterior));
    posterior = cb_clamp(posterior, cfg->posterior_floor, cfg->posterior_ceiling);

    out->posterior          = posterior;
    out->prior_log_odds     = prior_log_odds;
    out->evidence_log_odds  = evidence;
    out->posterior_log_odds = posterior_log_odds;
    out->n_items            = (int32_t)n_items;
    out->n_groups_used      = n_groups_used;
    out->n_items_clamped    = n_items_clamped;
    out->n_groups_clamped   = n_groups_clamped;
    return CB_OK;
}

cb_status_t cb_fusion_posterior(const cb_fusion_config_t *cfg,
                                const cb_evidence_t *items,
                                size_t n_items,
                                double *group_scratch,
                                size_t n_groups,
                                cb_fusion_result_t *out)
{
    int use_groups;

    CB_REQUIRE(cfg != NULL, CB_ERR_NULL);
    use_groups = (cfg->group_cap_enabled != 0);
    return fusion_core(cfg, items, n_items, group_scratch, n_groups, use_groups, out);
}

cb_status_t cb_fusion_posterior_independent(const cb_fusion_config_t *cfg,
                                            const cb_evidence_t *items,
                                            size_t n_items,
                                            cb_fusion_result_t *out)
{
    return fusion_core(cfg, items, n_items, NULL, 0, 0, out);
}

/* Fault-tolerance inequality */

cb_status_t cb_ft_params_init(cb_ft_params_t *params)
{
    CB_REQUIRE(params != NULL, CB_ERR_NULL);
    params->lr_cap          = 8.0;  /* design brief worked example */
    params->prior           = 0.02;
    params->threshold       = 0.20;
    params->max_weight      = 1.0;
    params->odds_multiplier = 1.0;
    return CB_OK;
}

/* Shared derived quantities: L0 is the adjusted prior log-odds
 * logit(p0)+ln(m), step is w*ln(c), target is logit(T). Kept in one place
 * so the three functions below stay consistent. */
static cb_status_t ft_derive(const cb_ft_params_t *p,
                             double *out_l0, double *out_step, double *out_target)
{
    double logit_prior, logit_target;

    CB_REQUIRE(p != NULL, CB_ERR_NULL);
    CB_REQUIRE(cb_finite(p->lr_cap) && cb_finite(p->prior) &&
               cb_finite(p->threshold) && cb_finite(p->max_weight) &&
               cb_finite(p->odds_multiplier), CB_ERR_NOT_FINITE);
    CB_REQUIRE(p->lr_cap > 1.0, CB_ERR_DOMAIN);
    CB_REQUIRE(p->prior > 0.0 && p->prior < 1.0, CB_ERR_DOMAIN);
    CB_REQUIRE(p->threshold > 0.0 && p->threshold < 1.0, CB_ERR_DOMAIN);
    CB_REQUIRE(p->max_weight > 0.0 && p->max_weight <= 1.0, CB_ERR_DOMAIN);
    CB_REQUIRE(p->odds_multiplier > 0.0, CB_ERR_DOMAIN);

    CB_CHECK(cb_logit(p->prior, &logit_prior));
    CB_CHECK(cb_logit(p->threshold, &logit_target));

    *out_l0     = logit_prior + log(p->odds_multiplier);
    *out_step   = p->max_weight * log(p->lr_cap);
    *out_target = logit_target;

    CB_REQUIRE(cb_finite(*out_l0) && cb_finite(*out_step), CB_ERR_NOT_FINITE);
    CB_REQUIRE(*out_step > 0.0, CB_ERR_DOMAIN); /* underflow of w*ln(c) to 0 */
    return CB_OK;
}

/* Does n capped groups' posterior meet the threshold? */
static cb_status_t ft_reaches(const cb_ft_params_t *p, double l0, double step,
                              int32_t n, int32_t *out_reaches)
{
    double posterior;

    CB_CHECK(cb_logistic(l0 + (double)n * step, &posterior));
    *out_reaches = (posterior >= p->threshold) ? 1 : 0;
    return CB_OK;
}

cb_status_t cb_ft_max_posterior(const cb_ft_params_t *params,
                                int32_t n_sources,
                                double *out_posterior)
{
    double l0, step, target, log_odds;

    CB_REQUIRE(out_posterior != NULL, CB_ERR_NULL);
    CB_REQUIRE(n_sources >= 0, CB_ERR_DOMAIN);
    CB_CHECK(ft_derive(params, &l0, &step, &target));

    log_odds = l0 + (double)n_sources * step;
    return cb_logistic(log_odds, out_posterior);
}

cb_status_t cb_ft_min_sources(const cb_ft_params_t *params, int32_t *out_min_sources)
{
    double l0, step, target, exact;
    int32_t n;
    int guard;

    CB_REQUIRE(out_min_sources != NULL, CB_ERR_NULL);
    CB_CHECK(ft_derive(params, &l0, &step, &target));

    if (l0 >= target) {
        /* The adjusted prior already meets the threshold. Zero evidence is
         * required, which means G3 cannot hold for these parameters. */
        *out_min_sources = 0;
        return CB_OK;
    }

    exact = (target - l0) / step;
    if (!cb_finite(exact) || exact > (double)CB_FT_MAX_SOURCES) {
        return CB_ERR_CAPACITY;
    }

    n = (int32_t)ceil(exact);
    if (n < 0) { n = 0; }

    /* Settle the exact boundary by evaluating the posterior with the same
     * comparison cb_ft_check_invariant uses, instead of trusting ceil() --
     * otherwise the two could disagree by a rounding error right at the
     * threshold. Takes at most a couple of steps; guard bounds the loop. */
    for (guard = 0; guard < 64; ++guard) {
        int32_t reaches_here = 0;
        int32_t reaches_below = 0;

        if (n > 0) {
            CB_CHECK(ft_reaches(params, l0, step, n - 1, &reaches_below));
        }
        CB_CHECK(ft_reaches(params, l0, step, n, &reaches_here));

        if (n > 0 && reaches_below) {
            n--;
        } else if (!reaches_here) {
            if (n >= CB_FT_MAX_SOURCES) { return CB_ERR_CAPACITY; }
            n++;
        } else {
            break;
        }
    }
    {
        int32_t reaches_final = 0;
        CB_CHECK(ft_reaches(params, l0, step, n, &reaches_final));
        if (!reaches_final) {
            return CB_ERR_CAPACITY;
        }
    }

    *out_min_sources = n;
    return CB_OK;
}

cb_status_t cb_ft_check_invariant(const cb_ft_params_t *params,
                                  int32_t n_controlled,
                                  int32_t *out_safe)
{
    double l0, step, target;
    int32_t reaches = 0;

    CB_REQUIRE(out_safe != NULL, CB_ERR_NULL);
    CB_REQUIRE(n_controlled >= 0, CB_ERR_DOMAIN);
    CB_CHECK(ft_derive(params, &l0, &step, &target));
    CB_CHECK(ft_reaches(params, l0, step, n_controlled, &reaches));

    *out_safe = reaches ? 0 : 1;
    return CB_OK;
}
