# Skyrim SE Spell Vanilla-Balancer

Reprice added spell packs (Apocalypse, Kittytail, Darenii, your own mod…) to the **vanilla
standard** — headless, no xEdit and no Creation Kit. It edits `.esp`/`.esl` plugins directly
in Python, and it **derives** the balance from vanilla data instead of trusting each mod's
(inconsistent) numbers.

Point it at a list of plugins and it rewrites their magicka cost and damage so every pack
sits on one coherent, vanilla-calibrated curve — with knobs (where **100 = vanilla**) so you
can shift the whole thing up or down later, per magic-tier or globally.

```
python3 spell_balance.py --data "/path/to/Skyrim/Data" MySpells.esp AnotherPack.esl --deploy
```

## Why

Every spell pack ships on its own power/cost scale — one mod's Adept firebolt costs 150 and
hits for 40, another's costs 600 and hits for 120. Vanilla, by contrast, is one scale that's
consistent across hundreds of spells. Crucially, a spell's magicka cost, damage, tier
(min-skill), duration and effect base-cost are **not independent** — the engine ties them
together. That makes the vanilla corpus an over-determined system you can *fit*, then move
each pack onto. This tool does that fitting and moving.

## Baselining against overhaul lists (Requiem, Simonrim, …)

By default the reference curve comes from `Skyrim.esm`. On a list whose overhaul **rewrites the vanilla
spells** (Requiem, Mysticism/Simonrim, …) that is the wrong target — balancing to unmodded vanilla
would leave added spells underpowered next to everything else on the list.

Point it at the load order instead and the baseline is rebuilt from the **winning version of each
vanilla spell**, i.e. the overhaul's own scale:

```sh
python3 spell_balance.py --data "<list>/Game Root/Data" \
                         --mods "<list>/mods" \
                         --order "<list>/profiles/<profile>/loadorder.txt" \
                         --dry  "<list>/mods/Some Pack/SomePack.esp"
```

It reports how much of the baseline actually moved, e.g.
`baseline: 827 vanilla spells, 440 overridden by the load order`.
A sanity check worth doing: run the overhaul's own plugin through it — a mod that *is* the baseline
should come back at `x1.00`.

Magic-effect references are followed **across the whole load order**, not just into the vanilla
masters: a winning vanilla-spell override whose damage effects live in the overhaul's own plugin
(Requiem's Lightning Storm, say) is resolved and measured like any other.

## How the balance is derived (the math)

Everything below is measured from the baseline at runtime — with one stated exception, called
out explicitly.

### One idea: equal magicka buys equal damage, within a tier

A spell is bought with magicka and paid out in damage. At a given rank the exchange rate is a
single number, `E(tier)`, and **how** the damage arrives doesn't change what it cost to buy. So
every spell is scored on one axis and burst, damage-over-time and concentration become directly
comparable — there is no separate curve per delivery style to populate:

| delivery | score | why |
|---|---|---|
| **concentration** | `magnitude` (= damage/sec) | Cost drains per second too, so rate against rate *is* the exchange rate. Duration is the **player's** choice — how long they hold it — never the spell's, so it must not enter the valuation at all. |
| **fire-and-forget** | `magnitude × dur_eff(duration)` | Paid once at cast, so one payout. A burst scores its magnitude; a DoT scores more, because it puts nothing on the target at t=0 and makes the player wait out a timer *the spell* fixed. |
| **field** (wall, hazard, cloak, aura) | as above, exposure capped | The recorded duration is how long the **field** lasts, not how long anything is being damaged. A Wall of Fire burns 10s; a target walks through it for a couple of seconds. |

Damage counts only hostile Health-modifying effects **with a nonzero base cost**. That excludes
illusion "fear level", magicka drain and other utility magnitudes — and it excludes vanilla's
`PerkDisintegrate*` riders, which are magnitude 200, duration 1, base cost 0. Counting them made
Sparks read 208 damage instead of 8 and flipped every vanilla shock spell (Sparks, Lightning
Bolt, Chain Lightning, Thunderbolt, Wall of Storms, Lightning Storm) into the DoT bucket.

### Why this is what makes DoT balance possible

Vanilla contains **zero player damage-over-time spells**. Not few — zero. Every duration-bearing
damage spell in `Skyrim.esm` is a trap, a hazard, a creature attack or a shout. Requiem lists add
none either. So a per-(tier, DoT) damage curve can never be *measured*; three of its five cells
have to be invented, and DoT-heavy packs then swing on that extrapolation rather than on any real
imbalance. That was the flaw in earlier versions — the same pack could read ×0.58 one way and
×1.58 the other.

Here **nothing about a DoT is sourced from other DoTs.** It is priced off the same dense
`E(tier)` as every burst, and the only DoT-specific quantity is a single global constant shared
by all five tiers, so no cell can be defined by one sample.

### Concentration, same trick

Concentration is as data-starved as DoT, for the same structural reason: vanilla has exactly three
player concentration damage spells (Flames, Frostbite, Sparks — all Novice). **Apprentice and Adept
have zero samples on every baseline tested**, and the higher cells that do exist are the Wall
spells, which are famously underpowered. Per-cell, the curve came out nearly flat (`8/9/11/11/11`),
which crushed any high-tier concentration spell a pack added.

It doesn't need per-tier samples either. Both classes are bought with the same magicka at the same
rank, so the whole concentration curve is the **dense** fire-and-forget curve times a measured
conc/fnf ratio. That ratio is not one constant — it **rises with tier**, and vanilla says so
itself: Flames deals 8/sec at Novice (~0.38 of a Novice burst), Lightning Storm deals 75/sec at
Master against Firestorm's 100 burst. Bethesda's own magic-effect pricing agrees structurally —
the fire-and-forget damage effects' base cost climbs with tier (which is exactly why burst
damage-per-magicka falls) while the concentration effects' stays flat — so concentration keeps
its exchange rate while burst pays a rising premium, and the damage ratio between them widens.

The ratio is fit as a geometric line through the only two honest anchors any baseline has:

```
k(t) = k_lo × g^(t − lo)        g = (k_hi / k_lo)^(1/(hi−lo)),  clamped to [1, 1.5] per tier
P(conc, t) = k(t) × P(fnf, t)
```

`lo` is the lowest tier with a real sample mass (Novice, ~50 spells: the intercept); `hi` is the
highest tier with any concentration sample — in practice the list's winning **Lightning Storm**,
the only high-tier player concentration spell Bethesda ever shipped, re-valued by whatever
overhaul wins it. The interior sampled cells are deliberately not fit: on every baseline examined
they are the Wall spells, whose recorded magnitude is only the spray tip (the real damage is the
hazard the wall leaves, invisible to the spell record — the engine prices ~8× more damage into
their base cost than their magnitude shows), and fitting them would flip the slope negative.

Three guards bound what a junk top anchor can do: a *falling* anchor drops the slope entirely; a
rising one is clamped to ×1.5/tier; and an anchor claiming a ratio above **1.0** (sustained dps
outbuying an equal-tier burst's whole payout, every second — no design does that; vanilla's own
maximum is 0.83) is rejected as junk. In each fallback case the curve uses the constant instead —
the **low-tier mass median**, not the whole-pool median, since the pool contains the same wall
contamination the fit excludes. Thin anchors are *named* in the printout so you can see exactly
which spell is driving the slope.

Everything is measured per baseline: unmodded vanilla rises 0.38 → 0.83 (×1.22/tier — the derived
Master target is 75 dps, Lightning Storm itself), a Simonrim list 0.35 → 0.67 (×1.17/tier — and
the fit's interpolated Adept ratio, 0.48, lands within 4% of the 0.50 Mysticism's own conc line
uses), Requiem stays at its constant 0.51: its winning Lightning Storm measures 0.43 against its
own unusually strong Novice line, so the slope is genuinely absent there — measured absent, not
missing.

### Spells nobody casts

A record with **cost 0** isn't a spell the player casts and pays for — it's the damage component of
a cloak, a proc or a scripted ability, and its parent does the paying. Vanilla has 13; some packs
are up to 78% them. They're excluded from the baseline fit (they have no cost, so no efficiency,
and they sit at whatever magnitude their parent wanted). Their *damage* is still pinned like
anything else — vanilla's own Flame Cloak sits exactly on the Novice concentration curve, so the
comparison is honest — but no magicka cost is written onto them.

### The solve, per spell

1. **Baseline curves** — `P(class, tier)` = median score, `E(tier)` = median score per magicka.
   Taken from `Skyrim.esm`, or from the **winning** version of each vanilla spell in a load order
   (`--order`, for lists whose overhaul rewrites vanilla magic).
2. **Damage** — pin the spell's score to `P` for its own (class, tier), log-blended back toward
   the author's value by `VARIETY`. Score is *linear* in every magnitude, so the target ratio is
   read straight off and applied to all the spell's damage effects, preserving its structure.
3. **Cost** — solved from the same relation, `cost = score / E(tier)`. Not derived from the mod's
   own effect base-costs, which are frequently inflated. A soft per-(tier, class) ceiling guards
   the rare tail.

### The one assumed number

`DOT_EXP` sets how much extra total damage a fire-and-forget DoT gets as compensation for
delivering nothing up front: `total = burst_target × dur^(1-DOT_EXP)`.

| `DOT_EXP` | 10s DoT gets | 30s DoT gets |
|---|---|---|
| `1.00` | 1.0× a burst's total | 1.0× |
| `0.70` (default) | 2.0× | 2.7× |
| `0.50` | 3.2× | 5.5× |

This is the only number in the model that is asserted rather than measured, because **no baseline
can identify it** — see above. The default sits where Mysticism's own poison line sits, i.e. it
makes Mysticism reproduce itself at ×1.00. Poison is the most situational damage type in the game
(resisted; immune on undead and automatons), so treat `0.70` as the generous end — a fire DoT
arguably deserves less compensation, meaning a higher number. Each run prints what the baseline's
own DoT samples imply, as a diagnostic; adopt it with `--dot-exp` only after reading them.

Concentration and burst spells are **unaffected** by this knob.

## Knobs (100 = vanilla)

Edit the CONFIG block at the top of `spell_balance.py`, or pass on the CLI:

| knob | meaning |
|---|---|
| `OVERALL` / `--overall` | global multiplier on cost and damage |
| `TIER_COST[tier]` | per-tier (Novice…Master) magicka-cost multiplier |
| `TIER_MAG[tier]`  | per-tier damage multiplier |
| `VARIETY` / `--variety` | 0 = pure vanilla damage, 1 = author's damage untouched |
| `DOT_EXP` / `--dot-exp` | DoT delay compensation; 1.0 = a DoT's total damage equals an equal-cost burst's |

Effective mult = `OVERALL/100 × TIER_x[tier]/100`. Re-run any time: on an in-place re-run the
tool reads the `.bak` it made on the first `--deploy`, so it always balances the pristine original
and tuning never compounds. (Keep that `.bak` — delete it and a re-run *will* stack.)

## Usage

```
# report only (writes balanced copies to ./balanced_out, changes nothing):
python3 spell_balance.py --data "/path/to/Data" Pack1.esp Pack2.esl

# balance in place (a .bak is made once per file):
python3 spell_balance.py --data "/path/to/Data" Pack1.esp Pack2.esl --deploy

# make magic 20% cheaper everywhere, keep more of the authors' damage spread:
python3 spell_balance.py --data "/path/to/Data" --overall 80 --variety 0.6 Pack1.esp --deploy
```

`--data` is a folder holding your vanilla masters (your game `Data` folder, or a Mod
Organizer *Stock Game/Data*). You can also set `SKYRIM_DATA` in the environment.

## Safety

Edits are **fixed-width, in place**: only the SPIT cost/flags (`u32`) and EFIT magnitude
(`f32`) bytes change, so the file size never changes and no record/group sizes need fixing.
Records that are zlib-compressed are skipped rather than risked. The plugin's TES4 header and
master list are left byte-identical. Always keep the `.bak` (or your own backup) until you've
confirmed the result in-game.

## Requirements

Python 3.8+. No dependencies. Works on the plugin files directly — Mod Organizer / Vortex not
required (though you can point it at a virtual `Data`).

## Files

- `spell_balance.py` — the balancer (config, model, CLI).
- `spell_lib.py` — headless SPEL/MGEF plugin reader (record/subrecord walker, MGEF field
  offsets, the damage/tier/school helpers).

## Changelog

### 1.0.0 — 2026-08-24

First tagged release. The model change that made it shippable: **all delivery styles are scored
on one axis** (equal magicka buys equal damage, within a tier).

- `dot` is no longer an archetype. A damage-over-time spell is a fire-and-forget spell scored
  `magnitude × dur_eff(duration)`; nothing about a DoT is sourced from other DoTs (vanilla has
  zero player DoT damage spells, so the old per-(tier, DoT) curve was three-fifths invented).
  `DOT_EXP` (default 0.70) is the single asserted delay-compensation constant — every other
  number is measured from the baseline at runtime.
- The concentration curve is `k(t) × P(fnf, t)`, with `k(t)` a geometric line through the two
  honest anchors (the Novice mass and the list's winning Lightning Storm), guarded against junk
  anchors and falling back to the low-tier-mass constant.
- Magic-effect references resolve across the whole load order, so overhaul-owned effects
  (Requiem's spells) are measured instead of silently dropped.
- Damage requires a *priced* effect (`basecost > 0`): vanilla's zero-cost `PerkDisintegrate*`
  riders no longer read as 200 damage or flip shock spells into DoTs.
- Field spells (walls, hazards, cloaks, auras) credit at most `FIELD_EXPOSE` seconds of exposure.
- Cost-0 records (cloak/proc/scripted-ability components) are excluded from the baseline fit and
  never have a cost written onto them; their damage is still pinned.
- In-place re-runs re-read the pristine `.bak`, so tuning never compounds.
- Compressed SPEL records are skipped and reported instead of risked.

Validated by self-consistency: with a Mysticism (Simonrim) list as baseline, Mysticism itself
reproduces at ×1.00 in all four delivery styles (concentration / burst / per-target DoT /
field), and per-tier concentration at 1.00 / 0.98 / 1.00 (Novice / Adept / Master).

## License

MIT — see `LICENSE`. Not affiliated with Bethesda or any mod author; it only edits plugins you
already own, on your own machine.
