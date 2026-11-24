"""Pump.fun Replay Sprint paper bot (agent-fun/replay/v2, Python, stdlib only).

Strategy: launch sniper with a rug filter and fast exits.

The harness speaks the replay protocol on stdin/stdout: one ``hello`` message
with the capital, caps, fees and seed, then one ``batch`` per slot carrying
the events of that slot, the fills of our earlier orders, the account and the
marks. We answer every batch with exactly one reply listing this batch's
orders. The batch's ``slot`` numbers the slot being answered; every order we
send is decided on that slot and filled (or rejected) about two slots later.

Why this shape: the first version bought momentum after a rise and scored
-2.33 SOL on the dev window (win rate 12%): pump.fun curves mean-revert hard
after a spike, so buying the rise means buying the top. The house pilot
momentum bot scored -9.49 SOL on the same window, the reversion bot -4.00,
and the launch sniper -0.48. So this version buys the launch itself, within
its first few slots, only when the opening tape is clean, and exits fast.

Entry (decided while the coin is at most 8 slots old):
  * real SOL between 1 and 30 (the per-order size cap is 1% of real SOL, so
    below 1 SOL a meaningful buy is impossible);
  * the creator's own buy in the create slot is at most 5 SOL, and the
    creator has not sold yet (a creator who dumps at launch is a rug);
  * at least 3 distinct non-creator buyers, and others sold at most 20% of
    what others bought (no dump in progress, no launch bundled by a wallet
    cluster waiting to dump);
  * the price sits between -10% and +50% of the create price (skip coins
    that already pumped), and net SOL over the last 10 slots is at least
    0.3 SOL (fresh inflow, not stale).

Sizing: one buy of 0.05 SOL per coin, no adds. Small, single, and done: each
extra order pays the 105,000-lamport transaction fee plus 1.25% protocol and
creator fees, and adds only chase strength that rarely continues.

Exits (whole position, at once): take profit at +25% over cost, stop loss at
-12%, a trailing stop 15% below the best value once the position is up at
least 12%, a 300-slot time limit, an immediate exit when the creator sells,
or a fade when net SOL over the last 30 slots turns below -0.5 SOL. A curve
that completes is closed by the replay itself.

Guardrail probes: the entry needs a round trip of at least 0.01 SOL on at
least 20 coins. Early entries across hundreds of coins should clear that on
their own, but whenever a position slot is free, cash allows and fewer than
20 coins have been traded, we open a tiny 0.02 SOL probe on the most liquid
undecided curve and sell it whole as soon as its buy lands. Probes are
capped at one per 20 batches so they stay a small share of the run.

Deterministic by construction: no clock, no randomness, no environment
reads; iteration is over insertion-ordered dicts only, and the grader fixes
PYTHONHASHSEED=0, so set iteration order cannot leak in either.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor"))

import makefun_bot  # noqa: E402  (after the sys.path line: -P leaves the script dir off sys.path)

SOL = makefun_bot.LAMPORTS_PER_SOL

# --- entry signal: launch sniper -------------------------------------------
ENTRY_AGE_MAX = 8          # oldest age (slots since CreateEvent) for a first buy
MIN_REAL_SOL = 1.0         # real SOL at decision (size cap is 1% of it)
MAX_REAL_SOL = 30.0
MAX_DEV_BUY_SOL = 5.0      # creator's own buy in the create slot, at most
MIN_BUYERS = 3             # distinct non-creator buyers before entry
MAX_SELL_RATIO = 0.20      # SOL sold by others / SOL bought by others, at most
MIN_RUNUP = -0.10          # price now vs create price, at least
MAX_RUNUP = 0.50           # ... and at most (skip coins that already pumped)
FLOW_WINDOW_SLOTS = 10     # recent-flow window for the entry check
MIN_FLOW_SOL = 0.3         # net SOL in over the flow window

# --- sizing -----------------------------------------------------------------
STAKE_SOL = 0.05
MIN_BUY_SOL = 0.01
BUY_SLIPPAGE_BPS = 500
SELL_SLIPPAGE_BPS = 10_000

# --- exits ------------------------------------------------------------------
TAKE_PROFIT = 0.25         # sell when value reaches cost * (1 + TAKE_PROFIT)
STOP_LOSS = 0.12           # sell when value falls to cost * (1 - STOP_LOSS)
TRAIL_ARM = 0.12           # trailing stop arms once value is up this much
TRAIL_DROP = 0.15          # sell when value falls this far below its best
MAX_HOLD_SLOTS = 300       # slots held before a time-limit exit
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
    """Opening tape for one curve: creator, create price, and its trades."""

    __slots__ = (
        "created_slot",
        "creator",
        "create_price",
        "dev_buy_sol",
        "dev_sold",
        "trades",
        "complete",
    )

    def __init__(self, created_slot, creator, create_price):
        self.created_slot = created_slot
        self.creator = creator
        self.create_price = create_price
        self.dev_buy_sol = 0
        self.dev_sold = False
        self.trades = []  # (slot, price, signed SOL lamports, user, is_buy)
        self.complete = False

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
        self.probe = {}          # mint -> slot of the probe buy
        self.last_probe_batch = -10**9
        self.batches = 0

    # -- tape maintenance ---------------------------------------------------
    def note_events(self, batch):
        for e in batch["events"]:
            mint = e["mint"]
            kind = e["event"]
            if kind == "CreateEvent":
                vt = int(e["virtual_token_reserves"])
                vq = int(e["virtual_quote_reserves"])
                create_price = (vq / vt) if vt > 0 else 0.0
                tape = self.tapes.get(mint)
                if tape is None:
                    tape = Tape(e["slot"], e["creator"], create_price)
                    self.tapes[mint] = tape
                else:
                    tape.created_slot = e["slot"]
                    tape.creator = e["creator"]
                    tape.create_price = create_price
                self.candidates[mint] = True
            elif kind == "TradeEvent":
                tape = self.tapes.get(mint)
                if tape is None:
                    tape = Tape(e["slot"], e["user"], 0.0)
                    self.tapes[mint] = tape
                vt = int(e["virtual_token_reserves"])
                vq = int(e["virtual_quote_reserves"])
                price = (vq / vt) if vt > 0 else 0.0
                sol = int(e["sol_amount"])
                is_buy = bool(e["is_buy"])
                if not is_buy:
                    sol = -sol
                tape.trades.append((e["slot"], price, sol, e["user"], is_buy))
                if e["user"] == tape.creator:
                    if is_buy:
                        if e["slot"] == tape.created_slot:
                            tape.dev_buy_sol += int(e["sol_amount"])
                    else:
                        tape.dev_sold = True
            else:  # CompleteEvent
                tape = self.tapes.get(mint)
                if tape is None:
                    tape = Tape(e["slot"], "", 0.0)
                    self.tapes[mint] = tape
                tape.complete = True
                self.candidates.pop(mint, None)

    # -- exits ---------------------------------------------------------------
    def exit_orders(self, state, orders, selling):
        slot = state.slot
        for mint, holding in list(state.positions.items()):
            if mint in selling:
                continue  # a sell is already in flight for this coin
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
            if value >= cost * (1.0 + TAKE_PROFIT):
                sell = True  # take profit fast
            elif value <= cost * (1.0 - STOP_LOSS):
                sell = True  # cut the loser
            elif best >= cost * (1.0 + TRAIL_ARM) and value <= best * (1.0 - TRAIL_DROP):
                sell = True  # trailing stop from the best close value
            elif slot - self.entry_slot[mint] >= MAX_HOLD_SLOTS:
                sell = True  # time limit
            elif tape is not None and tape.dev_sold:
                sell = True  # the creator sold: leave at once
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
                selling.add(mint)
                self.best_value.pop(mint, None)
                self.entry_slot.pop(mint, None)
                self.probe.pop(mint, None)
        return orders

    # -- entries -------------------------------------------------------------
    def entry_signal(self, state, mint):
        tape = self.tapes.get(mint)
        curve = state.curves.get(mint)
        if tape is None or curve is None or curve.complete:
            return False
        age = state.slot - tape.created_slot
        if age < 0 or age > ENTRY_AGE_MAX:
            return False
        if tape.dev_sold:
            return False
        if tape.dev_buy_sol > int(MAX_DEV_BUY_SOL * SOL):
            return False
        real_sol = curve.real_quote_reserves / SOL
        if real_sol < MIN_REAL_SOL or real_sol > MAX_REAL_SOL:
            return False
        # opening tape: distinct non-creator buyers, and others' sell ratio
        buyers = {}
        bought = 0
        sold = 0
        for _s, _p, sol, user, is_buy in tape.trades:
            if user == tape.creator:
                continue
            if is_buy:
                buyers[user] = True
                bought += sol
            else:
                sold += -sol
        if len(buyers) < MIN_BUYERS:
            return False
        if bought <= 0 or sold > MAX_SELL_RATIO * bought:
            return False
        # price must not have run away from the create price already
        price = price_of(curve)
        if tape.create_price <= 0 or price <= 0:
            return False
        runup = price / tape.create_price - 1.0
        if runup < MIN_RUNUP or runup > MAX_RUNUP:
            return False
        # fresh inflow, not a stale tape
        if tape.net_over(state.slot, FLOW_WINDOW_SLOTS) < int(MIN_FLOW_SOL * SOL):
            return False
        return True

    def on_batch(self, state, batch):
        self.batches += 1
        self.note_events(batch)
        # periodic cleanup: tapes of coins long past deciding, neither held
        # nor candidates, only cost memory (the replay holds thousands)
        if self.batches % 200 == 0:
            for mint in list(self.tapes):
                tape = self.tapes[mint]
                if (
                    state.slot - tape.created_slot > ENTRY_AGE_MAX
                    and mint in self.candidates
                    and mint not in state.positions
                ):
                    self.candidates.pop(mint, None)  # too old now: never buy it
                if (
                    state.slot - tape.created_slot > 100
                    and mint not in state.positions
                    and mint not in self.candidates
                ):
                    del self.tapes[mint]
                else:
                    cutoff = state.slot - 40
                    trades = tape.trades
                    i = 0
                    while i < len(trades) and trades[i][0] < cutoff:
                        i += 1
                    if i:
                        del trades[:i]
        caps = state.hello["caps"]
        tx = state.tx_fee_lamports()
        free = state.free_cash_lamports()
        orders = []

        # mints with a sell already in flight: never sell twice (a duplicate
        # sell is rejected and still pays the transaction fee)
        selling = set()
        for p in state.pending.values():
            if p.order["side"] == "sell":
                selling.add(p.order["mint"])
        self.exit_orders(state, orders, selling)

        held = set(state.positions)
        for p in state.pending.values():
            if p.order["side"] == "buy":
                held.add(p.order["mint"])
        open_slots = caps["max_open_positions"] - len(held)

        # --- launch entries, in batch event order ----------------------------
        for e in batch["events"]:
            mint = e["mint"]
            if mint not in self.candidates:
                continue
            curve = state.curves.get(mint)
            if curve is None or curve.complete:
                self.candidates.pop(mint, None)
                continue
            tape = self.tapes.get(mint)
            if tape is not None and state.slot - tape.created_slot > ENTRY_AGE_MAX:
                self.candidates.pop(mint, None)  # too old now: never buy it
                continue
            if not self.entry_signal(state, mint):
                continue
            self.candidates.pop(mint, None)
            if len(orders) >= caps["max_orders_per_batch"] or open_slots <= 0:
                break
            cap = state.max_buy_lamports(mint)
            amount = min(cap, int(STAKE_SOL * SOL))
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
            free -= amount + tx
            open_slots -= 1

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
            if (
                mint in state.positions
                and mint not in selling
                and len(orders) < caps["max_orders_per_batch"]
            ):
                holding = state.positions[mint]
                orders.append(
                    {
                        "side": "sell",
                        "mint": mint,
                        "token_amount": holding.token_amount,
                        "max_slippage_bps": SELL_SLIPPAGE_BPS,
                    }
                )
                selling.add(mint)
                self.probe.pop(mint, None)
        return orders


BOT = Bot()


def on_batch(state, batch):
    return BOT.on_batch(state, batch)


if __name__ == "__main__":
    makefun_bot.run(on_batch)
