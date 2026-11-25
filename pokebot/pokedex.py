"""Read-only access to the bundled Pokédex database."""

from __future__ import annotations

import difflib
import re
import sqlite3
import unicodedata
from collections import defaultdict
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from pokebot import config

KINDS = ("pokemon", "move", "ability", "item", "type")
STATS = ("hp", "attack", "defense", "sp_attack", "sp_defense", "speed")
STAT_LABELS = {"hp": "HP", "attack": "Atk", "defense": "Def", "sp_attack": "SpA", "sp_defense": "SpD", "speed": "Spe"}

# Single words that are also move/ability/item names but usually appear in questions as plain English.
COMMON_WORDS = {
    "rest", "return", "protect", "attract", "flash", "strength", "simple", "pressure", "stall", "cut",
    "bind", "block", "charge", "counter", "detect", "endure", "present", "rage", "reflect", "refresh",
    "revenge", "round", "struggle", "substitute", "trick", "wish", "wrap", "growth", "teleport", "curse",
    "payback", "facade", "defiant", "unaware", "analytic", "oblivious", "competitive", "honey", "stick",
    "lure", "mail", "run", "hold", "light", "power", "magic", "pain", "split", "heal", "trace", "focus",
}
MOVE_HINTS = ("move", "learn", "know", "teach", "tm ", "attack")

def normalize(text: str) -> str:
    text = text.replace("♀", " f ").replace("♂", " m ")
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    text = re.sub(r"['’.]", "", text.lower())
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


@dataclass(frozen=True)
class Entity:
    kind: str
    id: int
    name: str


class Pokedex:
    def __init__(self, path: Path = config.DB_PATH):
        if not Path(path).exists():
            raise FileNotFoundError(f"Pokédex database not found at {path}. Rebuild it with `python -m pokebot.build_db`.")
        self.con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, check_same_thread=False)
        self.con.row_factory = sqlite3.Row
        self.type_names = {r["identifier"]: r["name"] for r in self.q("SELECT identifier, name FROM types")}
        ids = {r["id"]: r["identifier"] for r in self.q("SELECT id, identifier FROM types")}
        self._efficacy = {
            (ids[r["attacker"]], ids[r["defender"]]): r["factor"]
            for r in self.q("SELECT * FROM type_efficacy")
            if r["attacker"] in ids and r["defender"] in ids
        }
        self._build_index()

    # ------------------------------------------------------------------ helpers
    def q(self, sql: str, *args) -> list[sqlite3.Row]:
        return self.con.execute(sql, args).fetchall()

    def q1(self, sql: str, *args) -> sqlite3.Row | None:
        return self.con.execute(sql, args).fetchone()

    def _build_index(self) -> None:
        self.index: dict[str, list[Entity]] = defaultdict(list)
        self.keys_by_kind: dict[str, list[str]] = defaultdict(list)

        def add(kind: str, id_: int, name: str, *aliases: str) -> None:
            entity = Entity(kind, id_, name)
            for alias in (name, *aliases):
                key = normalize(alias)
                if key and entity not in self.index[key]:
                    self.index[key].append(entity)
                    self.keys_by_kind[kind].append(key)

        for r in self.q("SELECT id, identifier, name, is_default FROM pokemon ORDER BY is_default DESC, id"):
            add("pokemon", r["id"], r["name"], r["identifier"])
        for table, kind in (("moves", "move"), ("abilities", "ability"), ("items", "item")):
            for r in self.q(f"SELECT id, identifier, name FROM {table}"):
                add(kind, r["id"], r["name"], r["identifier"])
        for ident, name in self.type_names.items():
            add("type", 0, name, ident)
        self.type_by_key = {normalize(ident): ident for ident in self.type_names}

    # ------------------------------------------------------------------ lookup
    def resolve(self, text: str, kind: str | None = None) -> Entity | None:
        """Best match for an explicit name, tolerating typos."""
        key = normalize(text)
        if key.isdigit() and kind in (None, "pokemon"):
            # Dex numbers and default-form ids coincide; larger ids are alternate forms.
            row = self.q1("SELECT id, name FROM pokemon WHERE id = ?", int(key))
            return Entity("pokemon", row["id"], row["name"]) if row else None
        kinds = [kind] if kind else list(KINDS)
        for k in kinds:
            for entity in self.index.get(key, []):
                if entity.kind == k:
                    return entity
        for k in kinds:
            close = difflib.get_close_matches(key, self.keys_by_kind[k], n=1, cutoff=0.75)
            if close:
                return next(e for e in self.index[close[0]] if e.kind == k)
        return None

    def find_entities(self, text: str, limit: int = 6) -> list[Entity]:
        """Pick out Pokémon/moves/abilities/items/types mentioned anywhere in free text."""
        tokens = normalize(text).split()
        lowered = text.lower()
        wants = set()
        if any(h in lowered for h in MOVE_HINTS):
            wants.add("move")
        if "abilit" in lowered:
            wants.add("ability")
        if "item" in lowered or "hold" in lowered:
            wants.add("item")
        found: list[Entity] = []
        used = [False] * len(tokens)
        i = 0
        while i < len(tokens):
            for n in range(min(4, len(tokens) - i), 0, -1):
                key = " ".join(tokens[i:i + n])
                candidates = self.index.get(key)
                if not candidates:
                    continue
                if n == 1:
                    candidates = [c for c in candidates if c.kind in ("pokemon", "type") or key not in COMMON_WORDS or c.kind in wants]
                    if not candidates or (len(key) < 3 and not key.isdigit()):
                        continue
                    if key.isdigit():
                        continue
                # A word naming both a type and a move (e.g. "psychic") is most likely the type.
                order = {"pokemon": 0, "type": 1, "move": 2, "ability": 3, "item": 4}
                if wants:
                    order.update({k: -1 for k in wants})
                pick = min(candidates, key=lambda c: order[c.kind])
                picks = [pick] + [c for c in candidates if c.kind == "type" and c is not pick]
                found.extend(p for p in picks if p not in found)
                used[i:i + n] = [True] * n
                i += n
                break
            else:
                i += 1
        pokemon_keys = [k for k in self.keys_by_kind["pokemon"] if " " not in k]
        # In a short message with nothing else recognised, a unique near-complete prefix
        # ("meow", "show me pika") is a truncated name; elsewhere it is usually an English word.
        allow_prefix = not found and len(tokens) <= 4
        for idx, tok in enumerate(tokens):
            if used[idx] or len(tok) < 4 or tok in COMMON_WORDS:
                continue
            close = [k for k in pokemon_keys if k.startswith(tok) and len(k) - len(tok) <= 3] if allow_prefix else []
            if len(close) != 1:
                close = [] if len(tok) < 5 else difflib.get_close_matches(
                    tok, [k for k in pokemon_keys if k[0] == tok[0]], n=1, cutoff=0.8)
            if close:
                entity = self.index[close[0]][0]
                if entity not in found:
                    found.append(entity)
        return found[:limit]

    # ------------------------------------------------------------------ records
    @lru_cache(maxsize=2048)
    def pokemon(self, pid: int) -> dict:
        row = self.q1(
            "SELECT p.*, s.name AS species_name, s.genus, s.generation, s.evolves_from, s.evolution_chain, s.color, "
            "s.habitat, s.shape, s.gender_rate, s.capture_rate, s.base_happiness, s.growth_rate, s.is_baby, "
            "s.is_legendary, s.is_mythical, s.flavor_text, s.evolution_condition "
            "FROM pokemon p JOIN species s ON s.id = p.species_id WHERE p.id = ?",
            pid,
        )
        if row is None:
            raise KeyError(pid)
        p = dict(row)
        p["types"] = [t for t in (p["type1"], p["type2"]) if t]
        p["stats"] = {s: p[s] for s in STATS}
        p["abilities"] = [
            dict(r) for r in self.q(
                "SELECT a.id, a.identifier, a.name, a.short_effect, pa.is_hidden FROM pokemon_abilities pa "
                "JOIN abilities a ON a.id = pa.ability_id WHERE pa.pokemon_id = ? ORDER BY pa.slot", pid)
        ]
        return p

    def default_form(self, species_id: int) -> int:
        return self.q1("SELECT id FROM pokemon WHERE species_id = ? AND is_default = 1", species_id)["id"]

    def learnset(self, pid: int) -> list[dict]:
        sql = ("SELECT m.*, pm.method, pm.level FROM pokemon_moves pm JOIN moves m ON m.id = pm.move_id "
               "WHERE pm.pokemon_id = ? ORDER BY pm.method, pm.level, m.name")
        rows = self.q(sql, pid)
        if not rows:
            rows = self.q(sql, self.default_form(self.pokemon(pid)["species_id"]))
        return [dict(r) for r in rows]

    def can_learn(self, pid: int, move_id: int) -> list[str]:
        methods = []
        for m in self.learnset(pid):
            if m["id"] == move_id:
                methods.append(f"level {m['level']}" if m["method"] == "level-up" else m["method"])
        return methods

    @lru_cache(maxsize=1024)
    def move(self, mid: int) -> dict:
        m = dict(self.q1("SELECT * FROM moves WHERE id = ?", mid))
        m["stat_changes"] = [(r["stat"], r["change"]) for r in self.q("SELECT stat, change FROM move_stat_changes WHERE move_id = ?", mid)]
        m["flags"] = set(filter(None, (m["flags"] or "").split(",")))
        return m

    def move_by_identifier(self, identifier: str) -> dict:
        return self.move(self.q1("SELECT id FROM moves WHERE identifier = ?", identifier)["id"])

    def ability(self, aid: int) -> dict:
        return dict(self.q1("SELECT * FROM abilities WHERE id = ?", aid))

    def item(self, iid: int) -> dict:
        return dict(self.q1("SELECT * FROM items WHERE id = ?", iid))

    def pokemon_with_ability(self, aid: int) -> list[dict]:
        return [dict(r) for r in self.q(
            "SELECT p.id, p.name, pa.is_hidden FROM pokemon_abilities pa JOIN pokemon p ON p.id = pa.pokemon_id "
            "WHERE pa.ability_id = ? ORDER BY p.is_default DESC, p.species_id", aid)]

    def pokemon_learning(self, mid: int) -> list[str]:
        return [r["name"] for r in self.q(
            "SELECT DISTINCT p.name FROM pokemon_moves pm JOIN pokemon p ON p.id = pm.pokemon_id "
            "WHERE pm.move_id = ? AND p.is_default = 1 ORDER BY p.total DESC", mid)]

    def pokemon_of_type(self, type_ident: str, order_by: str = "total", limit: int = 10) -> list[dict]:
        return [dict(r) for r in self.q(
            f"SELECT id, name, type1, type2, {order_by} AS value FROM pokemon "
            f"WHERE is_default = 1 AND (type1 = ? OR type2 = ?) ORDER BY {order_by} DESC LIMIT ?",
            type_ident, type_ident, limit)]

    def top(self, stat: str, type_ident: str | None = None, ascending: bool = False, limit: int = 10,
            include_forms: bool = False) -> list[dict]:
        if stat not in (*STATS, "total", "weight", "height"):
            raise ValueError(stat)
        where = ["1=1" if include_forms else "is_default = 1"]
        args: list = []
        if type_ident:
            where.append("(type1 = ? OR type2 = ?)")
            args += [type_ident, type_ident]
        sql = (f"SELECT id, name, type1, type2, {stat} AS value FROM pokemon WHERE {' AND '.join(where)} "
               f"ORDER BY {stat} {'ASC' if ascending else 'DESC'}, id LIMIT ?")
        return [dict(r) for r in self.q(sql, *args, limit)]

    def random_pokemon(self) -> int:
        return self.q1("SELECT id FROM pokemon WHERE is_default = 1 ORDER BY RANDOM() LIMIT 1")["id"]

    def counts(self) -> dict[str, int]:
        return {
            "species": self.q1("SELECT COUNT(*) FROM species")[0],
            "forms": self.q1("SELECT COUNT(*) FROM pokemon")[0],
            "moves": self.q1("SELECT COUNT(*) FROM moves")[0],
            "abilities": self.q1("SELECT COUNT(*) FROM abilities")[0],
            "items": self.q1("SELECT COUNT(*) FROM items")[0],
            "types": len(self.type_names),
            "generations": self.q1("SELECT MAX(generation) FROM species")[0],
        }

    # ------------------------------------------------------------------ types
    def multiplier(self, attack_type: str, defender_types: list[str]) -> float:
        result = 1.0
        for t in defender_types:
            result *= self._efficacy.get((attack_type, t), 1.0)
        return result

    def defense_profile(self, defender_types: list[str]) -> dict[float, list[str]]:
        profile: dict[float, list[str]] = defaultdict(list)
        for attack_type in self.type_names:
            profile[self.multiplier(attack_type, defender_types)].append(attack_type)
        return dict(profile)

    def offense_profile(self, attack_type: str) -> dict[float, list[str]]:
        profile: dict[float, list[str]] = defaultdict(list)
        for defender in self.type_names:
            profile[self.multiplier(attack_type, [defender])].append(defender)
        return dict(profile)

    # ------------------------------------------------------------------ evolution
    def evolution_lines(self, species_id: int) -> list[list[tuple[str, str | None]]]:
        """Every root-to-leaf path in the species' evolution family, as (name, condition) steps."""
        chain = self.q1("SELECT evolution_chain FROM species WHERE id = ?", species_id)["evolution_chain"]
        rows = [dict(r) for r in self.q("SELECT id, name, evolves_from, evolution_condition FROM species WHERE evolution_chain = ? ORDER BY id", chain)]
        children: dict[int | None, list[dict]] = defaultdict(list)
        for r in rows:
            children[r["evolves_from"]].append(r)
        roots = [r for r in rows if r["evolves_from"] not in {x["id"] for x in rows}]
        lines: list[list[tuple[str, str | None]]] = []

        def walk(node: dict, path: list) -> None:
            path = path + [(node["name"], node["evolution_condition"] if path else None)]
            if not children[node["id"]]:
                lines.append(path)
            for child in children[node["id"]]:
                walk(child, path)

        for root in roots:
            walk(root, [])
        return lines

    # ------------------------------------------------------------------ art
    def ascii(self, pokemon_id: int, clean: bool = True) -> str | None:
        row = self.q1("SELECT art FROM ascii WHERE pokemon_id = ?", pokemon_id)
        if row is None:
            return None
        art = row["art"].replace("@", " ") if clean else row["art"]
        lines = [line.rstrip() for line in art.split("\n")]
        while lines and not lines[0].strip():
            lines.pop(0)
        while lines and not lines[-1].strip():
            lines.pop()
        return "\n".join(lines)
