#!/usr/bin/env python3
"""Skyrim SE spell balancer — reprice ADDED spell packs to the VANILLA standard, with
the balance DERIVED from vanilla data (no xEdit / no CK). Knobs are 100 = vanilla.

WHY THIS EXISTS
  Every spell pack ships on its own power/cost scale. Vanilla is one consistent scale,
  observed across hundreds of spells: a spell's magicka cost, damage, tier (min-skill),
  duration and effect base-cost are all tied by the engine. That over-determined system
  is solvable — so this tool FITS the vanilla relationships and moves each pack onto them,
  instead of trusting the author's (inconsistent) numbers or leaving damage untouched.

THE ONE IDEA: equal magicka buys equal damage, within a tier
  A spell is bought with magicka and paid out in damage. At a given rank the exchange rate
  is a single number, E(tier), and HOW the damage arrives does not change what it cost to
  buy. Every spell is scored on that one axis, so burst, damage-over-time and concentration
  are directly comparable and there is no separate curve per delivery style to populate:

    concentration    score = magnitude, i.e. damage per SECOND. Its cost drains per second
                     too, so rate against rate is already the exchange rate. Duration is the
                     PLAYER's choice (how long they hold it), never the spell's, so it must
                     not appear anywhere in a concentration spell's valuation.
    fire-and-forget  score = magnitude x dur_eff(duration). Paid once at cast, so one payout.
                     A burst scores its magnitude. A DoT scores more, because it puts nothing
                     on the target at t=0 and makes the player wait out a timer the SPELL
                     fixed — the extra total damage is compensation for that delay, set by
                     DOT_EXP (1.0 = none, 0.70 = a 10s DoT gets 2x a burst's total).
    field spells     walls, hazards, cloaks, self-auras: the recorded duration is how long
                     the FIELD lasts, not how long anything is being damaged, so credited
                     exposure is capped at FIELD_EXPOSE seconds.

  This is what makes DoT balance possible at all. Vanilla contains ZERO player DoT damage
  spells, so a per-(tier, DoT) curve can never be measured — three of its five cells would be
  invented, and DoT-heavy packs then swing on that extrapolation. Here nothing about a DoT is
  sourced from other DoTs: it is priced off the same dense E(tier) as every burst, and the
  only DoT-specific quantity is one global constant shared by all five tiers.

THE SOLVE (per spell)
  1. Baseline curves, from Skyrim.esm or from the WINNING version of each vanilla spell in a
     load order (--order, for Requiem/Simonrim lists whose overhaul rewrites vanilla magic):
       P(class, tier) = median score          E(tier) = median score per magicka
  2. Damage: pin the spell's score to P for its own (class, tier), log-blended back toward the
     author's value by VARIETY. Score is LINEAR in every magnitude, so the target ratio is read
     straight off and applied to all the spell's damage effects, preserving its structure.
  3. Cost: solved from the SAME relation, cost = score / E(tier) — not from the mod's own
     effect base-costs, which are frequently inflated. A soft per-(tier, class) ceiling from
     the baseline guards the rare tail.

  Damage counts only HOSTILE Health-modifying effects with a nonzero base cost (see
  spell_lib.is_damage) — that excludes utility magnitudes and the zero-cost PerkDisintegrate
  riders, which otherwise read as 200 damage and flip every vanilla shock spell into a DoT.

KNOBS (edit CONFIG or pass on CLI; 100 = vanilla default, scale from there)
  OVERALL, TIER_COST[tier], TIER_MAG[tier], VARIETY, DOT_EXP (--dot-exp).
  effective cost mult = OVERALL/100 * TIER_COST[tier]/100
  effective mag  mult = OVERALL/100 * TIER_MAG[tier]/100

USAGE
  python3 spell_balance.py --data DIR a.esp b.esl  # report only, results in OUT_DIR
  python3 spell_balance.py --data DIR a.esp --deploy
  python3 spell_balance.py --data DIR --mods M --order loadorder.txt --dry a.esp
Edits are FIXED-WIDTH in-place (SPIT cost u32 + flags, EFIT magnitude f32), so file size
never changes and no size fixups are needed. zlib-compressed SPEL records cannot be edited
that way and are skipped and reported instead.
"""
import sys, os, struct, math, statistics, collections, shutil, argparse
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import spell_lib as S

__version__ = "1.0.0"

# ============================== CONFIG (edit me) ==============================
VANILLA_DATA = os.environ.get("SKYRIM_DATA", "./Data")
VANILLA_MASTERS = ["Skyrim.esm", "Update.esm", "Dawnguard.esm", "HearthFires.esm", "Dragonborn.esm"]
CURVE_MASTER = "Skyrim.esm"        # which master to fit the vanilla curve from

OVERALL   = 100
TIER_COST = {'Novice':100,'Apprentice':100,'Adept':100,'Expert':100,'Master':100}
TIER_MAG  = {'Novice':100,'Apprentice':100,'Adept':100,'Expert':100,'Master':100}
# Each spell's damage is pinned to the baseline curve for its own (tier, class); VARIETY
# log-blends back toward the author's value: 0.0 = pure vanilla (every same-tier spell equal),
# 1.0 = author's damage untouched. Default keeps mostly-vanilla with a little author character.
VARIETY   = 0.35
COST_CEIL_HEADROOM = 1.5            # soft cost clamp = vanilla tier max cost * this

COST_EXP  = 1.1                     # autocalc magnitude exponent, fit to Skyrim.esm

# --- THE EXCHANGE RATE: equal magicka buys equal damage, within a tier --------------------
# The single assumption that makes every delivery style commensurable, and what removes the need
# for DoT samples in any tier. A spell is bought with magicka and paid out in damage; at a given
# rank the exchange rate is one number, and HOW the damage arrives does not change what it cost:
#
#   concentration   drains cost-per-second while held and deals damage-per-second, so its rate
#                   IS its exchange rate. Duration is the PLAYER's choice, never the spell's, so
#                   it must not appear anywhere in a concentration spell's valuation.
#   fire-and-forget is paid ONCE at cast, whether it lands as a burst or ticks for 30 seconds.
#                   One payment, so one payout.
#
# Both reduce to a single damage-per-magicka per tier, E(tier), fit from the baseline -- which is
# dense, because every spell has a cost and a damage payout regardless of how it delivers. A DoT
# is priced off that same curve, so vanilla having zero player DoT spells no longer matters:
# nothing about a DoT is sourced from other DoTs.
#
# DOT_EXP is the one dial on it. A fire-and-forget DoT puts NOTHING on the target at t=0 and makes
# the player wait out a timer the SPELL fixed for them, so it is paid more total damage than an
# equal-cost burst as compensation:  total = burst_target * dur^(1-DOT_EXP)
#   1.00 -> no compensation: equal magicka buys equal TOTAL damage, however slowly it lands
#   0.70 -> a 10s DoT gets 2.0x a burst's total, a 30s DoT 2.7x
#   0.50 -> a 10s DoT gets 3.2x, a 30s DoT 5.5x
# Concentration and burst spells are unaffected by it.
#
# This is the ONE number in the model asserted rather than measured, because no baseline can
# identify it. Vanilla has zero player DoT damage spells; Requiem lists have none either; and the
# DoT spells a Simonrim list DOES put in the baseline are Turn Undead (a niche anti-undead utility
# reworked into a DoT) and creature poison spit, neither of which prices a damage spell. Each run
# prints what the baseline's own DoTs imply, as a DIAGNOSTIC -- adopt it with --dot-exp only after
# reading the samples, and do not wire it back in as the driver: a per-list fit would make a
# universal tool inherit whatever junk each baseline happens to contain.
#
# 0.70 is where Mysticism's own poison line sits, i.e. it makes Mysticism reproduce itself at
# x1.00 under the self-consistency test. Poison is the most situational damage type in the game
# (resisted; immune on undead and automatons), so treat 0.70 as the GENEROUS end -- a fire DoT
# arguably deserves less compensation, which means a higher number.
DOT_EXP_DEFAULT = 0.70
FIELD_EXPOSE    = 4.0    # seconds a target is realistically inside a wall/hazard/aura
MIN_N           = 3      # baseline samples a (class,tier) cell needs before it is trusted
MIN_DOT_N       = 6      # baseline DoT samples needed before the DIAGNOSTIC rate is shown
DOT_EXP_OVERRIDE = None  # set by --dot-exp

OUT_DIR   = os.environ.get("SPELLBAL_OUT", "./balanced_out")   # dry-run output dir
# Built-in list (Terminal Destiny). Each: (pristine source, deploy path, display name).
PLUGINS = []   # optional built-in list; normally pass plugins on the CLI
# =============================================================================

TIERS = ['Novice','Apprentice','Adept','Expert','Master']; TI = {t:i for i,t in enumerate(TIERS)}
# Only TWO delivery classes. 'dot' is NOT one of them: a damage-over-time spell is a
# fire-and-forget spell whose value is discounted by dur_eff(), so it shares the fnf curve
# instead of needing its own (and vanilla has no player DoT samples to build one from).
CLASSES = ['fnf','conc']
# fallback curve (used only for tiers with no vanilla sample) — extrapolated from vanilla shape
FALLBACK = {'fnf':[15,30,45,60,90], 'conc':[8,11,14,18,40]}

def spell_class(castType):
    return 'conc' if castType==2 else 'fnf'

# Deliveries where the effect creates a FIELD (a wall, a hazard, a cloak, a self-aura) rather
# than landing on one target. Their recorded duration is how long the FIELD persists, not how
# long anything is being damaged -- a Wall of Fire burns for 10s but a target walks through it
# for a couple of seconds. Crediting the full duration values them as if an enemy stood in the
# fire for the whole burn, so their exposure is capped instead.
FIELD_DELIVERY = {0, 1, 4}          # 0=Self 1=Contact 4=TargetLocation (2=Aimed 3=TargetActor)

def dur_eff(dur, delivery=None, exp=None):
    """Payout-seconds of a fire-and-forget effect: how many seconds of its magnitude one cast
    actually buys.

    exp=1 makes this the duration itself, i.e. the effect pays out its TOTAL damage -- equal
    magicka buys equal damage, however it arrives. Below 1 the DoT is paid MORE total damage,
    compensating it for putting nothing on the target at t=0 and making the player wait out a
    timer they did not choose (unlike concentration, where the duration is the player's).
    """
    e = DOT_EXP_DEFAULT if exp is None else exp
    d = max(dur, 1.0)
    if delivery in FIELD_DELIVERY: d = min(d, FIELD_EXPOSE)
    return d ** e

AUTOCALC_DUR_DIV = 10.0   # Bethesda's own divisor; NOT our exchange rate -- see DOT_EXP
def vanilla_cost(effs):
    """Bethesda's autocalc formula, fit to Skyrim.esm (median error 0%). Only used for spells
    with no damage at all, where there is no damage payout to price against."""
    tot = 0.0
    for m, e in effs:
        if not m: continue
        bc=m['basecost']; mag=max(e['mag'],1.0); dur=e['dur']
        tot += bc*(mag**COST_EXP)*(max(dur/AUTOCALC_DUR_DIV,1.0) if dur>0 else 1.0)
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

def score(effs, castType, delivery=None, exp=None):
    """The ONE number a spell is balanced on, comparable across every delivery style.

    concentration  : magnitude, i.e. damage per SECOND -- its cost is drained per second too, so
                     rate against rate is already the exchange rate. Duration is the player's
                     choice, not the spell's, so it must never enter a concentration valuation.
    fire-and-forget: magnitude x dur_eff(duration) -- paid once at cast, so one payout. A burst
                     scores its magnitude; a DoT scores more, because its fixed timer is a
                     handicap the spell did not choose and the extra damage is the compensation.

    Summed over ALL damage effects, so riders and hybrid burst+DoT spells score correctly, and
    LINEAR in every magnitude -- which is what makes the per-spell solve exact: scaling every
    magnitude by r scales the score by r, so the target ratio can be read straight off.
    """
    if castType == 2:
        return sum(e['mag'] for m,e in effs if S.is_damage(m) and e['mag']>0)
    return sum(e['mag']*dur_eff(e['dur'], delivery, exp)
               for m,e in effs if S.is_damage(m) and e['mag']>0)

def is_per_target_dot(sp, effs):
    """A fire-and-forget spell that ticks on ONE target for a fixed, spell-defined timer -- the
    only case where the recorded duration really is damage-delivery time."""
    if sp['castType']==2 or sp['delivery'] in FIELD_DELIVERY: return False
    d=[e['dur'] for m,e in effs if S.is_damage(m) and e['mag']>0]
    return bool(d) and max(d)>1

def has_damage(effs):
    return any(S.is_damage(m) and e['mag']>0 for m,e in effs)

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
        own=S.read_mgef_map(buf)
        # F5: let this plugin's MGEF overrides win in any map already held, so an overhaul that
        # re-costs/re-tiers vanilla magic effects is visible to the baseline (Simonrim does).
        # And publish its OWN new MGEFs under the plugin's name, so a later plugin's (or a
        # target pack's) EFID reference into it can be followed: without this, a winning
        # vanilla-spell override whose damage effects live in the overhaul's own plugin
        # (Requiem's Lightning Storm) silently dropped out of the baseline.
        for fid,m in own.items():
            hi=fid>>24
            if hi<len(masters):
                if masters[hi] in van_low3: van_low3[masters[hi]][fid & 0xFFFFFF]=m
            else:
                van_low3.setdefault(os.path.basename(path),{})[fid & 0xFFFFFF]=m
        idx=[i for i,m in enumerate(masters) if m.lower()==CURVE_MASTER.lower()]
        if not idx: continue                      # can't override vanilla without mastering it
        sky=idx[0]
        res=build_resolver(masters, own, van_low3)
        for r in S.iter_top_records(buf,{b'SPEL'}):
            if (r.formid>>24)!=sky: continue      # only overrides of Skyrim.esm records
            winners[r.formid & 0xFFFFFF]=(S.parse_spel(r.data), res)
    return winners

def fit_dot_exp(base):
    """Fit the ONE delay-compensation rate from the baseline itself, pooled over every per-target
    DoT at every tier.

    This is what makes DoT balance possible at all. Vanilla has no player DoT damage spells, so a
    per-(tier, DoT) curve can never be measured -- three of its five cells would be invented. But
    the rate is a SINGLE number shared by every tier, so every DoT in the corpus votes on it at
    once, and no cell can be defined by one sample. Solve for the exp at which the baseline's own
    DoTs sit exactly on its burst damage-per-magicka curve:

        median over DoTs of  log( score(exp)/cost ) - log( E_burst(tier) )  ==  0

    Returns (exp, n_samples, identified?). REPORTED ONLY, never used to drive the model: on every
    baseline tested the DoTs that reach here are Turn Undead and creature poison spit, which price
    a niche utility and a creature attack rather than a damage spell. Adopt it with --dot-exp only
    after reading the sample list.
    """
    burst=collections.defaultdict(list); dots=[]
    for sp,res_i in base.values():
        if sp['type']!=0 or sp['spit_off'] is None or sp['cost']<=0: continue
        effs=[(res_i(e['mgef']),e) for e in sp['effects']]
        if not has_damage(effs): continue
        t=TI[classify_tier(effs)]
        if is_per_target_dot(sp, effs):
            dots.append((t,[(e['mag'],e['dur']) for m,e in effs if S.is_damage(m) and e['mag']>0],sp['cost']))
        elif sp['castType']!=2:
            b=score(effs, sp['castType'], sp['delivery'], 1.0)
            if b>0: burst[t].append(b/sp['cost'])
    Eb={t: statistics.median(v) for t,v in burst.items() if len(v)>=MIN_N}
    use=[d for d in dots if d[0] in Eb]
    if len(use) < MIN_DOT_N:
        return None, len(use), False
    def resid(e):
        return statistics.median([math.log(sum(m*max(u,1.0)**e for m,u in d)/c) - math.log(Eb[t])
                                  for t,d,c in use])
    lo,hi=0.0,2.0
    if resid(lo)*resid(hi) > 0:                 # no crossing -> baseline cannot identify a rate
        return None, len(use), False
    for _ in range(60):
        mid=(lo+hi)/2
        if resid(lo)*resid(mid) <= 0: hi=mid
        else: lo=mid
    return round((lo+hi)/2, 3), len(use), True

def build_vanilla_model(van_low3, van_paths, order_paths=None):
    path=os.path.join(VANILLA_DATA, CURVE_MASTER)
    buf=open(path,'rb').read()
    own=S.read_mgef_map(buf)
    res=build_resolver([CURVE_MASTER], own, van_low3)
    per=collections.defaultdict(list)          # (cls,tier)->[score]
    conc_ids=collections.defaultdict(list)     # tier->[edid], to name thin conc anchors
    costs=collections.defaultdict(list)        # (tier,'conc'|'fnf')->[cost]
    eff_rows=[]                                # (cls, tier, score, cost) for the efficiency fit
    dotmix=collections.defaultdict(lambda:[0,0])  # tier->[n_burst, n_dot], diagnostics only
    base={}
    for r in S.iter_top_records(buf,{b'SPEL'}):
        base[r.formid & 0xFFFFFF]=(S.parse_spel(r.data), res)
    n_over=0
    if order_paths:
        for fid,(sp2,res2) in effective_vanilla_spells(order_paths, van_low3).items():
            if fid in base: base[fid]=(sp2,res2); n_over+=1
        print(f"baseline: {len(base)} vanilla spells, {n_over} overridden by the load order")
    dot_exp = DOT_EXP_OVERRIDE if DOT_EXP_OVERRIDE is not None else DOT_EXP_DEFAULT
    src = "--dot-exp" if DOT_EXP_OVERRIDE is not None else "default"
    implied, n_dot, ok = fit_dot_exp(base)
    hint = (f"baseline's own {n_dot} per-target DoTs imply {implied}" if ok
            else f"baseline has only {n_dot} per-target DoTs, cannot imply one")
    print(f"  DoT delay-compensation rate: {dot_exp} ({src}); {hint}")
    for sp,res_i in base.values():
        if sp['type']!=0 or sp['spit_off'] is None: continue
        effs=[(res_i(e['mgef']),e) for e in sp['effects']]
        if not has_damage(effs): continue
        tier=classify_tier(effs); cls=spell_class(sp['castType'])
        V=score(effs, sp['castType'], sp['delivery'], dot_exp)
        if V<=0: continue
        # A cost-0 record is not a spell anyone casts and pays for -- it is the damage component
        # of a cloak, a proc or a scripted ability, priced (if at all) by its parent. It cannot
        # contribute an efficiency, and it must not define the damage curve either: vanilla has 13
        # of them and packs are up to 78% them, all sitting at whatever magnitude their parent
        # wanted. Only priced spells describe the baseline's economy.
        if sp['cost']<=0: continue
        per[(cls,tier)].append(V)
        if cls=='conc': conc_ids[tier].append(sp['edid'])
        if cls=='fnf':      # diagnostics: how much of the fnf pool is over-time
            dotmix[tier][1 if max(e['dur'] for m,e in effs if S.is_damage(m) and e['mag']>0)>0 else 0]+=1
        costs[(tier,cls)].append(sp['cost'])
        eff_rows.append((cls,tier,V,sp['cost']))
    curve={}
    for a in CLASSES:
        cnt=[len(per[(a,t)]) for t in TIERS]
        vt={TI[t]: statistics.median(per[(a,t)]) for t in TIERS if len(per[(a,t)])>=MIN_N}
        if a=='conc':
            # Placeholder only. The conc curve is REPLACED below by k x the fnf curve, so its
            # sparse tiers never have to be guessed at -- report the counts and move on.
            curve[a]=smooth_fill(vt, FALLBACK[a]) if vt else list(FALLBACK[a])
            print(f"  samples {a}: {cnt}  (curve derived from fnf, see the ratio below)")
            continue
        if len(vt)>=2:
            curve[a]=smooth_fill(vt, FALLBACK[a])
            note=""
        else:
            # Fewer than 2 sampled tiers cannot define a slope, and smooth_fill would extrapolate
            # FLAT from the single point -- a curve that does not rise with tier, which is never
            # right and badly over-nerfs high-tier spells. Keep the vanilla SHAPE and anchor it
            # to whatever level we did observe.
            fb=FALLBACK[a]
            if vt:
                i,v=next(iter(vt.items())); k=(v/fb[i]) if fb[i] else 1.0
                curve[a]=[round(x*k,2) for x in fb]
                note=f"  -> only tier {i} sampled; vanilla shape anchored x{k:.2f}"
            else:
                curve[a]=list(fb); note="  -> no samples; vanilla fallback shape"
        print(f"  samples {a}: {cnt}{note}")
    print(f"  (fnf pool is burst+DoT on one curve: burst={[dotmix[t][0] for t in TIERS]} "
          f"dot={[dotmix[t][1] for t in TIERS]}; DoTs are priced by dur_eff, not by these counts)")
    # soft cost ceiling per (tier, class): concentration cost is per-SECOND, so its ceiling is
    # far below a fire-and-forget burst's. Guards against inflated modded effect base-costs.
    ceil={}
    for t in TIERS:
        for cls in CLASSES:
            c=costs[(t,cls)]
            ceil[(t,cls)]=max(c)*COST_CEIL_HEADROOM if c else (60 if cls=='conc' else 99999)
    # ---- cost norm C(t,cls): dense (every spell has a cost) ----
    C={}
    for cls in CLASSES:
        vt={TI[t]: statistics.median(costs[(t,cls)]) for t in TIERS if len(costs[(t,cls)])>=MIN_N}
        C[cls]=smooth_fill(vt, [c if c else 1 for c in FALLBACK[cls]])
    # ---- efficiency E(cls,t) = score per magicka, POOLED across classes for the tier slope ----
    # conc gives dps / cost-per-second, fnf gives instant-equivalent damage / cast cost: same
    # units, so pooling is dimensionally sound and lets a dense tier slope be estimated instead
    # of per-cell scraps.
    by_t=collections.defaultdict(list)
    for a,t,V,c in eff_rows: by_t[t].append(V/c)
    et={TI[t]: statistics.median(by_t[t]) for t in TIERS if len(by_t[t])>=MIN_N}
    base_e=fill_gaps(et, [0.4]*5)
    # ONE exchange rate per tier, shared by both classes: same tier + same magicka spent =>
    # same damage delivered, however it is delivered. Not split per class -- a per-class factor
    # would be fit on the handful of concentration samples a baseline has (vanilla: 1 at Expert)
    # and would then quietly say that sustained damage is worth less than burst damage at that
    # rank, which is a balance claim the data is far too thin to make.
    E={a:[max(base_e[i],1e-6) for i in range(5)] for a in CLASSES}
    # ---- concentration rides the fnf tier SHAPE, times a measured ratio LINE ----------------
    # Concentration is as data-starved as DoT was, and for the same structural reason: vanilla has
    # exactly three player concentration damage spells (Flames/Frostbite/Sparks, all Novice), so
    # Apprentice and Adept have ZERO samples on every baseline tested. Left per-cell the curve
    # comes out nearly FLAT (8/9/11/11/11), which crushes any high-tier conc spell a pack adds.
    #
    # It does not need per-tier samples. Both classes are bought with the same magicka at the same
    # rank, so the whole conc curve is the (dense) fnf curve times a conc/fnf ratio -- but that
    # ratio is NOT one constant: it RISES with tier, and vanilla says so itself (Flames deals 8/s
    # at Novice, ~0.38 of a Novice burst; Lightning Storm deals 75/s at Master vs Firestorm's 100
    # burst). Bethesda's own MGEF pricing agrees structurally: the player fnf damage effects'
    # base cost climbs with tier (FF 25 -> FFAimed75 3.55 -> FFSelfArea100 6.0, which is exactly
    # why fnf damage-per-magicka falls) while the conc effects' stays flat (FireDamageConcAimed
    # 1.5 at Novice, ShockDamageMassConcAimed 1.2 at Master) -- so conc keeps its exchange rate
    # while fnf pays a rising premium, and the damage ratio between them must widen.
    #
    # Measured as a geometric line through the only two honest anchors any baseline has:
    #     k(t) = k_lo * g^(t - lo)   with  g = (k_hi / k_lo)^(1/(hi-lo)),  clamped to [1, 1.5]
    #   lo = lowest tier with >= MIN_N conc samples (the Novice mass, ~50 spells: intercept)
    #   hi = highest tier with any conc sample     (the list's winning Lightning Storm: slope)
    # The INTERIOR sampled cells are deliberately not fit: on every baseline examined they are
    # the Wall spells and quest walls (Potema), whose SPEL-visible magnitude is only the spray
    # tip -- the real damage is the hazard the wall leaves, living in HAZD records the SPEL never
    # references. The engine's own autocalc betrays this: the Barrier effects' base cost is
    # 12-14.8 vs the 1.2-2.3 every honest conc damage effect carries, i.e. Bethesda priced ~8x
    # more damage into them than their magnitude shows. Fitting them would flip the slope
    # negative (walls sit at k=0.13) and re-crush high-tier conc.
    # The hi cell is usually n=1, and that is accepted: it is Lightning Storm, the only
    # high-tier player conc spell Bethesda ever shipped, re-valued by whatever overhaul wins it
    # -- the one real datum for "what the design pays sustained damage at rank". Validated: on a
    # Mysticism list the fit predicts Adept k=0.49 vs 0.50 in Mysticism's own conc line
    # (8/20/40 dps at Nov/Ade/Mas). Three guards bound the damage a junk anchor can do: g<1
    # falls back to the constant (a FALLING ratio is exactly the failure this fit exists to
    # prevent); g>1.5 is capped just above the steepest legitimate rise observed (unmodded
    # vanilla, x1.22/tier); and an anchor whose ratio exceeds K_ANCHOR_MAX is rejected outright
    # (see below). Thin anchors are NAMED in the printout so a junk one can be seen.
    # NOT inverted from MGEF base costs directly: cost ~ bc*mag^1.1 makes any closed-form for
    # magnitude a ~10th power of a bc ratio -- analytically hopeless, so bc corroborates the
    # direction only and the level comes from spells.
    K_STEP_MAX   = 1.5   # per-tier rise cap: just above the steepest legitimate rise observed (vanilla 1.22)
    # A conc/fnf ratio above 1 would claim sustained dps outbuys an equal-tier burst's WHOLE
    # payout every second -- no design does that (vanilla's own maximum is Lightning Storm at
    # 0.83), so an anchor above it can only be a junk sample (a creature or quest spell the
    # overhaul re-tiered) and the slope is not trusted. k(t) is also capped here absolutely, so
    # extrapolation past a sub-Master anchor cannot exceed it either. Costs no real anchor.
    K_ANCHOR_MAX = 1.0
    kc=[V/curve['fnf'][TI[t]] for t in TIERS for V in per[('conc',t)] if curve['fnf'][TI[t]]>0]
    kmed={i: statistics.median([V/curve['fnf'][i] for V in per[('conc',TIERS[i])]])
          for i in range(5) if per[('conc',TIERS[i])] and curve['fnf'][i]>0}
    def _anames(i, cap=2):
        n=conc_ids.get(TIERS[i], [])
        return " ["+", ".join(x or '?' for x in n)+"]" if 0<len(n)<=cap else ""
    kt=None
    if len(kc)>=MIN_N:
        lo=min((i for i in kmed if len(per[('conc',TIERS[i])])>=MIN_N), default=None)
        hi=max(kmed) if kmed else None
        # Fallback constant: the LOW-tier mass median, not the whole pool. The interior cells
        # (walls) understate their real damage -- the same contamination the fit above excludes
        # -- and on a wall-heavy baseline they drag a pooled median well below the honest level.
        k_conc=kmed[lo] if lo is not None else statistics.median(kc)
        if lo is not None and hi is not None and hi>lo and kmed[hi]<=K_ANCHOR_MAX:
            g=(kmed[hi]/kmed[lo])**(1.0/(hi-lo))
            if g>1.0:
                g=min(g, K_STEP_MAX)
                kt=[min(kmed[lo]*g**(i-lo), K_ANCHOR_MAX) for i in range(5)]
                print(f"  conc/fnf ratio: rises {kmed[lo]:.3f} ({TIERS[lo][:3]}, "
                      f"n={len(per[('conc',TIERS[lo])])}) -> {kt[hi]:.3f} "
                      f"({TIERS[hi][:3]}, n={len(per[('conc',TIERS[hi])])}{_anames(hi)}), "
                      f"x{g:.3f}/tier; k(t)={[round(x,3) for x in kt]}")
            else:
                print(f"  conc/fnf ratio: {k_conc:.3f} constant ({len(kc)} conc spells; top-tier "
                      f"anchor {TIERS[hi][:3]}={kmed[hi]:.3f}{_anames(hi)} does not rise above "
                      f"{TIERS[lo][:3]}={kmed[lo]:.3f}, slope not applied)")
        elif lo is not None and hi is not None and hi>lo:
            print(f"  conc/fnf ratio: {k_conc:.3f} constant (top-tier anchor {TIERS[hi][:3]}="
                  f"{kmed[hi]:.3f}{_anames(hi)} exceeds {K_ANCHOR_MAX} -- not a plausible player "
                  f"conc sample, slope not applied)")
        else:
            print(f"  conc/fnf ratio: {k_conc:.3f} constant, measured from {len(kc)} conc spells "
                  f"(no second sampled tier to slope from)")
    else:
        k_conc=statistics.median([FALLBACK['conc'][i]/FALLBACK['fnf'][i] for i in range(5)])
        print(f"  conc/fnf ratio: {k_conc:.3f} (default; baseline has {len(kc)} concentration spells)")
    curve['conc']=[round(curve['fnf'][i]*(kt[i] if kt else k_conc),2) for i in range(5)]
    # any remaining sparse fnf cell: fill from the DENSE data (cost norm x efficiency).
    # ORDER MATTERS: this must stay AFTER the conc derivation above. kmed's denominators and the
    # k x fnf multiplication use the same pre-fill fnf curve, which is what makes the hi anchor
    # self-reproducing (kt[hi]*fnf[hi] == the observed conc median, e.g. vanilla Master conc = 75
    # = Lightning Storm itself). Filling fnf first would silently detune the anchor.
    for i in range(5):
        if len(per[('fnf',TIERS[i])])<MIN_N:
            curve['fnf'][i]=round(C['fnf'][i]*E['fnf'][i],2)
    for a in CLASSES:                    # sampled + derived cells must not disagree in direction
        for i in range(1,5):
            if curve[a][i] < curve[a][i-1]: curve[a][i]=curve[a][i-1]
    print(f"  damage per magicka by tier (shared by every delivery style): "
          f"{[round(x,3) for x in E['fnf']]}")
    return curve, ceil, {'C':C, 'E':E, 'dot_exp':dot_exp}

def balance_plugin(src, curve, ceil, fit, van_low3, knobs):
    OVR, TC, TM, VAR = knobs
    buf=bytearray(open(src,'rb').read())
    masters=S.masters(buf); own=S.read_mgef_map(buf)
    res=build_resolver(masters, own, van_low3)
    n_mag=n_cost=n_unpriced=n_comp=0; dmg_ratios=[]
    for r in S.iter_top_records(buf,{b'SPEL'}):
        if r.comp:
            # A zlib-compressed SPEL cannot be edited fixed-width in place: the parsed offsets
            # index the DECOMPRESSED body, not the raw bytes, and recompressing would change the
            # record size. Refuse and report rather than risk corrupting it.
            n_comp+=1; continue
        sp=S.parse_spel(r.data)
        if sp['type']!=0 or sp['spit_off'] is None: continue
        effs=[(res(e['mgef']),e) for e in sp['effects']]
        tier=classify_tier(effs); cls=spell_class(sp['castType'])
        # ONE solve for burst, DoT and concentration alike: pin the spell's SCORE to the baseline's
        # score for its (class, tier), log-blended toward the author by VARIETY, then read cost off
        # the same relation. A DoT needs no DoT samples and a high-tier concentration spell needs no
        # high-tier concentration samples -- score() and the conc/fnf ratio convert both onto the
        # dense fire-and-forget curve, and convert the answer back.
        V0=score(effs, sp['castType'], sp['delivery'], fit['dot_exp'])
        if V0>0:
            target=curve[cls][TI[tier]]
            Vstar=(target**(1-VAR))*(V0**VAR)                    # log-blend, on the score
            ratio=(Vstar/V0)*(OVR/100)*(TM[tier]/100)            # score is linear in the mags
            dmg_ratios.append(ratio)
            for m,e in effs:
                if S.is_damage(m) and e['mag']>0:
                    nm=e['mag']*ratio
                    if abs(nm-e['mag'])>1e-4:
                        struct.pack_into('<f', buf, r.data_off+e['efit_off'], float(nm)); n_mag+=1
                    e['mag']=nm
        # A record that already cost 0 is not cast and paid for by the player -- it is the damage
        # component of a cloak, a proc or a scripted ability, and its parent does the paying. Its
        # DAMAGE is still real and is pinned above (vanilla's own Flame Cloak sits exactly on the
        # Novice concentration curve, so the comparison is honest), but writing a magicka cost onto
        # it would invent a charge the engine never asked for. Leave it at 0.
        if sp['cost']<=0:
            if V0>0: n_unpriced+=1
            continue
        if V0>0:
            vc=(Vstar*(OVR/100)*(TM[tier]/100))/fit['E'][cls][TI[tier]]
            vc*=(OVR/100)*(TC[tier]/100)
        else:
            vc=vanilla_cost(effs)*(OVR/100)*(TC[tier]/100)
        vc=min(vc, ceil[(tier, cls)])
        newcost=max(int(round(vc)),0)
        if newcost!=sp['cost']:
            struct.pack_into('<I', buf, r.data_off+sp['spit_off'], newcost)
            struct.pack_into('<I', buf, r.data_off+sp['spit_off']+4, sp['flags']|S.SPIT_FLAG_MANUAL_COST)
            n_cost+=1
    med_ratio=statistics.median(dmg_ratios) if dmg_ratios else 1.0
    return bytes(buf), med_ratio, n_mag, n_cost, len(dmg_ratios), n_unpriced, n_comp

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
    ap.add_argument('--dot-exp', type=float, default=None, dest='dot_exp',
                    help="DoT delay-compensation rate (default %.2f). 1.0 = a fire-and-forget DoT's "
                         "TOTAL damage equals an equal-cost burst's; lower pays it more for putting "
                         "nothing on the target at cast. Concentration and burst are unaffected."
                         % DOT_EXP_DEFAULT)
    a=ap.parse_args()
    global DOT_EXP_OVERRIDE
    DOT_EXP_OVERRIDE=a.dot_exp
    VANILLA_DATA=a.data
    van_low3={n: low3_map(os.path.join(VANILLA_DATA,n)) for n in VANILLA_MASTERS}
    van_low3={n:v for n,v in van_low3.items() if v}
    order_paths=None
    if a.order:
        order_paths=resolve_order(a.order, VANILLA_DATA, a.mods)
        print(f"load order: {len(order_paths)} plugins resolved from {os.path.basename(a.order)}")
    curve, ceil, fit = build_vanilla_model(van_low3, None, order_paths)
    print("BASELINE score curve by tier (fnf = instant-equivalent damage, conc = damage/sec):")
    for cl in CLASSES: print(f"  {cl:4}: "+"  ".join(f"{t[:3]}={curve[cl][TI[t]]:.0f}" for t in TIERS))
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
        # Re-runs must not compound. Once a plugin has been deployed to in place, the file on disk
        # IS the balanced output; balancing it again would stack the ratio. The .bak written on the
        # first deploy is the pristine original, so read from that whenever it exists.
        read_from = src
        if os.path.abspath(src)==os.path.abspath(deploy) and os.path.exists(deploy+'.bak'):
            read_from = deploy+'.bak'
        out, med_ratio, nm, nc, ndmg, nunp, ncomp = balance_plugin(read_from, curve, ceil, fit, van_low3, knobs)
        open(os.path.join(OUT_DIR, os.path.basename(src)),'wb').write(out)
        if a.deploy and not a.dry:
            if os.path.abspath(src)==os.path.abspath(deploy) and not os.path.exists(deploy+'.bak'):
                shutil.copy2(deploy, deploy+'.bak')
            open(deploy,'wb').write(out)
        fresh = "" if read_from==src else "  [re-read pristine .bak]"
        unp = f"  ({nunp} unpriced: damage pinned, cost left 0)" if nunp else ""
        cmp_note = f"  [{ncomp} compressed SPEL skipped]" if ncomp else ""
        print(f"  {name:18} {ndmg:5}  {nm:5} {nc:4}   ×{med_ratio:.2f}{unp}{cmp_note}{fresh}")
        tot_m+=nm; tot_c+=nc
    mode = "DEPLOYED" if (a.deploy and not a.dry) else "dry-run (OUT_DIR only)"
    print(f"\nTOTAL magΔ={tot_m} costΔ={tot_c}  [{mode}]  OVERALL={a.overall} VARIETY={a.variety}")

if __name__ == '__main__':
    main()
