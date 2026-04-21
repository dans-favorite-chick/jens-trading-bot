# Adaptive Params (S9)

This agent is **deterministic** — there is no LLM prompt. Safety bounds
are enforced in `agents/adaptive_params.py::SafetyBounds`.

Input format (from S8 Learner, `logs/ai_learner/pending_recommendations.json`):

```json
[
  {
    "strategy": "bias_momentum",
    "param": "stop_atr_mult",
    "current": 2.0,
    "proposed": 1.8,
    "rationale": "Observed 60 trades; 1.8x reduced avg loss by 12% without hurting WR.",
    "expected_impact": "Estimated +0.15 PF improvement over next 100 trades."
  }
]
```

Validated recommendations become human-readable `.md` proposals in
`logs/ai_learner/proposals/`. Rejected ones append to
`logs/ai_learner/rejected.jsonl`.

Never auto-applies. Always routes through `tools/approve_proposal.py`
which stops after test-run for manual merge.
