#!/usr/bin/env python3
"""Skyrim SE spell balancer — reprice ADDED spell packs to the VANILLA standard, with
the balance DERIVED from vanilla data (no xEdit / no CK). Knobs are 100 = vanilla.

WHY THIS EXISTS
  Every spell pack ships on its own power/cost scale. Vanilla is one consistent scale,
  observed across hundreds of spells: a spell's magicka cost, damage, tier (min-skill),
  duration and effect base-cost are all tied by the engine. That over-determined system
  is solvable — so this tool FITS the vanilla relationships and moves each pack onto them,
  instead of trusting the author's (inconsistent) numbers or leaving damage untouched.

THE SOLVE (per plugin = per pack)
  1. Vanilla damage curve: from Skyrim.esm, median primary damage magnitude per
     (archetype, tier) — archetype = concentration / instant / DoT, tier = Novice..Master
     from the effect's minimum-skill. Damage = a HOSTILE effect that modifies Health
     (so illusion "fear level" / magicka-drain / utility magnitudes are never rescaled).
     The 5-tier vector per archetype is monotone-smoothed and gap-filled.
  2. Pack damage factor = clamped median over the pack's damage spells of
     (vanilla_curve[arch][tier] / author_magnitude). ONE robust number per pack: it moves
     the pack's damage distribution onto vanilla while preserving the author's internal
     per-tier / per-spell variety, and can't be wrecked by a single outlier spell.
  3. Apply: every damage effect's magnitude *= factor * knobs. Then magicka cost is
     RECOMPUTED from the (now vanilla-scaled) effects with Bethesda's own autocalc formula
     (fit to Skyrim.esm, median error 0%):
         cost = Σ_eff  baseCost * max(mag,1)^1.1 * max(duration/10, 1)
     Because damage is now vanilla-scale, cost lands in vanilla range on its own — no
     arbitrary caps. A soft clamp at the vanilla per-tier ceiling only guards the rare tail.

KNOBS (edit CONFIG or pass on CLI; 100 = vanilla default, scale from there)
  OVERALL, TIER_COST[tier], TIER_MAG[tier].
  effective cost mult = OVERALL/100 * TIER_COST[tier]/100
  effective mag  mult = OVERALL/100 * TIER_MAG[tier]/100  (times the pack factor)

USAGE
  python3 spell_balance.py                         # use built-in PLUGINS config, dry (report + OUT_DIR)
  python3 spell_balance.py --deploy                # also write to each plugin's deploy path
  python3 spell_balance.py --data DIR a.esp b.esl  # balance ANY plugins in place (needs --deploy)
  python3 spell_balance.py --data DIR --dry a.esp  # report only
Edits are FIXED-WIDTH in-place (SPIT cost u32 + flags, EFIT magnitude f32); no spell record
in these packs is compressed, so file size never changes and no size fixups are needed.
"""
import sys, os, struct, math, statistics, collections, shutil, argparse
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import spell_lib as S

# ============================== CONFIG (edit me) ==============================
VANILLA_DATA = os.environ.get("SKYRIM_DATA", "./Data")
VANILLA_MASTERS = ["Skyrim.esm", "Update.esm", "Dawnguard.esm", "HearthFires.esm", "Dragonborn.esm"]
CURVE_MASTER = "Skyrim.esm"        # which master to fit the vanilla curve from

OVERALL   = 100
TIER_COST = {'Novice':100,'Apprentice':100,'Adept':100,'Expert':100,'Master':100}
TIER_MAG  = {'Novice':100,'Apprentice':100,'Adept':100,'Expert':100,'Master':100}
# Each spell's damage is pinned to the vanilla curve for its own (tier, archetype); VARIETY
# log-blends back toward the author's value: 0.0 = pure vanilla (every same-tier spell equal),
# 1.0 = author's damage untouched. Default keeps mostly-vanilla with a little author character.
VARIETY   = 0.35
DELIVERY  = 0.25                    # DoT delivery-speed surcharge exponent (0 = off)
COST_CEIL_HEADROOM = 1.5            # soft cost clamp = vanilla tier max cost * this

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC  = os.path.join(_HERE, "..", "planning", "spell_src")
_MODS = os.environ.get("SPELLBAL_MODS", "./mods")
OUT_DIR   = os.environ.get("SPELLBAL_OUT", "./balanced_out")   # dry-run output dir
# Built-in list (Terminal Destiny). Each: (pristine source, deploy path, display name).
PLUGINS = []   # optional built-in list; normally pass plugins on the CLI
# =============================================================================

TIERS = ['Novice','Apprentice','Adept','Expert','Master']; TI = {t:i for i,t in enumerate(TIERS)}
ARCHS = ['inst','conc','dot']
# fallback curve (used only for tiers with no vanilla sample) — extrapolated from vanilla shape
FALLBACK = {'inst':[15,30,45,60,90], 'conc':[8,11,14,18,40], 'dot':[130,200,300,450,700]}  # dot = TOTALS

def archetype(castType, dur):
    return 'conc' if castType==2 else ('dot' if dur>0 else 'inst')

def vanilla_cost(effs):
    tot = 0.0
    for m, e in effs:
        if not m: continue
        bc=m['basecost']; mag=max(e['mag'],1.0); dur=e['dur']
        tot += bc*(mag**1.1)*(max(dur/10.0,1.0) if dur>0 else 1.0)
    return tot

def classify_tier(effs):
    valid=[m for m,e in effs if m]
    if not valid: return 'Novice'
    return S.tier_of(max((m['minskill'] for m in valid if m['minskill'] is not None), default=0)) or 'Novice'

def low3_map(path):
    if not os.path.exists(path): return {}
    return {fid & 0xFFFFFF: f for fid,f in S.read_mgef_map(open(path,'rb').read()).items()}

def build_resolver(masters, own_map, van_low3):
    def resolve(fid):
        hi=fid>>24
        if hi>=len(masters): return own_map.get(fid)
        name=masters[hi]
        return van_low3.get(name,{}).get(fid & 0xFFFFFF)
    return resolve

def delivered(effs, is_conc):
    """Archetype-correct DELIVERED damage. Concentration magnitude is already a per-SECOND rate
    (and its cost is per-second too), so it is used as-is; everything else delivers mag*duration
    (a burst has dur 0 -> mag). Summed over ALL damage effects, so riders count. This is the one
    observable that survives a baseline scaling EITHER magnitude OR duration."""
    return sum((e['mag'] if is_conc else e['mag']*max(e['dur'],1))
               for m,e in effs if S.is_damage(m) and e['mag']>0)

def primary_damage(effs):
    dmg=[(m,e) for m,e in effs if S.is_damage(m) and e['mag']>0]
    return max(dmg, key=lambda me: me[0]['basecost']) if dmg else None

def fill_gaps(vec_by_tier, fallback):
    """Interpolate gaps WITHOUT forcing monotonicity. smooth_fill() is for damage (rises with
    tier); efficiency (damage per magicka) legitimately FALLS with tier, so it needs this."""
    out=[vec_by_tier.get(i) for i in range(5)]
    known=[i for i in range(5) if out[i] is not None]
    if not known: return list(fallback)
    for i in range(5):
        if out[i] is None:
            lo=max([k for k in known if k<i], default=None)
            hi=min([k for k in known if k>i], default=None)
            if lo is not None and hi is not None:
                out[i]=out[lo]+(out[hi]-out[lo])*(i-lo)/(hi-lo)
            else:
                out[i]=out[lo if lo is not None else hi]
    return out

def smooth_fill(vec_by_tier, fallback):
    """vec_by_tier: {tier_index: median}. Return monotone 5-vector, gaps interpolated,
    ends extrapolated, missing → fallback."""
    out=[vec_by_tier.get(i) for i in range(5)]
    # linear interpolate interior gaps between known points
    known=[i for i in range(5) if out[i] is not None]
    if known:
        for i in range(5):
            if out[i] is None:
                lo=[k for k in known if k<i]; hi=[k for k in known if k>i]
                if lo and hi:
                    a,b=lo[-1],hi[0]; out[i]=out[a]+(out[b]-out[a])*(i-a)/(b-a)
                elif lo:  out[i]=out[lo[-1]]*(fallback[i]/fallback[lo[-1]])
                elif hi:  out[i]=out[hi[0]]*(fallback[i]/fallback[hi[0]])
    else:
        out=list(fallback)
    for i in range(5):
        if out[i] is None: out[i]=fallback[i]
    for i in range(1,5):                       # enforce non-decreasing
        out[i]=max(out[i], out[i-1])
    return out


def resolve_order(order_file, data_dir, mods_dir=None):
    """Read a loadorder.txt/plugins.txt and map each plugin NAME to a real file path.
    Looks in the data dir first, then <mods>/*/<name>. Returns paths in LOAD ORDER."""
    import glob as _glob
    names=[]
    for ln in open(order_file, encoding='utf-8', errors='replace').read().splitlines():
        ln=ln.strip()
        if not ln or ln.startswith('#'): continue
        names.append(ln[1:].strip() if ln.startswith('*') else ln)
    out=[]
    for n in names:
        cand=os.path.join(data_dir, n)
        if not os.path.exists(cand) and mods_dir:
            hits=_glob.glob(os.path.join(mods_dir, '*', n))
            cand=hits[0] if hits else None
        if cand and os.path.exists(cand): out.append(cand)
    return out

def effective_vanilla_spells(order_paths, van_low3):
    """{vanilla_low3: (parsed_spel, mgef_resolver)} for every vanilla SPEL that a later plugin
    OVERRIDES. Later plugins win, so this yields the list's ACTUAL baseline (e.g. Requiem's
    rescaled vanilla spells) rather than unmodded Skyrim.esm values."""
    winners={}
    for path in order_paths:
        try: buf=open(path,'rb').read()
        except OSError: continue
        try: masters=S.masters(buf)
        except Exception: continue
        idx=[i for i,m in enumerate(masters) if m.lower()==CURVE_MASTER.lower()]
        if not idx: continue                      # can't override vanilla without mastering it
        sky=idx[0]; own=S.read_mgef_map(buf)
        # F5: also let this plugin's overrides of MASTER-file MGEFs win, so an overhaul that
        # re-costs/re-tiers vanilla magic effects is visible to the baseline (Simonrim does).
        for fid,m in own.items():
            hi=fid>>24
            if hi<len(masters) and masters[hi] in van_low3:
                van_low3[masters[hi]][fid & 0xFFFFFF]=m
        res=build_resolver(masters, own, van_low3)
        for r in S.iter_top_records(buf,{b'SPEL'}):
            if (r.formid>>24)!=sky: continue      # only overrides of Skyrim.esm records
            winners[r.formid & 0xFFFFFF]=(S.parse_spel(r.data), res)
    return winners

def build_vanilla_model(van_low3, van_paths, order_paths=None):
    path=os.path.join(VANILLA_DATA, CURVE_MASTER)
    buf=open(path,'rb').read()
    own=S.read_mgef_map(buf)
    res=build_resolver([CURVE_MASTER], own, van_low3)
    per=collections.defaultdict(list)          # (arch,tier)->[DELIVERED damage]
    costs=collections.defaultdict(list)        # (tier,'conc'|'fnf')->[cost]
    durs=collections.defaultdict(list)         # (arch,tier)->[damage duration]
    eff_rows=[]                                # (arch, tier, D, cost) for the efficiency fit
    base={}
    for r in S.iter_top_records(buf,{b'SPEL'}):
        base[r.formid & 0xFFFFFF]=(S.parse_spel(r.data), res)
    n_over=0
    if order_paths:
        for fid,(sp2,res2) in effective_vanilla_spells(order_paths, van_low3).items():
            if fid in base: base[fid]=(sp2,res2); n_over+=1
        print(f"baseline: {len(base)} vanilla spells, {n_over} overridden by the load order")
    for sp,res_i in base.values():
        if sp['type']!=0 or sp['spit_off'] is None: continue
        effs=[(res_i(e['mgef']),e) for e in sp['effects']]
        p=primary_damage(effs)
        if not p: continue
        tier=classify_tier(effs); a=archetype(sp['castType'], p[1]['dur'])
        D=delivered(effs, sp['castType']==2)
        if D<=0: continue
        per[(a,tier)].append(D)
        if p[1]['dur']>0: durs[(a,tier)].append(p[1]['dur'])
        cls='conc' if sp['castType']==2 else 'fnf'
        if sp['cost']>0:
            costs[(tier,cls)].append(sp['cost'])
            eff_rows.append((a,tier,D,sp['cost']))
    MIN_N=3   # a (arch,tier) cell must have >=MIN_N vanilla samples to be trusted
    curve={}
    for a in ARCHS:
        cnt=[len(per[(a,t)]) for t in TIERS]
        vt={TI[t]: statistics.median(per[(a,t)]) for t in TIERS if len(per[(a,t)])>=MIN_N}
        if len(vt)>=2:
            curve[a]=smooth_fill(vt, FALLBACK[a])
            note=""
        else:
            # Fewer than 2 sampled tiers cannot define a slope, and smooth_fill would extrapolate
            # FLAT from the single point -- a curve that does not rise with tier, which is never
            # right and badly over-nerfs high-tier spells (seen on lists whose overhaul re-times
            # vanilla DoTs so they stop classifying as 'dot'). Keep the vanilla SHAPE and anchor
            # it to whatever level we did observe.
            fb=FALLBACK[a]
            if vt:
                i,v=next(iter(vt.items())); k=(v/fb[i]) if fb[i] else 1.0
                curve[a]=[round(x*k,2) for x in fb]
                note=f"  -> only tier {i} sampled; vanilla shape anchored x{k:.2f}"
            else:
                curve[a]=list(fb); note="  -> no samples; vanilla fallback shape"
        print(f"  samples {a}: {cnt}{note}")
    # soft cost ceiling per (tier, class): concentration cost is per-SECOND, so its ceiling is
    # far below a fire-and-forget burst's. Guards against inflated modded effect base-costs.
    ceil={}
    for t in TIERS:
        for cls in ('conc','fnf'):
            c=costs[(t,cls)]
            ceil[(t,cls)]=max(c)*COST_CEIL_HEADROOM if c else (60 if cls=='conc' else 99999)
    # ---- cost norm C(t,cls): dense (every spell has a cost) ----
    C={}
    for cls in ('conc','fnf'):
        vt={TI[t]: statistics.median(costs[(t,cls)]) for t in TIERS if len(costs[(t,cls)])>=MIN_N}
        C[cls]=smooth_fill(vt, [c if c else 1 for c in (FALLBACK['conc'] if cls=='conc' else FALLBACK['inst'])])
    # ---- efficiency E(a,t) = damage per magicka, POOLED across archetypes for the tier slope ----
    # conc gives dps / cost-per-second, inst/dot give total / cast cost: same units, so pooling is
    # dimensionally sound and lets a dense tier slope be estimated instead of per-cell scraps.
    by_t=collections.defaultdict(list)
    for a,t,D,c in eff_rows: by_t[t].append(D/c)
    et={TI[t]: statistics.median(by_t[t]) for t in TIERS if len(by_t[t])>=MIN_N}
    base_e=fill_gaps(et, [0.4]*5)
    k={}
    for a in ARCHS:
        rs=[(D/c)/base_e[TI[t]] for aa,t,D,c in eff_rows if aa==a and base_e[TI[t]]>0]
        k[a]=statistics.median(rs) if len(rs)>=MIN_N else 1.0
    E={a:[max(base_e[i]*k[a],1e-6) for i in range(5)] for a in ARCHS}
    # sparse damage cells: fill from the DENSE data (cost norm x efficiency) before any constant
    for a in ARCHS:
        cls='conc' if a=='conc' else 'fnf'
        for i in range(5):
            if len(per[(a,TIERS[i])])<MIN_N:
                curve[a][i]=round(C[cls][i]*E[a][i],2)
    for a in ARCHS:                      # sampled + derived cells must not disagree in direction
        for i in range(1,5):
            if curve[a][i] < curve[a][i-1]: curve[a][i]=curve[a][i-1]
    # duration norm per (arch,tier) for the DoT delivery-speed factor
    T={a:[ (statistics.median(durs[(a,TIERS[i])]) if len(durs[(a,TIERS[i])])>=MIN_N else 0) for i in range(5)]
       for a in ARCHS}
    for a in ARCHS:
        known=[x for x in T[a] if x>0]
        if known: T[a]=[x if x>0 else statistics.median(known) for x in T[a]]
    print(f"  efficiency (dmg per magicka) by tier: inst={[round(x,2) for x in E['inst']]}")
    print(f"                                        conc={[round(x,2) for x in E['conc']]}  dot={[round(x,2) for x in E['dot']]}")
    return curve, ceil, {'C':C, 'E':E, 'T':T}

def balance_plugin(src, curve, ceil, fit, van_low3, knobs):
    OVR, TC, TM, VAR = knobs
    buf=bytearray(open(src,'rb').read())
    masters=S.masters(buf); own=S.read_mgef_map(buf)
    res=build_resolver(masters, own, van_low3)
    n_mag=n_cost=0; dmg_ratios=[]
    for r in S.iter_top_records(buf,{b'SPEL'}):
        sp=S.parse_spel(r.data)
        if sp['type']!=0 or sp['spit_off'] is None: continue
        effs=[(res(e['mgef']),e) for e in sp['effects']]
        tier=classify_tier(effs)
        p=primary_damage(effs)
        # per-spell damage pin: primary → vanilla curve, log-blended toward author by VARIETY.
        # ratio applies to ALL the spell's damage effects, preserving its internal structure.
        D0=delivered(effs, sp['castType']==2)
        if p and D0>0:
            a=archetype(sp['castType'], p[1]['dur'])
            target=curve[a][TI[tier]]
            eff_target=(target**(1-VAR))*(D0**VAR)                   # log-blend, on DELIVERED damage
            ratio=eff_target/D0                                      # D is linear in the mags
            ratio*=(OVR/100)*(TM[tier]/100)
            dmg_ratios.append(ratio)
            for m,e in effs:
                if S.is_damage(m) and e['mag']>0:
                    nm=e['mag']*ratio
                    if abs(nm-e['mag'])>1e-4:
                        struct.pack_into('<f', buf, r.data_off+e['efit_off'], float(nm)); n_mag+=1
                    e['mag']=nm
        # concentration cost = vanilla per-second economy (ratio × dmg/sec), ignoring the
        # mod's (often inflated) effect base-cost; fire-and-forget uses the autocalc formula.
        if p and D0>0:
            # JOINT SOLVE: cost comes from the same (rank, cost, damage) relation as the damage pin,
            # i.e. the baseline's own damage-per-magicka at this rank -- NOT from the mod's basecosts.
            a=archetype(sp['castType'], p[1]['dur'])
            Dstar=eff_target*(OVR/100)*(TM[tier]/100)
            vc=Dstar/fit['E'][a][TI[tier]]
            if a=='dot' and DELIVERY>0 and p[1]['dur']>0:
                Tn=fit['T'][a][TI[tier]]
                if Tn>0:
                    # equal totals are NOT equal value: front-loading the same damage into a shorter
                    # duration is worth more, so it pays more. This is why total-damage pinning is
                    # only correct with cost as the third factor.
                    vc*=min(max((Tn/p[1]['dur'])**DELIVERY, 0.7), 1.4)
            vc*=(OVR/100)*(TC[tier]/100)
        else:
            vc=vanilla_cost(effs)*(OVR/100)*(TC[tier]/100)
        vc=min(vc, ceil[(tier, 'conc' if sp['castType']==2 else 'fnf')])
        newcost=max(int(round(vc)),0)
        if newcost!=sp['cost']:
            struct.pack_into('<I', buf, r.data_off+sp['spit_off'], newcost)
            struct.pack_into('<I', buf, r.data_off+sp['spit_off']+4, sp['flags']|S.SPIT_FLAG_MANUAL_COST)
            n_cost+=1
    med_ratio=statistics.median(dmg_ratios) if dmg_ratios else 1.0
    return bytes(buf), med_ratio, n_mag, n_cost, len(dmg_ratios)

def main():
    global VANILLA_DATA
    ap=argparse.ArgumentParser()
    ap.add_argument('plugins', nargs='*', help="plugin paths to balance in place (else use built-in PLUGINS)")
    ap.add_argument('--data', default=VANILLA_DATA)
    ap.add_argument('--deploy', action='store_true', help="write results (else dry-run to OUT_DIR)")
    ap.add_argument('--dry', action='store_true')
    ap.add_argument('--order', help="loadorder.txt/plugins.txt: derive the baseline from the WINNING "
                    "version of each vanilla spell in THIS list (use for Requiem/overhaul lists)")
    ap.add_argument('--mods', help="mods root, to resolve plugin names from --order")
    ap.add_argument('--overall', type=int, default=OVERALL)
    ap.add_argument('--variety', type=float, default=VARIETY, help="0=pure vanilla dmg, 1=author dmg")
    a=ap.parse_args()
    VANILLA_DATA=a.data
    van_low3={n: low3_map(os.path.join(VANILLA_DATA,n)) for n in VANILLA_MASTERS}
    van_low3={n:v for n,v in van_low3.items() if v}
    order_paths=None
    if a.order:
        order_paths=resolve_order(a.order, VANILLA_DATA, a.mods)
        print(f"load order: {len(order_paths)} plugins resolved from {os.path.basename(a.order)}")
    curve, ceil, fit = build_vanilla_model(van_low3, None, order_paths)
    print("VANILLA damage curve (magnitude by tier):")
    for ar in ARCHS: print(f"  {ar:4}: "+"  ".join(f"{t[:3]}={curve[ar][TI[t]]:.0f}" for t in TIERS))
    knobs=(a.overall, TIER_COST, TIER_MAG, a.variety)
    print(f"VARIETY={a.variety} (0=pure vanilla dmg, 1=author dmg)")
    # target list
    if a.plugins:
        targets=[(p, p, os.path.basename(p)) for p in a.plugins]   # in-place
    else:
        targets=PLUGINS
    os.makedirs(OUT_DIR, exist_ok=True)
    tot_m=tot_c=0
    print(f"\n{'pack':18}  spells  magΔ costΔ  median-dmg×")
    for src, deploy, name in targets:
        out, med_ratio, nm, nc, ndmg = balance_plugin(src, curve, ceil, fit, van_low3, knobs)
        open(os.path.join(OUT_DIR, os.path.basename(src)),'wb').write(out)
        if a.deploy and not a.dry:
            if os.path.abspath(src)==os.path.abspath(deploy) and not os.path.exists(deploy+'.bak'):
                shutil.copy2(deploy, deploy+'.bak')
            open(deploy,'wb').write(out)
        print(f"  {name:18} {ndmg:5}  {nm:5} {nc:4}   ×{med_ratio:.2f}")
        tot_m+=nm; tot_c+=nc
    mode = "DEPLOYED" if (a.deploy and not a.dry) else "dry-run (OUT_DIR only)"
    print(f"\nTOTAL magΔ={tot_m} costΔ={tot_c}  [{mode}]  OVERALL={a.overall} VARIETY={a.variety}")

if __name__ == '__main__':
    main()
