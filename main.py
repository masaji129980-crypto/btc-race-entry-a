"""EMA trend-following bot for the btc-race competition (agent-fun/bars-replay/v1).

Strategy: long-only trend follower on CBBTC 1-minute bars.
  - Trend filter: EMA-12 above EMA-48 on the closes seen so far (the fast
    EMA crossing above the slow EMA defines the uptrend regime).
  - Entry: the close breaks above the highest close of the previous 20 bars
    by at least 10 bps while the EMA trend filter holds. One full-size buy
    (all cash, up to 100% of equity).
  - Exits (whole position): take-profit +150 bps, stop-loss -150 bps, or a
    trailing stop of 60 bps below the highest close since entry. All measured
    against the entry fill price.
  - Orders decided on one batch fill at the next bar's open, so every signal
    is evaluated on the bar that just closed and fills one bar later.
  - Deterministic: no randomness, no wall clock, no files, no network.
    The bot only reads the hello and each batch it is sent and answers each
    batch with exactly one reply echoing its seq.

Wire protocol is handled by the vendored shim (vendor/makefun_bars.py, a
byte-for-byte copy of the kit's sdk/makefun_bars.py).
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor"))

import makefun_bars

SYMBOL = "CBBTC"

FAST_SPAN = 12
SLOW_SPAN = 48
DONCHIAN_BARS = 20
BREAKOUT_BPS = 10
TAKE_PROFIT_BPS = 150
STOP_LOSS_BPS = 150
TRAILING_BPS = 60
MAX_SLIPPAGE_BPS = 100
# Both windows are 1,440 one-minute bars (RULES.txt, "Windows"). A position
# opened this late would fill at the last bar's open and be flattened at the
# last close one bar later, so it can only pay the round-trip cost: never
# open one in the final 30 bars of the window.
LATE_WINDOW_CUTOFF = 1440 - 30


class TrendBot:
    """Keeps the closes, the two EMAs and the open position's entry price."""

    def __init__(self):
        self.closes = []          # closes as ints (price-scale units)
        self.ema_fast = None      # float, same units as the closes
        self.ema_slow = None
        self.entry_price = None   # fill price of the open position (int)
        self.peak = None          # highest close since entry (int)
        self.pending_buy = None   # client_id of a buy awaiting its fill

    def _push_close(self, close):
        self.closes.append(close)
        if self.ema_fast is None:
            self.ema_fast = float(close)
            self.ema_slow = float(close)
            return
        kf = 2.0 / (FAST_SPAN + 1)
        ks = 2.0 / (SLOW_SPAN + 1)
        self.ema_fast += kf * (close - self.ema_fast)
        self.ema_slow += ks * (close - self.ema_slow)

    def _learn_fills(self, batch):
        for fill in batch.get("fills", []):
            if fill.get("symbol") != SYMBOL:
                continue
            if fill.get("client_id") == self.pending_buy:
                self.pending_buy = None
                if fill.get("status") == "filled" and fill.get("side") == "buy":
                    self.entry_price = int(fill["price"])
                    self.peak = self.entry_price

    def on_batch(self, state, batch):
        if state.warming_up:
            return []
        self._learn_fills(batch)
        bars = [b for b in batch.get("bars", []) if b.get("symbol") == SYMBOL]
        if not bars:
            return []
        close = int(bars[-1]["c"])
        self._push_close(close)

        held = state.positions.get(SYMBOL)
        if held is not None and held.qty > 0:
            if self.entry_price is None:
                # Fallback: derive the entry price from the position's cost.
                qs = state.hello["capital"]["quote_scale"]
                ps = state.symbols[SYMBOL]["price_scale"]
                ks = state.symbols[SYMBOL]["qty_scale"]
                self.entry_price = held.cost * 10 ** (ks + ps) // (held.qty * 10 ** qs)
                self.peak = self.entry_price
            if close > self.peak:
                self.peak = close
            ret_bps = (close - self.entry_price) * 10000.0 / self.entry_price
            trail_bps = (self.peak - close) * 10000.0 / self.peak
            if (
                ret_bps >= TAKE_PROFIT_BPS
                or ret_bps <= -STOP_LOSS_BPS
                or trail_bps >= TRAILING_BPS
            ):
                self.entry_price = None
                self.peak = None
                return [
                    {
                        "side": "sell",
                        "symbol": SYMBOL,
                        "qty": held.qty,
                        "max_slippage_bps": MAX_SLIPPAGE_BPS,
                    }
                ]
            return []

        # Flat: look for a trend entry on this bar.
        if state.seq >= LATE_WINDOW_CUTOFF:
            return []
        if len(self.closes) < SLOW_SPAN or self.ema_fast <= self.ema_slow:
            return []
        if len(self.closes) <= DONCHIAN_BARS:
            return []
        prev_high = max(self.closes[-DONCHIAN_BARS - 1 : -1])
        if close * 10000 <= prev_high * (10000 + BREAKOUT_BPS):
            return []
        notional = state.cash
        room = state.weight_room(SYMBOL)
        if room < notional:
            notional = room
        if notional < 1_000_000:  # below the 1 USDC minimum notional
            return []
        cid = state.client_id()
        self.pending_buy = cid
        return [
            {
                "side": "buy",
                "symbol": SYMBOL,
                "client_id": cid,
                "notional_quote_units": notional,
                "max_slippage_bps": MAX_SLIPPAGE_BPS,
            }
        ]


BOT = TrendBot()


def on_batch(state, batch):
    return BOT.on_batch(state, batch)


makefun_bars.run(on_batch)
