"""B81: global OIF_INCOMING isolation for pytest.

Without this, any test that calls bridge.oif_writer.write_* writes a real
OIF file into NT8's live incoming folder — NT8 immediately places the
order. This is how the phantom "Sell STP @ 100.00" and "Sell STP @
21000.00" orders appeared on Jennifer's chart (tests literals = prices).

This autouse fixture redirects OIF_INCOMING to a per-test tempdir for
every test in this suite. Individual tests can still override (e.g. the
existing _OIFIsolator helper) — this just guarantees the default is safe.
"""
from __future__ import annotations

import shutil
import tempfile

import pytest


@pytest.fixture(autouse=True)
def _isolate_oif_incoming(monkeypatch):
    """Every test gets a clean tempdir for OIF_INCOMING. No test shall
    ever write to the real NT8 incoming folder.

    P0.4 addition: also flip _PYTEST_BYPASS_CONSUME_CHECK = True so the
    mandatory post-write consume-check (which would raise OIFStuckError
    because no simulated NT8 consumer deletes the tmp files) becomes a
    no-op for the default test. Tests that specifically exercise the
    stuck-raise behaviour (tests/test_verify_consumed_mandatory.py)
    monkeypatch it back to False inside their own fixture scope.
    """
    tmp = tempfile.mkdtemp(prefix="phoenix_oif_test_")
    try:
        import bridge.oif_writer as _oif
        monkeypatch.setattr(_oif, "OIF_INCOMING", tmp, raising=False)
        monkeypatch.setattr(
            _oif, "_PYTEST_BYPASS_CONSUME_CHECK", True, raising=False,
        )
        # 2026-04-24: bypass the price-sanity outbound check for default
        # tests. The guard is intentionally strict (2% deviation from last
        # accepted tick / FMP reference) which is correct for production
        # but would reject every test order that uses arbitrary literals
        # like 18500 / 21000 / 26500. Tests that specifically exercise the
        # guard should override this flag back to False inside their own
        # fixture scope (see tests/test_price_sanity.py).
        monkeypatch.setattr(
            _oif, "_PYTEST_BYPASS_SANITY_CHECK", True, raising=False,
        )
    except Exception:
        # oif_writer may not be importable in ultra-minimal test envs
        pass
    # Reset the sanity singleton so per-test state never leaks across tests.
    try:
        from core import price_sanity
        price_sanity.reset()
    except Exception:
        pass
    yield tmp
    shutil.rmtree(tmp, ignore_errors=True)
