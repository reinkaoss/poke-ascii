"""Routes a user message to a battle, a Pokémon display, or a grounded answer."""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass, field

from pokebot import facts
from pokebot.pokedex import STAT_LABELS, Entity, Pokedex

BATTLE_WORDS = re.compile(r"\b(fight|battle|vs\.?|versus|who (would|will|wins)|win against|simulate|duel|beat)\b", re.I)
EVOLVE_WORDS = re.compile(r"\b(evolv\w*|evolution\w*)\b", re.I)
EGG_WORDS = re.compile(r"\b(hatch\w*|eggs?)\b", re.I)
SHOW_WORDS = re.compile(r"\b(show|draw|picture|image|ascii|art|look(s)? like|display|see)\b", re.I)
STAT_NAMES = {"hp": "HP", "attack": "Attack", "defense": "Defense", "sp_attack": "Special Attack",
              "sp_defense": "Special Defense", "speed": "Speed", "total": "base stat total"}
COMPARISONS = {  # word -> (field, unit, whether a bigger value wins)
    "faster": ("speed", "", True), "slower": ("speed", "", False),
    "heavier": ("weight", " kg", True), "lighter": ("weight", " kg", False),
    "taller": ("height", " m", True), "shorter": ("height", " m", False),
    "bulkier": ("hp", "", True), "stronger": ("total", "", True), "weaker": ("total", "", False),
}
SENTENCE_END = re.compile(r"(?<=[.!?])\s+")
FOLLOW_UP = re.compile(r"\b(it|its|it's|they|their|them|this|that|he|she|him|her)\b", re.I)


@dataclass
class Plan:
    intent: str  # "battle" | "show" | "evolve" | "answer" | "unknown"
    entities: list[Entity] = field(default_factory=list)
    battle: tuple[int, int] | None = None
    key_fact: str | None = None
    facts: str = ""


def entity_dict(e: Entity) -> dict:
    return {"kind": e.kind, "id": e.id, "name": e.name}


class Assistant:
    def __init__(self, dex: Pokedex, llm=None):
        self.dex = dex
        self.llm = llm

    # ---------------------------------------------------------------- routing
    def plan(self, question: str, context: list[dict] | None = None) -> Plan:
        entities = self.dex.find_entities(question)
        if EGG_WORDS.search(question):
            entities = [e for e in entities if not (e.kind == "item" and e.name == "Egg")]
        if not entities and EGG_WORDS.search(question):
            pid = self.random_hatchling()
            entity = Entity("pokemon", pid, self.dex.pokemon(pid)["name"])
            return Plan("evolve", [entity], key_fact="Oh? An Egg is about to hatch! Watch the scene to see what comes out.")
        short_follow_up = len(question.split()) <= 6 and not SHOW_WORDS.search(question) and not BATTLE_WORDS.search(question)
        if not entities and context and (FOLLOW_UP.search(question) or short_follow_up):
            entities = [Entity(c["kind"], c["id"], c["name"]) for c in context]
        pokes = [e for e in entities if e.kind == "pokemon"]
        if pokes and (EVOLVE_WORDS.search(question) or EGG_WORDS.search(question)) and not BATTLE_WORDS.search(question):
            p = self.dex.pokemon(pokes[0].id)
            key = self.evolution_answer(p)
            return Plan("evolve", entities, key_fact=key, facts=facts.gather(self.dex, question, entities))
        if BATTLE_WORDS.search(question) and pokes:
            if len(pokes) == 1 and "random" in question.lower():
                rid = self.dex.random_pokemon()
                pokes.append(Entity("pokemon", rid, self.dex.pokemon(rid)["name"]))
            if len(pokes) >= 2:
                return Plan("battle", entities, battle=(pokes[0].id, pokes[1].id))
        if SHOW_WORDS.search(question) and pokes and len(question.split()) <= 8:
            return Plan("show", entities, key_fact=self.describe(pokes[0].id), facts=facts.gather(self.dex, question, entities))
        ranking = facts.superlative_facts(self.dex, question, entities)
        if not entities and not ranking:
            return Plan("unknown")
        key = self.key_fact(question, entities)
        return Plan("answer", entities, key_fact=key, facts=facts.gather(self.dex, question, entities))

    def random_hatchling(self) -> int:
        """A random first-stage Pokémon that can come from an Egg."""
        row = self.dex.q1(
            "SELECT p.id FROM species s JOIN pokemon p ON p.species_id = s.id AND p.is_default = 1 "
            "WHERE s.evolves_from IS NULL AND NOT s.is_legendary AND NOT s.is_mythical ORDER BY random() LIMIT 1")
        return row["id"]

    # ---------------------------------------------------------------- answers
    def evolution_answer(self, p: dict) -> str:
        text = facts.evolution_text(self.dex, p["species_id"], short=True)
        if text == "Does not evolve.":
            return f"{p['name']} does not evolve."
        return f"{p['name']}'s evolution family:\n· " + text.replace(" | ", "\n· ")

    def describe(self, pid: int) -> str:
        p = self.dex.pokemon(pid)
        types = " / ".join(self.dex.type_names[t] for t in p["types"])
        tags = " legendary" if p["is_legendary"] else " mythical" if p["is_mythical"] else ""
        top = max(p["stats"].values())
        best = [STAT_NAMES[s] for s, v in p["stats"].items() if v == top]
        best_text = (f"Its best stat is {best[0]} ({top})" if len(best) == 1
                     else f"Its stats are all {top}" if len(best) == 6 else f"Its best stats are {' and '.join(best)} ({top})")
        return (f"{p['name']} is a{'n' if types[0] in 'AEIOU' else ''} {types}-type{tags} {p['genus']} from "
                f"Generation {p['generation']} (National Dex #{p['species_id']}). {best_text}, with a base "
                f"stat total of {p['total']}. {p['flavor_text']}")

    def key_fact(self, question: str, entities: list[Entity]) -> str | None:
        """A direct, verified answer for the most common question shapes."""
        q = question.lower()
        dex = self.dex
        pokes = [e for e in entities if e.kind == "pokemon"]
        moves = [e for e in entities if e.kind == "move"]
        abilities = [e for e in entities if e.kind == "ability"]
        types = [next(k for k, v in dex.type_names.items() if v == e.name) for e in entities if e.kind == "type"]
        ranking = facts.superlative_facts(dex, question, entities)
        if ranking:
            return ranking[0].replace("RANKING - ", "")
        if pokes and moves:
            p, m = pokes[0], moves[0]
            how = dex.can_learn(p.id, m.id)
            return f"Yes, {p.name} can learn {m.name} ({', '.join(how)})." if how else f"No, {p.name} cannot learn {m.name} in the latest games."
        if pokes and abilities:
            p, a = pokes[0], abilities[0]
            info = dex.pokemon(p.id)
            match = next((x for x in info["abilities"] if x["id"] == a.id), None)
            if match:
                return f"Yes, {p.name} can have {a.name}{' as its hidden ability' if match['is_hidden'] else ''}."
            names = ", ".join(x["name"] for x in info["abilities"])
            return f"No, {p.name} cannot have {a.name}. Its abilities are: {names}."
        if pokes and types and any(w in q for w in ("effective", "weak", "strong", "good", "resist", "damage")):
            p = pokes[0]
            mult = dex.multiplier(types[0], dex.pokemon(p.id)["types"])
            return f"{dex.type_names[types[0]]}-type moves deal {mult:g}x damage to {p.name}."
        if len(types) >= 2 and not pokes:
            a, b = types[0], types[1]
            mult = dex.multiplier(a, [b])
            verdict = {0: "has no effect on", 0.5: "is not very effective against", 1: "deals normal damage to", 2: "is super effective against"}[mult]
            return f"{dex.type_names[a]} {verdict} {dex.type_names[b]} ({mult:g}x)."
        comparison = next(((word, how) for word, how in COMPARISONS.items() if re.search(rf"\b{word}\b", q)), None)
        if len(pokes) >= 2 and comparison:
            word, (field, unit, bigger) = comparison
            a, b = dex.pokemon(pokes[0].id), dex.pokemon(pokes[1].id)
            va, vb = (a["stats"][field], b["stats"][field]) if field in a["stats"] else (a[field], b[field])
            label = STAT_NAMES.get(field, field)
            if va == vb:
                return f"{a['name']} and {b['name']} are tied on {label} ({va:g}{unit})."
            first, second = (a, b) if (va > vb) == bigger else (b, a)
            hi, lo = (va, vb) if first is a else (vb, va)
            return f"{first['name']} is {word} than {second['name']} ({label} {hi:g}{unit} vs {lo:g}{unit})."
        if pokes:
            p = dex.pokemon(pokes[0].id)
            if "evol" in q:
                return self.evolution_answer(p)
            if any(w in q for w in ("weak", "counter", "beat", "strong against", "resist", "immune")):
                prof = dex.defense_profile(p["types"])
                weak = [f"{dex.type_names[t]} ({m:g}x)" for m in (4.0, 2.0) for t in prof.get(m, [])]
                resist = [dex.type_names[t] for m in (0.5, 0.25) for t in prof.get(m, [])]
                immune = [dex.type_names[t] for t in prof.get(0.0, [])]
                text = f"{p['name']} is weak to {', '.join(weak) or 'nothing'}"
                text += f"; resists {', '.join(resist)}" if resist else ""
                text += f"; and is immune to {', '.join(immune)}" if immune else ""
                return text + "."
            if "abilit" in q:
                return f"{p['name']}'s abilities: " + "; ".join(
                    f"{a['name']}{' (hidden)' if a['is_hidden'] else ''} - {a['short_effect'].rstrip('.')}" for a in p["abilities"]) + "."
            if any(w in q for w in ("stat", "fast", "speed", "strong", "hp", "attack", "defense")):
                return f"{p['name']}'s base stats: " + ", ".join(f"{STAT_NAMES[s]} {v}" for s, v in p["stats"].items()) + f" (total {p['total']})."
            if len(entities) == 1 or any(w in q for w in ("type", "what is", "who is", "tell me", "about")):
                return self.describe(pokes[0].id)
        if types and len(entities) == 1:
            t = types[0]
            off = dex.offense_profile(t)
            dfn = dex.defense_profile([t])
            name = dex.type_names[t]
            return (f"{name} attacks are super effective against {facts._type_list(dex, off.get(2.0, []))}. "
                    f"{name} Pokémon are weak to {facts._type_list(dex, dfn.get(2.0, []))}.")
        if entities and entities[0].kind in ("move", "ability", "item"):
            e = entities[0]
            record = {"move": dex.move, "ability": dex.ability, "item": dex.item}[e.kind](e.id)
            return f"{e.name}: {record['short_effect']}"
        return None

    def offline_answer(self, plan: Plan) -> str:
        if plan.key_fact:
            return plan.key_fact
        first = plan.entities[0]
        if first.kind == "pokemon":
            return self.describe(first.id)
        return facts.sheet_for(self.dex, first, with_moves=False)

    def answer_stream(self, question: str, plan: Plan, history: list[dict] | None = None) -> Iterator[str]:
        if plan.intent == "unknown":
            yield ("I couldn't spot a Pokémon, move, ability, item or type in that. Try something like "
                   "\"what is Gengar weak to?\", \"show me Eevee\" or \"Pikachu vs Squirtle\".")
            return
        # The scene shows the evolution; a small model only invents breeding trivia around it.
        if self.llm is None or plan.intent == "evolve":
            yield self.offline_answer(plan)
            return
        if plan.key_fact:
            yield plan.key_fact
            known = set(re.findall(r"\w+", plan.key_fact.lower()))
            buffer = ""
            for token in self.llm.stream(question, plan.facts, history, key_fact=plan.key_fact, max_tokens=120):
                buffer += token
                *sentences, buffer = SENTENCE_END.split(buffer)
                for sentence in sentences:
                    if not self._adds_to(sentence, known):
                        continue
                    yield " " + sentence.strip()
            if buffer.strip() and self._adds_to(buffer, known):
                yield " " + buffer.strip()
            return
        yield from self.llm.stream(question, plan.facts, history)

    @staticmethod
    def _adds_to(sentence: str, known: set[str]) -> bool:
        """False when the small model restates (or contradicts with yes/no) the verified sentence."""
        words = set(re.findall(r"\w+", sentence.lower()))
        if not words or re.match(r"\s*(yes|no)\b", sentence, re.I):
            return False
        return len(words - known) / len(words) > 0.4

    def battle_summary(self, result: dict) -> str:
        a, b = result["a"], result["b"]
        odds = result["percent"]
        winner = {"a": a["name"], "b": b["name"]}.get(result["winner"])
        turns = f"{result['turns']} turn{'s' if result['turns'] != 1 else ''}"
        verdict = f"{winner} won in {turns}" if winner else f"It ended in a draw after {turns}"
        draws = f" (draws {odds['draw']}%)" if odds["draw"] else ""
        return f"{verdict}. Over 200 re-runs {a['name']} wins {odds['a']}% of the time and {b['name']} {odds['b']}%{draws}."
