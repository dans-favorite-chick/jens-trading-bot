"""
2026-06-01 master fix Phase 6 — tests for compute_mae_elbow and
compute_mfe_percentile.

Coverage per master plan §6:
  (a) clean cliff at tick 8 -> elbow_ticks=8
  (b) gradual decay, no clear elbow -> elbow_found=False
  (c) sparse data (n<50 per total) -> elbow_found=False with
      reason="insufficient_sample"
  + MFE p90 happy path + sparse handling.
"""
from __future__ import annotations


# ──────────────────────────────────────────────────────────────────────
# compute_mae_elbow
# ──────────────────────────────────────────────────────────────────────


def _bucket(tick: int, n: int, wr: float) -> dict:
    return {"bucket_ticks": tick, "n_trades": n, "win_rate": wr}


class TestCleanElbow:
    """High WR through bucket K, sudden drop at K+1."""

    BUCKETS = [
        # Each bucket has n>=5 to clear the per-bucket floor; total n
        # comfortably above the 50-trade global floor.
        _bucket(1, 20, 0.98),
        _bucket(2, 18, 0.96),
        _bucket(3, 22, 0.95),
        _bucket(4, 20, 0.94),
        _bucket(5, 18, 0.93),
        _bucket(6, 17, 0.91),
        _bucket(7, 15, 0.90),       # last bucket at threshold
        _bucket(8, 12, 0.45),       # CLIFF — first below 0.90
        _bucket(9, 10, 0.40),
        _bucket(10, 8, 0.30),
    ]

    def test_elbow_at_8(self):
        from analytics.compute_engine import compute_mae_elbow
        result = compute_mae_elbow(self.BUCKETS)
        assert result["elbow_found"] is True
        assert result["elbow_ticks"] == 8
        assert abs(result["wr_at_elbow"] - 0.45) < 1e-9
        assert result["reason"] is None

    def test_n_above_counts_pre_elbow_buckets(self):
        from analytics.compute_engine import compute_mae_elbow
        result = compute_mae_elbow(self.BUCKETS)
        # Buckets 1-7 are all >= threshold (n: 20+18+22+20+18+17+15 = 130)
        assert result["n_above_elbow"] == 130


class TestNoElbow:
    """WR never crosses below 0.90 (case b: still in the high-WR shelf)."""

    BUCKETS = [
        _bucket(1, 30, 0.98),
        _bucket(2, 28, 0.95),
        _bucket(3, 25, 0.93),
        _bucket(4, 20, 0.91),
        _bucket(5, 15, 0.92),
    ]

    def test_no_elbow_when_wr_stays_above(self):
        from analytics.compute_engine import compute_mae_elbow
        result = compute_mae_elbow(self.BUCKETS)
        assert result["elbow_found"] is False
        assert result["reason"] == "no_elbow_found"
        assert result["elbow_ticks"] is None


class TestGradualDecay:
    """No WR ever above the threshold -> no clean elbow."""

    BUCKETS = [
        _bucket(1, 20, 0.65),
        _bucket(2, 18, 0.60),
        _bucket(3, 22, 0.55),
        _bucket(4, 20, 0.45),
        _bucket(5, 18, 0.30),
    ]

    def test_gradual_decay_no_elbow(self):
        from analytics.compute_engine import compute_mae_elbow
        result = compute_mae_elbow(self.BUCKETS)
        # WR never gets >= threshold so "seen_above" stays False;
        # the helper returns no_elbow_found rather than fabricating
        # an elbow at the first bucket.
        assert result["elbow_found"] is False
        assert result["reason"] == "no_elbow_found"


class TestSparseTotal:
    """Total n < 50 -> insufficient sample, no elbow even if structure
    would otherwise suggest one."""

    BUCKETS = [
        _bucket(1, 10, 0.98),
        _bucket(2, 8, 0.95),
        _bucket(3, 5, 0.40),    # would be a cliff if n_total were larger
    ]

    def test_sparse_total_returns_insufficient(self):
        from analytics.compute_engine import compute_mae_elbow
        result = compute_mae_elbow(self.BUCKETS)
        assert result["elbow_found"] is False
        assert result["reason"] == "insufficient_sample"


class TestEmpty:
    def test_empty_input(self):
        from analytics.compute_engine import compute_mae_elbow
        result = compute_mae_elbow([])
        assert result["elbow_found"] is False
        assert result["reason"] == "insufficient_sample"
        assert result["n_total"] == 0


class TestPerBucketFloor:
    """Sparse buckets (n<5) are filtered out before evaluation. If the
    cliff bucket has n<5 the elbow is missed (by design)."""

    BUCKETS = [
        _bucket(1, 30, 0.98),
        _bucket(2, 28, 0.95),
        _bucket(3, 25, 0.93),
        _bucket(4, 20, 0.91),
        _bucket(5, 3, 0.20),    # below min_bucket_n=5 -> dropped
        _bucket(6, 18, 0.93),   # back above threshold
    ]

    def test_sparse_bucket_doesnt_mislabel_elbow(self):
        from analytics.compute_engine import compute_mae_elbow
        result = compute_mae_elbow(self.BUCKETS)
        # The n=3 cliff bucket is filtered out. Remaining buckets all
        # stay >= 0.90 so no elbow.
        assert result["elbow_found"] is False


# ──────────────────────────────────────────────────────────────────────
# compute_mfe_percentile
# ──────────────────────────────────────────────────────────────────────


class TestMfePercentile:

    def test_p90_at_correct_bucket(self):
        from analytics.compute_engine import compute_mfe_percentile
        # 100 total trades, weighted heavily in low buckets, with the
        # 90th-percentile bucket landing at tick=12.
        buckets = [
            _bucket(1, 30, 0.5),
            _bucket(2, 25, 0.5),
            _bucket(4, 20, 0.5),
            _bucket(8, 15, 0.5),   # cumulative reaches 90 here
            _bucket(12, 8, 0.5),
            _bucket(20, 2, 0.5),
        ]
        result = compute_mfe_percentile(buckets, percentile=0.90)
        assert result["found"] is True
        # Cumulative 30+25+20+15 = 90 at tick 8 (>= 0.9*100=90).
        # So p_ticks should be 8.
        assert result["p_ticks"] == 8

    def test_p99_lands_at_far_tail(self):
        from analytics.compute_engine import compute_mfe_percentile
        buckets = [
            _bucket(1, 30, 0.5),
            _bucket(2, 25, 0.5),
            _bucket(4, 20, 0.5),
            _bucket(8, 15, 0.5),
            _bucket(12, 8, 0.5),
            _bucket(20, 2, 0.5),   # last 2% trades
        ]
        result = compute_mfe_percentile(buckets, percentile=0.99)
        assert result["found"] is True
        # 0.99 * 100 = 99 -> need cumulative >= 99 -> tick 20
        assert result["p_ticks"] == 20

    def test_sparse_returns_insufficient(self):
        from analytics.compute_engine import compute_mfe_percentile
        # Total n=8, below the 50-floor.
        buckets = [
            _bucket(1, 5, 0.5),
            _bucket(2, 3, 0.5),
        ]
        result = compute_mfe_percentile(buckets)
        assert result["found"] is False
        assert result["reason"] == "insufficient_sample"

    def test_empty_input(self):
        from analytics.compute_engine import compute_mfe_percentile
        result = compute_mfe_percentile([])
        assert result["found"] is False
        assert result["reason"] == "insufficient_sample"


# ──────────────────────────────────────────────────────────────────────
# Output contract: returned dicts are JSON-safe (no NaN / inf leaks)
# ──────────────────────────────────────────────────────────────────────


class TestJsonSafe:

    def test_mae_elbow_output_is_json_safe(self):
        import json
        from analytics.compute_engine import compute_mae_elbow
        result = compute_mae_elbow(TestCleanElbow.BUCKETS)
        # Must roundtrip through json.dumps with allow_nan=False
        # (matches the constraint the facts.json sanitizer enforces).
        json.dumps(result, allow_nan=False)

    def test_mfe_percentile_output_is_json_safe(self):
        import json
        from analytics.compute_engine import compute_mfe_percentile
        result = compute_mfe_percentile([
            _bucket(1, 30, 0.5),
            _bucket(8, 30, 0.5),
        ], percentile=0.50)
        json.dumps(result, allow_nan=False)


# ──────────────────────────────────────────────────────────────────────
# Public surface
# ──────────────────────────────────────────────────────────────────────


def test_helpers_in_all():
    import analytics.compute_engine as ce
    assert "compute_mae_elbow" in ce.__all__
    assert "compute_mfe_percentile" in ce.__all__
