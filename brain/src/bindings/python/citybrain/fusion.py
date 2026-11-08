"""
Wrapper for citybrain/fusion.h: log-odds evidence fusion with a likelihood-
ratio cap, and the fault-tolerance bound. `weight` is just a caller-supplied
confidence number in [0, 1]; this module doesn't know or care where it came
from.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from citybrain._raw import ffi, lib
from citybrain.errors import check


@dataclass(frozen=True)
class Evidence:
    """A likelihood ratio, a weight, and an independence group id."""
    likelihood_ratio: float
    weight: float
    group_id: int = 0


@dataclass(frozen=True)
class FusionConfig:
    lr_cap: float = 8.0
    prior: float = 0.02
    posterior_floor: float = 0.0
    posterior_ceiling: float = 1.0
    group_cap_enabled: bool = True


@dataclass(frozen=True)
class FusionResult:
    posterior: float
    prior_log_odds: float
    evidence_log_odds: float
    posterior_log_odds: float
    n_items: int
    n_groups_used: int
    n_items_clamped: int
    n_groups_clamped: int


def default_config() -> FusionConfig:
    """Illustrative defaults: lr_cap=8, prior=0.02, floor=0, ceiling=1."""
    c = ffi.new("cb_fusion_config_t *")
    check(lib.cb_fusion_config_init(c))
    return FusionConfig(
        lr_cap=c.lr_cap, prior=c.prior,
        posterior_floor=c.posterior_floor, posterior_ceiling=c.posterior_ceiling,
        group_cap_enabled=bool(c.group_cap_enabled),
    )


def _to_c_config(cfg: FusionConfig):
    c = ffi.new("cb_fusion_config_t *")
    c.lr_cap = cfg.lr_cap
    c.prior = cfg.prior
    c.posterior_floor = cfg.posterior_floor
    c.posterior_ceiling = cfg.posterior_ceiling
    c.group_cap_enabled = 1 if cfg.group_cap_enabled else 0
    return c


def fuse(config: FusionConfig, items: Sequence[Evidence]) -> FusionResult:
    """Fuse evidence items into a posterior probability."""
    cfg_c = _to_c_config(config)
    n = len(items)
    c_items = ffi.new("cb_evidence_t[]", max(n, 1)) if n else ffi.NULL
    for i, it in enumerate(items):
        c_items[i].likelihood_ratio = it.likelihood_ratio
        c_items[i].weight = it.weight
        c_items[i].group_id = it.group_id

    result = ffi.new("cb_fusion_result_t *")
    if config.group_cap_enabled:
        n_groups = max((it.group_id for it in items), default=-1) + 1
        n_groups = max(n_groups, 1)
        scratch = ffi.new("double[]", n_groups)
        status = lib.cb_fusion_posterior(cfg_c, c_items, n, scratch, n_groups, result)
    else:
        status = lib.cb_fusion_posterior(cfg_c, c_items, n, ffi.NULL, 0, result)
    check(status)
    return _result_from_c(result)


def fuse_independent(config: FusionConfig, items: Sequence[Evidence]) -> FusionResult:
    """Same as fuse(), but every item is its own group."""
    cfg_c = _to_c_config(config)
    n = len(items)
    c_items = ffi.new("cb_evidence_t[]", max(n, 1)) if n else ffi.NULL
    for i, it in enumerate(items):
        c_items[i].likelihood_ratio = it.likelihood_ratio
        c_items[i].weight = it.weight
        c_items[i].group_id = it.group_id
    result = ffi.new("cb_fusion_result_t *")
    check(lib.cb_fusion_posterior_independent(cfg_c, c_items, n, result))
    return _result_from_c(result)


def _result_from_c(r) -> FusionResult:
    return FusionResult(
        posterior=r.posterior,
        prior_log_odds=r.prior_log_odds,
        evidence_log_odds=r.evidence_log_odds,
        posterior_log_odds=r.posterior_log_odds,
        n_items=r.n_items,
        n_groups_used=r.n_groups_used,
        n_items_clamped=r.n_items_clamped,
        n_groups_clamped=r.n_groups_clamped,
    )


def logit(probability: float) -> float:
    """logit(p) = ln(p / (1-p))."""
    out = ffi.new("double *")
    check(lib.cb_logit(probability, out))
    return out[0]


def logistic(log_odds: float) -> float:
    """logistic(L) = 1 / (1 + exp(-L))."""
    out = ffi.new("double *")
    check(lib.cb_logistic(log_odds, out))
    return out[0]


@dataclass(frozen=True)
class FaultToleranceParams:
    """How many independent capped sources are needed to reach threshold T,
    given a cap, a prior, a max weight, and an upstream odds multiplier."""
    lr_cap: float = 8.0
    prior: float = 0.02
    threshold: float = 0.20
    max_weight: float = 1.0
    odds_multiplier: float = 1.0


def default_ft_params() -> FaultToleranceParams:
    p = ffi.new("cb_ft_params_t *")
    check(lib.cb_ft_params_init(p))
    return FaultToleranceParams(
        lr_cap=p.lr_cap, prior=p.prior, threshold=p.threshold,
        max_weight=p.max_weight, odds_multiplier=p.odds_multiplier,
    )


def _to_c_ft(params: FaultToleranceParams):
    p = ffi.new("cb_ft_params_t *")
    p.lr_cap = params.lr_cap
    p.prior = params.prior
    p.threshold = params.threshold
    p.max_weight = params.max_weight
    p.odds_multiplier = params.odds_multiplier
    return p


def min_sources(params: FaultToleranceParams) -> int:
    """Smallest number of capped sources that can reach the threshold."""
    p = _to_c_ft(params)
    out = ffi.new("int32_t *")
    check(lib.cb_ft_min_sources(p, out))
    return out[0]


def max_posterior(params: FaultToleranceParams, n_sources: int) -> float:
    """Largest posterior reachable with exactly n_sources capped sources."""
    p = _to_c_ft(params)
    out = ffi.new("double *")
    check(lib.cb_ft_max_posterior(p, n_sources, out))
    return out[0]


def check_invariant(params: FaultToleranceParams, n_controlled: int) -> bool:
    """True if n_controlled capped sources, acting alone, cannot reach threshold."""
    p = _to_c_ft(params)
    out = ffi.new("int32_t *")
    check(lib.cb_ft_check_invariant(p, n_controlled, out))
    return bool(out[0])
