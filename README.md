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

### The corpus: only spells the design sells

The baseline is fit **only from tome-taught spells** — the ones a spell tome (`BOOK` record)
teaches. That is the structural marker for "priced by the design": every vanilla player spell has
a tome, and no trap, hazard, creature attack, shout or quest spell does. It matters more than it
sounds: most of the vanilla damage-spell pool is *not* player spells (`crChaurusPoisonSpit02`:
49 damage for 3 magicka), and at some tiers the junk is the whole pool — vanilla's Novice
fire-and-forget cell is 100% traps/hazards/spit. Fitting on that pool is what silently skewed
earlier versions' costs. Conditional damage (sun/anti-undead effects carrying `CTDA` conditions)
is excluded from the fit too: real spells, but priced for a damage clause that only sometimes
applies.

### One idea: a spell's score, comparable across delivery styles

A spell is bought with magicka and paid out in damage, and every spell is scored on one axis so
burst, damage-over-time and concentration become directly comparable:

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

### Pricing: what a magicka buys depends on how the damage arrives

Within a tier the exchange rate is measured per **delivery family**, because the vanilla design
prices delivery itself: at Apprentice an aimed bolt buys ~0.5 damage per magicka, a rune ~0.19,
and Firestorm ~0.07. The families are structural (cast type + delivery field):

- **aimed** (projectile / on-target) — the dense family; per-tier median efficiency.
- **loc** (placed at a location: runes, placed fields) and **self** (area around the caster:
  Firestorm, novas) — aimed efficiency times one pooled, measured ratio each (vanilla: both ~0.3×;
  overhauls move it, so it is re-measured per baseline).
- **concentration** — its own two-anchor efficiency line (below).

Damage targets use the same family split (aimed medians; loc/self by measured ratio; conc its own
line). A single pooled exchange rate — what earlier versions used — made every family's cost
drift toward whatever mix of deliveries its tier happened to contain, and over-charged Master
concentration ~2.5× into the cost ceiling.

### Why this is what makes DoT balance possible

Vanilla contains **zero player damage-over-time spells**. Not few — zero. Every duration-bearing
damage spell in `Skyrim.esm` is a trap, a hazard, a creature attack or a shout. Requiem lists add
none either. So a per-(tier, DoT) damage curve can never be *measured*; three of its five cells
have to be invented, and DoT-heavy packs then swing on that extrapolation rather than on any real
imbalance. That was the flaw in earlier versions — the same pack could read ×0.58 one way and
×1.58 the other.

Here **nothing about a DoT is sourced from other DoTs.** It is priced off the same dense
aimed curve as every burst, and the only DoT-specific quantity is a single global constant shared
by all five tiers, so no cell can be defined by one sample. On the cost side a DoT keeps its
delay compensation in damage but is charged **part of it back** in magicka
(`× (total/score)^0.4`, between pricing the score and pricing the total damage): the timer is a
real handicap, so the compensation is a discount — not a free lunch, and the strongest available
reference agrees (calibrated jointly with `DOT_EXP`, Mysticism's own tome poison line reproduces
at ×1.00 damage and ×1.01 cost).

### Concentration: a two-anchor line through the design's own dps

Concentration is as data-starved as DoT, for the same structural reason: the corpus has player
concentration damage at exactly two places on every baseline — the Novice line (Flames,
Frostbite, Sparks, as re-priced by whatever overhaul wins them) and **Lightning Storm** at
Master, the only high-tier player concentration spell Bethesda ever shipped. The concentration
curve is the geometric line through those two anchors:

```
conc(t) = c_lo × g^(t − lo)        g = (c_hi / c_lo)^(1/(hi−lo))
```

The interior sampled cells are deliberately not fit: on every baseline examined they are the Wall
spells, whose recorded magnitude is only the spray tip — the real damage is the hazard the wall
leaves, invisible to the spell record (the engine prices ~8× more damage into their base cost
than their magnitude shows; and there is no structural marker that could separate them, verified:
archetype and associated-item fields match honest damage effects). Guards: a top anchor that does
not rise above the intercept (the wall signature), or that claims more sustained dps than an
equal-tier burst's whole payout per second, is not trusted — the curve then inherits the aimed
curve's shape through the intercept instead. The per-tier rise is capped at ×2.0 (vanilla's own
line is ×1.75: 8 → 75 dps). Thin anchors are *named* in the printout.

Concentration **cost** gets the same treatment: a dps-per-magicka/s line through the same two
anchors. This is measured per baseline — unmodded vanilla is nearly flat (0.50 → 0.54: Lightning
Storm keeps Novice efficiency), a Mysticism list falls ×0.84/tier (0.44 → 0.22), Requiem falls
×0.76/tier (0.40 → 0.13). The single shared exchange rate this replaces priced Master
concentration against burst efficiency and over-charged it ~2.5× into the cost ceiling.

### Spells nobody casts

Three structural markers identify records that look like spells but are never cast and paid for
by an actor, and each gets exactly the treatment its evidence supports:

- **Hazard payloads** (`HAZD` records name the spell they apply to actors inside the field —
  walls, blizzards, gas clouds, fire-plate traps): **skipped entirely** — no damage rewrite, no
  cost rewrite, reported per pack. Their token costs (1, 8, 10 magicka) are never charged by the
  engine, and their magnitudes are tuned to the field's tick pattern, not to a cast. Earlier
  versions re-priced them ×4–×21.
- **Cloak/proc payloads** (a magic effect's associated item names the spell a cloak applies to
  nearby targets): damage is still pinned — an enemy inside the cloak really takes it, and
  vanilla's own Flame Cloak sits exactly on the Novice concentration curve — but the token cost
  is kept, never rewritten.
- **Cost-0 records** (scripted-ability damage components): as before, damage pinned, no cost
  invented.

Spells with **no damage at all** (summons, wards, utility) are left completely untouched now: the
model prices damage and has no opinion on utility spells. (The old autocalc re-costing path also
zeroed any spell whose effects carry no engine base cost — Transmute went 261 → 0 magicka.)

### The solve, per spell

1. **Baseline curves** — per delivery family: `P(family, tier)` = median score, `E(family,
   tier)` = score per magicka. Taken from the tome-taught corpus of `Skyrim.esm`, or from the
   **winning** version of each of those spells in a load order (`--order`, for lists whose
   overhaul rewrites vanilla magic).
2. **Damage** — pin the spell's score to `P` for its own (family, tier), log-blended back toward
   the author's value by `VARIETY`. Score is *linear* in every magnitude, so the target ratio is
   read straight off and applied to all the spell's damage effects, preserving its structure.
3. **Cost** — `cost = score × (total/score)^0.4 / E(family, tier)` (the second factor is 1 for
   anything that isn't a DoT). Not derived from the mod's own effect base-costs, which are
   frequently inflated. A soft per-(tier, class) ceiling, interpolated from the corpus costs,
   guards the rare tail.

### The one assumed number

`DOT_EXP` sets how much extra total damage a fire-and-forget DoT gets as compensation for
delivering nothing up front: `total = burst_target × dur^(1-DOT_EXP)`.

| `DOT_EXP` | 10s DoT gets | 30s DoT gets |
|---|---|---|
| `1.00` | 1.0× a burst's total | 1.0× |
| `0.75` (default) | 1.8× | 2.3× |
| `0.70` | 2.0× | 2.7× |
| `0.50` | 3.2× | 5.5× |

This is the only number in the model that is asserted rather than measured, because **no baseline
can identify it** — see above. The default sits where Mysticism's own poison line sits under the
tome-taught corpus (damage and cost jointly within a few percent of self-reproduction). Poison is
the most situational damage type in the game (resisted; immune on undead and automatons), so a
fire DoT arguably deserves less compensation, meaning a higher number. Each run prints what the
baseline's own DoT samples imply, as a diagnostic; adopt it with `--dot-exp` only after reading
them.

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

First tagged release. Two structural decisions made it shippable: **all delivery styles are
scored on one axis** (a spell's score), and **everything is fit from — and priced against — the
spells the design actually sells**.

- The baseline corpus is tome-taught player spells only. Traps, hazards, creature attacks, shouts
  and quest spells (most of the raw pool; at some tiers all of it) never touch the fit, and
  conditional (sun/anti-undead) damage is excluded from it too.
- Hazard payload spells (named by `HAZD` records) are skipped entirely — not refit, not
  re-costed, reported per pack. Cloak/proc payloads keep their token costs; their damage is still
  pinned. Spells with no damage are left untouched (the old autocalc path zeroed some utility
  spells' costs).
- `dot` is not an archetype. A damage-over-time spell is a fire-and-forget spell scored
  `magnitude × dur_eff(duration)`; nothing about a DoT is sourced from other DoTs (vanilla has
  zero player DoT damage spells). `DOT_EXP` (default 0.75) and the cost charge-back
  `DOT_COST_BACK` (0.40, `cost × (total/score)^0.4`) are the two asserted DoT constants,
  calibrated jointly — every other number is measured from the baseline at runtime.
- Costs are priced per delivery family (aimed / placed / self-area / concentration), each
  family's efficiency measured from the corpus; concentration damage **and** cost are two-anchor
  geometric lines through the Novice mass and the list's winning Lightning Storm, guarded against
  junk anchors (walls).
- Magic-effect references resolve across the whole load order, so overhaul-owned effects
  (Requiem's spells) are measured instead of silently dropped.
- Damage requires a *priced* effect (`basecost > 0`): vanilla's zero-cost `PerkDisintegrate*`
  riders no longer read as 200 damage or flip shock spells into DoTs.
- Field spells (walls, hazards, cloaks, auras) credit at most `FIELD_EXPOSE` seconds of exposure.
- In-place re-runs re-read the pristine `.bak`, so tuning never compounds.
- Compressed SPEL records are skipped and reported instead of risked.

Validated by self-consistency on damage **and cost**: with a Mysticism (Simonrim) list as
baseline, Mysticism itself reproduces at ×1.00 median damage in every delivery category, and
×0.97–1.03 median cost per category (burst / DoT / concentration / field / rune), with
concentration per tier at 1.00 / 1.03 / 1.05 (Novice / Adept / Master) on cost and
1.00 / 0.93 / 1.00 on damage. Hazard payloads report as skipped instead of re-priced ×4–×21.

## License

MIT — see `LICENSE`. Not affiliated with Bethesda or any mod author; it only edits plugins you
already own, on your own machine.
