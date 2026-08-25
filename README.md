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

## How the balance is derived (the math)

Everything below is measured from `Skyrim.esm` at runtime — nothing is hand-tuned.

1. **Vanilla damage curve.** For vanilla player spells, take the primary *damage* effect
   (a hostile effect that modifies **Health** — so illusion "fear level", magicka drain and
   other non-damage magnitudes are never touched) and record its magnitude by
   **(archetype, tier)**. Archetype = concentration / instant / damage-over-time; tier =
   Novice…Master from the effect's minimum-skill. Vanilla damage depends on tier+archetype,
   **not element** (Flames = Frostbite = Sparks), so the curve pools across schools. Each
   5-tier vector is monotone-smoothed; cells with too few samples are interpolated.

2. **Per-spell damage pin.** Each spell's primary damage is set to the vanilla curve value
   for *its own* (tier, archetype), log-blended back toward the author's value by a `VARIETY`
   knob (`0` = pure vanilla, `1` = author untouched, default `0.35`). The same ratio scales
   every damage effect in the spell, preserving its internal structure. Because it's
   per-spell, one over-tuned outlier can't drag its packmates.

3. **Cost follows.**
   - *Fire-and-forget* cost is recomputed with Bethesda's own autocalc formula, fit against
     `Skyrim.esm` (median error 0%): `cost = Σ baseCost · max(mag,1)^1.1 · max(dur/10, 1)`.
   - *Concentration* cost uses vanilla's per-second economy (`cost ≈ 2.1 × damage/sec`, the
     Flames/Frostbite/Sparks ratio) — the mod's own effect base-cost is ignored because it's
     frequently inflated.
   - A soft per-(tier, archetype) ceiling from vanilla guards the rare tail so nothing becomes
     an uncastable spell.

Because damage is now vanilla-scale, cost lands in vanilla range on its own — there are no
arbitrary caps.

### Rank, cost and damage are solved together

Damage is pinned on **delivered damage**, which is archetype-correct: concentration magnitude is a
per-second *rate* (its cost is per-second too), everything else delivers `magnitude x duration`, summed
over all damage effects so riders count. That one quantity is stable whether a baseline scales its
spells by magnitude (vanilla) or by duration (Mysticism scales DoTs this way — a master DoT ticks the
same but lasts longer).

Magicka cost is then **solved from the same relation**, not derived afterwards from the mod's own
effect base-costs: the tool fits `E(rank, archetype)` = damage-per-magicka from the baseline's winning
costs, and sets `cost = delivered_damage / E`. DoTs additionally pay a delivery-speed factor, because
two spells with the same total damage are not equal value if one front-loads it.

> **Caveat — damage-over-time at high rank.** Vanilla contains *no* Adept-or-above DoT damage spells
> (sample counts print as `dot: [26, 7, 0, 0, 0]`), and most overhauls add few. Those cells are
> therefore derived rather than measured, and DoT-heavy packs can swing hard on that extrapolation.
> Check the printed per-tier sample counts before trusting a DoT-heavy result, and prefer `--dry`.

## Knobs (100 = vanilla)

Edit the CONFIG block at the top of `spell_balance.py`, or pass on the CLI:

| knob | meaning |
|---|---|
| `OVERALL` / `--overall` | global multiplier on cost and damage |
| `TIER_COST[tier]` | per-tier (Novice…Master) magicka-cost multiplier |
| `TIER_MAG[tier]`  | per-tier damage multiplier |
| `VARIETY` / `--variety` | 0 = pure vanilla damage, 1 = author's damage untouched |

Effective mult = `OVERALL/100 × TIER_x[tier]/100`. Re-run any time; each run reads the
original plugin fresh, so tuning never compounds.

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

## License

MIT — see `LICENSE`. Not affiliated with Bethesda or any mod author; it only edits plugins you
already own, on your own machine.
