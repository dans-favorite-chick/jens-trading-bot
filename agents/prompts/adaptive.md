# Adaptive Params (S9)

This agent is **deterministic** — there is no LLM prompt. Safety bounds
are enforced in `agents/adaptive_params.py::SafetyBounds`.

Input source (migrated 2026-06-01 — Task 8): the Strategy Oracle queue at
`logs/oracle/pending_changes.json`. Top-level key is `pending`. Each item
uses the Oracle's verbose field names; `adaptive_params` normalizes them
to the legacy short names before validation.

```json
{
  "pending": [
    {
      "strategy": "bias_momentum",
      "direction": "LONG",
      "parameter_name": "stop_atr_mult",
      "current_value": 2.0,
      "proposed_value": 1.8,
      "rationale": "MAE elbow at n=122 (DSR=0.76).",
      "expected_improvement": "Estimated +0.15 PF over next 100 trades.",
      "confidence": "HIGH",
      "sample_size": 122,
      "finding_id": "bm_finding_2",
      "run_mode": "weekly",
      "metrics": {"dsr": 0.76, "psr": 0.83, "n_trades": 122},
      "status": "PENDING_HUMAN_REVIEW",
      "approved": false,
      "applied": false,
      "proposed_at": "2026-06-01"
    }
  ]
}
```

After normalization the per-item shape seen by `validate_recommendation`
maps `parameter_name` -> `param`, `current_value` -> `current`,
`proposed_value` -> `proposed`, `expected_improvement` -> `expected_impact`.

Validated recommendations become human-readable `.md` proposals in
`logs/ai_learner/proposals/`. Rejected ones append to
`logs/ai_learner/rejected.jsonl`. (The `ai_learner/` folder is kept as the
proposal/rejection sink and as the immutable archive of the pre-Oracle
weekly reports; the Oracle's own outputs live under `logs/oracle/`.)

Never auto-applies. Always routes through `tools/approve_proposal.py`
which stops after test-run for manual merge.
