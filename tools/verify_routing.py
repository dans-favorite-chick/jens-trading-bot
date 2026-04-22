"""
Phoenix Bot — Account Routing Sanity Check

Verifies every enabled strategy resolves to a dedicated NT8 account
(not the Sim101 fallback). Run after any edit to
config/account_routing.py or config/strategies.py.

Usage:
    python tools\verify_routing.py

Expected output on success:
    *** ALL GOOD — 16 unique accounts, zero Sim101 fallbacks ***
"""

from config.account_routing import get_account_for_signal, validate_account_map


CHECKS = [
    # opening_session sub-strategies
    ('opening_session', 'open_drive'),
    ('opening_session', 'open_test_drive'),
    ('opening_session', 'open_auction_in'),
    ('opening_session', 'open_auction_out'),
    ('opening_session', 'premarket_breakout'),
    ('opening_session', 'orb'),
    # Top-level strategies
    ('bias_momentum', None),
    ('spring_setup', None),
    ('vwap_pullback', None),
    ('vwap_band_pullback', None),
    ('dom_pullback', None),
    ('ib_breakout', None),
    ('compression_breakout_15m', None),
    ('compression_breakout_30m', None),
    ('noise_area', None),
    ('orb', None),
]

EXPECTED_ACCOUNT_COUNT = 17  # 16 dedicated + Sim101 fallback

def main() -> int:
    print('=== ROUTING SANITY CHECK ===')
    fails = 0
    for strategy, sub in CHECKS:
        acct = get_account_for_signal(strategy, sub)
        label = f'{strategy}.{sub}' if sub else strategy
        if acct == 'Sim101':
            fails += 1
            mark = '  <-- FAIL: fell to Sim101 fallback'
        else:
            mark = ''
        print(f'  {label:40s} -> {acct}{mark}')

    print()
    print('=== UNIQUE ACCOUNTS (compare to NT8 screenshots) ===')
    accounts = validate_account_map()
    for a in accounts:
        print(f'  {a}')

    total = len(accounts)
    print()
    print(f'Total unique accounts: {total}')
    print(f'Failures (routed to Sim101 by accident): {fails}')
    print()

    if fails == 0 and total == EXPECTED_ACCOUNT_COUNT:
        print(f'*** ALL GOOD — {EXPECTED_ACCOUNT_COUNT} unique accounts, '
              f'zero Sim101 fallbacks ***')
        return 0
    else:
        print(f'*** CHECK OUTPUT — expected {EXPECTED_ACCOUNT_COUNT} '
              f'accounts, 0 fallbacks; got {total}, {fails} ***')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
