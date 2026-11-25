"""Maintainer tool: compile local clones of PokeAPI, vsoch/pokemon and PokeAPI/sprites into the bundled SQLite file.

End users never run this; the compiled `pokebot/data/pokedex.sqlite` ships with the package.
Requires Pillow (`pip install -e ".[build]"`) to convert the sprite artwork.

    git clone --depth 1 https://github.com/PokeAPI/pokeapi.git ../pokeapi
    git clone --depth 1 https://github.com/vsoch/pokemon.git ../vsoch-pokemon
    git clone --depth 1 --filter=blob:none --sparse https://github.com/PokeAPI/sprites.git ../sprites
    git -C ../sprites sparse-checkout set sprites/pokemon/other/official-artwork sprites/pokemon/back
    python -m pokebot.build_db ../pokeapi ../vsoch-pokemon ../sprites

The sprites clone is optional; without it, newer species and alternate forms have no artwork.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import re
import sqlite3
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from pokebot import config

STAT_COLUMNS = {1: "hp", 2: "attack", 3: "defense", 4: "sp_attack", 5: "sp_defense", 6: "speed"}
STAT_IDS = {1: "hp", 2: "attack", 3: "defense", 4: "sp_attack", 5: "sp_defense", 6: "speed", 7: "accuracy", 8: "evasion"}
AILMENTS = {1: "paralysis", 2: "sleep", 3: "freeze", 4: "burn", 5: "poison", 6: "confusion"}
LEARN_METHODS = {"level-up", "egg", "tutor", "machine"}

MARKUP = re.compile(r"\[([^\]]*)\]\{[a-z\-]+:([a-z0-9\-]+)\}")
TWO_TURN = re.compile(
    r"requires a turn to charge|charges for one turn|charges up|flies high|digs underground|"
    r"dives underwater|springs up|vanishes|disappears|jumps up|on the next turn|hits next turn|drops it next turn|"
    r"takes one turn to charge",
    re.I,
)
CONDITIONAL = re.compile(
    r"can only be used|only works|fails if|two turns later|only inflicts damage if|is canceled|"
    r"power doubles every turn|this move fails",
    re.I,
)
# Spin-offs and remakes whose learnsets differ from the main series; used only if nothing else exists.
SIDE_VERSION_GROUPS = {
    "legends-arceus", "legends-za", "mega-dimension", "champions", "red-green-japan", "blue-japan",
    "colosseum", "xd", "lets-go-pikachu-lets-go-eevee",
}


def pretty(identifier: str) -> str:
    return identifier.replace("-", " ").title()


def clean(text: str | None, chance: str | None = None) -> str:
    if not text:
        return ""
    text = MARKUP.sub(lambda m: m.group(1) or pretty(m.group(2)), text)
    if chance:
        text = text.replace("$effect_chance", chance)
    text = text.replace("\f", " ").replace("\u00ad", "").replace("\u2019", "'")
    return re.sub(r"\s+", " ", text).strip()


def csv_reader(csv_dir: Path):
    def read(name: str) -> list[dict[str, str]]:
        with open(csv_dir / f"{name}.csv", newline="", encoding="utf-8") as f:
            return list(csv.DictReader(f))
    return read


def english(rows, key: str, field: str = "name") -> dict[int, str]:
    return {int(r[key]): r[field] for r in rows if r.get("local_language_id", r.get("language_id")) == str(config.ENGLISH)}


def latest_english_text(rows, key: str, order_of) -> dict[int, str]:
    """Pick the newest English flavor text per object; `order_of(row)` gives a sortable recency."""
    best: dict[int, tuple] = {}
    for r in rows:
        if r.get("language_id") != str(config.ENGLISH):
            continue
        k, o = int(r[key]), order_of(r)
        if k not in best or o > best[k][0]:
            best[k] = (o, clean(r["flavor_text"]))
    return {k: v[1] for k, v in best.items()}


def num(value: str):
    return int(value) if value not in ("", None) else None


SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE types (id INTEGER PRIMARY KEY, identifier TEXT, name TEXT);
CREATE TABLE type_efficacy (attacker INTEGER, defender INTEGER, factor REAL);
CREATE TABLE species (
    id INTEGER PRIMARY KEY, identifier TEXT, name TEXT, genus TEXT, generation INTEGER,
    evolves_from INTEGER, evolution_chain INTEGER, color TEXT, habitat TEXT, shape TEXT,
    gender_rate INTEGER, capture_rate INTEGER, base_happiness INTEGER, growth_rate TEXT,
    is_baby INTEGER, is_legendary INTEGER, is_mythical INTEGER, flavor_text TEXT, evolution_condition TEXT
);
CREATE TABLE pokemon (
    id INTEGER PRIMARY KEY, identifier TEXT, name TEXT, species_id INTEGER, is_default INTEGER,
    height REAL, weight REAL, base_experience INTEGER, type1 TEXT, type2 TEXT,
    hp INTEGER, attack INTEGER, defense INTEGER, sp_attack INTEGER, sp_defense INTEGER, speed INTEGER, total INTEGER
);
CREATE TABLE abilities (
    id INTEGER PRIMARY KEY, identifier TEXT, name TEXT, generation INTEGER, short_effect TEXT, effect TEXT
);
CREATE TABLE pokemon_abilities (pokemon_id INTEGER, ability_id INTEGER, is_hidden INTEGER, slot INTEGER);
CREATE TABLE moves (
    id INTEGER PRIMARY KEY, identifier TEXT, name TEXT, generation INTEGER, type TEXT, power INTEGER,
    pp INTEGER, accuracy INTEGER, priority INTEGER, target_id INTEGER, damage_class TEXT,
    effect_id INTEGER, effect_chance INTEGER, short_effect TEXT, effect TEXT,
    meta_category INTEGER, ailment TEXT, ailment_chance INTEGER, min_hits INTEGER, max_hits INTEGER,
    drain INTEGER, healing INTEGER, crit_rate INTEGER, flinch_chance INTEGER, stat_chance INTEGER,
    flags TEXT
);
CREATE TABLE move_stat_changes (move_id INTEGER, stat TEXT, change INTEGER);
CREATE TABLE pokemon_moves (pokemon_id INTEGER, move_id INTEGER, method TEXT, level INTEGER);
CREATE TABLE items (
    id INTEGER PRIMARY KEY, identifier TEXT, name TEXT, category TEXT, cost INTEGER, fling_power INTEGER,
    short_effect TEXT, effect TEXT
);
CREATE TABLE ascii (pokemon_id INTEGER PRIMARY KEY, art TEXT);
CREATE TABLE sprites (pokemon_id INTEGER PRIMARY KEY, webp BLOB, width INTEGER, height INTEGER);
CREATE TABLE back_sprites (pokemon_id INTEGER PRIMARY KEY, webp BLOB, width INTEGER, height INTEGER);
CREATE INDEX idx_pm_pokemon ON pokemon_moves(pokemon_id);
CREATE INDEX idx_pm_move ON pokemon_moves(move_id);
CREATE INDEX idx_pa_ability ON pokemon_abilities(ability_id);
CREATE INDEX idx_pokemon_species ON pokemon(species_id);
"""


def describe_evolution(r: dict, items: dict, moves: dict, types: dict, species: dict, locations: dict) -> str:
    trigger = r["_trigger"]
    parts: list[str] = []
    if trigger == "level-up":
        parts.append(f"level {r['minimum_level']}" if r["minimum_level"] else "level up")
    elif trigger == "use-item":
        parts.append(f"use {items.get(num(r['trigger_item_id']), 'an item')}")
    elif trigger == "trade":
        parts.append("trade")
    else:
        parts.append(pretty(trigger))
    if r["held_item_id"]:
        parts.append(f"holding {items.get(num(r['held_item_id']), 'an item')}")
    if r["known_move_id"]:
        parts.append(f"knowing {moves.get(num(r['known_move_id']), 'a move')}")
    if r["known_move_type_id"]:
        parts.append(f"knowing a {types.get(num(r['known_move_type_id']), '?')}-type move")
    if r["minimum_happiness"]:
        parts.append(f"with high friendship ({r['minimum_happiness']}+)")
    if r["minimum_affection"]:
        parts.append(f"with affection {r['minimum_affection']}+")
    if r["minimum_beauty"]:
        parts.append(f"with beauty {r['minimum_beauty']}+")
    if r["time_of_day"]:
        parts.append(f"during the {r['time_of_day']}")
    if r["gender_id"]:
        parts.append({"1": "(female only)", "2": "(male only)"}.get(r["gender_id"], ""))
    if r["location_id"]:
        parts.append(f"at {locations.get(num(r['location_id']), 'a special location')}")
    if r["relative_physical_stats"]:
        parts.append({"1": "if Attack > Defense", "0": "if Attack = Defense", "-1": "if Attack < Defense"}[r["relative_physical_stats"]])
    if r["party_species_id"]:
        parts.append(f"with {species.get(num(r['party_species_id']), '?')} in the party")
    if r["party_type_id"]:
        parts.append(f"with a {types.get(num(r['party_type_id']), '?')}-type in the party")
    if r["trade_species_id"]:
        parts.append(f"for {species.get(num(r['trade_species_id']), '?')}")
    if r["needs_overworld_rain"] == "1":
        parts.append("while raining")
    if r["turn_upside_down"] == "1":
        parts.append("holding the console upside down")
    return " ".join(p for p in parts if p)


def transparent_sprite(path: Path, max_side: int = 256) -> tuple[bytes, int, int]:
    """Shrink a white-background artwork and flood-fill the background to transparent (WebP)."""
    from PIL import Image, ImageChops, ImageDraw

    key = (255, 0, 255)
    im = Image.open(path).convert("RGB")
    im.thumbnail((max_side, max_side), Image.LANCZOS)
    w, h = im.size
    padded = Image.new("RGB", (w + 2, h + 2), (255, 255, 255))
    padded.paste(im, (1, 1))
    ImageDraw.floodfill(padded, (0, 0), key, thresh=40)
    r, g, b = padded.split()
    is_key = ImageChops.multiply(
        ImageChops.multiply(r.point(lambda v: 255 if v == key[0] else 0), g.point(lambda v: 255 if v == key[1] else 0)),
        b.point(lambda v: 255 if v == key[2] else 0),
    )
    alpha = ImageChops.invert(is_key).crop((1, 1, w + 1, h + 1))
    out = im.convert("RGBA")
    out.putalpha(alpha)
    buf = io.BytesIO()
    out.save(buf, "WEBP", quality=82, method=6)
    return buf.getvalue(), w, h


def artwork_sprite(path: Path, max_side: int = 256) -> tuple[bytes, int, int]:
    """Shrink a transparent PNG artwork (PokeAPI/sprites) to WebP."""
    from PIL import Image

    im = Image.open(path).convert("RGBA")
    im.thumbnail((max_side, max_side), Image.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, "WEBP", quality=80, method=6)
    return buf.getvalue(), *im.size


def back_sprite(path: Path) -> tuple[bytes, int, int]:
    """A 96 px pixel-art back sprite, cropped to its visible pixels and kept lossless."""
    from PIL import Image

    im = Image.open(path).convert("RGBA")
    im = im.crop(im.getchannel("A").getbbox() or (0, 0, *im.size))
    buf = io.BytesIO()
    im.save(buf, "WEBP", lossless=True)
    return buf.getvalue(), *im.size


VSOCH_CHARS = "#?%.S+.*:,@"


def ascii_art(path: Path, width: int = 30) -> str:
    """ASCII art in the same style as vsoch/pokemon (its convert.py), for Pokémon it doesn't cover."""
    from PIL import Image

    im = Image.open(path).convert("RGBA")
    white = Image.new("RGBA", im.size, (255, 255, 255, 255))
    gray = Image.alpha_composite(white, im).convert("L")
    rows = int(gray.height / gray.width * width)
    gray = gray.resize((width * 2, rows))
    chars = [VSOCH_CHARS[v // 25] for v in gray.tobytes()]
    return "\n".join("".join(chars[i:i + width * 2]) for i in range(0, len(chars), width * 2))


def build(pokeapi_dir: Path, vsoch_dir: Path, sprites_dir: Path | None = None, db_path: Path = config.DB_PATH) -> None:
    t0 = time.time()
    read = csv_reader(pokeapi_dir / "data" / "v2" / "csv")
    art_path = vsoch_dir / "pokemon" / "database" / "pokemons.json"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = db_path.with_suffix(".tmp")
    tmp.unlink(missing_ok=True)
    con = sqlite3.connect(tmp)
    con.executescript(SCHEMA)

    vg_order = {int(r["id"]): int(r["order"]) for r in read("version_groups")}
    version_order = {int(r["id"]): (vg_order[int(r["version_group_id"])], int(r["id"])) for r in read("versions")}
    generation_of_vg = {int(r["id"]): int(r["generation_id"]) for r in read("version_groups")}

    # Types
    type_ident = {int(r["id"]): r["identifier"] for r in read("types")}
    type_names = english(read("type_names"), "type_id")
    real_types = {tid: ident for tid, ident in type_ident.items() if tid < 10000 and ident not in ("unknown", "shadow", "stellar")}
    con.executemany("INSERT INTO types VALUES (?,?,?)", [(t, i, type_names.get(t, pretty(i))) for t, i in real_types.items()])
    con.executemany(
        "INSERT INTO type_efficacy VALUES (?,?,?)",
        [(int(r["damage_type_id"]), int(r["target_type_id"]), int(r["damage_factor"]) / 100) for r in read("type_efficacy")],
    )

    # Names used across tables
    item_names = english(read("item_names"), "item_id")
    move_names = english(read("move_names"), "move_id")
    species_names = english(read("pokemon_species_names"), "pokemon_species_id")
    genus = english(read("pokemon_species_names"), "pokemon_species_id", "genus")
    location_names = english(read("location_names"), "location_id")
    for r in read("locations"):
        location_names.setdefault(int(r["id"]), pretty(r["identifier"]))

    # Evolutions
    triggers = {r["id"]: r["identifier"] for r in read("evolution_triggers")}
    evo_text: dict[int, list[str]] = defaultdict(list)
    for r in read("pokemon_evolution"):
        r["_trigger"] = triggers.get(r["evolution_trigger_id"], "level-up")
        text = describe_evolution(r, item_names, move_names, type_names, species_names, location_names)
        sid = int(r["evolved_species_id"])
        if text not in evo_text[sid]:
            evo_text[sid].append(text)

    # Species
    colors = {r["id"]: r["identifier"] for r in read("pokemon_colors")}
    habitats = {r["id"]: r["identifier"] for r in read("pokemon_habitats")}
    shapes = {r["id"]: r["identifier"] for r in read("pokemon_shapes")}
    growth = {r["id"]: r["identifier"] for r in read("growth_rates")}
    flavor = latest_english_text(read("pokemon_species_flavor_text"), "species_id", lambda r: version_order.get(int(r["version_id"]), (0, 0)))
    species_rows = []
    for r in read("pokemon_species"):
        sid = int(r["id"])
        species_rows.append((
            sid, r["identifier"], species_names.get(sid, pretty(r["identifier"])), genus.get(sid, ""),
            int(r["generation_id"]), num(r["evolves_from_species_id"]), num(r["evolution_chain_id"]),
            colors.get(r["color_id"]), habitats.get(r["habitat_id"]), shapes.get(r["shape_id"]),
            num(r["gender_rate"]), num(r["capture_rate"]), num(r["base_happiness"]), growth.get(r["growth_rate_id"]),
            int(r["is_baby"]), int(r["is_legendary"]), int(r["is_mythical"]), flavor.get(sid, ""),
            "; or ".join(evo_text.get(sid, [])) or None,
        ))
    con.executemany(f"INSERT INTO species VALUES ({','.join('?' * 19)})", species_rows)

    # Pokémon (includes alternate forms such as megas and regional variants)
    form_names = english(read("pokemon_form_names"), "pokemon_form_id", "pokemon_name")
    pokemon_form_name: dict[int, str] = {}
    for r in read("pokemon_forms"):
        if r["is_default"] == "1" and form_names.get(int(r["id"])):
            pokemon_form_name[int(r["pokemon_id"])] = form_names[int(r["id"])]
    types_of: dict[int, list[str]] = defaultdict(list)
    for r in sorted(read("pokemon_types"), key=lambda r: int(r["slot"])):
        types_of[int(r["pokemon_id"])].append(type_ident[int(r["type_id"])])
    stats_of: dict[int, dict[str, int]] = defaultdict(dict)
    for r in read("pokemon_stats"):
        if int(r["stat_id"]) in STAT_COLUMNS:
            stats_of[int(r["pokemon_id"])][STAT_COLUMNS[int(r["stat_id"])]] = int(r["base_stat"])
    pokemon_rows = []
    for r in read("pokemon"):
        pid, sid = int(r["id"]), int(r["species_id"])
        if r["is_default"] == "1":
            name = species_names.get(sid, pretty(r["identifier"]))
        else:
            name = pokemon_form_name.get(pid) or pretty(r["identifier"])
        st = stats_of[pid]
        t = types_of[pid] + [None]
        pokemon_rows.append((
            pid, r["identifier"], name, sid, int(r["is_default"]), int(r["height"]) / 10, int(r["weight"]) / 10,
            num(r["base_experience"]), t[0], t[1], *(st.get(c, 0) for c in STAT_COLUMNS.values()), sum(st.values()),
        ))
    con.executemany(f"INSERT INTO pokemon VALUES ({','.join('?' * 17)})", pokemon_rows)

    # Abilities
    ability_names = english(read("ability_names"), "ability_id")
    ability_prose = {int(r["ability_id"]): r for r in read("ability_prose") if r["local_language_id"] == str(config.ENGLISH)}
    ability_flavor = latest_english_text(read("ability_flavor_text"), "ability_id", lambda r: vg_order.get(int(r["version_group_id"]), 0))
    ability_rows = []
    for r in read("abilities"):
        aid = int(r["id"])
        if r["is_main_series"] != "1":
            continue
        prose = ability_prose.get(aid, {})
        short = clean(prose.get("short_effect")) or ability_flavor.get(aid, "")
        ability_rows.append((aid, r["identifier"], ability_names.get(aid, pretty(r["identifier"])), int(r["generation_id"]), short, clean(prose.get("effect")) or short))
    con.executemany("INSERT INTO abilities VALUES (?,?,?,?,?,?)", ability_rows)
    con.executemany(
        "INSERT INTO pokemon_abilities VALUES (?,?,?,?)",
        [(int(r["pokemon_id"]), int(r["ability_id"]), int(r["is_hidden"]), int(r["slot"])) for r in read("pokemon_abilities")],
    )

    # Moves
    damage_classes = {r["id"]: r["identifier"] for r in read("move_damage_classes")}
    effect_prose = {int(r["move_effect_id"]): r for r in read("move_effect_prose") if r["local_language_id"] == str(config.ENGLISH)}
    move_flavor = latest_english_text(read("move_flavor_text"), "move_id", lambda r: vg_order.get(int(r["version_group_id"]), 0))
    meta = {int(r["move_id"]): r for r in read("move_meta")}
    move_rows = []
    for r in read("moves"):
        mid = int(r["id"])
        if mid >= 10000 or int(r["type_id"]) not in real_types:
            continue
        prose = effect_prose.get(num(r["effect_id"]) or -1, {})
        short = clean(prose.get("short_effect"), r["effect_chance"]) or move_flavor.get(mid, "")
        m = meta.get(mid, {})
        flags = set()
        lowered = short.lower()
        if "recharge" in lowered:
            flags.add("recharge")
        if TWO_TURN.search(short) and "two turns later" not in lowered:
            flags.add("two_turn")
        if re.search(r"(^|\. )user faints", lowered):
            flags.add("self_ko")
        if "one-hit ko" in lowered:
            flags.add("ohko")
        if "damage equal to the user's level" in lowered:
            flags.add("level_damage")
        if CONDITIONAL.search(short):
            flags.add("conditional")
        move_rows.append((
            mid, r["identifier"], move_names.get(mid, pretty(r["identifier"])), int(r["generation_id"]),
            type_ident[int(r["type_id"])], num(r["power"]), num(r["pp"]), num(r["accuracy"]), int(r["priority"]),
            num(r["target_id"]), damage_classes.get(r["damage_class_id"]), num(r["effect_id"]), num(r["effect_chance"]),
            short, clean(prose.get("effect"), r["effect_chance"]) or short,
            num(m.get("meta_category_id", "")), AILMENTS.get(num(m.get("meta_ailment_id", "")) or 0),
            num(m.get("ailment_chance", "")), num(m.get("min_hits", "")), num(m.get("max_hits", "")),
            num(m.get("drain", "")), num(m.get("healing", "")), num(m.get("crit_rate", "")),
            num(m.get("flinch_chance", "")), num(m.get("stat_chance", "")), ",".join(sorted(flags)),
        ))
    con.executemany(f"INSERT INTO moves VALUES ({','.join('?' * 26)})", move_rows)
    con.executemany(
        "INSERT INTO move_stat_changes VALUES (?,?,?)",
        [(int(r["move_id"]), STAT_IDS[int(r["stat_id"])], int(r["change"])) for r in read("move_meta_stat_changes") if int(r["stat_id"]) in STAT_IDS],
    )

    # Learnsets: the newest main-series generation each Pokémon appears in (base game + DLC merged)
    vg_ident = {int(r["id"]): r["identifier"] for r in read("version_groups")}
    methods = {r["id"]: r["identifier"] for r in read("pokemon_move_methods")}
    by_pokemon: dict[int, dict[int, list]] = defaultdict(lambda: defaultdict(list))
    for r in read("pokemon_moves"):
        method = methods.get(r["pokemon_move_method_id"])
        if method in LEARN_METHODS:
            by_pokemon[int(r["pokemon_id"])][int(r["version_group_id"])].append((int(r["move_id"]), method, num(r["level"])))
    learn_rows = []
    for pid, groups in by_pokemon.items():
        main = [vg for vg in groups if vg_ident.get(vg) not in SIDE_VERSION_GROUPS] or list(groups)
        latest_gen = max(generation_of_vg[vg] for vg in main)
        chosen = sorted((vg for vg in main if generation_of_vg[vg] == latest_gen), key=lambda vg: vg_order.get(vg, 0), reverse=True)
        seen = set()
        for vg in chosen:
            for move_id, method, level in groups[vg]:
                if (move_id, method) not in seen:
                    seen.add((move_id, method))
                    learn_rows.append((pid, move_id, method, level))
    con.executemany("INSERT INTO pokemon_moves VALUES (?,?,?,?)", learn_rows)

    # Items
    categories = {r["id"]: r["identifier"] for r in read("item_categories")}
    item_prose = {int(r["item_id"]): r for r in read("item_prose") if r["local_language_id"] == str(config.ENGLISH)}
    item_flavor = latest_english_text(read("item_flavor_text"), "item_id", lambda r: vg_order.get(int(r["version_group_id"]), 0))
    item_rows = []
    for r in read("items"):
        iid = int(r["id"])
        prose = item_prose.get(iid, {})
        short = clean(prose.get("short_effect")) or item_flavor.get(iid, "")
        item_rows.append((iid, r["identifier"], item_names.get(iid, pretty(r["identifier"])), pretty(categories.get(r["category_id"], "")),
                          num(r["cost"]), num(r["fling_power"]), short, clean(prose.get("effect")) or short))
    con.executemany("INSERT INTO items VALUES (?,?,?,?,?,?,?,?)", item_rows)

    # Artwork, keyed by Pokémon id (a default form's id equals its species id). vsoch/pokemon covers the
    # default forms it knows; PokeAPI/sprites fills in newer species and alternate forms (megas, regionals...).
    with open(art_path, encoding="utf-8") as f:
        art = json.load(f)
    ascii_rows = {int(k): v["ascii"] for k, v in art.items() if v.get("ascii")}
    vsoch_images = {int(p.stem): p for p in (vsoch_dir / "pokemon" / "database" / "images").glob("*.jpg")}
    artwork = sprites_dir / "sprites" / "pokemon" / "other" / "official-artwork" if sprites_dir else None
    backs = sprites_dir / "sprites" / "pokemon" / "back" if sprites_dir else None
    species_of = {row[0]: row[3] for row in pokemon_rows}
    front_jobs: dict[int, tuple] = {}
    for pid, sid in species_of.items():
        if pid in vsoch_images:
            front_jobs[pid] = (transparent_sprite, vsoch_images[pid])
        elif artwork and (artwork / f"{pid}.png").exists():
            front_jobs[pid] = (artwork_sprite, artwork / f"{pid}.png")
    for pid, sid in species_of.items():
        if pid not in front_jobs and sid in front_jobs:
            front_jobs[pid] = front_jobs[sid]  # a form without its own artwork looks like the default form
    back_jobs = {pid: backs / f"{pid}.png" for pid in species_of if backs and (backs / f"{pid}.png").exists()}
    with ThreadPoolExecutor() as pool:
        fronts = list(pool.map(lambda item: (item[0], *item[1][0](item[1][1])), front_jobs.items()))
        back_rows = list(pool.map(lambda item: (item[0], *back_sprite(item[1])), back_jobs.items()))
        missing_art = [pid for pid in front_jobs if pid not in ascii_rows]
        generated = pool.map(lambda pid: ascii_art(front_jobs[pid][1]), missing_art)
        ascii_rows.update(zip(missing_art, generated))
    con.executemany("INSERT INTO ascii VALUES (?,?)", sorted(ascii_rows.items()))
    con.executemany("INSERT INTO sprites VALUES (?,?,?,?)", fronts)
    con.executemany("INSERT INTO back_sprites VALUES (?,?,?,?)", back_rows)

    latest_gen = max(generation_of_vg.values())
    con.executemany("INSERT INTO meta VALUES (?,?)", [
        ("built_at", time.strftime("%Y-%m-%d %H:%M:%S")),
        ("latest_generation", str(latest_gen)),
    ])
    con.commit()
    con.execute("VACUUM")
    con.close()
    tmp.replace(db_path)
    print(f"Built {db_path} in {time.time() - t0:.1f}s: {len(species_rows)} species, {len(pokemon_rows)} Pokémon/forms, "
          f"{len(move_rows)} moves, {len(ability_rows)} abilities, {len(item_rows)} items, "
          f"{len(ascii_rows)} ASCII arts ({len(missing_art)} generated), {len(fronts)} sprites, {len(back_rows)} back sprites.")


def write_licenses(pokeapi_dir: Path, vsoch_dir: Path, sprites_dir: Path | None, dest: Path) -> None:
    pokeapi = (pokeapi_dir / "LICENSE.md").read_text(encoding="utf-8").strip()
    vsoch = (vsoch_dir / "LICENSE").read_text(encoding="utf-8").strip()
    sprites = ""
    if sprites_dir and (sprites_dir / "LICENCE.txt").exists():
        first = (sprites_dir / "LICENCE.txt").read_text(encoding="utf-8").strip().split("\n\n")[:2]
        sprites = "\n\n## PokeAPI/sprites\n\n" + "\n\n".join(first) + "\n\nFull text: https://github.com/PokeAPI/sprites/blob/master/LICENCE.txt\n"
    dest.write_text(
        "# Bundled data licenses\n\n"
        "`pokedex.sqlite` is compiled from the CSV data of https://github.com/PokeAPI/pokeapi, the\n"
        "ASCII art and artwork of https://github.com/vsoch/pokemon and the artwork and back sprites of\n"
        "https://github.com/PokeAPI/sprites. Pokémon and Pokémon character names are trademarks of Nintendo.\n\n"
        f"## PokeAPI\n\n{pokeapi}\n\n## vsoch/pokemon\n\n{vsoch}\n{sprites}",
        encoding="utf-8",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("pokeapi", type=Path, help="local clone of github.com/PokeAPI/pokeapi")
    parser.add_argument("vsoch", type=Path, help="local clone of github.com/vsoch/pokemon")
    parser.add_argument("sprites", type=Path, nargs="?", help="sparse clone of github.com/PokeAPI/sprites (optional)")
    parser.add_argument("--out", type=Path, default=config.DB_PATH)
    args = parser.parse_args(argv)
    sources = [
        (args.pokeapi, args.pokeapi / "data" / "v2" / "csv" / "pokemon.csv", "https://github.com/PokeAPI/pokeapi.git"),
        (args.vsoch, args.vsoch / "pokemon" / "database" / "pokemons.json", "https://github.com/vsoch/pokemon.git"),
    ]
    if args.sprites:
        sources.append((args.sprites, args.sprites / "sprites" / "pokemon" / "other" / "official-artwork" / "1.png",
                        "https://github.com/PokeAPI/sprites.git"))
    missing = [f"  {path}  (clone it with: git clone --depth 1 {url} {path})" for path, marker, url in sources if not marker.exists()]
    if missing:
        parser.exit(1, "Source clone not found:\n" + "\n".join(missing) + "\n")
    build(args.pokeapi, args.vsoch, args.sprites, args.out)
    write_licenses(args.pokeapi, args.vsoch, args.sprites, args.out.parent / "LICENSES.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
