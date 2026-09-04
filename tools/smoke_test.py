"""Smoke test for the trading workbench components."""
from tools.strategy_engine import backtest, ALL_STRATEGIES
from tools.mt5_connector import get_ohlcv, get_account, get_symbol_info, compute_size, DEMO

print('MT5 mode:', 'DEMO' if DEMO else 'LIVE')
acct = get_account()
print(f'Account: {acct.balance:.2f} {acct.currency}')
print()

for sym in ['EURUSD', 'GBPUSD', 'XAUUSD', 'BTCUSD']:
    bars = get_ohlcv(sym, 'H1', 500)
    print(f'--- {sym}: {len(bars)} bars ---')
    for name, fn in ALL_STRATEGIES.items():
        r = backtest(bars, fn, sym)
        if 'error' in r:
            err = r['error']
            print(f'  {name:30s}: {err}')
        else:
            print(f'  {name:30s}: {r["sample"]:3d}t WR {r["win_rate"]:.0%} E {r["expectancy_r"]:+.2f}R PF {r["profit_factor"]:.2f} MaxDD {r["max_dd"]:.0%}')

print()
print('POSITION SIZER:')
for sym in ['EURUSD', 'XAUUSD', 'BTCUSD', 'NVDA']:
    size = compute_size(sym, 10000, 0.01, 100, 95)
    info = get_symbol_info(sym)
    print(f'  {sym:10s}: {size:.4f} lots (contract={info.contract_size}, digits={info.digits})')
