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
#   0.75 -> a 10s DoT gets 1.8x a burst's total, a 30s DoT 2.3x
#   0.70 -> a 10s DoT gets 2.0x, a 30s DoT 2.7x
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
# 0.75 is where Mysticism's own poison line sits against the honest tome-corpus curve: measured
# on its 10 tome-taught per-target DoTs, damage reproduces at exactly x1.00 (0.70, the old
# default, was the same calibration run against a curve inflated by trap/hazard junk and now
# reads x1.08; 0.60 reads x1.26). Poison is the most situational damage type in the game
# (resisted; immune on undead and automatons), so treat 0.75 as the generous end -- a fire DoT
# arguably deserves less compensation, a higher number.
# The COST side has its own dial, DOT_COST_BACK below: the author charges a DoT for part of its
# delay compensation instead of granting it free (Mysticism's DoTs are priced less
# score-efficient than its bursts, which one exponent alone cannot express). cost multiplies by
# (total/score)^DOT_COST_BACK -- 1.0 for bursts and concentration. 0.40 is where Mysticism's
# tome DoT costs reproduce at x1.01 with damage at x1.00; both dials are calibrated jointly on
# the only per-target DoT line any list in the corpus ships, and both are asserted, not
# per-list-fit, for the same reason DOT_EXP always was.
DOT_EXP_DEFAULT = 0.75
FIELD_EXPOSE    = 4.0    # seconds a target is realistically inside a wall/hazard/aura
MIN_N           = 3      # baseline samples a (class,tier) cell needs before it is trusted
MIN_DOT_N       = 6      # baseline DoT samples needed before the DIAGNOSTIC rate is shown
DOT_EXP_OVERRIDE = None  # set by --dot-exp
DOT_COST_BACK   = 0.40   # share of a DoT's delay compensation charged back into its cost

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

# Delivery families for PRICING. The one exchange rate held per tier only as long as every fnf
# spell was priced on the same pooled median -- but the vanilla design prices delivery itself:
# an aimed bolt buys ~0.5 damage/magicka at Apprentice while a rune buys ~0.19 and Firestorm 0.07.
# Pooling them made each family's cost drift toward the tier mix. Each family's level is MEASURED
# from the baseline (medians / pooled ratios; concentration gets the same two-anchor line as its
# damage ratio) -- no per-list constants.
SUBCATS = ['aimed','loc','self','conc']
def subcat(castType, delivery):
    """aimed = lands on a target you point at (2=Aimed, 3=TargetActor), the dense family;
    loc = placed at a location (4=TargetLocation: runes, placed fields);
    self = area around the caster (0=Self, 1=Contact: Firestorm, novas);
    conc = concentration, priced per second."""
    if castType==2: return 'conc'
    if delivery==4: return 'loc'
    if delivery in (0,1): return 'self'
    return 'aimed'

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

def nocast_keys(buf, own_name, masters=None):
    """Structural 'nobody casts this' markers of one plugin, as (plugin_name, low3) keys:
    full-skip = spells applied BY a hazard field (HAZD payloads -- walls, blizzards, gas, traps);
    cost-skip = cloak/proc payloads (MGEF associated items): their damage is honest (an enemy in
    the field really takes it) but their magicka cost is a token the engine never charges."""
    if masters is None: masters=S.masters(buf)
    def key(fid):
        hi=fid>>24
        return ((masters[hi] if hi<len(masters) else own_name), fid & 0xFFFFFF)
    full={key(f) for f in S.read_hazard_payloads(buf)}
    cost={key(f) for f in S.read_mgef_assoc(buf)}
    return full, cost

def effective_vanilla_spells(order_paths, van_low3, nocast=None):
    """{vanilla_low3: (parsed_spel, mgef_resolver)} for every vanilla SPEL that a later plugin
    OVERRIDES. Later plugins win, so this yields the list's ACTUAL baseline (e.g. Requiem's
    rescaled vanilla spells) rather than unmodded Skyrim.esm values.
    nocast, if given, is a (full_set, cost_set) pair that each order plugin's own hazard/cloak
    payload keys are accumulated into (see nocast_keys)."""
    winners={}
    for path in order_paths:
        try: buf=open(path,'rb').read()
        except OSError: continue
        try: masters=S.masters(buf)
        except Exception: continue
        own=S.read_mgef_map(buf)
        if nocast is not None:
            f,c=nocast_keys(buf, os.path.basename(path), masters)
            nocast[0]|=f; nocast[1]|=c
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
        if any(S.is_damage(m) and m.get('cond') for m,e in effs): continue
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
    # ---- structural no-cast sets (hazard payloads / cloak payloads), vanilla-wide -------------
    # Collected from every vanilla master and every order plugin, keyed (plugin, low3) so a pack's
    # override of e.g. vanilla HazardWallofFireSpell is recognized no matter which file wins it.
    nocast=[set(), set()]
    for n in VANILLA_MASTERS:
        p=os.path.join(VANILLA_DATA, n)
        if not os.path.exists(p): continue
        b=buf if n==CURVE_MASTER else open(p,'rb').read()
        f,c=nocast_keys(b, n)
        nocast[0]|=f; nocast[1]|=c
    # ---- the corpus: spells the design SELLS ---------------------------------------------------
    # Only tome-taught spells describe the baseline economy. The rest of the damage-spell pool --
    # traps, hazard payloads, creature attacks, shout and quest spells -- carries costs the engine
    # never charges a player (crChaurusPoisonSpit02: 49 damage for 3 magicka) and at some tiers it
    # is the ENTIRE pool: vanilla's Novice fnf cell is 100% traps/hazards/spit, zero player spells.
    # A spell tome is the structural marker for "priced by the design": every vanilla player spell
    # has one, no trap/hazard/creature spell does.
    tomes={f & 0xFFFFFF for f in S.read_tome_spells(buf)}
    base={}
    for r in S.iter_top_records(buf,{b'SPEL'}):
        base[r.formid & 0xFFFFFF]=(S.parse_spel(r.data), res)
    n_all=sum(1 for fid,(sp,_) in base.items()
              if sp['type']==0 and sp['spit_off'] is not None)
    n_over=0
    if order_paths:
        for fid,(sp2,res2) in effective_vanilla_spells(order_paths, van_low3, nocast).items():
            if fid in base: base[fid]=(sp2,res2); n_over+=1
    corpus={fid:v for fid,v in base.items() if fid in tomes}
    print(f"baseline: {len(base)} vanilla spells ({n_over} overridden by the load order), "
          f"corpus = {len(corpus)} tome-taught player spells")
    dot_exp = DOT_EXP_OVERRIDE if DOT_EXP_OVERRIDE is not None else DOT_EXP_DEFAULT
    src = "--dot-exp" if DOT_EXP_OVERRIDE is not None else "default"
    implied, n_dot, ok = fit_dot_exp(corpus)
    hint = (f"corpus's own {n_dot} per-target DoTs imply {implied}" if ok
            else f"corpus has only {n_dot} per-target DoTs, cannot imply one")
    print(f"  DoT delay-compensation rate: {dot_exp} ({src}); {hint}")
    # ---- corpus rows, by delivery family -------------------------------------------------------
    per=collections.defaultdict(list)          # (sub,tier)->[score]
    eff=collections.defaultdict(list)          # (sub,tier)->[score/cost]
    conc_ids=collections.defaultdict(list)     # tier->[edid], to name thin conc anchors
    costs=collections.defaultdict(list)        # (tier,'conc'|'fnf')->[cost] for the ceiling
    n_cond=0
    for sp,res_i in corpus.values():
        if sp['type']!=0 or sp['spit_off'] is None: continue
        effs=[(res_i(e['mgef']),e) for e in sp['effects']]
        if not has_damage(effs): continue
        tier=classify_tier(effs)
        V=score(effs, sp['castType'], sp['delivery'], dot_exp)
        if V<=0 or sp['cost']<=0: continue
        # Conditional damage (sun/anti-undead: the MGEF carries CTDA conditions) only lands on
        # some targets, so its magnitude-vs-cost sits far off the unconditional economy (Bane of
        # the Undead reads 13 score for 868 magicka). Real spells, wrong rows -- excluded.
        if any(S.is_damage(m) and m.get('cond') for m,e in effs):
            n_cond+=1; continue
        sub=subcat(sp['castType'], sp['delivery'])
        per[(sub,tier)].append(V)
        eff[(sub,tier)].append(V/sp['cost'])
        if sub=='conc': conc_ids[tier].append(sp['edid'])
        costs[(tier, 'conc' if sub=='conc' else 'fnf')].append(sp['cost'])
    if n_cond: print(f"  ({n_cond} conditional-damage spells excluded from the fit)")
    for a in SUBCATS:
        print(f"  samples {a:5}: {[len(per[(a,t)]) for t in TIERS]}")
    # ---- damage curve: aimed is the dense family; loc/self ride it by a measured ratio ---------
    # The corpus is all designed player spells, so every sampled cell is trusted (n>=1): at some
    # tiers the design's ONLY datum is one spell (Firestorm at Master) and dropping it would mean
    # inventing the cell instead. Empty cells interpolate/extrapolate along the vanilla shape.
    vt={i: statistics.median(per[('aimed',TIERS[i])]) for i in range(5) if per[('aimed',TIERS[i])]}
    P=smooth_fill(vt, FALLBACK['fnf'])
    def pooled_ratio(subs, of):
        rows=[V/of[i] for sb in subs for i in range(5) for V in per[(sb,TIERS[i])] if of[i]>0]
        return (statistics.median(rows), len(rows)) if rows else (None, 0)
    r_loc, n_loc  = pooled_ratio(['loc'],  P)
    r_self,n_self = pooled_ratio(['self'], P)
    r_fam, n_fam  = pooled_ratio(['loc','self'], P)   # family fallback: both are placed fields
    if r_loc  is None: r_loc  = r_fam if r_fam is not None else 1.0
    if r_self is None: r_self = r_fam if r_fam is not None else 1.0
    curve={'aimed': P,
           'loc':  [round(x*r_loc, 2) for x in P],
           'self': [round(x*r_self,2) for x in P]}
    print(f"  loc/aimed damage ratio {r_loc:.2f} (n={n_loc}), self/aimed {r_self:.2f} (n={n_self})")
    # ---- concentration: an absolute two-anchor geometric line through the design's own dps ----
    # Vanilla ships player conc damage at exactly two tiers: the Novice line (Flames/Frostbite/
    # Sparks) and Lightning Storm at Master -- and every overhaul re-prices those same spells. The
    # conc curve is the geometric line through those two anchors: conc(t) = lo * g^(t-lo_tier).
    # It is NOT expressed as a ratio of the aimed curve any more: the aimed Novice cell is always
    # EMPTY (vanilla sells no Novice fnf damage spell), and an intercept ratio taken against an
    # extrapolated denominator poisoned the whole line. Interior sampled cells are still
    # deliberately not fit: on every baseline examined they are the Wall spells, whose
    # SPEL-visible magnitude is only the spray tip (the real damage is the HAZD the wall leaves).
    # Validated: on a Mysticism list the line predicts Adept 18 dps vs the author's own 20.
    G_CONC_MAX = 2.0     # per-tier rise cap; vanilla's own line is x1.75/tier (8 -> 75)
    kmed={i: statistics.median(per[('conc',TIERS[i])])
          for i in range(5) if per[('conc',TIERS[i])]}
    def _anames(i, cap=3):
        n=conc_ids.get(TIERS[i], [])
        return " ["+", ".join(x or '?' for x in n)+"]" if 0<len(n)<=cap else ""
    if kmed:
        lo=min((i for i in kmed if len(per[('conc',TIERS[i])])>=MIN_N), default=min(kmed))
        hi=max(kmed)
        if hi>lo and kmed[hi]>kmed[lo]:
            g=min((kmed[hi]/kmed[lo])**(1.0/(hi-lo)), G_CONC_MAX)
            cvec=[kmed[lo]*g**(i-lo) for i in range(5)]
            print(f"  conc dps line: {kmed[lo]:.1f} ({TIERS[lo][:3]}, "
                  f"n={len(per[('conc',TIERS[lo])])}{_anames(lo)}) -> {cvec[hi]:.1f} "
                  f"({TIERS[hi][:3]}, n={len(per[('conc',TIERS[hi])])}{_anames(hi)}), x{g:.2f}/tier")
        else:
            cvec=[kmed[lo]]*5
            print(f"  conc dps line: {kmed[lo]:.1f} constant ({TIERS[lo][:3]} anchor; "
                  f"no higher-tier rise to slope from)")
    else:
        cvec=[curve['aimed'][i]*(FALLBACK['conc'][i]/FALLBACK['fnf'][i]) for i in range(5)]
        print("  conc dps line: default shape (corpus has no concentration damage spells)")
    # sustained dps cannot outbuy an equal-tier burst's whole payout every second -- but only a
    # SAMPLED aimed cell can veto (Requiem really does sell 16 dps conc at Novice, and the Novice
    # aimed cell is always empty/extrapolated, so an extrapolation must not trim a measured anchor)
    curve['conc']=[round(min(cvec[i], P[i]) if per[('aimed',TIERS[i])] else cvec[i], 2)
                   for i in range(5)]
    for a in curve:                      # sampled + derived cells must not disagree in direction
        for i in range(1,5):
            if curve[a][i] < curve[a][i-1]: curve[a][i]=curve[a][i-1]
    # ---- efficiency per family: what a magicka buys depends on HOW the damage arrives ----------
    # aimed: dense, per-tier medians. loc/self: aimed times a pooled measured ratio (runes and
    # self-areas run ~0.3x aimed efficiency in vanilla; overhauls move it, so it is re-measured).
    # conc: the same two-anchor line as its damage ratio -- the Novice conc mass sets the level,
    # the list's winning Lightning Storm sets the slope, and the interior wall cells (whose
    # SPEL-visible magnitude hides the hazard damage) are deliberately not fit. This replaces the
    # single shared exchange rate, which priced a Master conc spell against burst efficiency and
    # over-charged it ~2.5x into the cost ceiling.
    et={i: statistics.median(eff[('aimed',TIERS[i])]) for i in range(5) if eff[('aimed',TIERS[i])]}
    E_aimed=fill_gaps(et, [0.4]*5)
    def pooled_eff_ratio(subs):
        rows=[e/E_aimed[i] for sb in subs for i in range(5)
              for e in eff[(sb,TIERS[i])] if E_aimed[i]>0]
        return statistics.median(rows) if rows else None
    er_loc=pooled_eff_ratio(['loc']); er_self=pooled_eff_ratio(['self'])
    er_fam=pooled_eff_ratio(['loc','self'])
    if er_loc  is None: er_loc  = er_fam if er_fam is not None else 1.0
    if er_self is None: er_self = er_fam if er_fam is not None else 1.0
    emed={i: statistics.median(eff[('conc',TIERS[i])]) for i in range(5) if eff[('conc',TIERS[i])]}
    if emed:
        elo=min((i for i in emed if len(eff[('conc',TIERS[i])])>=MIN_N), default=min(emed))
        ehi=max(emed)
        if ehi>elo:
            ge=(emed[ehi]/emed[elo])**(1.0/(ehi-elo))
            ge=min(max(ge, 1.0/1.5), 1.5)   # clamp: efficiency drift per tier bounded like the damage line
            E_conc=[emed[elo]*ge**(i-elo) for i in range(5)]
            print(f"  conc dps-per-magicka/s: {emed[elo]:.3f} ({TIERS[elo][:3]}) -> "
                  f"{E_conc[ehi]:.3f} ({TIERS[ehi][:3]}{_anames(ehi)}), x{ge:.3f}/tier")
        else:
            E_conc=[emed[elo]]*5
            print(f"  conc dps-per-magicka/s: {emed[elo]:.3f} constant (one sampled tier)")
    else:
        E_conc=list(E_aimed)
        print("  conc dps-per-magicka/s: NO conc samples -- falling back to aimed efficiency")
    E={'aimed': [max(x,1e-6) for x in E_aimed],
       'loc':   [max(x*er_loc, 1e-6) for x in E_aimed],
       'self':  [max(x*er_self,1e-6) for x in E_aimed],
       'conc':  [max(x,1e-6) for x in E_conc]}
    print(f"  damage per magicka by tier, aimed: {[round(x,3) for x in E['aimed']]}  "
          f"(loc x{er_loc:.2f}, self x{er_self:.2f})")
    # ---- soft cost ceiling per (tier, class), from corpus costs; gaps interpolated -------------
    ceil={}
    for cls in ('fnf','conc'):
        vt_c={i: max(costs[(TIERS[i],cls)])*COST_CEIL_HEADROOM
              for i in range(5) if costs[(TIERS[i],cls)]}
        vec=fill_gaps(vt_c, [99999]*5)
        for i in range(5): ceil[(TIERS[i],cls)]=vec[i]
    return curve, ceil, {'E':E, 'dot_exp':dot_exp,
                         'nocast_full':nocast[0], 'nocast_cost':nocast[1]}

def balance_plugin(src, curve, ceil, fit, van_low3, knobs):
    OVR, TC, TM, VAR = knobs
    buf=bytearray(open(src,'rb').read())
    masters=S.masters(buf); own=S.read_mgef_map(buf)
    res=build_resolver(masters, own, van_low3)
    # this file's own hazard/cloak payloads, plus the vanilla-wide sets from the model
    own_name=os.path.basename(src)
    if own_name.endswith('.bak'): own_name=own_name[:-4]
    f_own,c_own=nocast_keys(bytes(buf), own_name, masters)
    skip_full=fit['nocast_full'] | f_own
    skip_cost=fit['nocast_cost'] | c_own
    def rec_key(fid):
        hi=fid>>24
        return ((masters[hi] if hi<len(masters) else own_name), fid & 0xFFFFFF)
    n_mag=n_cost=n_unpriced=n_comp=n_haz=0; dmg_ratios=[]
    for r in S.iter_top_records(buf,{b'SPEL'}):
        if r.comp:
            # A zlib-compressed SPEL cannot be edited fixed-width in place: the parsed offsets
            # index the DECOMPRESSED body, not the raw bytes, and recompressing would change the
            # record size. Refuse and report rather than risk corrupting it.
            n_comp+=1; continue
        sp=S.parse_spel(r.data)
        if sp['type']!=0 or sp['spit_off'] is None: continue
        key=rec_key(r.formid)
        if key in skip_full:
            # A hazard payload. Nobody casts it: the wall/blizzard/trap field applies it, its
            # magicka cost is a token the engine never charges, and its magnitude is tuned to the
            # field's tick pattern. Not a player spell -- not refit, not repriced. (User decision
            # 2026-08-24: hazards and traps are excluded entirely.)
            n_haz+=1; continue
        effs=[(res(e['mgef']),e) for e in sp['effects']]
        tier=classify_tier(effs); cls=spell_class(sp['castType'])
        sub=subcat(sp['castType'], sp['delivery'])
        # ONE solve for burst, DoT and concentration alike: pin the spell's SCORE to the baseline's
        # score for its (family, tier), log-blended toward the author by VARIETY, then read cost off
        # the family's measured efficiency for the tier.
        V0=score(effs, sp['castType'], sp['delivery'], fit['dot_exp'])
        if V0>0:
            target=curve[sub][TI[tier]]
            Vstar=(target**(1-VAR))*(V0**VAR)                    # log-blend, on the score
            ratio=(Vstar/V0)*(OVR/100)*(TM[tier]/100)            # score is linear in the mags
            dmg_ratios.append(ratio)
            for m,e in effs:
                if S.is_damage(m) and e['mag']>0:
                    nm=e['mag']*ratio
                    if abs(nm-e['mag'])>1e-4:
                        struct.pack_into('<f', buf, r.data_off+e['efit_off'], float(nm)); n_mag+=1
                    e['mag']=nm
        # No cost is written when:
        #   cost == 0        -- a proc/scripted damage component; its parent does the paying.
        #   cloak payload    -- same family, but authors often stamp a token cost (Whirlwind
        #                       Cloak's proc costs 2); the damage above is honest (an enemy inside
        #                       the cloak really takes it), the charge is not.
        #   no damage payout -- the model prices damage; it has no opinion on utility spells.
        #                       (The old autocalc path also ZEROED any spell whose effects carry
        #                       no engine base cost -- Transmute went 261 -> 0.)
        if sp['cost']<=0 or key in skip_cost or V0<=0:
            if V0>0 and sp['cost']>0: n_unpriced+=1
            continue
        # A DoT's score credits it EXTRA total damage as delay compensation (dur_eff). The damage
        # keeps all of it, but the price charges part of it back: the fixed timer is a real
        # handicap, so the compensation is a discount -- not a free lunch. The charge-back is
        # (total/score)^DOT_COST_BACK; bursts and concentration have total == score, so it is
        # exactly 1 for them. Like DOT_EXP itself, the rate is unmeasurable from any vanilla
        # baseline (zero corpus DoTs) and is calibrated the same documented way, jointly with
        # DOT_EXP: at (0.75, 0.40) Mysticism's tome DoT line reproduces at x1.00 damage and
        # x1.01 cost (its DoTs are priced less score-efficient than its bursts, which one
        # exponent alone cannot express).
        V1=score(effs, sp['castType'], sp['delivery'], 1.0)
        sur=(V1/V0)**DOT_COST_BACK if V1>V0 else 1.0
        vc=(Vstar*sur*(OVR/100)*(TM[tier]/100))/fit['E'][sub][TI[tier]]
        vc*=(OVR/100)*(TC[tier]/100)
        vc=min(vc, ceil[(tier, cls)])
        newcost=max(int(round(vc)),0)
        if newcost!=sp['cost']:
            struct.pack_into('<I', buf, r.data_off+sp['spit_off'], newcost)
            struct.pack_into('<I', buf, r.data_off+sp['spit_off']+4, sp['flags']|S.SPIT_FLAG_MANUAL_COST)
            n_cost+=1
    med_ratio=statistics.median(dmg_ratios) if dmg_ratios else 1.0
    return bytes(buf), med_ratio, n_mag, n_cost, len(dmg_ratios), n_unpriced, n_comp, n_haz

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
    print("BASELINE score curve by tier (aimed/loc/self = instant-equivalent damage, conc = damage/sec):")
    for cl in SUBCATS: print(f"  {cl:5}: "+"  ".join(f"{t[:3]}={curve[cl][TI[t]]:.0f}" for t in TIERS))
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
        out, med_ratio, nm, nc, ndmg, nunp, ncomp, nhaz = balance_plugin(read_from, curve, ceil, fit, van_low3, knobs)
        open(os.path.join(OUT_DIR, os.path.basename(src)),'wb').write(out)
        if a.deploy and not a.dry:
            if os.path.abspath(src)==os.path.abspath(deploy) and not os.path.exists(deploy+'.bak'):
                shutil.copy2(deploy, deploy+'.bak')
            open(deploy,'wb').write(out)
        fresh = "" if read_from==src else "  [re-read pristine .bak]"
        unp = f"  ({nunp} proc/cloak: damage pinned, token cost kept)" if nunp else ""
        haz = f"  [{nhaz} hazard/trap payloads skipped]" if nhaz else ""
        cmp_note = f"  [{ncomp} compressed SPEL skipped]" if ncomp else ""
        print(f"  {name:18} {ndmg:5}  {nm:5} {nc:4}   ×{med_ratio:.2f}{haz}{unp}{cmp_note}{fresh}")
        tot_m+=nm; tot_c+=nc
    mode = "DEPLOYED" if (a.deploy and not a.dry) else "dry-run (OUT_DIR only)"
    print(f"\nTOTAL magΔ={tot_m} costΔ={tot_c}  [{mode}]  OVERALL={a.overall} VARIETY={a.variety}")

if __name__ == '__main__':
    main()
