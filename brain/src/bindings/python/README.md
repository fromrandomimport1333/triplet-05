# citybrain (Python bindings)

Pythonic bindings for the `citybrain` C core: bounded-influence evidence
fusion, change-point detection, a bounded multiplier graph, expected-cost
decisions, optimal assignment, cache-key algebra, bounded calibration
updates, and a tabular MDP solver.

```bash
pip install -e .
```

This single command also builds the C core (via CMake) the first time it
runs, if it has not been built yet — no separate manual build step is
required, including on a brand-new clean checkout. See
`../../docs/build.md` for the full story, including the exact toolchain
this requires (CMake and a C99 compiler).

```python
import citybrain

cfg = citybrain.fusion.default_config()
result = citybrain.fusion.fuse(cfg, [
    citybrain.fusion.Evidence(likelihood_ratio=8.0, weight=1.0, group_id=0),
])
print(result.posterior)
```

Every citybrain function raises a subclass of `citybrain.errors.CityBrainError`
on bad input rather than returning a raw C status code; see
`../../docs/api_reference.md` for the full mapping, and
`../../docs/usage.md` for a fuller usage guide.
