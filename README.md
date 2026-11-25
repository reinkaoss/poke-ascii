# pokebot

An offline Pokémon assistant: ask questions about Pokémon, moves, abilities, items and types, and
simulate 1v1 battles, in the terminal or in an ASCII-art web UI.

Everything it needs ships in this repo (`pokebot/data/pokedex.sqlite`, ~17 MB), so it runs with no
internet connection. The core needs no third-party Python packages; a small local LLM is optional
and only makes the answers more conversational.

## Requirements

- Python 3.10+
- Optional, for written answers: `llama-cpp-python` and a ~1 GB model file (see below)

## Install (one time)

```bash
uv tool install --editable .        # puts a `pokebot` command on your PATH
# or, inside a virtualenv:
pip install -e .
```

Without installing, `python -m pokebot …` works the same way from the project folder.

## Quick start

```bash
pokebot                             # opens the web app at http://127.0.0.1:8765
pokebot what is gengar weak to      # any text is a question
pokebot pikachu vs charizard        # ...including battles
```

The web app works on phones too: run `pokebot serve --host 0.0.0.0` and open
`http://<your-computer's-ip>:8765` on a device on the same network.

- **chat** (right, or below on phones): ask anything, e.g. `what is gengar weak to?`,
  `can snorlax learn surf?`, `fastest dragon pokemon`, `show me eevee`. Follow-ups like
  `what are its abilities?` work.
- **scope**: an animated ASCII portrait and stat card of the Pokémon you last asked about.
  `[text art]` switches to the original ASCII drawing, `[evolve]` and `[fight]` start the scenes below.
  Species with alternate forms (Mega Evolutions, Gigantamax, regional variants, …) get a form picker.
- **pokédex**: every species, searchable by name or number and filterable by type, generation and
  whether you've seen it yet. Pokémon you look at, battle or evolve are marked as seen.
- **evolution**: `how does eevee evolve?`, `evolve charmander` or `hatch an egg` plays the family as an
  ASCII scene: the Egg wobbles, cracks and hatches, then you press `[evolve]` for each stage. Where a
  Pokémon can evolve in several ways (Eevee, Tyrogue, …) you pick which one, and fully evolved
  Pokémon with a Mega Stone or Primal form can Mega Evolve as a last step.
- **arena**: `pikachu vs charizard` (or `mewtwo vs a random pokemon`) plays the battle as an ASCII
  animation with HP bars, a battle log and win odds, laid out like the Game Boy games: the foe on a
  platform top-right with its HP box top-left, your Pokémon seen from behind bottom-left with its HP
  box bottom-right. Both are sized by their real heights.
- **scene** (top right) changes the ASCII rendering style: full color, phosphor, braille or pixel.

Options: `pokebot serve --port 9000`, `--host 0.0.0.0`, `--no-llm`, `--open`.

## Optional: the local LLM

Without a model, answers come straight from the Pokédex (marked `[pokedex]`). With one, the
assistant still writes the verified answer first and lets the model add context (marked `[llm]`).

```bash
uv tool install --editable ".[llm]"     # or, in a virtualenv: pip install -e ".[llm]"
pokebot download-model                  # Qwen2.5 1.5B Instruct, ~1.1 GB, into ./models
pokebot
```

`download-model --size 0.5b` (~400 MB, faster, weaker) and `--size 3b` (~2 GB) are also available.
On Apple Silicon, install with `CMAKE_ARGS="-DGGML_METAL=on"` to run the model on the GPU. If
Python can't verify HTTPS certificates (common with python.org installers), the download falls
back to `curl` automatically. Set `POKEBOT_MODEL=/path/to/model.gguf` to use any other GGUF model.

## Terminal commands

```bash
pokebot show pikachu                    # ASCII art + Pokédex sheet
pokebot show charizard --moves          # ...including its learnset
pokebot fight pikachu squirtle          # simulate a battle (with win odds)
pokebot fight mewtwo "mega charizard x" --level 100 --seed 42
pokebot info levitate                   # any Pokémon, move, ability, item or type
pokebot info psychic --kind move
pokebot can charizard learn surf        # same as: pokebot ask "can charizard learn surf"
pokebot ask "why is dragonite so popular" --facts   # print the facts used instead
```

Names are forgiving: typos (`charzard`), truncated names (`show me pika`), forms (`alolan vulpix`,
`mega charizard x`) and National Dex numbers (`25`) all work.

## Battles

`fight` (and `X vs Y` in the web UI) picks a moveset automatically from each Pokémon's latest
main-series learnset: strong same-type attacks, type coverage, and at most one useful status move.
It then plays out the battle turn by turn and re-runs it 200 more times silently to estimate win
odds. Pass `--seed` to replay the same battle.

The simulator follows main-series damage rules: stats at the chosen level (IV 31, EV 0, neutral
nature), STAB, the type chart, critical hits, accuracy, priority, speed order, multi-hit moves,
drain and recoil, healing, stat stages, status conditions (burn, poison, toxic, paralysis, sleep,
freeze, confusion), flinching, recharge and two-turn moves, one-hit-KO moves, Transform, and
battle-relevant abilities such as Levitate, Intimidate, Sturdy, Wonder Guard and Blaze/Torrent.

It does not simulate held items, weather, terrain or switching.

## Project layout

```
pokebot/
  data/pokedex.sqlite   bundled database (Pokédex data, ASCII art, sprites)
  data/LICENSES.md      licenses of the bundled data
  pokedex.py            database access and name matching
  facts.py              plain-text fact sheets (answers and LLM context)
  assistant.py          question planning and verified answers
  llm.py                optional local model (llama-cpp-python)
  battle.py             battle simulator
  server.py             web server and JSON/streaming API
  cli.py                terminal commands
  build_db.py           maintainer tool that regenerates the database
  web/
    index.html, shell.*   page shell: layout, theme picker, routing between views
    lib/                  shared code: event bus, API client, OpenASCII scene helper
    lib/openascii.js      vendored OpenASCII runtime
    mf/chat/              <poke-chat>  micro-frontend: streaming Q&A
    mf/scope/             <poke-scope>  micro-frontend: portrait and stat card
    mf/dex/               <poke-dex>    micro-frontend: Pokédex browser
    mf/evolve/            <poke-evolve> micro-frontend: Egg → hatch → evolution scene
    mf/arena/             <poke-arena>  micro-frontend: animated battles
```

The web UI has no build step. Each micro-frontend is a self-contained Web Component that only
talks to the others through events on `lib/bus.js` (`pokemon:focus`, `battle:play`,
`evolve:play`, `chat:ask`, …).

## Rebuilding the database (maintainers only)

You only need this to pull in newer PokeAPI data. It needs Pillow for the sprites:

```bash
pip install pillow
git clone --depth 1 https://github.com/PokeAPI/pokeapi.git ../pokeapi
git clone --depth 1 https://github.com/vsoch/pokemon.git ../vsoch-pokemon
git clone --depth 1 --filter=blob:none --sparse https://github.com/PokeAPI/sprites.git ../sprites
git -C ../sprites sparse-checkout set sprites/pokemon/other/official-artwork sprites/pokemon/back
python -m pokebot.build_db ../pokeapi ../vsoch-pokemon ../sprites
```

vsoch/pokemon provides the ASCII art and artwork up to #1008. PokeAPI/sprites fills in newer
species and every alternate form, plus the back sprites used in battles. For Pokémon vsoch/pokemon
doesn't cover, the ASCII art is generated from that artwork with the same converter vsoch used.
The sprites clone is optional (about 500 MB on disk; only ~6 MB ends up in the database).

## Credits

pokebot only exists thanks to these projects. The full license texts of everything bundled in
`pokedex.sqlite` are in [`pokebot/data/LICENSES.md`](pokebot/data/LICENSES.md).

### Data and artwork (bundled)

| What | Source | License |
| --- | --- | --- |
| Pokédex data: species, forms, stats, types, moves, learnsets, abilities, items, evolutions, flavor text | [PokeAPI](https://github.com/PokeAPI/pokeapi) © Paul Hallett and PokéAPI contributors | BSD 3-Clause |
| ASCII art and artwork for #1–#1008, plus the image-to-ASCII converter reused for the rest | [vsoch/pokemon](https://github.com/vsoch/pokemon) © Vanessa Sochat | MIT |
| Official artwork for #1009–#1025 and alternate forms (Mega, Gigantamax, regional…), battle back sprites | [PokeAPI/sprites](https://github.com/PokeAPI/sprites) | CC0 1.0 (repository); image contents © The Pokémon Company |

### Software

| What | Project | License |
| --- | --- | --- |
| ASCII scene renderer, vendored unmodified as `web/lib/openascii.js` (v1.5.0) | [OpenASCII](https://github.com/saswatsundar123/OpenASCII) © Saswat Sundar Rath | MIT |
| Local model runtime (optional, `.[llm]`) | [llama-cpp-python](https://github.com/abetlen/llama-cpp-python) © Andrei Betlen, built on [llama.cpp](https://github.com/ggml-org/llama.cpp) © the ggml authors | MIT |
| Sprite conversion when rebuilding the database (maintainers only) | [Pillow](https://github.com/python-pillow/Pillow) © Jeffrey A. Clark and contributors | MIT-CMU |

Everything else runs on the Python standard library and plain browser APIs.

### Language model (optional, downloaded on demand)

[Qwen2.5 Instruct](https://huggingface.co/Qwen) by the Qwen team at Alibaba Cloud, in the official
Q4_K_M GGUF builds ([0.5B](https://huggingface.co/Qwen/Qwen2.5-0.5B-Instruct-GGUF),
[1.5B](https://huggingface.co/Qwen/Qwen2.5-1.5B-Instruct-GGUF),
[3B](https://huggingface.co/Qwen/Qwen2.5-3B-Instruct-GGUF)). The 0.5B and 1.5B models are
Apache 2.0; the 3B model uses the Qwen Research License (non-commercial). The model isn't
bundled: `pokebot download-model` fetches it from Hugging Face.

### Trademarks

pokebot is an unofficial fan project for learning purposes. It is not affiliated with, endorsed or
sponsored by Nintendo, Creatures Inc., GAME FREAK inc. or The Pokémon Company. Pokémon and
Pokémon character names are trademarks of Nintendo; Pokémon artwork is © The Pokémon Company.
