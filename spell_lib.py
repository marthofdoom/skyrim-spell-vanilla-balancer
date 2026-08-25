#!/usr/bin/env python3
"""Headless SPEL/MGEF/BOOK reader+writer for Skyrim SE plugins (no xEdit).

Core model: a plugin is TES4 + a sequence of top-level GRUPs. Records may be
zlib-compressed (record flag 0x00040000). We expose:
  - masters(buf)                     -> [master filenames]
  - iter_records(buf, {b'SPEL',...}) -> yields RecRef(type, formid, flags, hdr_off,
                                        data_off, data_end, comp) with DECOMPRESSED .data
  - parse_spel(data) / parse_mgef(data)
Editing strategy (see spell_balance.py): SPIT cost/flags and EFIT magnitude/area/
duration are FIXED-WIDTH. For UNCOMPRESSED records we overwrite those bytes in the
original file buffer in place (no size change -> no group/size fixups needed). We
refuse to in-place-edit compressed records and report them so the caller can decide.
"""
import struct, zlib

COMP = 0x00040000

def read_subrecords(data):
    """Yield (type, bytes, abs_offset_of_subrecord_data) for each subrecord.
    abs offset is relative to the start of `data` (the decompressed record body)."""
    i = 0; n = len(data); pending = None
    while i + 6 <= n:
        stype = data[i:i+4]
        ssize = struct.unpack_from('<H', data, i+4)[0]
        i += 6
        if stype == b'XXXX':
            pending = struct.unpack_from('<I', data, i)[0]; i += ssize; continue
        real = pending if pending is not None else ssize
        pending = None
        yield stype, data[i:i+real], i
        i += real

def masters(buf):
    assert buf[0:4] == b'TES4'
    size = struct.unpack_from('<I', buf, 4)[0]
    out = []
    for st, sd, _ in read_subrecords(buf[24:24+size]):
        if st == b'MAST':
            out.append(sd.split(b'\x00',1)[0].decode('cp1252'))
    return out

class RecRef:
    __slots__ = ('type','formid','flags','hdr_off','data_off','data_end','comp','data')
    def __init__(s, type, formid, flags, hdr_off, data_off, data_end, comp, data):
        s.type=type; s.formid=formid; s.flags=flags; s.hdr_off=hdr_off
        s.data_off=data_off; s.data_end=data_end; s.comp=comp; s.data=data

def iter_records(buf, want):
    """Recursively walk all GRUPs; yield RecRef for records whose type in `want`.
    .data is decompressed. data_off/data_end index the RAW (possibly compressed)
    body in `buf`."""
    n = len(buf)
    assert buf[0:4] == b'TES4'
    tes4_size = struct.unpack_from('<I', buf, 4)[0]
    start = 24 + tes4_size
    stack = [(start, n)]
    while stack:
        pos, end = stack.pop()
        p = pos
        while p + 24 <= end:
            rtype = buf[p:p+4]
            if rtype == b'GRUP':
                gsize = struct.unpack_from('<I', buf, p+4)[0]
                if gsize < 24: break
                stack.append((p+24, p+gsize))
                p += gsize
            else:
                dsize = struct.unpack_from('<I', buf, p+4)[0]
                rflags = struct.unpack_from('<I', buf, p+8)[0]
                formid = struct.unpack_from('<I', buf, p+12)[0]
                data_off = p+24; data_end = p+24+dsize
                if rtype in want:
                    raw = buf[data_off:data_end]
                    comp = bool(rflags & COMP)
                    data = zlib.decompress(raw[4:]) if comp else raw
                    yield RecRef(rtype, formid, rflags, p, data_off, data_end, comp, data)
                p = data_end

def iter_top_records(buf, want):
    """Fast path: only descend into TOP-LEVEL GRUPs whose label is a wanted record
    signature. SPEL/MGEF/BOOK are never nested, so this skips CELL/WRLD entirely."""
    n = len(buf)
    tes4_size = struct.unpack_from('<I', buf, 4)[0]
    p = 24 + tes4_size
    while p + 24 <= n:
        assert buf[p:p+4] == b'GRUP', buf[p:p+4]
        gsize = struct.unpack_from('<I', buf, p+4)[0]
        gtype = struct.unpack_from('<i', buf, p+12)[0]  # group type; 0 = top
        label = bytes(buf[p+8:p+12])
        if gtype == 0 and label in want:
            q = p + 24; gend = p + gsize
            while q + 24 <= gend:
                rt = buf[q:q+4]
                if rt == b'GRUP':
                    gs = struct.unpack_from('<I', buf, q+4)[0]; q += gs; continue
                dsize = struct.unpack_from('<I', buf, q+4)[0]
                rflags = struct.unpack_from('<I', buf, q+8)[0]
                formid = struct.unpack_from('<I', buf, q+12)[0]
                raw = buf[q+24:q+24+dsize]
                comp = bool(rflags & COMP)
                data = zlib.decompress(raw[4:]) if comp else raw
                yield RecRef(rt, formid, rflags, q, q+24, q+24+dsize, comp, data)
                q += 24 + dsize
        p += gsize

# ---- SPEL ----
# SPIT (Skyrim SE, 36 bytes): 0 cost(u32) 4 flags(u32) 8 type(u32) 12 chargeTime(f32)
# 16 castType(u32) 20 delivery(u32) 24 castDuration(f32) 28 range(f32) 32 halfCostPerk(fid)
SPIT_FLAG_MANUAL_COST = 0x00000001
def parse_spel(data):
    """Return dict: edid, spit_off (abs in data), cost, flags, type, castType,
    delivery, effects=[{mgef, mag, area, dur, efit_off}]. Offsets are into `data`."""
    d = {'edid':None,'spit_off':None,'cost':None,'flags':None,'type':None,
         'castType':None,'delivery':None,'effects':[]}
    cur = None
    for st, sd, off in read_subrecords(data):
        if st == b'EDID':
            d['edid'] = sd.split(b'\x00',1)[0].decode('cp1252','replace')
        elif st == b'SPIT' and len(sd) >= 36:
            d['spit_off'] = off
            (d['cost'], d['flags'], d['type'], _ct, d['castType'],
             d['delivery']) = struct.unpack_from('<IIIfII', sd, 0)
        elif st == b'EFID' and len(sd) == 4:
            cur = {'mgef': struct.unpack_from('<I', sd, 0)[0]}
        elif st == b'EFIT' and len(sd) >= 12:
            mag, area, dur = struct.unpack_from('<fII', sd, 0)
            if cur is None: cur = {'mgef': None}
            cur.update(mag=mag, area=area, dur=dur, efit_off=off)
            d['effects'].append(cur); cur = None
    return d

# ---- MGEF ----
# DATA is large (SSE ~152 bytes). We derive offsets empirically (find_mgef_offsets).
MGEF_HOSTILE = 0x00000001  # flag bit 0 in MGEF DATA flags
# Condition functions that gate a damage effect OFF in the base, general case. Engine-level
# function indices (language- and mod-independent):
#   560=HasKeyword, 69=GetIsRace, 71=GetInFaction -- "the victim IS an X": sun/anti-undead lines.
#     Niche damage; it prices a situational tool, not the general economy.
#   448=HasPerk -- a rider that only exists once a specific perk is taken (Adamant's Burning
#     Light). Not the spell's own base damage.
# Only equality-required conditions (cmp '==', value 1.0) count: an EXCLUSION like "victim is not
# a machine" (Mysticism's frost line) leaves the damage general and is ignored here.
_VICTIM_CLASS_FN = {560, 69, 71, 448}
def parse_mgef_raw(data):
    edid=None; datab=None; vcond=False
    for st, sd, off in read_subrecords(data):
        if st == b'EDID': edid = sd.split(b'\x00',1)[0].decode('cp1252','replace')
        elif st == b'DATA': datab = sd
        elif st == b'CTDA' and len(sd) >= 12:
            op = sd[0] >> 5                              # comparison: 0 is '=='
            fn = struct.unpack_from('<H', sd, 8)[0]
            val = struct.unpack_from('<f', sd, 4)[0]
            if fn in _VICTIM_CLASS_FN and op == 0 and val == 1.0:
                vcond = True
    return edid, datab, vcond

# MGEF DATA offsets (SSE, 152-byte DATA) — derived empirically from Skyrim.esm:
MGEF_OFF_FLAGS=0; MGEF_OFF_BASECOST=4; MGEF_OFF_SKILL=12; MGEF_OFF_MINSKILL=40
MGEF_OFF_AV=68   # actor value the effect modifies (24=Health,25=Magicka,26=Stamina); -1=none
AV_HEALTH=24
def read_mgef_map(buf):
    """{full_formid: {edid,flags,basecost,school,minskill}} for all MGEF in a plugin."""
    out={}
    for r in iter_top_records(buf,{b'MGEF'}):
        ed,db,vcond=parse_mgef_raw(r.data)
        if db is None or len(db)<44: continue
        out[r.formid]={'edid':ed,'cond':vcond,
            'assoc':struct.unpack_from('<I',db,8)[0] if len(db)>=12 else 0,
            'flags':struct.unpack_from('<I',db,MGEF_OFF_FLAGS)[0],
            'basecost':struct.unpack_from('<f',db,MGEF_OFF_BASECOST)[0],
            'school':struct.unpack_from('<i',db,MGEF_OFF_SKILL)[0],
            'minskill':struct.unpack_from('<i',db,MGEF_OFF_MINSKILL)[0],
            'av':struct.unpack_from('<i',db,MGEF_OFF_AV)[0] if len(db)>=MGEF_OFF_AV+4 else -1}
    return out
# ---- structural "not a player-cast spell" markers (verified empirically on Skyrim.esm) ----
# BOOK DATA (16 bytes): 0 flags(u32) 4 teaches(formid|skill) 8 value 12 weight. When the book is a
# spell tome, offset 4 holds the taught SPEL. 93 of Skyrim.esm's 821 BOOKs hit a SPEL formid there
# and they are exactly the spell tomes -- no trap/hazard/creature spell has one.
def read_tome_spells(buf):
    """FormIDs taught by any BOOK in the plugin (spell tomes). The vanilla design prices exactly
    the spells it sells -- this is the structural marker for 'a spell the player buys and casts'."""
    out=set()
    for r in iter_top_records(buf,{b'BOOK'}):
        for st,sd,off in read_subrecords(r.data):
            if st==b'DATA' and len(sd)>=8:
                out.add(struct.unpack_from('<I',sd,4)[0])
    return out

# HAZD DATA (40 bytes): offset 24 = the SPEL the hazard applies to actors inside it (verified: all
# 18 spell hits in Skyrim.esm's 44 HAZD records sit at that offset -- Hazard*, TrapFirePlate*, oil).
def read_hazard_payloads(buf):
    """FormIDs of spells applied BY a hazard field (walls, blizzards, gas clouds, fire plates).
    Nobody casts these and their magicka cost is a token the engine never charges."""
    out=set()
    for r in iter_top_records(buf,{b'HAZD'}):
        for st,sd,off in read_subrecords(r.data):
            if st==b'DATA' and len(sd)>=28:
                out.add(struct.unpack_from('<I',sd,24)[0])
    return out

# MGEF DATA offset 8 = associated item. For Cloak-archetype effects it is the SPEL the cloak
# applies to nearby targets (verified: 13 hits in Skyrim.esm, all *CloakDmg / charge payloads).
# For other archetypes it holds non-SPEL forms (summons' NPC_, bound WEAP) -- harmless to collect,
# a non-SPEL formid never matches a spell record.
def read_hazd_ids(buf):
    """FormIDs of the HAZD records themselves. An MGEF whose associated item is one of these is
    a hazard-SPAWNING effect (walls, blizzards): the spell's visible magnitude is only the spray
    tip and the real damage lives in the hazard payload -- unmeasurable from the SPEL record."""
    return {r.formid for r in iter_top_records(buf,{b'HAZD'})}

def read_mgef_assoc(buf):
    """Associated-item FormIDs of every MGEF in the plugin (cloak damage payloads and the like)."""
    out=set()
    for r in iter_top_records(buf,{b'MGEF'}):
        ed,db,vcond=parse_mgef_raw(r.data)
        if db is not None and len(db)>=12:
            out.add(struct.unpack_from('<I',db,8)[0])
    return out

def is_damage(m):
    """True if MGEF m is a hostile Health-modifying (damage) effect that the engine
    actually PRICES.

    basecost == 0 is the discriminator for riders. Vanilla's PerkDisintegrate* effects are
    hostile Health effects with magnitude 200 and duration 1, but base cost 0 -- they are the
    shock perk's "disintegrate below 10% health" flag, not damage. Counting them wrecks the
    model twice over: Sparks reads 208 damage instead of 8, and every shock spell (Sparks,
    Lightning Bolt, Chain Lightning, Thunderbolt, Wall of Storms, Lightning Storm) is
    misclassified as a damage-over-time spell because of the rider's duration. By the engine's
    own cost formula a zero-base-cost effect contributes nothing, so it is not damage here
    either. Mod-added script/flag riders follow the same convention.
    """
    return bool(m) and (m['flags'] & MGEF_HOSTILE) and m['av']==AV_HEALTH and m['basecost']>0

SCHOOL_AV = {18:'Alteration',19:'Conjuration',20:'Destruction',21:'Illusion',22:'Restoration'}
def tier_of(min_skill):
    if min_skill is None: return None
    if min_skill >= 100: return 'Master'
    if min_skill >= 75:  return 'Expert'
    if min_skill >= 50:  return 'Adept'
    if min_skill >= 25:  return 'Apprentice'
    return 'Novice'
