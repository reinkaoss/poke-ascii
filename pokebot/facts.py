"""Turn database records into compact, plain-text fact sheets (LLM context and offline answers)."""

from __future__ import annotations

from pokebot.pokedex import STAT_LABELS, Entity, Pokedex

STAT_WORDS = {
    "hp": "hp", "health": "hp", "attack": "attack", "defense": "defense", "defence": "defense",
    "special attack": "sp_attack", "sp atk": "sp_attack", "special defense": "sp_defense",
    "special defence": "sp_defense", "sp def": "sp_defense", "speed": "speed", "fastest": "speed",
    "slowest": "speed", "strongest": "total", "weakest": "total", "bulkiest": "hp", "tankiest": "hp",
    "heaviest": "weight", "lightest": "weight", "tallest": "height", "shortest": "height",
    "base stat total": "total", "bst": "total", "total": "total",
}
ASCENDING_WORDS = ("slowest", "weakest", "lightest", "shortest", "lowest", "least")


def _types(dex: Pokedex, types: list[str]) -> str:
    return "/".join(dex.type_names[t] for t in types)


def _type_list(dex: Pokedex, types: list[str]) -> str:
    return ", ".join(dex.type_names[t] for t in types) or "none"


def matchup_lines(dex: Pokedex, profile: dict[float, list[str]], defending: bool) -> list[str]:
    labels = (
        [(4.0, "Takes 4x from"), (2.0, "Weak to (2x)"), (0.5, "Resists (0.5x)"), (0.25, "Resists (0.25x)"), (0.0, "Immune to")]
        if defending else
        [(2.0, "Super effective (2x) against"), (0.5, "Not very effective (0.5x) against"), (0.0, "No effect on")]
    )
    return [f"{label}: {_type_list(dex, profile.get(mult, []))}" for mult, label in labels if profile.get(mult) or mult in (2.0, 0.5)]


def evolution_text(dex: Pokedex, species_id: int, short: bool = False) -> str:
    """Every evolution path. `short` keeps one method per step instead of every game's location."""
    lines = dex.evolution_lines(species_id)
    if len(lines) == 1 and len(lines[0]) == 1:
        return "Does not evolve."
    if short:
        lines = [[(name, cond and min(cond.split("; or "), key=len)) for name, cond in line] for line in lines]
    return " | ".join(
        " -> ".join(name if cond is None else f"{name} ({cond})" for name, cond in line) for line in lines
    )


def pokemon_sheet(dex: Pokedex, pid: int, with_moves: bool = True) -> str:
    p = dex.pokemon(pid)
    tags = [t for t, flag in (("Legendary", p["is_legendary"]), ("Mythical", p["is_mythical"]), ("Baby", p["is_baby"])) if flag]
    abilities = "; ".join(
        f"{a['name']}{' (hidden)' if a['is_hidden'] else ''}: {a['short_effect']}" for a in p["abilities"]
    )
    stats = ", ".join(f"{STAT_LABELS[s]} {v}" for s, v in p["stats"].items())
    gender = "genderless" if p["gender_rate"] == -1 else f"{p['gender_rate'] / 8:.1%} female"
    lines = [
        f"POKEMON: {p['name']} (National Dex #{p['species_id']}, Generation {p['generation']}) - the {p['genus']}"
        + (f" [{', '.join(tags)}]" if tags else ""),
        f"Type: {_types(dex, p['types'])}",
        f"Height {p['height']} m, Weight {p['weight']} kg, Color {p['color']}, Habitat {p['habitat'] or 'unknown'}, {gender}",
        f"Abilities: {abilities}",
        f"Base stats: {stats} (total {p['total']})",
        *matchup_lines(dex, dex.defense_profile(p["types"]), defending=True),
        f"Evolution: {evolution_text(dex, p['species_id'])}",
        f"Capture rate {p['capture_rate']}, base friendship {p['base_happiness']}, growth rate {p['growth_rate']}",
    ]
    if p["flavor_text"]:
        lines.append(f"Pokedex entry: {p['flavor_text']}")
    if with_moves:
        learnset = dex.learnset(pid)
        level_up = [f"{m['name']} (L{m['level']})" for m in learnset if m["method"] == "level-up"]
        machine = sorted({m["name"] for m in learnset if m["method"] == "machine"})
        lines.append(f"Level-up moves: {', '.join(level_up) or 'none'}")
        if machine:
            lines.append(f"TM moves ({len(machine)}): {', '.join(machine[:25])}{' ...' if len(machine) > 25 else ''}")
    return "\n".join(lines)


def move_sheet(dex: Pokedex, mid: int) -> str:
    m = dex.move(mid)
    learners = dex.pokemon_learning(mid)
    accuracy = f"{m['accuracy']}%" if m["accuracy"] else "never misses"
    return "\n".join([
        f"MOVE: {m['name']} (Generation {m['generation']})",
        f"Type {dex.type_names[m['type']]}, category {m['damage_class']}, power {m['power'] or '-'}, "
        f"accuracy {accuracy}, PP {m['pp']}, priority {m['priority']:+d}",
        f"Effect: {m['effect'][:500]}",
        f"Learned by {len(learners)} Pokemon, e.g. {', '.join(learners[:12]) or 'none'}",
    ])


def ability_sheet(dex: Pokedex, aid: int) -> str:
    a = dex.ability(aid)
    holders = dex.pokemon_with_ability(aid)
    names = [f"{h['name']}{' (hidden)' if h['is_hidden'] else ''}" for h in holders]
    return "\n".join([
        f"ABILITY: {a['name']} (Generation {a['generation']})",
        f"Effect: {a['effect'][:600]}",
        f"Pokemon with it ({len(names)}): {', '.join(names[:25])}{' ...' if len(names) > 25 else ''}",
    ])


def item_sheet(dex: Pokedex, iid: int) -> str:
    i = dex.item(iid)
    cost = f", costs {i['cost']}" if i["cost"] else ""
    return f"ITEM: {i['name']} (category {i['category']}{cost})\nEffect: {i['effect'][:500]}"


def type_sheet(dex: Pokedex, type_ident: str) -> str:
    notable = dex.pokemon_of_type(type_ident, limit=8)
    fastest = dex.top("speed", type_ident, limit=3)
    return "\n".join([
        f"TYPE: {dex.type_names[type_ident]}",
        "As an attacking type -> " + "; ".join(matchup_lines(dex, dex.offense_profile(type_ident), defending=False)),
        "As a defending type -> " + "; ".join(matchup_lines(dex, dex.defense_profile([type_ident]), defending=True)),
        f"Strongest {dex.type_names[type_ident]} Pokemon by base stat total: "
        + ", ".join(f"{p['name']} ({p['value']})" for p in notable),
        "Fastest: " + ", ".join(f"{p['name']} ({p['value']})" for p in fastest),
    ])


def relation_facts(dex: Pokedex, entities: list[Entity]) -> list[str]:
    """Facts that connect two mentioned things, which small models can't reliably derive themselves."""
    facts = []
    pokes = [e for e in entities if e.kind == "pokemon"]
    for p in pokes:
        info = dex.pokemon(p.id)
        for e in entities:
            if e.kind == "move":
                how = dex.can_learn(p.id, e.id)
                facts.append(f"{p.name} {'CAN learn ' + e.name + ' (' + ', '.join(how) + ')' if how else 'CANNOT learn ' + e.name}.")
            elif e.kind == "ability":
                has = any(a["id"] == e.id for a in info["abilities"])
                facts.append(f"{p.name} {'HAS' if has else 'does NOT have'} the ability {e.name}.")
            elif e.kind == "type":
                ident = next(k for k, v in dex.type_names.items() if v == e.name)
                facts.append(f"{e.name}-type moves deal {dex.multiplier(ident, info['types']):g}x damage to {p.name}.")
    if len(pokes) >= 2:
        a, b = pokes[0], pokes[1]
        pa, pb = dex.pokemon(a.id), dex.pokemon(b.id)
        for (x, px), (y, py) in (((a, pa), (b, pb)), ((b, pb), (a, pa))):
            best = max(px["types"], key=lambda t: dex.multiplier(t, py["types"]))
            facts.append(f"{x.name}'s best same-type (STAB) attack type, {dex.type_names[best]}, deals "
                         f"{dex.multiplier(best, py['types']):g}x damage to {y.name}.")
        faster = a.name if pa["speed"] > pb["speed"] else b.name if pb["speed"] > pa["speed"] else "neither (speed tie)"
        facts.append(f"Base stat totals: {a.name} {pa['total']} vs {b.name} {pb['total']}. Faster: {faster}.")
    types = [next(k for k, v in dex.type_names.items() if v == e.name) for e in entities if e.kind == "type"]
    for t1 in types:
        for t2 in types:
            if t1 != t2:
                facts.append(f"{dex.type_names[t1]} attacks deal {dex.multiplier(t1, [t2]):g}x damage to {dex.type_names[t2]} Pokemon.")
    return facts


def superlative_facts(dex: Pokedex, question: str, entities: list[Entity]) -> list[str]:
    """Answer 'fastest/strongest/heaviest ... (fire) pokemon' style questions with a ranked list."""
    lowered = question.lower()
    stat = next((s for w, s in sorted(STAT_WORDS.items(), key=lambda kv: -len(kv[0])) if w in lowered), None)
    if stat is None or not any(w in lowered for w in ("est ", "est?", "highest", "lowest", "most", "least", "best", "top", "rank")) and not lowered.rstrip("?").endswith("est"):
        return []
    type_ident = next((next(k for k, v in dex.type_names.items() if v == e.name) for e in entities if e.kind == "type"), None)
    ascending = any(w in lowered for w in ASCENDING_WORDS)
    rows = dex.top(stat, type_ident, ascending=ascending, limit=10)
    label = {"total": "base stat total", "weight": "weight (kg)", "height": "height (m)"}.get(stat, STAT_LABELS.get(stat, stat))
    scope = f"{dex.type_names[type_ident]}-type " if type_ident else ""
    order = "lowest" if ascending else "highest"
    ranked = ", ".join(f"{i}. {r['name']} ({r['value']})" for i, r in enumerate(rows, 1))
    return [f"RANKING - {scope}Pokemon with the {order} {label} (default forms only): {ranked}"]


def general_facts(dex: Pokedex) -> str:
    c = dex.counts()
    return (f"DATABASE: {c['species']} Pokemon species across Generations 1-{c['generations']} ({c['forms']} including "
            f"alternate forms), {c['types']} types, {c['moves']} moves, {c['abilities']} abilities, {c['items']} items.")


def sheet_for(dex: Pokedex, entity: Entity, with_moves: bool = True) -> str:
    if entity.kind == "pokemon":
        return pokemon_sheet(dex, entity.id, with_moves=with_moves)
    if entity.kind == "move":
        return move_sheet(dex, entity.id)
    if entity.kind == "ability":
        return ability_sheet(dex, entity.id)
    if entity.kind == "item":
        return item_sheet(dex, entity.id)
    return type_sheet(dex, next(k for k, v in dex.type_names.items() if v == entity.name))


def gather(dex: Pokedex, question: str, entities: list[Entity]) -> str:
    """Everything the model needs to answer `question`, kept small enough for a 4k context."""
    many = len(entities) > 2
    lowered = question.lower()
    want_moves = not many or any(w in lowered for w in ("move", "learn", "attack"))
    parts = [sheet_for(dex, e, with_moves=want_moves) for e in entities[:4]]
    extra = relation_facts(dex, entities) + superlative_facts(dex, question, entities)
    if extra:
        parts.append("RELATIONS:\n" + "\n".join(f"- {f}" for f in extra))
    parts.append(general_facts(dex))
    return "\n\n".join(parts)
