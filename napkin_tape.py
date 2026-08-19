#!/usr/bin/env python3
"""napkin-tape: a calibrated replay-market simulator built from ClawStreet's own bars.

One file, three commands:
  collect    pull daily (+intraday hourly) bars for the fixed universe into out/tape/
  selfcheck  no-network asserts on a synthetic tape: cost model to the cent,
             accounting identity vs an independently coded second implementation,
             replay determinism, flat-policy invariance, look-ahead leak *caught*
  baselines  flat / buy-and-hold / momentum on the real tape, vs archived leaderboard

Repo 1 of the napkin-trader series. See README.md for registered hypotheses.
"""
import json, math, os, sys, time, urllib.error, urllib.parse, urllib.request
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
TAPE_DIR = os.path.join(HERE, "out", "tape")
STOCKS = ["AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "AVGO",
          "JPM", "V", "XOM", "LLY", "WMT", "COST", "UNH"]
CRYPTOS = ["X:BTCUSD", "X:ETHUSD", "X:SOLUSD"]
UNIVERSE = STOCKS + CRYPTOS
NYSE_HOLIDAYS_2026 = {date(2026, 1, 1), date(2026, 1, 19), date(2026, 2, 16),
                      date(2026, 4, 3), date(2026, 5, 25), date(2026, 6, 19),
                      date(2026, 7, 3), date(2026, 9, 7), date(2026, 11, 26),
                      date(2026, 12, 25)}
ET = ZoneInfo("America/New_York")
PAUSE = 1.1  # 55 req/min < the venue's 60/min per-key limit

# ---------------------------------------------------------------- collection

def _key():
    for line in open(os.path.expanduser("~/.clawstreet/credentials.env")):
        if line.startswith("CLAWSTREET_API_KEY="):
            return line.strip().split("=", 1)[1]
    raise SystemExit("no API key in ~/.clawstreet/credentials.env")


def _get(url, key=None):
    req = urllib.request.Request(url)
    if key:
        req.add_header("Authorization", "Bearer " + key)
    for i in range(3):
        try:
            with urllib.request.urlopen(req, timeout=45) as r:
                return json.load(r)
        except Exception as e:
            if i == 2:
                raise
            time.sleep(5 * (i + 1))


def is_trading_day(d):
    return d.weekday() < 5 and d not in NYSE_HOLIDAYS_2026


def prev_trading_day(d):
    d -= timedelta(days=1)
    while not is_trading_day(d):
        d -= timedelta(days=1)
    return d


def last_completed_session(now_et):
    """Most recent NYSE session whose 16:00 ET close has passed."""
    d = now_et.date()
    if not is_trading_day(d) or now_et.hour < 16:
        d = prev_trading_day(d)
    return d


def tape_path(sym):
    return os.path.join(TAPE_DIR, sym.replace(":", "_") + ".jsonl")


def load_rows(sym):
    p = tape_path(sym)
    if not os.path.exists(p):
        return []
    return [json.loads(l) for l in open(p)]


def merge_rows(sym, new_rows):
    """Upsert by (res, date, idx). Existing bars must match incoming ones exactly —
    a silent history rewrite (split/adjustment/API change) fails loudly."""
    rows = load_rows(sym)
    by_k = {(r["res"], r["date"], r.get("idx", 0)): r for r in rows}
    added = 0
    for r in new_rows:
        k = (r["res"], r["date"], r.get("idx", 0))
        if k in by_k:
            old = by_k[k]
            for f in ("o", "c"):
                assert abs(old[f] - r[f]) < 1e-6, \
                    f"{sym} {k}: stored {f}={old[f]} but API now says {r[f]} — history rewrite?"
        else:
            by_k[k] = r
            added += 1
    rows = sorted(by_k.values(), key=lambda r: (r["res"], r["date"], r.get("idx", 0)))
    os.makedirs(TAPE_DIR, exist_ok=True)
    with open(tape_path(sym), "w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    return added


def collect():
    key = _key()
    now_et = datetime.now(ET)
    is_open = _get("https://www.clawstreet.io/api/market-status")["isOpen"]
    anchor = last_completed_session(now_et)
    total = 0
    for sym in UNIVERSE:
        crypto = sym.startswith("X:")
        h = _get(f"https://www.clawstreet.io/api/data/history?symbol={urllib.parse.quote(sym)}&periods=100",
                 key)[sym]
        time.sleep(PAUSE)
        o, hi, lo, c, v = h["open"], h["high"], h["low"], h["prices"], h["volumes"]
        n = len(c)
        # last bar is in-progress: always for 24/7 crypto, for stocks while market is open
        drop = 1 if (crypto or is_open) else 0
        n -= drop
        # walk dates back from the anchor (bars carry no timestamps — mark inferred)
        if crypto:
            d = datetime.now(ZoneInfo("UTC")).date() - timedelta(days=1)
            dates = []
            for _ in range(n):
                dates.append(d)
                d -= timedelta(days=1)
        else:
            d = anchor
            dates = []
            for _ in range(n):
                dates.append(d)
                d = prev_trading_day(d)
        dates.reverse()
        rows = [{"res": "day", "date": dates[i].isoformat(), "o": o[i], "h": hi[i],
                 "l": lo[i], "c": c[i], "v": v[i], "inferred": True} for i in range(n)]
        # intraday hourly (stocks only; API serves them for the current session only)
        if not crypto:
            hh = _get(f"https://www.clawstreet.io/api/data/history?symbol={sym}&periods=100&timespan=hour",
                      key)[sym]
            time.sleep(PAUSE)
            m = len(hh["prices"]) - (1 if is_open else 0)
            today = now_et.date().isoformat()
            rows += [{"res": "hour", "date": today, "idx": i, "o": hh["open"][i],
                      "h": hh["high"][i], "l": hh["low"][i], "c": hh["prices"][i],
                      "v": hh["volumes"][i]} for i in range(m)]
        total += merge_rows(sym, rows)
    # sanity on what's now stored
    for sym in UNIVERSE:
        days = [r for r in load_rows(sym) if r["res"] == "day"]
        assert len(days) >= 95, f"{sym}: only {len(days)} daily bars"
        ds = [r["date"] for r in days]
        assert ds == sorted(set(ds)), f"{sym}: dates not strictly increasing"
        if not sym.startswith("X:"):
            assert all(date.fromisoformat(x).weekday() < 5 for x in ds), f"{sym}: weekend bar"
    print(f"collect: +{total} bars across {len(UNIVERSE)} symbols "
          f"(anchor {anchor}, market {'open' if is_open else 'closed'})")


# ---------------------------------------------------------------- the sim

class LookaheadError(Exception):
    pass


class Tape:
    """Aligned daily bars: dates common to every symbol, oldest first."""
    def __init__(self, bars_by_sym):
        common = None
        for sym, rows in bars_by_sym.items():
            ds = {r["date"] for r in rows}
            common = ds if common is None else common & ds
        self.dates = sorted(common)
        self.syms = sorted(bars_by_sym)
        self.bars = {}
        for sym, rows in bars_by_sym.items():
            by_d = {r["date"]: r for r in rows}
            self.bars[sym] = [by_d[d] for d in self.dates]

    @classmethod
    def load(cls, since=None):
        by_sym = {}
        for sym in UNIVERSE:
            rows = [r for r in load_rows(sym) if r["res"] == "day"]
            if since:
                rows = [r for r in rows if r["date"] >= since]
            by_sym[sym] = rows
        return cls(by_sym)

    def __len__(self):
        return len(self.dates)


class View:
    """What a policy is allowed to see at time t: bars [0..t] and nothing else."""
    def __init__(self, tape, t):
        self._tape, self._t = tape, t
        self.syms, self.t = tape.syms, t

    def bar(self, sym, i):
        if i > self._t or i < 0:
            raise LookaheadError(f"access to bar {i} at t={self._t}")
        return self._tape.bars[sym][i]

    def closes(self, sym, n):
        lo = max(0, self._t + 1 - n)
        return [self.bar(sym, i)["c"] for i in range(lo, self._t + 1)]


def dollar_volume(tape, sym, t, n=20):
    lo = max(0, t + 1 - n)
    bars = tape.bars[sym][lo:t + 1]
    return sum(b["v"] * b["c"] for b in bars) / len(bars)


def fill_cost(sym, side_qty, open_price, dv):
    """ClawStreet's published execution model. Returns (fill_price, commission).
    slippage = (notional / daily_dollar_volume) * 50 bps, adverse to the taker."""
    notional = abs(side_qty) * open_price
    slip = (notional / dv) * 50 * 1e-4 if dv > 0 else 0.0
    fill = open_price * (1 + slip) if side_qty > 0 else open_price * (1 - slip)
    if sym.startswith("X:"):
        comm = 0.0005 * abs(side_qty) * fill
    else:
        comm = 0.005 * abs(side_qty)
    return fill, comm


def run_sim(tape, policy, cash=100_000.0, warmup=20):
    """Decide on bar t's close, execute at bar t+1's open, mark at t+1's close.
    policy(view) -> {sym: target_equity_fraction in [-1,1]} for symbols to adjust
    (empty dict = hold). Returns (equity_curve, fills)."""
    pos = {s: 0.0 for s in tape.syms}
    fills = []
    curve = []
    for t in range(warmup, len(tape) - 1):
        closes = {s: tape.bars[s][t]["c"] for s in tape.syms}
        equity = cash + sum(pos[s] * closes[s] for s in tape.syms)
        targets = policy(View(tape, t)) or {}
        for sym in sorted(targets):
            open_next = tape.bars[sym][t + 1]["o"]
            want_qty = targets[sym] * equity / open_next
            dq = want_qty - pos[sym]
            if abs(dq) * open_next < 1.0:  # dust
                continue
            gross = sum(abs(pos[s]) * closes[s] for s in tape.syms if s != sym)
            if gross + abs(want_qty) * open_next > 2.0 * equity:  # venue leverage cap
                continue
            dv = dollar_volume(tape, sym, t)
            fill, comm = fill_cost(sym, dq, open_next, dv)
            cash -= dq * fill + comm
            pos[sym] += dq
            fills.append({"t": t + 1, "sym": sym, "dq": dq, "fill": fill, "comm": comm})
        marks = {s: tape.bars[s][t + 1]["c"] for s in tape.syms}
        curve.append(cash + sum(pos[s] * marks[s] for s in tape.syms))
    return curve, fills


def replay_fills(tape, fills, cash=100_000.0, warmup=20):
    """Second, independent accounting implementation: rebuild the equity curve from
    the fill log alone. Deliberately structured differently from run_sim."""
    fills_at = {}
    for f in fills:
        fills_at.setdefault(f["t"], []).append(f)
    holdings = {}
    curve = []
    for t in range(warmup + 1, len(tape)):
        for f in fills_at.get(t, ()):
            cash = cash - f["dq"] * f["fill"] - f["comm"]
            holdings[f["sym"]] = holdings.get(f["sym"], 0.0) + f["dq"]
        mtm = 0.0
        for sym, q in holdings.items():
            mtm += q * tape.bars[sym][t]["c"]
        curve.append(cash + mtm)
    return curve


# ---------------------------------------------------------------- policies

def flat_policy(view):
    return {}


def make_buy_and_hold(start_t):
    def policy(view):
        if view.t == start_t:
            return {s: 1.0 / len(view.syms) for s in view.syms}
        return {}
    return policy


def make_momentum(lookback=20, top=5, every=5):
    def policy(view):
        if (view.t % every) != 0:
            return {}
        scores = {}
        for s in view.syms:
            c = view.closes(s, lookback + 1)
            if len(c) > lookback and c[0] > 0:
                scores[s] = c[-1] / c[0] - 1
        winners = sorted(scores, key=scores.get, reverse=True)[:top]
        return {s: (0.95 / top if s in winners else 0.0) for s in view.syms}
    return policy


# ---------------------------------------------------------------- selfcheck

def synthetic_tape():
    """3 symbols, 40 bars, deterministic prices; SYN1 built for hand-checkable fills."""
    by_sym = {}
    for sym, base, drift in (("SYN1", 100.0, 0.0), ("SYN2", 50.0, 0.5), ("X:SYNUSD", 1000.0, -2.0)):
        rows = []
        for i in range(40):
            o = base + drift * i + (5 * math.sin(i / 3) if sym == "SYN2" else 0)
            c = o + (0.5 if sym != "X:SYNUSD" else -1.0)
            rows.append({"date": f"2026-01-{i + 1:02d}x", "o": round(o, 4), "h": round(o + 2, 4),
                         "l": round(o - 2, 4), "c": round(c, 4), "v": 100_000.0})
        by_sym[sym] = rows
    return Tape(by_sym)


def selfcheck():
    tape = synthetic_tape()

    # 1. cost model to the cent, hand-computed:
    #    SYN1 open=100, v=100000 c=100.5 flat-ish -> dv ~= 100000*100.5; buy 100 shares
    dv = dollar_volume(tape, "SYN1", 20)
    fill, comm = fill_cost("SYN1", 100, 100.0, dv)
    slip = (100 * 100.0 / dv) * 50 * 1e-4
    assert abs(fill - 100.0 * (1 + slip)) < 1e-12 and abs(comm - 0.50) < 1e-12
    fill_c, comm_c = fill_cost("X:SYNUSD", 1.0, 1000.0, dv)
    assert abs(comm_c - 0.0005 * fill_c) < 1e-12, "crypto commission is 5bps of notional"
    print("selfcheck 1/6: cost model matches hand computation")

    # 2. flat policy: equity is exactly 100k forever
    curve, fills = run_sim(tape, flat_policy)
    assert fills == [] and all(x == 100_000.0 for x in curve)
    print("selfcheck 2/6: flat policy holds $100,000.00 exactly")

    # 3. determinism: same tape + policy twice -> byte-identical curves
    a, fa = run_sim(tape, make_momentum(lookback=5, top=2, every=3))
    b, fb = run_sim(tape, make_momentum(lookback=5, top=2, every=3))
    assert repr(a) == repr(b) and repr(fa) == repr(fb)
    print("selfcheck 3/6: replay is deterministic")

    # 4. accounting identity vs the independent second implementation, to the cent
    assert len(fa) > 5, "momentum produced no trades on synthetic tape?"
    a2 = replay_fills(tape, fa)
    assert len(a) == len(a2) and all(abs(x - y) < 0.01 for x, y in zip(a, a2)), \
        f"accounting mismatch: max diff {max(abs(x - y) for x, y in zip(a, a2))}"
    print(f"selfcheck 4/6: two independent accountings agree to the cent over {len(a)} bars, {len(fa)} fills")

    # 5. look-ahead: a leaky policy must be CAUGHT
    def leaky(view):
        view.bar("SYN1", view.t + 1)  # peek at tomorrow
        return {}
    caught = False
    try:
        run_sim(tape, leaky)
    except LookaheadError:
        caught = True
    assert caught, "leaky policy was NOT caught"
    v = View(tape, 10)
    assert v.closes("SYN1", 5) == [tape.bars["SYN1"][i]["c"] for i in range(6, 11)]
    print("selfcheck 5/6: look-ahead leak is caught; honest access works")

    # 6. real-tape sanity (only if collected)
    if os.path.exists(tape_path("AAPL")):
        real = Tape.load()
        assert len(real) >= 90, f"aligned real tape only {len(real)} bars"
        assert real.dates == sorted(real.dates)
        print(f"selfcheck 6/6: real tape aligned — {len(real)} common daily bars x {len(real.syms)} symbols")
    else:
        print("selfcheck 6/6: skipped (no real tape yet — run collect)")
    print("ALL SELFCHECKS PASS")


# ---------------------------------------------------------------- baselines

def baselines(window_start="2026-06-08"):
    full = Tape.load()
    start_t = next(i for i, d in enumerate(full.dates) if d >= window_start)
    warmup = start_t - 1  # decisions start on the last bar before the window
    results = {}
    for name, pol in [("flat", flat_policy),
                      ("buy_and_hold", make_buy_and_hold(warmup)),
                      ("momentum", make_momentum())]:
        curve, fills = run_sim(full, pol, warmup=warmup)
        ident = replay_fills(full, fills, warmup=warmup)
        assert all(abs(x - y) < 0.01 for x, y in zip(curve, ident)), f"{name}: accounting mismatch"
        ret = (curve[-1] / 100_000.0 - 1) * 100
        results[name] = {"return_pct": round(ret, 3), "n_fills": len(fills),
                         "final_equity": round(curve[-1], 2)}
        print(f"{name:14} {ret:+7.2f}%  fills={len(fills):3}  "
              f"window {full.dates[warmup + 1]} → {full.dates[-1]}")

    # against the archived leaderboard (agents' lifetime total_return_pct — see README caveat)
    arch = sorted(os.path.join("/home/bob/clawstreet-archive/data", d, "leaderboard.json")
                  for d in os.listdir("/home/bob/clawstreet-archive/data")
                  if os.path.exists(os.path.join("/home/bob/clawstreet-archive/data", d, "leaderboard.json")))
    if arch:
        board = json.load(open(arch[-1]))["board"]
        rets = sorted(r["total_return_pct"] for r in board if r["total_return_pct"] is not None)
        for name in ("buy_and_hold", "momentum"):
            r = results[name]["return_pct"]
            pct_below = 100 * sum(1 for x in rets if x < r) / len(rets)
            results[name]["pct_of_board_below"] = round(pct_below, 1)
            print(f"{name}: beats {pct_below:.0f}% of the {len(rets)}-agent board")
        q1, q3 = rets[len(rets) // 4], rets[3 * len(rets) // 4]
        results["board"] = {"snapshot": arch[-1], "n": len(rets), "iqr": [q1, q3]}
        print(f"board IQR: [{q1:+.2f}%, {q3:+.2f}%]")
    os.makedirs(os.path.join(HERE, "out"), exist_ok=True)
    json.dump(results, open(os.path.join(HERE, "out", "baselines.json"), "w"), indent=1)
    print("wrote out/baselines.json")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "selfcheck"
    {"collect": collect, "selfcheck": selfcheck, "baselines": baselines}[cmd]()
