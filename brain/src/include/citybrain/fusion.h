/*
 * Log-odds evidence fusion with a likelihood-ratio cap, plus the
 * fault-tolerance bound that follows from it.
 *
 * Each evidence item has a likelihood ratio and a weight in [0, 1]. Items
 * are grouped by group_id (independent sources); each item's log-ratio is
 * clipped to [-ln c, +ln c] before weighting, and each group's total is
 * clipped the same way before being added to the prior log-odds. With one
 * item per group the two clips are equivalent, since weight <= 1 already
 * keeps a single weighted item inside the cap.
 *
 * The weight is just a caller-supplied confidence number; this module does
 * not know or care where it came from. Group assignment is the caller's
 * responsibility: two items should only be in different groups if a single
 * fault or attacker genuinely could not affect both at once (different
 * sensor type, different zone, different upstream system). Get that wrong
 * and the fault-tolerance bound below no longer means what it says.
 */
#ifndef CITYBRAIN_FUSION_H
#define CITYBRAIN_FUSION_H

#include "citybrain/common.h"

#ifdef __cplusplus
extern "C" {
#endif

#define CB_FUSION_MAX_ITEMS  ((size_t)1048576)
#define CB_FUSION_MAX_GROUPS ((size_t)1048576)
#define CB_FT_MAX_SOURCES    ((int32_t)1000000000)

typedef struct cb_evidence {
    double  likelihood_ratio; /* > 0, finite */
    double  weight;           /* confidence weight, in [0, 1] */
    int32_t group_id;         /* independence group */
} cb_evidence_t;

typedef struct cb_fusion_config {
    double  lr_cap;            /* c, > 1 */
    double  prior;             /* p0, in (0, 1) */
    double  posterior_floor;   /* clamp on the result, in [0, 1) */
    double  posterior_ceiling; /* clamp on the result, in (floor, 1] */
    int32_t group_cap_enabled; /* 0 or 1 */
} cb_fusion_config_t;

typedef struct cb_fusion_result {
    double  posterior;
    double  prior_log_odds;
    double  evidence_log_odds;
    double  posterior_log_odds;
    int32_t n_items;
    int32_t n_groups_used;
    int32_t n_items_clamped;
    int32_t n_groups_clamped;
} cb_fusion_result_t;

/* Default config: lr_cap=8, prior=0.02, floor=0, ceiling=1, group cap on. */
CB_API cb_status_t cb_fusion_config_init(cb_fusion_config_t *cfg);

/*
 * Fuse n_items evidence items into a posterior probability.
 *
 * group_scratch needs at least n_groups doubles of caller-owned working
 * space when group capping is on; pass NULL and n_groups=0 to disable it
 * and cap each item individually instead.
 *
 * Errors: CB_ERR_NULL for missing required pointers; CB_ERR_DOMAIN for
 * lr_cap <= 1, prior outside (0,1), weight outside [0,1], likelihood_ratio
 * <= 0, or an out-of-range group_id; CB_ERR_NOT_FINITE for NaN/infinite
 * input; CB_ERR_CAPACITY if n_items or n_groups is too large.
 *
 * n_items == 0 is valid and returns posterior == prior.
 */
CB_API cb_status_t cb_fusion_posterior(const cb_fusion_config_t *cfg,
                                       const cb_evidence_t *items,
                                       size_t n_items,
                                       double *group_scratch,
                                       size_t n_groups,
                                       cb_fusion_result_t *out);

/* Same as cb_fusion_posterior but every item is treated as its own group,
 * so no scratch buffer is needed. */
CB_API cb_status_t cb_fusion_posterior_independent(const cb_fusion_config_t *cfg,
                                                   const cb_evidence_t *items,
                                                   size_t n_items,
                                                   cb_fusion_result_t *out);

/* Standard logit/logistic pair, used internally and exposed because other
 * modules and callers need the same numerically stable implementation. */
CB_API cb_status_t cb_logit(double probability, double *out_log_odds);
CB_API cb_status_t cb_logistic(double log_odds, double *out_probability);

/*
 * Fault-tolerance bound: given a cap c, a prior p0, an action threshold T,
 * a max weight w and an upstream odds multiplier m, how many independent
 * capped sources does it take to reach T? See docs/math_reasoning.md for
 * the derivation.
 */
typedef struct cb_ft_params {
    double lr_cap;          /* c, > 1 */
    double prior;           /* p0, in (0, 1) */
    double threshold;       /* T, in (0, 1) */
    double max_weight;      /* w, in (0, 1] */
    double odds_multiplier; /* m, > 0; 1.0 for no adjustment */
} cb_ft_params_t;

/* Default: c=8, p0=0.02, T=0.20, w=1.0, m=1.0. */
CB_API cb_status_t cb_ft_params_init(cb_ft_params_t *params);

/* Smallest number of independent capped sources that can reach the
 * threshold. Errors: CB_ERR_NULL, CB_ERR_DOMAIN, CB_ERR_NOT_FINITE,
 * CB_ERR_CAPACITY if the count would exceed CB_FT_MAX_SOURCES. */
CB_API cb_status_t cb_ft_min_sources(const cb_ft_params_t *params,
                                     int32_t *out_min_sources);

/* Largest posterior reachable with exactly n_sources capped sources. */
CB_API cb_status_t cb_ft_max_posterior(const cb_ft_params_t *params,
                                       int32_t n_sources,
                                       double *out_posterior);

/* True if n_controlled capped sources, acting alone, cannot reach the
 * threshold. */
CB_API cb_status_t cb_ft_check_invariant(const cb_ft_params_t *params,
                                         int32_t n_controlled,
                                         int32_t *out_safe);

#ifdef __cplusplus
} /* extern "C" */
#endif

#endif /* CITYBRAIN_FUSION_H */
