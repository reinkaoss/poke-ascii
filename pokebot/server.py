"""Local web server: serves the ASCII front end and a small JSON API (standard library only)."""

from __future__ import annotations

import json
import mimetypes
import os
import threading
import traceback
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from pokebot import config, facts
from pokebot.assistant import Assistant, entity_dict
from pokebot.battle import simulate
from pokebot.pokedex import Pokedex, normalize

WEB_DIR = config.PACKAGE_DIR / "web"
mimetypes.add_type("text/javascript", ".js")
mimetypes.add_type("font/woff2", ".woff2")


def card(dex: Pokedex, pid: int) -> dict:
    p = dex.pokemon(pid)
    return {
        "id": p["id"], "name": p["name"], "species_id": p["species_id"], "genus": p["genus"],
        "generation": p["generation"], "types": p["types"], "stats": p["stats"], "total": p["total"],
        "height": p["height"], "weight": p["weight"], "legendary": bool(p["is_legendary"]),
        "mythical": bool(p["is_mythical"]), "flavor": p["flavor_text"],
        "abilities": [{"name": a["name"], "hidden": bool(a["is_hidden"]), "effect": a["short_effect"]} for a in p["abilities"]],
        "evolution": facts.evolution_text(dex, p["species_id"]),
        "weaknesses": {str(k): v for k, v in dex.defense_profile(p["types"]).items()},
        "ascii": dex.ascii(p["id"]),
        "sprite": f"/api/sprite/{p['id']}.webp",
        "forms": [{"id": r["id"], "name": r["name"]} for r in dex.q(
            "SELECT id, name FROM pokemon WHERE species_id = ? ORDER BY is_default DESC, id", p["species_id"])],
    }


def type_list(row) -> list[str]:
    return [t for t in (row["type1"], row["type2"]) if t]


CANNOT_HATCH_EXCEPT = {489, 490}  # Phione and Manaphy are mythical but come from Eggs
PRIMAL_ORBS = {"kyogre": "Blue Orb", "groudon": "Red Orb"}


def mega_condition(dex: Pokedex, species_identifier: str, form_identifier: str) -> str:
    """How a mega/primal form is triggered. PokeAPI doesn't link Mega Stones to Pokémon, so the stone is the
    one sharing the longest name prefix with the species (Blastoise → Blastoisinite) and the same X/Y suffix."""
    if species_identifier in PRIMAL_ORBS:
        return f"holding the {PRIMAL_ORBS[species_identifier]}"
    if species_identifier == "rayquaza":
        return "knowing Dragon Ascent"
    suffix = form_identifier.rsplit("-mega", 1)[1]  # "", "-x", "-y" or "-z"
    stones = [s for s in dex.q("SELECT identifier, name FROM items WHERE category = 'Mega Stones'")
              if s["identifier"].endswith(suffix) and (suffix or s["identifier"][-2:-1] != "-")]

    def shared(stone) -> int:
        return len(os.path.commonprefix([stone["identifier"], species_identifier]))

    best = max(stones, key=shared, default=None)
    return f"holding {best['name']}" if best and shared(best) >= 4 else "holding its Mega Stone"


def evolution(dex: Pokedex, pid: int) -> dict:
    """The whole evolution family of a Pokémon, for the egg → hatch → evolve scene.

    Nodes are keyed by Pokémon id: species use their default form, Mega and Primal forms hang off their species.
    """
    p = dex.pokemon(pid)
    rows = dex.q(
        "SELECT s.id, s.identifier, s.name, s.evolves_from, s.evolution_condition, s.is_baby, s.is_legendary, "
        "s.is_mythical, p.height, p.type1, p.type2 FROM species s "
        "JOIN pokemon p ON p.species_id = s.id AND p.is_default = 1 WHERE s.evolution_chain = ? ORDER BY s.id",
        p["evolution_chain"])
    ids = {r["id"] for r in rows}
    nodes = [{
        "id": r["id"], "species_id": r["id"], "name": r["name"], "mega": False,
        "parent": r["evolves_from"] if r["evolves_from"] in ids else None,
        # Multi-location methods list every game; the shortest alternative reads best on screen.
        "condition": min(r["evolution_condition"].split("; or "), key=len) if r["evolution_condition"] else None,
        "baby": bool(r["is_baby"]), "height": r["height"], "types": type_list(r),
    } for r in rows]
    species_ident = {r["id"]: r["identifier"] for r in rows}
    for f in dex.q(
            "SELECT id, identifier, name, species_id, height, type1, type2 FROM pokemon WHERE is_default = 0 "
            "AND (identifier LIKE '%-mega%' OR identifier LIKE '%-primal') "
            f"AND species_id IN ({','.join('?' * len(ids))}) ORDER BY id", *ids):
        nodes.append({
            "id": f["id"], "species_id": f["species_id"], "name": f["name"], "mega": True, "parent": f["species_id"],
            "condition": mega_condition(dex, species_ident[f["species_id"]], f["identifier"]),
            "baby": False, "height": f["height"], "types": type_list(f),
        })
    root = next(r for r in rows if r["evolves_from"] not in ids)
    rare = root["is_legendary"] or root["is_mythical"]
    target = p["id"] if any(n["id"] == p["id"] for n in nodes) else p["species_id"]
    return {"target": target, "nodes": nodes, "hatches": not rare or root["id"] in CANNOT_HATCH_EXCEPT}


def dex_index(dex: Pokedex) -> list[list]:
    """Compact list of every species for the Pokédex browser: [species, name, generation, types, pokemon id, tag]."""
    rows = dex.q(
        "SELECT s.id, s.name, s.generation, s.is_legendary, s.is_mythical, p.id AS pid, p.type1, p.type2 "
        "FROM species s JOIN pokemon p ON p.species_id = s.id AND p.is_default = 1 ORDER BY s.id")
    return [[r["id"], r["name"], r["generation"], [t for t in (r["type1"], r["type2"]) if t], r["pid"],
             "legendary" if r["is_legendary"] else "mythical" if r["is_mythical"] else ""] for r in rows]


class App:
    def __init__(self, dex: Pokedex, llm=None):
        self.dex = dex
        self.llm = llm
        self.assistant = Assistant(dex, llm)
        names = dex.q("SELECT id, name, species_id, is_default FROM pokemon ORDER BY is_default DESC, species_id")
        self.suggestions = [(normalize(r["name"]), r["name"], r["id"]) for r in names]
        self.dex_index = dex_index(dex)

    def resolve_pokemon(self, value) -> int:
        if isinstance(value, int):
            return value
        entity = self.dex.resolve(str(value), "pokemon")
        if entity is None:
            raise LookupError(f"Unknown Pokémon: {value}")
        return entity.id

    def battle(self, a, b, level: int = 50, seed: int | None = None) -> dict:
        a_id, b_id = self.resolve_pokemon(a), self.resolve_pokemon(b)
        result = simulate(self.dex, a_id, b_id, level=max(1, min(100, int(level))), seed=seed)
        result["cards"] = {"a": card(self.dex, a_id), "b": card(self.dex, b_id)}
        result["summary"] = self.assistant.battle_summary(result)
        return result


class Handler(BaseHTTPRequestHandler):
    app: App
    server_version = "pokebot"

    def log_message(self, fmt, *args) -> None:
        if not self.path.startswith(("/api/sprite", "/lib", "/mf", "/fonts")):
            super().log_message(fmt, *args)

    # ---------------------------------------------------------------- responses
    def send_json(self, data, status: int = 200) -> None:
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_bytes(self, body: bytes, content_type: str, cache: bool = False) -> None:
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "public, max-age=86400" if cache else "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(length) or b"{}")

    # ---------------------------------------------------------------- routing
    def do_GET(self) -> None:
        url = urlparse(self.path)
        query = {k: v[0] for k, v in parse_qs(url.query).items()}
        try:
            if url.path == "/api/status":
                llm = self.app.llm
                return self.send_json({"llm": llm.name if llm else None, "counts": self.app.dex.counts()})
            if url.path.startswith("/api/sprite/"):
                table = "back_sprites" if url.path.startswith("/api/sprite/back/") else "sprites"
                row = self.app.dex.q1(f"SELECT webp FROM {table} WHERE pokemon_id = ?", int(Path(url.path).stem))
                if row is None:
                    return self.send_error(HTTPStatus.NOT_FOUND)
                return self.send_bytes(row["webp"], "image/webp", cache=True)
            if url.path.startswith("/api/pokemon/"):
                pid = self.app.resolve_pokemon(unquote(url.path.removeprefix("/api/pokemon/")))
                return self.send_json(card(self.app.dex, pid))
            if url.path.startswith("/api/evolution/"):
                pid = self.app.resolve_pokemon(unquote(url.path.removeprefix("/api/evolution/")))
                return self.send_json(evolution(self.app.dex, pid))
            if url.path == "/api/dex":
                return self.send_json(self.app.dex_index)
            if url.path == "/api/random":
                return self.send_json(card(self.app.dex, self.app.dex.random_pokemon()))
            if url.path == "/api/suggest":
                prefix = normalize(query.get("q", ""))
                hits = [{"name": n, "id": i} for key, n, i in self.app.suggestions if prefix and key.startswith(prefix)][:8]
                return self.send_json(hits)
            if url.path == "/api/battle":
                return self.send_json(self.app.battle(query["a"], query["b"], int(query.get("level", 50)),
                                                      int(query["seed"]) if query.get("seed") else None))
            return self.serve_static(url.path)
        except LookupError as exc:
            self.send_json({"error": str(exc)}, 404)
        except (KeyError, ValueError) as exc:
            self.send_json({"error": f"Bad request: {exc}"}, 400)

    def do_POST(self) -> None:
        url = urlparse(self.path)
        try:
            body = self.read_json()
            if url.path == "/api/ask":
                return self.stream_answer(body)
            if url.path == "/api/battle":
                return self.send_json(self.app.battle(body["a"], body["b"], body.get("level", 50), body.get("seed")))
            self.send_error(HTTPStatus.NOT_FOUND)
        except LookupError as exc:
            self.send_json({"error": str(exc)}, 404)
        except (KeyError, ValueError, json.JSONDecodeError) as exc:
            self.send_json({"error": f"Bad request: {exc}"}, 400)

    def serve_static(self, path: str) -> None:
        target = (WEB_DIR / unquote(path).lstrip("/")).resolve()
        if path in ("", "/"):
            target = WEB_DIR / "index.html"
        if not target.is_relative_to(WEB_DIR.resolve()) or not target.is_file():
            return self.send_error(HTTPStatus.NOT_FOUND)
        content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        self.send_bytes(target.read_bytes(), content_type, cache=target.suffix == ".woff2")

    # ---------------------------------------------------------------- streaming
    def stream_answer(self, body: dict) -> None:
        """Newline-delimited JSON events: plan, card/battle, token..., done."""
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()

        def emit(event: dict) -> None:
            self.wfile.write((json.dumps(event, ensure_ascii=False) + "\n").encode())
            self.wfile.flush()

        app = self.app
        question = str(body.get("question", "")).strip()[:500]
        try:
            plan = app.assistant.plan(question, body.get("context"))
            emit({"type": "plan", "intent": plan.intent, "entities": [entity_dict(e) for e in plan.entities]})
            if plan.intent == "battle":
                result = app.battle(*plan.battle, level=int(body.get("level", 50)))
                emit({"type": "battle", "result": result})
                emit({"type": "token", "text": result["summary"]})
                emit({"type": "done", "source": "simulator", "context": [entity_dict(e) for e in plan.entities]})
                return
            first = next((e for e in plan.entities if e.kind == "pokemon"), None)
            if first:
                emit({"type": "card", "card": card(app.dex, first.id)})
            if plan.intent == "evolve" and first:
                emit({"type": "evolve", "evolution": evolution(app.dex, first.id)})
            for text in app.assistant.answer_stream(question, plan, body.get("history")):
                emit({"type": "token", "text": text})
            source = "llm" if app.llm and plan.intent not in ("unknown", "evolve") else "pokedex"
            emit({"type": "done", "source": source, "context": [entity_dict(e) for e in plan.entities]})
        except BrokenPipeError:
            pass
        except Exception as exc:  # report to the client instead of silently dropping the stream
            traceback.print_exc()
            emit({"type": "error", "message": str(exc)})


def serve(host: str = "127.0.0.1", port: int = 8765, use_llm: bool = True, open_browser: bool = False) -> None:
    from pokebot import llm as llm_module

    dex = Pokedex()
    model = llm_module.load() if use_llm else None
    Handler.app = App(dex, model)
    httpd = ThreadingHTTPServer((host, port), Handler)
    url = f"http://{host}:{port}/"
    print(f"pokebot running at {url}  ({'LLM: ' + model.name if model else 'no LLM - template answers'})  Ctrl+C to stop")
    if open_browser:
        threading.Timer(0.5, webbrowser.open, args=(url,)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        httpd.server_close()
