"""Turn-based 1v1 battle simulator using main-series damage rules and PokeAPI move metadata.

Simulated: stats from base stats (IV 31, EV 0, neutral nature), STAB, type chart, crits, accuracy,
priority, speed order, multi-hit, drain/recoil, healing, stat stages, burn/poison/toxic/paralysis/
sleep/freeze/confusion, flinch, recharge and two-turn moves, OHKO and level-damage moves, plus a
handful of battle-relevant abilities. Not simulated: items, weather, terrain, switching, and
move-specific special cases beyond what PokeAPI's move_meta describes.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field

from pokebot.pokedex import Pokedex

STAGE_STATS = ("attack", "defense", "sp_attack", "sp_defense", "speed", "accuracy", "evasion")
STAT_NAMES = {"attack": "Attack", "defense": "Defense", "sp_attack": "Sp. Atk", "sp_defense": "Sp. Def",
              "speed": "Speed", "accuracy": "accuracy", "evasion": "evasiveness"}
SELF_TARGETS = {4, 5, 7, 13}
ABILITY_IMMUNITY = {
    "levitate": "ground", "flash-fire": "fire", "water-absorb": "water", "volt-absorb": "electric",
    "lightning-rod": "electric", "storm-drain": "water", "sap-sipper": "grass", "motor-drive": "electric",
    "dry-skin": "water", "earth-eater": "ground", "well-baked-body": "fire",
}
ABSORB_HEAL = {"water-absorb", "volt-absorb", "dry-skin", "earth-eater"}
PINCH = {"blaze": "fire", "torrent": "water", "overgrow": "grass", "swarm": "bug"}
STATUS_IMMUNE_TYPES = {"burn": {"fire"}, "poison": {"poison", "steel"}, "paralysis": {"electric"}, "freeze": {"ice"}}
STATUS_ABBR = {"burn": "BRN", "poison": "PSN", "toxic": "TOX", "paralysis": "PAR", "sleep": "SLP", "freeze": "FRZ"}
USEFUL_STATUS_MOVES = (
    "swords-dance", "dragon-dance", "nasty-plot", "calm-mind", "quiver-dance", "shell-smash", "bulk-up",
    "will-o-wisp", "thunder-wave", "toxic", "spore", "sleep-powder", "hypnosis", "recover", "roost",
    "slack-off", "soft-boiled", "synthesis", "moonlight", "agility", "iron-defense", "amnesia",
)
AVOID_FLAGS = {"ohko", "self_ko", "two_turn", "recharge", "conditional"}
HP_SCALING_MOVES = {"water-spout", "eruption", "dragon-energy"}
STRUGGLE = {
    "id": 165, "identifier": "struggle", "name": "Struggle", "type": None, "power": 50, "accuracy": None,
    "pp": 1, "priority": 0, "damage_class": "physical", "target_id": 10, "meta_category": 0, "ailment": None,
    "ailment_chance": 0, "min_hits": None, "max_hits": None, "drain": 0, "healing": 0, "crit_rate": 0,
    "flinch_chance": 0, "stat_chance": 0, "stat_changes": [], "flags": set(), "short_effect": "",
}


def calc_stat(base: int, level: int, is_hp: bool) -> int:
    core = (2 * base + 31) * level // 100
    return core + level + 10 if is_hp else core + 5


def stage_multiplier(stage: int, accuracy: bool = False) -> float:
    base = 3 if accuracy else 2
    return (base + stage) / base if stage >= 0 else base / (base - stage)


@dataclass
class Battler:
    side: str
    pokemon_id: int
    species_id: int
    name: str
    level: int
    types: list[str]
    ability: str
    ability_name: str
    stats: dict[str, int]
    moves: list[dict]
    hp: int = 0
    status: str | None = None
    sleep_turns: int = 0
    toxic_counter: int = 0
    confused: int = 0
    stages: dict[str, int] = field(default_factory=lambda: dict.fromkeys(STAGE_STATS, 0))
    must_recharge: bool = False
    charging: dict | None = None
    flinched: bool = False

    def __post_init__(self) -> None:
        self.hp = self.max_hp

    @property
    def max_hp(self) -> int:
        return self.stats["hp"]

    @property
    def fainted(self) -> bool:
        return self.hp <= 0

    def stat(self, name: str, crit_ignore: str | None = None) -> float:
        stage = self.stages[name]
        if crit_ignore == "negative":
            stage = max(0, stage)
        elif crit_ignore == "positive":
            stage = min(0, stage)
        value = self.stats[name] * stage_multiplier(stage)
        if name == "speed" and self.status == "paralysis":
            value *= 0.5
        return value

    def summary(self) -> dict:
        return {
            "side": self.side, "name": self.name, "pokemon_id": self.pokemon_id, "species_id": self.species_id,
            "level": self.level, "types": self.types, "ability": self.ability_name, "hp": self.hp,
            "max_hp": self.max_hp, "stats": self.stats,
            "moves": [{"name": m["name"], "type": m["type"], "power": m["power"], "accuracy": m["accuracy"],
                       "category": m["damage_class"], "pp": m["pp"]} for m in self.moves],
        }


class Battle:
    def __init__(self, dex: Pokedex, a: Battler, b: Battler, seed: int | None = None, record: bool = True):
        self.dex = dex
        self.a, self.b = a, b
        self.rng = random.Random(seed)
        self.record = record
        self.events: list[dict] = []
        self.turn = 0
        self.winner: Battler | None = None

    # ---------------------------------------------------------------- events
    def emit(self, kind: str, text: str, **data) -> None:
        if self.record:
            self.events.append({"t": kind, "text": text, "turn": self.turn, **data})

    def hp_event(self, kind: str, target: Battler, text: str, amount: int, **data) -> None:
        self.emit(kind, text, side=target.side, amount=amount, hp=target.hp, max_hp=target.max_hp, **data)

    def foe(self, mon: Battler) -> Battler:
        return self.b if mon is self.a else self.a

    # ---------------------------------------------------------------- run
    def run(self, max_turns: int = 80) -> Battler | None:
        self.emit("start", f"{self.a.name} (Lv{self.a.level}) vs {self.b.name} (Lv{self.b.level})!")
        for mon in (self.a, self.b):
            if mon.ability == "intimidate":
                self.change_stage(self.foe(mon), "attack", -1, source=f"{mon.name}'s Intimidate")
        while self.turn < max_turns and not (self.a.fainted or self.b.fainted):
            self.turn += 1
            self.emit("turn", f"Turn {self.turn}")
            self.play_turn()
        if self.a.fainted and self.b.fainted:
            self.winner = None
        elif self.b.fainted:
            self.winner = self.a
        elif self.a.fainted:
            self.winner = self.b
        else:
            self.winner = max((self.a, self.b), key=lambda m: m.hp / m.max_hp)
            self.emit("text", f"The battle hit the {max_turns}-turn limit; {self.winner.name} has more HP left.")
        self.emit("end", f"{self.winner.name} wins!" if self.winner else "Both Pokémon fainted - it's a draw!",
                  winner=self.winner.side if self.winner else None)
        return self.winner

    def play_turn(self) -> None:
        choices = {}
        for mon in (self.a, self.b):
            if mon.must_recharge:
                choices[mon.side] = None
            elif mon.charging:
                choices[mon.side] = mon.charging
            else:
                choices[mon.side] = choose_move(self.dex, mon, self.foe(mon), self.rng)

        def order_key(mon: Battler):
            move = choices[mon.side]
            return (move["priority"] if move else 0, mon.stat("speed"), self.rng.random())

        order = sorted((self.a, self.b), key=order_key, reverse=True)
        for idx, mon in enumerate(order):
            target = self.foe(mon)
            if mon.fainted or target.fainted:
                continue
            self.act(mon, target, choices[mon.side], moved_first=idx == 0)
            if target.fainted:
                self.emit("faint", f"{target.name} fainted!", side=target.side)
            if mon.fainted:
                self.emit("faint", f"{mon.name} fainted!", side=mon.side)
        for mon in order:
            mon.flinched = False
            if not mon.fainted:
                self.residual(mon)

    # ---------------------------------------------------------------- actions
    def can_act(self, mon: Battler) -> bool:
        if mon.must_recharge:
            mon.must_recharge = False
            self.emit("text", f"{mon.name} must recharge!", side=mon.side)
            return False
        if mon.status == "freeze":
            if self.rng.random() < 0.2:
                mon.status = None
                self.emit("status", f"{mon.name} thawed out!", side=mon.side, status=None)
            else:
                self.emit("text", f"{mon.name} is frozen solid!", side=mon.side)
                return False
        if mon.status == "sleep":
            mon.sleep_turns -= 1
            if mon.sleep_turns > 0:
                self.emit("text", f"{mon.name} is fast asleep.", side=mon.side)
                return False
            mon.status = None
            self.emit("status", f"{mon.name} woke up!", side=mon.side, status=None)
        if mon.flinched:
            self.emit("text", f"{mon.name} flinched and couldn't move!", side=mon.side)
            return False
        if mon.status == "paralysis" and self.rng.random() < 0.25:
            self.emit("text", f"{mon.name} is paralyzed! It can't move!", side=mon.side)
            return False
        if mon.confused:
            mon.confused -= 1
            if mon.confused == 0:
                self.emit("text", f"{mon.name} snapped out of its confusion!", side=mon.side)
            elif self.rng.random() < 1 / 3:
                dmg = self.raw_damage(mon, mon, 40, "physical", crit=False, random_factor=1.0)
                mon.hp = max(0, mon.hp - dmg)
                self.hp_event("damage", mon, f"{mon.name} is confused! It hurt itself in its confusion!", dmg, effectiveness=1, crit=False)
                return False
        return True

    def act(self, mon: Battler, target: Battler, move: dict | None, moved_first: bool) -> None:
        if move is None or not self.can_act(mon):
            mon.charging = None if mon.status in ("sleep", "freeze") else mon.charging
            return
        if "two_turn" in move["flags"] and mon.charging is None:
            mon.charging = move
            self.emit("charge", f"{mon.name} is charging {move['name']}!", side=mon.side, move=move["name"], type=move["type"])
            return
        mon.charging = None
        if move is not STRUGGLE:
            move["pp_left"] = move.get("pp_left", move["pp"] or 1) - 1
        self.emit("move", f"{mon.name} used {move['name']}!", side=mon.side, move=move["name"],
                  type=move["type"], category=move["damage_class"])

        if "ohko" in move["flags"]:
            if target.level > mon.level or self.rng.random() * 100 >= 30 + mon.level - target.level or self.dex.multiplier(move["type"], target.types) == 0:
                self.emit("miss", f"{target.name} avoided the attack!", side=target.side)
                return
            if target.ability == "sturdy":
                self.emit("text", f"{target.name} is protected by Sturdy!", side=target.side)
                return
            dmg = target.hp
            target.hp = 0
            self.hp_event("damage", target, "It's a one-hit KO!", dmg, effectiveness=1, crit=False)
            return

        if not self.hits(mon, target, move):
            self.emit("miss", f"{mon.name}'s attack missed!", side=target.side)
            return

        if move["damage_class"] == "status" or (not move["power"] and "level_damage" not in move["flags"]):
            self.apply_status_move(mon, target, move)
            return

        effectiveness = 1.0 if move["type"] is None else self.dex.multiplier(move["type"], target.types)
        immune_type = ABILITY_IMMUNITY.get(target.ability)
        if effectiveness == 0 or (immune_type and move["type"] == immune_type):
            reason = f" thanks to {target.ability_name}" if effectiveness else ""
            self.emit("immune", f"It doesn't affect {target.name}{reason}...", side=target.side)
            if effectiveness and target.ability in ABSORB_HEAL:
                self.heal(target, target.max_hp // 4, f"{target.name} restored HP using {target.ability_name}!")
            return
        if target.ability == "wonder-guard" and effectiveness <= 1:
            self.emit("immune", f"{target.name} is protected by Wonder Guard!", side=target.side)
            return

        hits = self.hit_count(move)
        total = 0
        for i in range(hits):
            if target.fainted:
                break
            if "level_damage" in move["flags"]:
                dmg, crit = mon.level, False
            else:
                crit = self.rng.random() < crit_chance(move["crit_rate"] or 0)
                dmg = self.damage(mon, target, move, effectiveness, crit)
            if target.ability == "sturdy" and target.hp == target.max_hp and dmg >= target.hp:
                dmg = target.hp - 1
                self.emit("text", f"{target.name} endured the hit with Sturdy!", side=target.side)
            target.hp = max(0, target.hp - dmg)
            total += dmg
            label = f"Hit {i + 1}: " if hits > 1 else ""
            crit_text = " A critical hit!" if crit else ""
            self.hp_event("damage", target, f"{label}{target.name} took {dmg} damage.{crit_text}", dmg,
                          effectiveness=effectiveness, crit=crit, move_type=move["type"])
        if effectiveness > 1:
            self.emit("text", "It's super effective!", side=target.side, effectiveness=effectiveness)
        elif effectiveness < 1:
            self.emit("text", "It's not very effective...", side=target.side, effectiveness=effectiveness)
        if hits > 1:
            self.emit("text", f"Hit {hits} times!", side=target.side)
        if target.status == "freeze" and move["type"] == "fire":
            target.status = None
            self.emit("status", f"{target.name} thawed out!", side=target.side, status=None)

        drain = move["drain"] or 0
        if move is STRUGGLE:
            recoil = max(1, mon.max_hp // 4)
            mon.hp = max(0, mon.hp - recoil)
            self.hp_event("damage", mon, f"{mon.name} is damaged by recoil!", recoil, effectiveness=1, crit=False)
        elif drain > 0 and total:
            self.heal(mon, max(1, total * drain // 100), f"{target.name} had its energy drained!")
        elif drain < 0 and total and mon.ability != "rock-head":
            recoil = max(1, total * -drain // 100)
            mon.hp = max(0, mon.hp - recoil)
            self.hp_event("damage", mon, f"{mon.name} is damaged by recoil!", recoil, effectiveness=1, crit=False)

        if not target.fainted:
            self.secondary_effects(mon, target, move, moved_first)
        if move["stat_changes"] and move["meta_category"] == 7 and self.chance(move["stat_chance"]):
            for stat, change in move["stat_changes"]:
                self.change_stage(mon, stat, change)
        if "self_ko" in move["flags"]:
            mon.hp = 0
        if "recharge" in move["flags"]:
            mon.must_recharge = True

    def transform(self, mon: Battler, target: Battler) -> None:
        mon.types = list(target.types)
        mon.ability, mon.ability_name = target.ability, target.ability_name
        mon.stats = {**target.stats, "hp": mon.stats["hp"]}
        mon.stages = dict(target.stages)
        mon.moves = [{**m, "pp": 5, "pp_left": 5} for m in target.moves if m["identifier"] != "transform"] or [dict(STRUGGLE)]
        mon.transformed = True
        self.emit("transform", f"{mon.name} transformed into {target.name}!", side=mon.side,
                  species_id=target.species_id, pokemon_id=target.pokemon_id, types=mon.types)

    def apply_status_move(self, mon: Battler, target: Battler, move: dict) -> None:
        if move["identifier"] == "transform":
            if getattr(mon, "transformed", False):
                self.emit("text", "But it failed!", side=mon.side)
            else:
                self.transform(mon, target)
            return
        did_something = False
        if move["healing"] and move["healing"] > 0:
            if mon.hp == mon.max_hp:
                self.emit("text", f"{mon.name}'s HP is already full!", side=mon.side)
            else:
                self.heal(mon, mon.max_hp * move["healing"] // 100, f"{mon.name} restored its HP!")
            did_something = True
        if move["ailment"]:
            if move["identifier"] in ("toxic",):
                did_something |= self.inflict(target, "toxic", announce_fail=True)
            else:
                did_something |= self.inflict(target, move["ailment"], announce_fail=True)
        if move["stat_changes"]:
            who = mon if move["target_id"] in SELF_TARGETS else target
            for stat, change in move["stat_changes"]:
                self.change_stage(who, stat, change)
            did_something = True
        if move["identifier"] == "rest" and mon.hp < mon.max_hp:
            mon.status, mon.sleep_turns = "sleep", 3
            self.heal(mon, mon.max_hp - mon.hp, f"{mon.name} slept and became healthy!")
            did_something = True
        if not did_something:
            self.emit("text", "But nothing happened... (this move's effect isn't simulated)", side=mon.side)

    def secondary_effects(self, mon: Battler, target: Battler, move: dict, moved_first: bool) -> None:
        if move["ailment"] and move["ailment_chance"] and self.chance(move["ailment_chance"]):
            self.inflict(target, move["ailment"])
        if move["flinch_chance"] and moved_first and self.chance(move["flinch_chance"]):
            target.flinched = True
        if move["stat_changes"] and move["meta_category"] == 6 and self.chance(move["stat_chance"]):
            for stat, change in move["stat_changes"]:
                self.change_stage(target, stat, change)

    def inflict(self, target: Battler, ailment: str, announce_fail: bool = False) -> bool:
        if ailment == "confusion":
            if target.confused:
                if announce_fail:
                    self.emit("text", f"{target.name} is already confused!", side=target.side)
                return False
            target.confused = self.rng.randint(2, 5)
            self.emit("status", f"{target.name} became confused!", side=target.side, status="CNF")
            return True
        if ailment not in ("burn", "poison", "toxic", "paralysis", "sleep", "freeze"):
            return False
        base = "poison" if ailment == "toxic" else ailment
        if target.status or set(target.types) & STATUS_IMMUNE_TYPES.get(base, set()):
            if announce_fail:
                self.emit("text", f"It doesn't affect {target.name}...", side=target.side)
            return False
        if ailment == "sleep" and target.ability in ("insomnia", "vital-spirit"):
            if announce_fail:
                self.emit("text", f"{target.name} stays awake thanks to {target.ability_name}!", side=target.side)
            return False
        target.status = ailment
        if ailment == "sleep":
            target.sleep_turns = self.rng.randint(1, 3) + 1
        if ailment == "toxic":
            target.toxic_counter = 0
        verb = {"burn": "was burned", "poison": "was poisoned", "toxic": "was badly poisoned",
                "paralysis": "is paralyzed! It may be unable to move", "sleep": "fell asleep", "freeze": "was frozen solid"}[ailment]
        self.emit("status", f"{target.name} {verb}!", side=target.side, status=STATUS_ABBR[ailment])
        return True

    def change_stage(self, mon: Battler, stat: str, change: int, source: str | None = None) -> None:
        old = mon.stages[stat]
        mon.stages[stat] = max(-6, min(6, old + change))
        prefix = f"{source}: " if source else ""
        if mon.stages[stat] == old:
            self.emit("text", f"{prefix}{mon.name}'s {STAT_NAMES[stat]} won't go any {'higher' if change > 0 else 'lower'}!", side=mon.side)
            return
        size = {1: "", 2: " sharply", 3: " drastically"}.get(abs(change), " drastically")
        direction = "rose" if change > 0 else "fell"
        self.emit("stat", f"{prefix}{mon.name}'s {STAT_NAMES[stat]}{size} {direction}!", side=mon.side, stat=stat, change=change)

    def heal(self, mon: Battler, amount: int, text: str) -> None:
        amount = min(amount, mon.max_hp - mon.hp)
        if amount <= 0:
            return
        mon.hp += amount
        self.hp_event("heal", mon, text, amount)

    def residual(self, mon: Battler) -> None:
        if mon.status == "burn":
            dmg, text = max(1, mon.max_hp // 16), f"{mon.name} is hurt by its burn!"
        elif mon.status == "poison":
            dmg, text = max(1, mon.max_hp // 8), f"{mon.name} is hurt by poison!"
        elif mon.status == "toxic":
            mon.toxic_counter += 1
            dmg, text = max(1, mon.max_hp * mon.toxic_counter // 16), f"{mon.name} is hurt by poison!"
        else:
            return
        mon.hp = max(0, mon.hp - dmg)
        self.hp_event("damage", mon, text, dmg, effectiveness=1, crit=False)
        if mon.fainted:
            self.emit("faint", f"{mon.name} fainted!", side=mon.side)

    # ---------------------------------------------------------------- maths
    def chance(self, percent: int | None) -> bool:
        return not percent or self.rng.random() * 100 < percent

    def hits(self, mon: Battler, target: Battler, move: dict) -> bool:
        if move["accuracy"] is None or move["target_id"] in SELF_TARGETS:
            return True
        stage = max(-6, min(6, mon.stages["accuracy"] - target.stages["evasion"]))
        return self.rng.random() * 100 < move["accuracy"] * stage_multiplier(stage, accuracy=True)

    def hit_count(self, move: dict) -> int:
        lo, hi = move["min_hits"], move["max_hits"]
        if not lo or not hi:
            return 1
        if (lo, hi) == (2, 5):
            return self.rng.choices([2, 3, 4, 5], weights=[35, 35, 15, 15])[0]
        return self.rng.randint(lo, hi)

    def raw_damage(self, mon: Battler, target: Battler, power: float, category: str, crit: bool, random_factor: float) -> int:
        physical = category == "physical"
        attack = mon.stat("attack" if physical else "sp_attack", "negative" if crit else None)
        defense = target.stat("defense" if physical else "sp_defense", "positive" if crit else None)
        if physical and mon.ability in ("huge-power", "pure-power"):
            attack *= 2
        if physical and mon.ability == "guts" and mon.status:
            attack *= 1.5
        elif physical and mon.status == "burn":
            attack *= 0.5
        base = math.floor(math.floor(math.floor(2 * mon.level / 5 + 2) * power * attack / defense) / 50) + 2
        return max(1, math.floor(base * random_factor))

    def damage(self, mon: Battler, target: Battler, move: dict, effectiveness: float, crit: bool) -> int:
        return estimate_damage(mon, target, move, effectiveness, crit, self.rng.uniform(0.85, 1.0), self)


def crit_chance(stage: int) -> float:
    return {0: 1 / 24, 1: 1 / 8, 2: 1 / 2}.get(stage, 1.0)


def estimate_damage(mon: Battler, target: Battler, move: dict, effectiveness: float, crit: bool,
                    random_factor: float, battle: Battle) -> int:
    power = move["power"] or 0
    if move["identifier"] in HP_SCALING_MOVES:
        power = max(1, power * mon.hp // mon.max_hp)
    if mon.ability == "technician" and power <= 60:
        power *= 1.5
    modifier = random_factor * effectiveness
    if move["type"] in mon.types:
        modifier *= 2 if mon.ability == "adaptability" else 1.5
    if crit:
        modifier *= 1.5
    if mon.ability in PINCH and move["type"] == PINCH[mon.ability] and mon.hp <= mon.max_hp / 3:
        modifier *= 1.5
    if target.ability == "thick-fat" and move["type"] in ("fire", "ice"):
        modifier *= 0.5
    return battle.raw_damage(mon, target, power, move["damage_class"], crit, modifier)


def choose_move(dex: Pokedex, mon: Battler, foe: Battler, rng: random.Random) -> dict:
    """Simple trainer AI: prefer KOs, otherwise highest expected damage, with occasional setup or status."""
    usable = [m for m in mon.moves if m.get("pp_left", m["pp"] or 1) > 0]
    if not usable:
        return STRUGGLE
    probe = Battle(dex, mon, foe, record=False)
    scored = []
    for m in usable:
        if m["damage_class"] == "status" or (not m["power"] and "level_damage" not in m["flags"]):
            value = 0.0
            if m["identifier"] == "transform":
                value = 0 if getattr(mon, "transformed", False) else 5
            elif m["ailment"] in ("burn", "poison", "paralysis", "sleep") and not foe.status:
                immune = set(foe.types) & STATUS_IMMUNE_TYPES.get(m["ailment"], set())
                value = 0 if immune else 0.45 if m["ailment"] == "sleep" else 0.3
            elif m["stat_changes"] and m["target_id"] in SELF_TARGETS:
                boost = sum(c for _, c in m["stat_changes"] if c > 0)
                stage = max(mon.stages[s] for s, _ in m["stat_changes"])
                value = 0.18 * boost if stage < 2 and mon.hp > mon.max_hp * 0.6 else 0
            elif m["healing"] and m["healing"] > 0:
                value = (1 - mon.hp / mon.max_hp) * 0.9
            scored.append((value, m))
            continue
        eff = 1.0 if m["type"] is None else dex.multiplier(m["type"], foe.types)
        if ABILITY_IMMUNITY.get(foe.ability) == m["type"]:
            eff = 0
        if "level_damage" in m["flags"]:
            dmg = mon.level if eff else 0
        else:
            dmg = estimate_damage(mon, foe, m, eff, False, 0.925, probe) if eff else 0
        lo, hi = m["min_hits"] or 1, m["max_hits"] or 1
        dmg *= (lo + hi) / 2 if hi > 1 else 1
        accuracy = (m["accuracy"] or 100) / 100
        value = (2 + accuracy) if dmg >= foe.hp and accuracy >= 0.85 else accuracy * min(dmg, foe.hp) / foe.max_hp
        if "recharge" in m["flags"] and dmg < foe.hp:
            value *= 0.55
        if "two_turn" in m["flags"]:
            value *= 0.5
        if "self_ko" in m["flags"]:
            value *= 1.0 if mon.hp < mon.max_hp * 0.25 else 0.15
        if m["priority"] > 0 and dmg >= foe.hp:
            value += 1
        scored.append((value, m))
    scored.sort(key=lambda vm: vm[0], reverse=True)
    if rng.random() < 0.85 or len(scored) == 1:
        return scored[0][1]
    positive = [m for v, m in scored if v > 0] or [scored[0][1]]
    return rng.choice(positive)


def auto_moveset(dex: Pokedex, pokemon_id: int, stats: dict[str, int], types: list[str]) -> list[dict]:
    """Pick up to 4 moves: strong same-type attacks, coverage, and at most one useful status move."""
    seen: dict[int, dict] = {}
    for m in dex.learnset(pokemon_id):
        seen.setdefault(m["id"], m)
    candidates = [dex.move(mid) for mid in seen]

    def score(m: dict) -> float:
        attack = stats["attack"] if m["damage_class"] == "physical" else stats["sp_attack"]
        hits = ((m["min_hits"] or 1) + (m["max_hits"] or 1)) / 2
        stab = 1.5 if m["type"] in types else 1.0
        recoil = 0.9 if (m["drain"] or 0) < 0 else 1.0
        return m["power"] * hits * (m["accuracy"] or 100) / 100 * stab * attack * recoil * (1 + m["priority"] * 0.05)

    attacks = [m for m in candidates if m["power"] and m["damage_class"] != "status" and not m["flags"] & AVOID_FLAGS]
    attacks.sort(key=score, reverse=True)
    chosen: list[dict] = []
    for _ in range(4):
        best, best_score = None, 0.0
        for m in attacks:
            if m in chosen:
                continue
            s = score(m) * (0.45 if any(c["type"] == m["type"] for c in chosen) else 1.0)
            if s > best_score:
                best, best_score = m, s
        if best is None:
            break
        chosen.append(best)
    if not chosen and any(m["identifier"] == "transform" for m in candidates):
        return [dict(next(m for m in candidates if m["identifier"] == "transform"))]
    status = [m for m in candidates if m["identifier"] in USEFUL_STATUS_MOVES]
    if status and len(chosen) >= 3:
        status.sort(key=lambda m: USEFUL_STATUS_MOVES.index(m["identifier"]))
        chosen = chosen[:3] + [status[0]]
    elif status and len(chosen) < 4:
        chosen += status[: 4 - len(chosen)]
    if not chosen:
        fallback = [m for m in candidates if m["power"] or "level_damage" in m["flags"]]
        chosen = sorted(fallback, key=lambda m: m["power"] or 0, reverse=True)[:4]
    return [dict(m) for m in chosen] or [dict(STRUGGLE)]


def make_battler(dex: Pokedex, pokemon_id: int, side: str, level: int = 50, move_names: list[str] | None = None) -> Battler:
    p = dex.pokemon(pokemon_id)
    stats = {s: calc_stat(p[s], level, s == "hp") for s in ("hp", "attack", "defense", "sp_attack", "sp_defense", "speed")}
    if p["identifier"] == "shedinja":
        stats["hp"] = 1
    regular = [a for a in p["abilities"] if not a["is_hidden"]] or p["abilities"]
    ability = regular[0] if regular else {"identifier": "", "name": "None"}
    moves = []
    for name in move_names or []:
        entity = dex.resolve(name, "move")
        if entity:
            moves.append(dict(dex.move(entity.id)))
    if not moves:
        moves = auto_moveset(dex, pokemon_id, stats, p["types"])
    return Battler(side, pokemon_id, p["species_id"], p["name"], level, p["types"], ability["identifier"],
                   ability["name"], stats, moves[:4])


def simulate(dex: Pokedex, a_id: int, b_id: int, level: int = 50, seed: int | None = None,
             moves_a: list[str] | None = None, moves_b: list[str] | None = None, odds_runs: int = 200) -> dict:
    """One fully logged battle plus win odds estimated from `odds_runs` silent re-runs."""
    seed = seed if seed is not None else random.randrange(1 << 30)
    a = make_battler(dex, a_id, "a", level, moves_a)
    b = make_battler(dex, b_id, "b", level, moves_b)
    move_names_a = [m["name"] for m in a.moves]
    move_names_b = [m["name"] for m in b.moves]
    a_start, b_start = a.summary(), b.summary()
    battle = Battle(dex, a, b, seed=seed)
    winner = battle.run()
    wins = {"a": 0, "b": 0, "draw": 0}
    for i in range(odds_runs):
        x = make_battler(dex, a_id, "a", level, move_names_a)
        y = make_battler(dex, b_id, "b", level, move_names_b)
        w = Battle(dex, x, y, seed=seed + i + 1, record=False).run()
        wins[w.side if w else "draw"] += 1
    return {
        "seed": seed, "level": level, "a": a_start, "b": b_start, "events": battle.events,
        "winner": winner.side if winner else None, "turns": battle.turn,
        "odds": {k: v / odds_runs for k, v in wins.items()} if odds_runs else None,
        "percent": whole_percents(wins) if odds_runs else None,
    }


def whole_percents(counts: dict[str, int]) -> dict[str, int]:
    """Round to whole percentages that still sum to 100 (largest remainder)."""
    total = sum(counts.values())
    exact = {k: 100 * v / total for k, v in counts.items()}
    result = {k: int(v) for k, v in exact.items()}
    for k in sorted(exact, key=lambda k: exact[k] - result[k], reverse=True)[:100 - sum(result.values())]:
        result[k] += 1
    return result
