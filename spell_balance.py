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

# ============================== CONFIG ==============================
# Point --data at a folder containing your vanilla masters (Skyrim.esm etc.), e.g. your
# game's Data folder or a Mod Organizer "Stock Game/Data". Pass the spell plugins to
# balance as positional args (balanced IN PLACE with a .bak when --deploy is given).
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
COST_CEIL_HEADROOM = 1.5            # soft cost clamp = vanilla tier max cost * this
OUT_DIR   = os.environ.get("SPELLBAL_OUT", "./balanced_out")   # dry-run output dir

# Optional: hard-code a reusable list instead of passing paths every time. Each entry is
# (source_path, deploy_path, display_name); leave empty to always use CLI positional args.
#   PLUGINS = [ ("mods/Astral/Astral.esl", "mods/Astral/Astral.esl", "Astral Magic 2"), ... ]
PLUGINS = []
# ===================================================================

TIERS = ['Novice','Apprentice','Adept','Expert','Master']; TI = {t:i for i,t in enumerate(TIERS)}
ARCHS = ['inst','conc','dot']
# fallback curve (used only for tiers with no vanilla sample) — extrapolated from vanilla shape
FALLBACK = {'inst':[15,30,45,60,90], 'conc':[8,11,14,18,40], 'dot':[13,20,30,45,70]}

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

def primary_damage(effs):
    dmg=[(m,e) for m,e in effs if S.is_damage(m) and e['mag']>0]
    return max(dmg, key=lambda me: me[0]['basecost']) if dmg else None

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

def build_vanilla_model(van_low3, van_paths):
    path=os.path.join(VANILLA_DATA, CURVE_MASTER)
    buf=open(path,'rb').read()
    own=S.read_mgef_map(buf)
    res=build_resolver([CURVE_MASTER], own, van_low3)
    per=collections.defaultdict(list)          # (arch,tier)->[mag]
    costs=collections.defaultdict(list)        # (tier,'conc'|'fnf')->[cost]
    conc_ratios=[]                             # vanilla concentration cost / (dmg per sec)
    for r in S.iter_top_records(buf,{b'SPEL'}):
        sp=S.parse_spel(r.data)
        if sp['type']!=0 or sp['spit_off'] is None: continue
        effs=[(res(e['mgef']),e) for e in sp['effects']]
        p=primary_damage(effs)
        if not p: continue
        tier=classify_tier(effs); a=archetype(sp['castType'], p[1]['dur'])
        per[(a,tier)].append(p[1]['mag'])
        cls='conc' if sp['castType']==2 else 'fnf'
        if sp['cost']>0: costs[(tier,cls)].append(sp['cost'])
        if sp['castType']==2 and sp['cost']>0 and p[1]['mag']>0:
            rt=sp['cost']/p[1]['mag']
            if 1.0<=rt<=3.0: conc_ratios.append(rt)   # player-spell band (excludes traps/walls)
    MIN_N=3   # a (arch,tier) cell must have >=MIN_N vanilla samples to be trusted
    curve={}
    for a in ARCHS:
        vt={TI[t]: statistics.median(per[(a,t)]) for t in TIERS if len(per[(a,t)])>=MIN_N}
        curve[a]=smooth_fill(vt, FALLBACK[a])
    # soft cost ceiling per (tier, class): concentration cost is per-SECOND, so its ceiling is
    # far below a fire-and-forget burst's. Guards against inflated modded effect base-costs.
    ceil={}
    for t in TIERS:
        for cls in ('conc','fnf'):
            c=costs[(t,cls)]
            ceil[(t,cls)]=max(c)*COST_CEIL_HEADROOM if c else (60 if cls=='conc' else 99999)
    conc_ratio=statistics.median(conc_ratios) if conc_ratios else 2.0
    return curve, ceil, conc_ratio

def balance_plugin(src, curve, ceil, conc_ratio, van_low3, knobs):
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
        if p:
            a=archetype(sp['castType'], p[1]['dur'])
            target=curve[a][TI[tier]]
            eff_target=(target**(1-VAR))*(p[1]['mag']**VAR)          # log-blend
            ratio=eff_target/p[1]['mag']
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
        if sp['castType']==2 and p:
            vc=conc_ratio*p[1]['mag']*(OVR/100)*(TC[tier]/100)
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
    ap.add_argument('--overall', type=int, default=OVERALL)
    ap.add_argument('--variety', type=float, default=VARIETY, help="0=pure vanilla dmg, 1=author dmg")
    a=ap.parse_args()
    VANILLA_DATA=a.data
    van_low3={n: low3_map(os.path.join(VANILLA_DATA,n)) for n in VANILLA_MASTERS}
    van_low3={n:v for n,v in van_low3.items() if v}
    curve, ceil, conc_ratio = build_vanilla_model(van_low3, None)
    print("VANILLA damage curve (magnitude by tier):")
    for ar in ARCHS: print(f"  {ar:4}: "+"  ".join(f"{t[:3]}={curve[ar][TI[t]]:.0f}" for t in TIERS))
    print(f"concentration cost/dmg ratio: {conc_ratio:.2f}   fnf ceiling:",
          {t:round(ceil[(t,'fnf')]) for t in TIERS})
    knobs=(a.overall, TIER_COST, TIER_MAG, a.variety)
    print(f"VARIETY={a.variety} (0=pure vanilla dmg, 1=author dmg)")
    # target list
    if a.plugins:
        targets=[(p, p, os.path.basename(p)) for p in a.plugins]   # in-place
    else:
        targets=PLUGINS
    if not targets:
        sys.exit("no plugins given — pass plugin paths as arguments (or set PLUGINS in the config).\n"
                 "  e.g.  python3 spell_balance.py --data /path/to/Data MySpells.esp --deploy")
    os.makedirs(OUT_DIR, exist_ok=True)
    tot_m=tot_c=0
    print(f"\n{'pack':18}  spells  magΔ costΔ  median-dmg×")
    for src, deploy, name in targets:
        out, med_ratio, nm, nc, ndmg = balance_plugin(src, curve, ceil, conc_ratio, van_low3, knobs)
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
