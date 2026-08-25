#!/usr/bin/env python3
"""Self-consistency verifier for spell_balance.py -- run it before every release and deploy.

THE BAR: run an overhaul's own plugin against a baseline derived from a list where that overhaul
wins the vanilla spells. The tool must be (close to) a no-op on it -- and that must hold for
DAMAGE and for COST, per delivery category. v1.0.0 shipped on a half-test: the damage table was
verified and cost was never measured, which hid a x16 repricing of hazard payloads and a x3
overcharge on Master concentration. Never again: this script runs the REAL balance_plugin and
diffs its input against its output on both axes.

Usage:
  python3 verify.py DATA_DIR MODS_DIR|- LOADORDER|- plugin.esp [plugin2.esp ...]
Example (Simonrim list, verifying its own overhaul):
  python3 verify.py "<list>/Stock Game/Data" "<list>/mods" "<list>/profiles/Default/loadorder.txt" \
      "<list>/mods/Mysticism - A Magic Overhaul/MysticismMagic.esp"

What to expect on the baseline's own overhaul (measured at (DOT_EXP, DOT_COST_BACK)=(0.75,0.40)):
  burst / conc damage AND cost medians x1.00; conc per-tier cost <= ~1.09; rune cost ~1.00 (rune
  damage reads the vanilla rune premium against the pack's own, ~x1.3); tome DoT damage x1.00 and
  cost ~x1.01 (whole-bucket medians move with the pack's creature/NPC spells, which are repriced
  by design); non-damage, cloak/proc and SKIP:hazard rows exactly x1.00 (untouched).
"""
import sys, os, io, contextlib, statistics, collections, math
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import spell_lib as S, spell_balance as B

def main():
    if len(sys.argv) < 5:
        print(__doc__); sys.exit(1)
    DATA, MODS, ORDER = sys.argv[1:4]; PLUGS = sys.argv[4:]
    B.VANILLA_DATA = DATA
    v = {n: B.low3_map(os.path.join(DATA, n)) for n in B.VANILLA_MASTERS}
    v = {a: b for a, b in v.items() if b}
    op = B.resolve_order(ORDER, DATA, MODS if MODS != '-' else None) if ORDER != '-' else None
    curve, ceil, fit = B.build_vanilla_model(v, None, op)
    knobs = (100, B.TIER_COST, B.TIER_MAG, B.VARIETY)

    def skipsets(buf, name):
        ms = S.masters(buf)
        if name.endswith('.bak'): name = name[:-4]
        f, c = B.nocast_keys(buf, name, ms)
        full = fit['nocast_full'] | f; cost = fit['nocast_cost'] | c
        def key(fid):
            hi = fid >> 24
            return ((ms[hi] if hi < len(ms) else name), fid & 0xFFFFFF)
        return full, cost, key

    def snapshot(buf, res, full, cost, key, tomes):
        out = {}
        for r in S.iter_top_records(buf, {b'SPEL'}):
            if r.comp: continue
            sp = S.parse_spel(r.data)
            if sp['type'] != 0 or sp['spit_off'] is None: continue
            k = key(r.formid)
            effs = [(res(e['mgef']), e) for e in sp['effects']]
            tier = B.classify_tier(effs)
            if k in full:
                out[r.formid] = (sp['edid'], 'SKIP:hazard', tier, sp['cost'], 0.0); continue
            if B.has_damage(effs):
                d = max(e['dur'] for m, e in effs if S.is_damage(m) and e['mag'] > 0)
                cls = B.spell_class(sp['castType'])
                cat = ('conc' if cls == 'conc' else
                       'field/hazard' if sp['delivery'] in B.FIELD_DELIVERY and d > 1 else
                       'rune' if sp['delivery'] == 4 else
                       'per-target DoT' if d > 1 else 'burst')
                sc = B.score(effs, sp['castType'], sp['delivery'], fit['dot_exp'])
            else:
                cat = 'non-damage'; sc = 0.0
            if k in cost and cat != 'non-damage': cat = 'cloak/proc'
            elif cat != 'non-damage' and r.formid not in tomes:
                cat += ' (npc)'   # no tome in the pack: NPC/summon/proc spell, not the design's player line
            out[r.formid] = (sp['edid'], cat, tier, sp['cost'], sc)
        return out

    def pct(rs, q): return rs[min(len(rs)-1, int(q*len(rs)))]
    def line(rs):
        if not rs: return "-"
        rs = sorted(rs); w = sum(1 for x in rs if 0.8 <= x <= 1.25)
        return (f"n={len(rs):3}  med x{statistics.median(rs):5.2f}  p10 x{pct(rs,0.10):5.2f}  "
                f"p90 x{pct(rs,0.90):5.2f}  within25% {100*w//len(rs):3}%")

    CATS = ['burst','per-target DoT','conc','field/hazard','rune',
            'burst (npc)','per-target DoT (npc)','conc (npc)','field/hazard (npc)','rune (npc)',
            'cloak/proc','non-damage','SKIP:hazard']
    for pl in PLUGS:
        buf = open(pl, 'rb').read()
        res = B.build_resolver(S.masters(buf), S.read_mgef_map(buf), v)
        out = B.balance_plugin(pl, curve, ceil, fit, v, knobs)[0]
        full, cost, key = skipsets(buf, os.path.basename(pl))
        tomes = S.read_tome_spells(buf)
        A = snapshot(buf, res, full, cost, key, tomes); Z = snapshot(out, res, full, cost, key, tomes)
        dmg = collections.defaultdict(list); cst = collections.defaultdict(list)
        conc_t = collections.defaultdict(lambda: ([], [])); movers = []
        for fid, (ed, cat, tier, c0, s0) in A.items():
            ez, catz, tz, c1, s1 = Z[fid]
            if s0 > 0 and s1 > 0:
                dmg[cat].append(s1/s0)
                if cat == 'conc': conc_t[tier][0].append(s1/s0)
            if c0 > 0:
                r = c1/c0; cst[cat].append(r)
                if cat == 'conc': conc_t[tier][1].append(r)
                movers.append((abs(math.log(max(r, 1e-9))), ed, cat, tier, c0, c1, r))
        print(f"\n=== {os.path.basename(pl)} ===")
        print(f"{'category':16} {'DAMAGE ratio':>52}     {'COST ratio':>52}")
        for cat in CATS:
            if cat not in dmg and cat not in cst: continue
            print(f"  {cat:19} {line(dmg.get(cat, [])):>50}     {line(cst.get(cat, [])):>50}")
        if conc_t:
            print("  conc by tier:  " + "   ".join(
                f"{t[:3]} dmg x{statistics.median(d):.2f}/cost x{statistics.median(c):.2f}(n{len(c)})"
                for t, (d, c) in sorted(conc_t.items(), key=lambda kv: B.TI[kv[0]]) if c))
        movers.sort(reverse=True)
        print("  largest cost movers:")
        for _, ed, cat, tier, c0, c1, r in movers[:10]:
            print(f"     {ed[:40]:42} {cat:14} {tier:10} {c0:6} -> {c1:6}  x{r:5.2f}")

if __name__ == '__main__':
    main()
