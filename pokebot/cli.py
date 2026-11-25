"""Terminal entry point: `python -m pokebot <command>`."""

from __future__ import annotations

import argparse
import shutil
import sys

from pokebot import config, facts
from pokebot.battle import simulate
from pokebot.pokedex import Pokedex


def hp_bar(hp: int, max_hp: int, width: int = 20) -> str:
    filled = round(width * hp / max_hp) if max_hp else 0
    return f"[{'█' * filled}{'░' * (width - filled)}] {hp:>3}/{max_hp}"


MIRROR = str.maketrans("()/\\<>[]{}dbpq", ")(\\/><][}{bdqp")


def scale_art(art: str, rows: int, max_cols: int, mirror: bool = False) -> list[str]:
    """Trim ASCII art, resample it to `rows` lines (nearest neighbour, never wider than `max_cols`)."""
    lines = [line for line in art.split("\n")]
    indent = min((len(line) - len(line.lstrip()) for line in lines if line.strip()), default=0)
    lines = [line[indent:] for line in lines]
    width = max(map(len, lines), default=1)
    lines = [line.ljust(width) for line in lines]
    scale = min(rows / len(lines), max_cols / width)
    out = []
    for i in range(max(1, round(len(lines) * scale))):
        src = lines[min(len(lines) - 1, int(i / scale))]
        row = "".join(src[min(width - 1, int(j / scale))] for j in range(max(1, round(width * scale))))
        out.append(row[::-1].translate(MIRROR) if mirror else row)
    return out


def side_by_side(left: list[str], right: list[str], gap: int = 6) -> str:
    """Both drawings standing on the same ground line."""
    rows = max(len(left), len(right))
    width = max(map(len, left), default=0)
    left = [""] * (rows - len(left)) + left
    right = [""] * (rows - len(right)) + right
    return "\n".join(f"{x:<{width}}{' ' * gap}{y}".rstrip() for x, y in zip(left, right))


def battle_art(dex: Pokedex, result: dict, columns: int) -> str | None:
    """The two fighters facing each other, sized by their real heights."""
    arts = [dex.ascii(result[s]["pokemon_id"]) for s in ("a", "b")]
    if not all(arts):
        return None
    heights = [max(0.1, dex.pokemon(result[s]["pokemon_id"])["height"]) for s in ("a", "b")]
    tallest = max(heights)
    max_cols = (columns - 6) // 2
    drawn = [scale_art(art, round(22 * max(0.4, (h / tallest) ** 0.5)), max_cols, mirror=(i == 0))
             for i, (art, h) in enumerate(zip(arts, heights))]
    return side_by_side(*drawn)


def need(dex: Pokedex, name):
    if isinstance(name, int):
        return dex.resolve(str(dex.pokemon(name)["name"]), "pokemon")
    entity = dex.resolve(name, "pokemon")
    if entity is None:
        sys.exit(f"Unknown Pokémon: {name}")
    return entity


def cmd_show(dex: Pokedex, args) -> None:
    entity = need(dex, " ".join(args.name))
    art = dex.ascii(entity.id)
    if art:
        print(art)
    print()
    print(facts.pokemon_sheet(dex, entity.id, with_moves=args.moves))


def cmd_info(dex: Pokedex, args) -> None:
    text = " ".join(args.name)
    entity = dex.resolve(text, args.kind)
    if entity is None:
        sys.exit(f"Nothing found for: {text}")
    print(facts.sheet_for(dex, entity))


def cmd_ask(dex: Pokedex, args) -> None:
    from pokebot import llm
    from pokebot.assistant import Assistant

    question = " ".join(args.question)
    assistant = Assistant(dex, None if args.no_llm else llm.load(quiet=True))
    plan = assistant.plan(question)
    if args.facts:
        print(plan.facts or "(no facts found)")
        return
    if plan.intent == "battle":
        args.a, args.b, args.level, args.seed = *plan.battle, 50, None
        return cmd_fight(dex, args)
    if plan.intent == "show":
        print(dex.ascii(plan.entities[0].id) or "")
    for text in assistant.answer_stream(question, plan):
        print(text, end="", flush=True)
    print()


def cmd_fight(dex: Pokedex, args) -> None:
    a, b = need(dex, args.a), need(dex, args.b)
    result = simulate(dex, a.id, b.id, level=args.level, seed=args.seed)
    columns = shutil.get_terminal_size().columns
    art = battle_art(dex, result, columns) if columns >= 50 else None
    if art:
        print(art)
    for side in ("a", "b"):
        mon = result[side]
        moves = ", ".join(m["name"] for m in mon["moves"])
        print(f"{mon['name']} Lv{mon['level']} ({'/'.join(mon['types'])}, {mon['ability']}): {moves}")
    print()
    hp = {s: (result[s]["max_hp"], result[s]["max_hp"]) for s in ("a", "b")}
    for event in result["events"]:
        if event["t"] == "turn":
            print(f"\n-- {event['text']} --")
            continue
        print(f"  {event['text']}")
        if event["t"] in ("damage", "heal"):
            hp[event["side"]] = (event["hp"], event["max_hp"])
            name = result[event["side"]]["name"]
            print(f"      {name:<14}{hp_bar(*hp[event['side']])}")
    odds = result["percent"]
    print(f"\nOdds over 200 re-runs: {result['a']['name']} {odds['a']}%, {result['b']['name']} {odds['b']}%, "
          f"draw {odds['draw']}%")


def cmd_serve(args) -> None:
    from pokebot.server import serve

    serve(args.host, args.port, use_llm=not args.no_llm, open_browser=args.open)


def cmd_download(args) -> None:
    from pokebot.llm import download_model

    download_model(args.size)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="pokebot", description="Offline Pokémon assistant",
        epilog='With no command it opens the web app; any other text is a question: pokebot "pikachu vs mewtwo"')
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("show", help="ASCII art + Pokédex sheet for a Pokémon")
    p.add_argument("name", nargs="+")
    p.add_argument("--moves", action="store_true", help="include the learnset")
    p.set_defaults(func=cmd_show)

    p = sub.add_parser("info", help="Fact sheet for a Pokémon, move, ability, item or type")
    p.add_argument("name", nargs="+")
    p.add_argument("--kind", choices=["pokemon", "move", "ability", "item", "type"])
    p.set_defaults(func=cmd_info)

    p = sub.add_parser("ask", help="Ask a question in plain English")
    p.add_argument("question", nargs="+")
    p.add_argument("--no-llm", action="store_true", help="answer from templates even if a model is installed")
    p.add_argument("--facts", action="store_true", help="print the facts gathered for the question instead")
    p.set_defaults(func=cmd_ask)

    p = sub.add_parser("serve", help="Start the browser app")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--no-llm", action="store_true")
    p.add_argument("--open", action="store_true", help="open the browser")
    p.set_defaults(func=cmd_serve, needs_dex=False)

    p = sub.add_parser("download-model", help="Download the local LLM (one time, ~1 GB)")
    p.add_argument("--size", choices=sorted(config.MODELS), default=config.DEFAULT_MODEL)
    p.set_defaults(func=cmd_download, needs_dex=False)

    p = sub.add_parser("fight", help="Simulate a 1v1 battle")
    p.add_argument("a")
    p.add_argument("b")
    p.add_argument("--level", type=int, default=50)
    p.add_argument("--seed", type=int)
    p.set_defaults(func=cmd_fight)

    argv = sys.argv[1:] if argv is None else argv
    if not argv:
        argv = ["serve", "--open"]
    else:
        first = next((a for a in argv if not a.startswith("-")), None)
        if first is not None and first not in sub.choices:
            argv = ["ask", *argv]
    args = parser.parse_args(argv)
    if getattr(args, "needs_dex", True):
        args.func(Pokedex(), args)
    else:
        args.func(args)
    return 0
