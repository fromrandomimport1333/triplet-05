# Usage guide

How to call this library from other code inside `brain/`.

## 1. Install it once

From `brain/`:

```bash
pip install -e src/bindings/python
```

This builds the C core via CMake the first time it runs and installs the
`citybrain` Python package in editable mode — no separate build step.

## 2. Import and call it

```python
import citybrain

# --- Evidence fusion ---
cfg = citybrain.fusion.FusionConfig(lr_cap=8.0, prior=0.02)
evidence = [
    citybrain.fusion.Evidence(likelihood_ratio=12.0, weight=1.0, group_id=0),
    citybrain.fusion.Evidence(likelihood_ratio=3.0, weight=0.7, group_id=1),
]
result = citybrain.fusion.fuse(cfg, evidence)
print(result.posterior)  # a float probability

# The weight on each Evidence item is YOUR trust/confidence number, in
# [0, 1]. This library does not know or care where it came from -- map
# whatever your trust signal is onto [0, 1] before calling this.

# --- Change-point detection ---
det_cfg = citybrain.detector.default_config()
detector = citybrain.detector.Detector(det_cfg, first_value=0.0, initial_sigma=1.0)
for reading in your_readings:  # already-validated readings, your own loop
    out = detector.update(reading)
    if out.shift_up:
        ...  # a sustained upward shift was detected

# --- Correlation graph ---
# Build your OWN graph with your OWN category indices -- the bundled
# example_profile is sample data for documentation/demos only.
graph_cfg = citybrain.graph.GraphConfig(m_min=0.5, m_max=3.0, n_nodes=4)
graph = citybrain.graph.Graph(graph_cfg)
graph.set_edge(0, 1, citybrain.graph.EdgeKind.RAISES, 2.0)
adjusted_prior, multiplier = graph.adjust_prior(target=1, active=[0], prior=0.02)

# --- Expected-cost decision ---
table = citybrain.decision.CostTable(
    cost_act_when_false=1.0, cost_hold_when_true=20.0,
    cost_act_when_true=0.0, cost_hold_when_false=0.0,
)
decision = citybrain.decision.expected_cost(table, result.posterior)
print(decision.action)  # Action.ACT or Action.HOLD

# --- Assignment ---
assignment, total_cost = citybrain.assignment.solve(your_cost_matrix)

# --- Errors ---
try:
    citybrain.fusion.fuse(cfg, [citybrain.fusion.Evidence(-1.0, 1.0, 0)])
except citybrain.errors.DomainError as e:
    print(f"bad input: {e}")
```

Full function-by-function reference, including C signatures for anyone
calling the library directly from C/C++: `docs/api_reference.md`.

## 3. Calling it from an LLM-facing component (MCP)

If some part of `brain/` has an LLM advisor that should see these
computations as tools rather than as a library call, point it at the
bundled MCP server instead of writing new tool-calling code:

```bash
pip install -e src/mcp_server
```

Then wire it into your MCP client/orchestrator by pointing at
`src/mcp_server/server.py` (stdio transport) or by using the
`citybrain-mcp` console script the install provides. See
`docs/architecture.md` for why this server is intentionally read-only and
cannot be extended into something that dispatches or actuates without that
being an obvious, visible change to `server.py` itself.

## 4. What you are responsible for as the caller

This library does the math. It does **not**:

- validate, authenticate, or freshness-check any reading (your reading
  gate's job),
- interpret a trust verdict from any other part of the system into the
  `[0,1]` weight this library takes (your verdict-interpretation layer's
  job — see `docs/architecture.md`, "What this component is NOT
  responsible for"),
- decide whether a change-point alarm reflects something real versus a
  compromised channel (a separate trust/security layer's job),
- dispatch anything, call any actuator, or have any side effect at all.

If your code needs any of the above, build it around this library, not
inside it — that separation is deliberate.

## 5. Re-verifying this hasn't regressed

Run from `brain/`:

```bash
cmake -S src -B src/build && cmake --build src/build && ctest --test-dir src/build
pip install -e "src/bindings/python[test]" && pytest tests/python/
```

See `docs/testing.md` for expected results.
