"""P4-1 Stage 3 — Active-path verification for bots/_oif_emitter.py.

After flipping the OIF emitter from shadow to active, `bb._sink_submit_*`
in `bots/base_bot.py` is a re-export of `_oif_emitter.submit_*`. This
test file pins the two properties that the flip MUST preserve:

  1. **Byte-identical request envelope**: calling `bb._sink_submit_X(...)`
     and `_oif_emitter.submit_X(...)` with the same arguments produces
     the same `req` dict handed to `sink.submit(req)`. We capture the
     request via a `RecordingSink` stub installed on `bb._OIF_SINK`.

  2. **Single source of truth for the sink cache**: resetting
     `bb._OIF_SINK = None` is observable from BOTH entry points on the
     next call. (This is the cache-reset contract that
     `tests/test_risk_gate_migration.py:251` depends on.)

We deliberately do NOT write real OIF files here — the legacy
file-emission path is covered by tests/test_risk_gate_migration.py
and tests/test_risk_gate_fail_closed.py. What this file pins is that
no behavior changed when the body moved.
"""

from __future__ import annotations

import copy
import os
import sys

import pytest

_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)


class _RecordingSink:
    """Captures every dict passed to .submit() for byte-by-byte compare.

    Returns a deterministic ACCEPT response so callers don't blow up on
    a missing 'decision' field."""

    def __init__(self):
        self.calls: list[dict] = []

    def submit(self, req: dict) -> dict:
        # Deep-copy so later mutations by the caller don't poison the
        # recording.
        self.calls.append(copy.deepcopy(req))
        return {
            "v": 1,
            "id": req.get("id", ""),
            "decision": "ACCEPT",
            "oif_path": "<recording>",
            "sink": "recording",
        }


@pytest.fixture
def bb_and_emitter(monkeypatch):
    """Import bots.base_bot fresh and force its sink cache to a recorder
    so we can capture and compare request dicts."""
    monkeypatch.delenv("PHOENIX_RISK_GATE", raising=False)
    for mod in ("bots.base_bot", "bots._oif_emitter"):
        if mod in sys.modules:
            del sys.modules[mod]
    import bots.base_bot as bb
    import bots._oif_emitter as oe
    recorder = _RecordingSink()
    bb._OIF_SINK = recorder
    yield bb, oe, recorder
    bb._OIF_SINK = None  # leave clean for subsequent tests


# ---------------------------------------------------------------------------
# Byte-equivalence: bb._sink_submit_X == _oif_emitter.submit_X
# ---------------------------------------------------------------------------

def test_place_request_envelope_byte_identical(bb_and_emitter):
    bb, oe, rec = bb_and_emitter
    args = dict(
        direction="LONG", qty=2, entry_type="LIMIT",
        entry_price=21000.25, stop_price=20990.0, target_price=21015.0,
        trade_id="T-1", account="Sim101",
        strategy="bias_momentum", sub_strategy=None,
    )
    bb._sink_submit_place(**args)
    oe.submit_place(**args)
    assert len(rec.calls) == 2
    assert rec.calls[0] == rec.calls[1]


def test_protect_request_envelope_byte_identical(bb_and_emitter):
    bb, oe, rec = bb_and_emitter
    args = dict(
        direction="LONG", qty=1, stop_price=20990.0, target_price=21015.0,
        trade_id="T-2", account="Sim101",
    )
    bb._sink_submit_protect(**args)
    oe.submit_protect(**args)
    assert len(rec.calls) == 2
    assert rec.calls[0] == rec.calls[1]


def test_exit_request_envelope_byte_identical(bb_and_emitter):
    bb, oe, rec = bb_and_emitter
    args = dict(qty=2, trade_id="T-3", account="Sim101", reason="EOD")
    bb._sink_submit_exit(**args)
    oe.submit_exit(**args)
    assert len(rec.calls) == 2
    assert rec.calls[0] == rec.calls[1]


def test_partial_exit_request_envelope_byte_identical(bb_and_emitter):
    bb, oe, rec = bb_and_emitter
    args = dict(
        direction="SHORT", n_contracts=1, trade_id="T-4", account="Sim101"
    )
    bb._sink_submit_partial_exit(**args)
    oe.submit_partial_exit(**args)
    assert len(rec.calls) == 2
    assert rec.calls[0] == rec.calls[1]


def test_modify_stop_request_envelope_byte_identical(bb_and_emitter):
    bb, oe, rec = bb_and_emitter
    args = dict(
        direction="LONG", new_stop_price=20995.0, n_contracts=1,
        trade_id="T-5", account="Sim101", old_stop_order_id="OID-9",
    )
    bb._sink_submit_modify_stop(**args)
    oe.submit_modify_stop(**args)
    assert len(rec.calls) == 2
    assert rec.calls[0] == rec.calls[1]


# ---------------------------------------------------------------------------
# Single-cache contract: bb._OIF_SINK is the only cache, observable
# from both entry points.
# ---------------------------------------------------------------------------

def test_bb_sink_submit_is_emitter_reexport(bb_and_emitter):
    """The active path is _oif_emitter — bb._sink_submit_X must resolve
    to the same underlying function as _oif_emitter.submit_X (compared
    by qualified name and module, since sys.modules manipulation across
    the test suite can produce parallel module objects).

    The byte-equivalence tests above already prove behavioral identity;
    this test additionally pins that we are NOT defining a separate
    function body in base_bot.py."""
    bb, oe, _ = bb_and_emitter
    pairs = [
        (bb._sink_submit_place, oe.submit_place),
        (bb._sink_submit_protect, oe.submit_protect),
        (bb._sink_submit_exit, oe.submit_exit),
        (bb._sink_submit_partial_exit, oe.submit_partial_exit),
        (bb._sink_submit_modify_stop, oe.submit_modify_stop),
    ]
    for bb_fn, oe_fn in pairs:
        # Same source module (i.e., both resolve to _oif_emitter.py),
        # not base_bot.py. This is the load-bearing assertion: it would
        # fail if base_bot were redefining the function locally.
        assert bb_fn.__module__ == oe_fn.__module__ == "bots._oif_emitter", (
            f"{bb_fn.__qualname__}: bb.__module__={bb_fn.__module__}, "
            f"oe.__module__={oe_fn.__module__}"
        )
        assert bb_fn.__qualname__ == oe_fn.__qualname__
        # Bytecode-equivalent. (Identity via `is` is unreliable here —
        # under pytest's sys.modules manipulation `bots._oif_emitter`
        # can get loaded twice from the same file path, producing
        # parallel-but-equivalent function/code objects.)
        assert bb_fn.__code__.co_code == oe_fn.__code__.co_code, (
            f"{bb_fn.__qualname__}: bytecode differs — base_bot is "
            f"redefining the function instead of re-exporting it."
        )
        assert bb_fn.__code__.co_filename == oe_fn.__code__.co_filename
        assert bb_fn.__code__.co_filename.endswith("_oif_emitter.py")


def test_single_oif_sink_cache_lives_on_base_bot(bb_and_emitter):
    """Setting bb._OIF_SINK = None must cause the next call (via either
    bb._sink_submit_* OR _oif_emitter.submit_*) to rebuild the sink."""
    bb, oe, _ = bb_and_emitter

    # First, install a sentinel and confirm both entry points see it.
    sentinel_a = _RecordingSink()
    bb._OIF_SINK = sentinel_a
    bb._sink_submit_exit(qty=1, trade_id="A", account="Sim101")
    oe.submit_exit(qty=1, trade_id="B", account="Sim101")
    assert len(sentinel_a.calls) == 2

    # Now flip the cache. Both entry points should pick up sentinel_b.
    sentinel_b = _RecordingSink()
    bb._OIF_SINK = sentinel_b
    bb._sink_submit_exit(qty=1, trade_id="C", account="Sim101")
    oe.submit_exit(qty=1, trade_id="D", account="Sim101")
    assert len(sentinel_b.calls) == 2
    # And sentinel_a stays at 2 — no leakage.
    assert len(sentinel_a.calls) == 2


def test_emitter_module_has_no_private_oif_sink_attribute():
    """The dual-cache bug fixed by Stage 3: _oif_emitter must NOT
    declare its own _OIF_SINK or _get_oif_sink. The single source of
    truth lives on bots.base_bot."""
    if "bots._oif_emitter" in sys.modules:
        del sys.modules["bots._oif_emitter"]
    import bots._oif_emitter as oe
    assert not hasattr(oe, "_OIF_SINK"), (
        "Stage 3 contract: _oif_emitter must not own a private sink cache; "
        "use bb._OIF_SINK as the single source of truth."
    )
    assert not hasattr(oe, "_get_oif_sink"), (
        "Stage 3 contract: _oif_emitter must lazy-import _get_oif_sink "
        "from bots.base_bot, not redefine it."
    )
