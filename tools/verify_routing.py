"""
Phoenix Bot - Account Routing Sanity Check

Verifies every enabled strategy resolves to a dedicated NT8 account
(not the Sim101 fallback). Run after any edit to
config/account_routing.py or config/strategies.py.

Usage:
    python tools\\verify_routing.py
    python -m tools.verify_routing

Expected output on success:
    *** ALL GOOD - 29 unique accounts, zero unintended Sim101 fallbacks ***

History
-------
2026-05-19 Phase 13 ship: added 4 lab winners (raschke_baseline,
  g_inside_bar_breakout, e_multi_day_breakout, a_asian_continuation),
  fixed vwap_band_reversion mapping (SimVwap_Reversion underscore),
  added V2 deployment strategies that were previously missing from
  CHECKS. INTENTIONAL_SIM101 set lists strategies routed to Sim101 by
  design (big_move_signal, es_nq_confluence - see account_routing.py
  comments). Switched all em-dashes to ASCII hyphens so cp1252 console
  doesn't mojibake the output.
"""

import os
import sys

# Make `python tools/verify_routing.py` work in addition to `python -m
# tools.verify_routing`. Project root is one level up from this file.
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from config.account_routing import get_account_for_signal, validate_account_map


# Strategies that should each route to their OWN dedicated account.
CHECKS = [
    # opening_session sub-strategies
    ('opening_session', 'open_drive'),
    ('opening_session', 'open_test_drive'),
    ('opening_session', 'open_auction_in'),
    ('opening_session', 'open_auction_out'),
    ('opening_session', 'premarket_breakout'),
    ('opening_session', 'orb'),
    # V1 top-level strategies
    ('bias_momentum', None),
    ('spring_setup', None),
    ('vwap_pullback', None),
    ('vwap_band_pullback', None),
    ('vwap_band_reversion', None),
    ('dom_pullback', None),
    ('ib_breakout', None),
    ('compression_breakout_15m', None),
    ('compression_breakout_30m', None),
    ('noise_area', None),
    ('orb', None),
    ('footprint_cvd_reversal', None),
    # 2026-05-17 V2 deployment
    ('nq_lsr', None),
    ('orb_fade', None),
    ('orb_v2', None),
    ('compression_breakout_v2', None),
    ('compression_breakout_micro', None),
    ('vwap_pullback_v2', None),
    # 2026-05-19 Phase 13 ship audit (commit 2c77d35)
    ('raschke_baseline', None),
    ('g_inside_bar_breakout', None),
    ('e_multi_day_breakout', None),
    ('a_asian_continuation', None),
]

# Strategies intentionally routed to Sim101 (temp, with documented reason
# in config/account_routing.py). These will NOT count as routing failures.
INTENTIONAL_SIM101 = {
    'big_move_signal',     # Phase 9.1 hotfix — promote when graduation needed
    'es_nq_confluence',    # Phase 12C — dormant until MES feed wired
}

# 29 dedicated accounts + Sim101 (default) = 29 unique values in the map
# because Sim101 is itself the value for both _default AND the two
# intentional fallbacks above. validate_account_map() returns the unique
# set, which is 29 (28 dedicated + Sim101).
EXPECTED_ACCOUNT_COUNT = 29

def main() -> int:
    print('=== ROUTING SANITY CHECK ===')
    fails = 0
    intentional_count = 0
    for strategy, sub in CHECKS:
        acct = get_account_for_signal(strategy, sub)
        label = f'{strategy}.{sub}' if sub else strategy
        if acct == 'Sim101':
            if strategy in INTENTIONAL_SIM101:
                intentional_count += 1
                mark = '  (intentional Sim101 - temp routing, documented)'
            else:
                fails += 1
                mark = '  <-- FAIL: fell to Sim101 fallback'
        else:
            mark = ''
        print(f'  {label:40s} -> {acct}{mark}')

    # Also report the intentional Sim101 routes for completeness.
    for s in sorted(INTENTIONAL_SIM101):
        if not any(c[0] == s for c in CHECKS):
            acct = get_account_for_signal(s, None)
            print(f'  {s:40s} -> {acct}  (intentional Sim101 - temp)')

    print()
    print('=== UNIQUE ACCOUNTS (compare to NT8 account list) ===')
    accounts = validate_account_map()
    for a in accounts:
        print(f'  {a}')

    total = len(accounts)
    print()
    print(f'Total unique accounts: {total}')
    print(f'Unintended Sim101 fallbacks: {fails}')
    print(f'Intentional Sim101 routings: {intentional_count}')
    print()

    if fails == 0 and total == EXPECTED_ACCOUNT_COUNT:
        print(f'*** ALL GOOD - {EXPECTED_ACCOUNT_COUNT} unique accounts, '
              f'zero unintended Sim101 fallbacks ***')
        return 0
    else:
        print(f'*** CHECK OUTPUT - expected {EXPECTED_ACCOUNT_COUNT} '
              f'accounts, 0 unintended fallbacks; got {total}, {fails} ***')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
