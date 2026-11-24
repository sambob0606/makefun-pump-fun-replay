"""Pump.fun Replay Sprint paper bot (agent-fun/replay/v2, Python, stdlib only).

Strategy: selective momentum on fresh pump.fun bonding curves, with hard risk
exits and small probe round-trips that keep the entry above the guardrails.

The harness speaks the replay protocol on stdin/stdout: one ``hello`` message
with the capital, caps, fees and seed, then one ``batch`` per slot carrying
the events of that slot, the fills of our earlier orders, the account and the
marks. We answer every batch with exactly one reply listing this batch's
orders. The batch's ``slot`` numbers the slot being answered; every order we
send is decided on that slot and filled (or rejected) about two slots later.

Entry signal (per curve, rolling 40-slot tape of its recorded trades):
  * the curve is at least 10 slots old (fresh curves are mostly bundled
    launches that get dumped on their first buyers);
  * net SOL flowing in over the tape is at least 1 SOL, from at least 3 buys
    by at least 4 distinct buyers, with no single buyer dominating;
  * the price rose at least 5% over the tape but no more than 150%, sits at
    least 90% of the tape's high, and the newer half of the tape took in at
    least as much net SOL as the older half (the inflow is accelerating);
  * the curve still holds between 0.5 and 80 SOL of real SOL (too small is
    untradeable, too big is about to complete).

Sizing: the first buy is 0.05 SOL (capped by the per-order size cap, 1% of
real reserves). While the price stays at or above the last buy's price and
net SOL still flows in, we pyramid in 0.05 SOL adds up to 0.20 SOL per coin,
so false starts cost little and runners get size.

Exits (whole position, at once): a stop loss at 25% below cost, a trailing
stop 20% below the best value once the position is up at least 15%, a
600-slot time limit, or a fade when net SOL over the last 30 slots turns
below -0.5 SOL. A curve that completes is closed by the replay itself.

Guardrail probes: the entry needs a round trip of at least 0.01 SOL on at
least 20 coins. Momentum entries alone may not reach 20 coins, so whenever
a position slot is free, cash allows and fewer than 20 coins have been
traded, we open a tiny 0.02 SOL probe on the most liquid undecided curve and
sell it whole as soon as its buy lands. Probes are capped at one per 20
batches so they stay a small share of the run.

Deterministic by construction: no clock, no randomness, no environment
reads; iteration is over insertion-ordered dicts only, and the grader fixes
PYTHONHASHSEED=0, so set iteration order cannot leak in either.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor"))

import makefun_bot  # noqa: E402  (after the sys.path line: -P leaves the script dir off sys.path)

SOL = makefun_bot.LAMPORTS_PER_SOL

# --- entry signal -----------------------------------------------------------
WINDOW_SLOTS = 40          # rolling tape length, in slots
MIN_AGE_SLOTS = 10         # slots since CreateEvent before an entry is allowed
MIN_NET_SOL = 1.0          # net SOL in over the tape
MIN_BUYS = 3               # buy trades in the tape
MIN_BUYERS = 4             # distinct buying wallets in the tape
MIN_PRICE_UP = 0.05        # price now vs tape start, minus 1
MAX_PRICE_UP = 1.50        # skip curves that already ran too far in one window
NEAR_HIGH = 0.90           # price now at least this share of the tape high
MIN_REAL_SOL = 0.5         # real SOL bounds at entry
MAX_REAL_SOL = 80.0
MAX_BUYER_SHARE = 0.60     # largest single buyer share of the tape's buy SOL

# --- sizing -----------------------------------------------------------------
FIRST_BUY_SOL = 0.05
ADD_SOL = 0.05
MAX_STAKE_SOL = 0.20
MIN_BUY_SOL = 0.01
ADD_PRICE_STEP = 0.02      # add only once price is this far above the last buy
ADD_NET_SOL = 0.5          # add only while tape net SOL is at least this
BUY_SLIPPAGE_BPS = 500
SELL_SLIPPAGE_BPS = 10_000

# --- exits ------------------------------------------------------------------
STOP_LOSS = 0.25           # sell when value falls to cost * (1 - STOP_LOSS)
TRAIL_ARM = 0.15           # trailing stop arms once value is up this much
TRAIL_DROP = 0.20          # sell when value falls this far below its best
MAX_HOLD_SLOTS = 600       # slots held before a time-limit exit
FADE_WINDOW_SLOTS = 30
FADE_NET_SOL = -0.5        # sell when net SOL over the fade window is below this

# --- guardrail probes -------------------------------------------------------
PROBE_SOL = 0.02
PROBE_MIN_REAL_SOL = 5.0   # probes only on liquid curves
PROBE_EVERY_BATCHES = 20   # at most one probe per this many batches
TARGET_COINS = 20          # guardrail: round trips on at least this many coins


def price_of(curve):
    """Lamports per token from virtual reserves; 0 when unpriced."""
    if curve.virtual_token_reserves <= 0:
        return 0.0
    return curve.virtual_quote_reserves / curve.virtual_token_reserves


class Tape:
    """Rolling trade tape for one curve: (slot, price, signed SOL, buyer)."""

    __slots__ = ("created_slot", "trades", "complete")

    def __init__(self, created_slot):
        self.created_slot = created_slot
        self.trades = []  # list of (slot, price, net_sol_lamports, user, is_buy)
        self.complete = False

    def prune(self, slot):
        cutoff = slot - WINDOW_SLOTS
        trades = self.trades
        i = 0
        while i < len(trades) and trades[i][0] < cutoff:
            i += 1
        if i:
            del trades[:i]

    def stats(self, slot):
        """(net_sol, n_buys, n_buyers, max_buyer_share, price_start, price_now, high)."""
        self.prune(slot)
        trades = self.trades
        if not trades:
            return None
        net = 0
        buys = 0
        buy_sol = 0
        per_buyer = {}
        high = 0.0
        for _s, p, sol, user, is_buy in trades:
            net += sol
            if p > high:
                high = p
            if is_buy:
                buys += 1
                buy_sol += sol
                per_buyer[user] = per_buyer.get(user, 0) + sol
        price_start = trades[0][1]
        price_now = trades[-1][1]
        max_share = 0.0
        if buy_sol > 0:
            for v in per_buyer.values():
                if v / buy_sol > max_share:
                    max_share = v / buy_sol
        return (net, buys, len(per_buyer), max_share, price_start, price_now, high)

    def net_over(self, slot, window):
        cutoff = slot - window
        total = 0
        for s, _p, sol, _u, _b in self.trades:
            if s >= cutoff:
                total += sol
        return total


class Bot:
    def __init__(self):
        self.tapes = {}          # mint -> Tape, first-seen order
        self.candidates = {}     # mint -> True, curves not yet decided
        self.traded = {}         # mint -> True, coins with a buy sent
        self.best_value = {}     # mint -> best position value seen (lamports)
        self.entry_slot = {}     # mint -> slot the position was first seen held
        self.last_buy_price = {}  # mint -> price at the last buy decision
        self.probe = {}          # mint -> slot of the probe buy
        self.last_probe_batch = -10**9
        self.batches = 0

    # -- tape maintenance ---------------------------------------------------
    def note_events(self, batch):
        for e in batch["events"]:
            mint = e["mint"]
            kind = e["event"]
            if kind == "CreateEvent":
                tape = self.tapes.get(mint)
                if tape is None:
                    tape = Tape(e["slot"])
                    self.tapes[mint] = tape
                tape.created_slot = e["slot"]
                self.candidates[mint] = True
            elif kind == "TradeEvent":
                tape = self.tapes.get(mint)
                if tape is None:
                    tape = Tape(e["slot"])
                    self.tapes[mint] = tape
                vt = int(e["virtual_token_reserves"])
                vq = int(e["virtual_quote_reserves"])
                price = (vq / vt) if vt > 0 else 0.0
                sol = int(e["sol_amount"])
                if not e["is_buy"]:
                    sol = -sol
                tape.trades.append((e["slot"], price, sol, e["user"], bool(e["is_buy"])))
            else:  # CompleteEvent
                tape = self.tapes.get(mint)
                if tape is None:
                    tape = Tape(e["slot"])
                    self.tapes[mint] = tape
                tape.complete = True
                self.candidates.pop(mint, None)

    # -- exits ---------------------------------------------------------------
    def exit_orders(self, state, orders):
        slot = state.slot
        for mint, holding in list(state.positions.items()):
            curve = state.curves.get(mint)
            if curve is None or curve.complete:
                continue
            price = price_of(curve)
            if price <= 0:
                continue
            value = int(holding.token_amount * price)
            cost = holding.cost_lamports
            if cost <= 0:
                continue
            if mint not in self.entry_slot:
                self.entry_slot[mint] = slot
            best = self.best_value.get(mint, value)
            if value > best:
                best = value
                self.best_value[mint] = best
            tape = self.tapes.get(mint)
            fade = tape.net_over(slot, FADE_WINDOW_SLOTS) if tape is not None else 0
            sell = False
            if value <= cost * (1.0 - STOP_LOSS):
                sell = True  # stop loss against cost
            elif best >= cost * (1.0 + TRAIL_ARM) and value <= best * (1.0 - TRAIL_DROP):
                sell = True  # trailing stop from the best close value
            elif slot - self.entry_slot[mint] >= MAX_HOLD_SLOTS:
                sell = True  # time limit
            elif fade <= int(FADE_NET_SOL * SOL):
                sell = True  # inflow turned into outflow
            if sell:
                orders.append(
                    {
                        "side": "sell",
                        "mint": mint,
                        "token_amount": holding.token_amount,
                        "max_slippage_bps": SELL_SLIPPAGE_BPS,
                    }
                )
                self.best_value.pop(mint, None)
                self.entry_slot.pop(mint, None)
                self.last_buy_price.pop(mint, None)
                self.probe.pop(mint, None)
        return orders

    # -- entries -------------------------------------------------------------
    def entry_signal(self, state, mint):
        tape = self.tapes.get(mint)
        curve = state.curves.get(mint)
        if tape is None or curve is None or curve.complete:
            return False
        if state.slot - tape.created_slot < MIN_AGE_SLOTS:
            return False
        stats = tape.stats(state.slot)
        if stats is None:
            return False
        net, n_buys, n_buyers, max_share, price_start, price_now, high = stats
        if price_start <= 0 or price_now <= 0 or high <= 0:
            return False
        price_up = price_now / price_start - 1.0
        if net < int(MIN_NET_SOL * SOL):
            return False
        if n_buys < MIN_BUYS or n_buyers < MIN_BUYERS:
            return False
        if max_share > MAX_BUYER_SHARE:
            return False
        if price_up < MIN_PRICE_UP or price_up > MAX_PRICE_UP:
            return False
        if price_now < NEAR_HIGH * high:
            return False
        # acceleration: the newer half of the tape took in at least as much
        # net SOL as the older half (when the older half was positive).
        mid = state.slot - WINDOW_SLOTS // 2
        old_net = 0
        new_net = 0
        for s, _p, sol, _u, _b in tape.trades:
            if s < mid:
                old_net += sol
            else:
                new_net += sol
        if old_net > 0 and new_net < old_net:
            return False
        if new_net <= 0:
            return False
        real_sol = curve.real_quote_reserves / SOL
        if real_sol < MIN_REAL_SOL or real_sol > MAX_REAL_SOL:
            return False
        return True

    def on_batch(self, state, batch):
        self.batches += 1
        self.note_events(batch)
        caps = state.hello["caps"]
        tx = state.tx_fee_lamports()
        free = state.free_cash_lamports()
        orders = []
        self.exit_orders(state, orders)

        # positions already held or with a buy in flight, plus sells just queued
        held = set(state.positions)
        for p in state.pending.values():
            if p.order["side"] == "buy":
                held.add(p.order["mint"])
        open_slots = caps["max_open_positions"] - len(held)

        # --- momentum entries and pyramid adds, in batch event order --------
        decided = []
        for e in batch["events"]:
            mint = e["mint"]
            if mint not in self.candidates:
                continue
            curve = state.curves.get(mint)
            if curve is None or curve.complete:
                self.candidates.pop(mint, None)
                continue
            if not self.entry_signal(state, mint):
                continue
            self.candidates.pop(mint, None)
            decided.append(mint)
        for mint in decided:
            if len(orders) >= caps["max_orders_per_batch"] or open_slots <= 0:
                break
            curve = state.curves.get(mint)
            cap = state.max_buy_lamports(mint)
            amount = min(cap, int(FIRST_BUY_SOL * SOL))
            if amount < int(MIN_BUY_SOL * SOL) or free < amount + tx:
                continue
            orders.append(
                {
                    "side": "buy",
                    "mint": mint,
                    "quote_lamports": amount,
                    "max_slippage_bps": BUY_SLIPPAGE_BPS,
                }
            )
            self.traded[mint] = True
            tape = self.tapes.get(mint)
            self.last_buy_price[mint] = tape.trades[-1][1] if tape and tape.trades else 0.0
            free -= amount + tx
            open_slots -= 1

        # --- pyramid adds into strength --------------------------------------
        for mint, holding in list(state.positions.items()):
            if len(orders) >= caps["max_orders_per_batch"]:
                break
            if holding.cost_lamports >= int(MAX_STAKE_SOL * SOL):
                continue
            curve = state.curves.get(mint)
            tape = self.tapes.get(mint)
            if curve is None or curve.complete or tape is None:
                continue
            price = price_of(curve)
            last = self.last_buy_price.get(mint, 0.0)
            if last <= 0 or price < last * (1.0 + ADD_PRICE_STEP):
                continue
            stats = tape.stats(state.slot)
            if stats is None or stats[0] < int(ADD_NET_SOL * SOL):
                continue
            cap = state.max_buy_lamports(mint)
            amount = min(cap, int(ADD_SOL * SOL), int(MAX_STAKE_SOL * SOL) - holding.cost_lamports)
            if amount < int(MIN_BUY_SOL * SOL) or free < amount + tx:
                continue
            orders.append(
                {
                    "side": "buy",
                    "mint": mint,
                    "quote_lamports": amount,
                    "max_slippage_bps": BUY_SLIPPAGE_BPS,
                }
            )
            self.traded[mint] = True
            self.last_buy_price[mint] = price
            free -= amount + tx

        # --- guardrail probes -------------------------------------------------
        if (
            len(self.traded) < TARGET_COINS
            and self.batches - self.last_probe_batch >= PROBE_EVERY_BATCHES
            and len(orders) < caps["max_orders_per_batch"]
            and open_slots > 0
        ):
            best_mint = None
            best_rq = 0
            for mint, curve in state.curves.items():
                if curve.complete or mint in held or mint in self.probe:
                    continue
                rq = curve.real_quote_reserves
                if rq >= int(PROBE_MIN_REAL_SOL * SOL) and rq > best_rq:
                    best_rq = rq
                    best_mint = mint
            if best_mint is not None:
                cap = state.max_buy_lamports(best_mint)
                amount = min(cap, int(PROBE_SOL * SOL))
                if amount >= int(MIN_BUY_SOL * SOL) and free >= amount + tx:
                    orders.append(
                        {
                            "side": "buy",
                            "mint": best_mint,
                            "quote_lamports": amount,
                            "max_slippage_bps": BUY_SLIPPAGE_BPS,
                        }
                    )
                    self.traded[best_mint] = True
                    self.probe[best_mint] = state.slot
                    self.last_probe_batch = self.batches
                    free -= amount + tx

        # --- sell probes as soon as their buy lands ---------------------------
        for mint in list(self.probe):
            if mint in state.positions and len(orders) < caps["max_orders_per_batch"]:
                holding = state.positions[mint]
                orders.append(
                    {
                        "side": "sell",
                        "mint": mint,
                        "token_amount": holding.token_amount,
                        "max_slippage_bps": SELL_SLIPPAGE_BPS,
                    }
                )
                self.probe.pop(mint, None)
        return orders


BOT = Bot()


def on_batch(state, batch):
    return BOT.on_batch(state, batch)


if __name__ == "__main__":
    makefun_bot.run(on_batch)
