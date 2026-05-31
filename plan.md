# ClaudePlaysPokemon — Implementation Plan (Maximized)

**Reference:** `finished.png` — "Claude Plays Pokemon, a Visual Guide"
**Source of truth:** Every box in `finished.png` must map to a build step below.

---

## 1. Endstate Decomposition (from `finished.png`)

The finished diagram defines **four** distinct subsystems. Plan must address all four.

**A. The Tools (4 tools)**
- `update_knowledge_base` — write key:value into long-term memory dict
- `wiki_simulator` — query game info **parsed directly from the ROM via PyBoy**
- `navigator` — A* over the collision grid (already implemented, gated)
- `acknowledgement` — no-op "thinking turn" that returns the latest screen + memory

**B. The Prompt (composed each turn)**
- Tool Definitions
- System Prompt (instructions)
- **Knowledge Base** (injected text, refreshed every turn)
- Summarization blurb (explains the summary convention)
- Conversation History

**C. The Core Loop (5 stages)**
1. Compose Prompt
2. LLM Model call
3. Execute Tools
4. **Check For Summarization**
5. **Save State** (emulator `.state` snapshot to disk)

**D. Summarization / Managing Long Context**
- Trigger when conversation history nears the 200k context window
- LLM #1 writes a summary of the last *n* turns
- Drop the raw history; **inject the summary as the first assistant message** (NOT a user message)
- LLM #2 (the **KB Critic**) inspects LLM #1's knowledge base and returns feedback to enforce KB hygiene

---

## 2. Gap Analysis: Current Repo vs. Endstate

### Already Working
- `press_buttons` tool + `Emulator` wrapper (`agent/emulator.py`)
- `PokemonRedReader` memory extraction (`agent/memory_reader.py`) — money, badges, party, items, dialog, location, coordinates
- Conversation summarization at `max_history` threshold (`SimpleAgent.summarize_history`)
- Prompt caching via `cache_control` ephemeral markers
- `EventStream` JSONL logger (`agent/event_stream.py`)
- `navigate_to` + A* pathfinding (`Emulator.find_path`) — gated behind `USE_NAVIGATOR = False`

### Gaps (mapped to endstate subsystems)

| # | Gap | Endstate Box | Files Affected |
|---|-----|--------------|----------------|
| 1 | Knowledge base persistence layer | A, B | `agent/knowledge_base.py` (new) |
| 2 | `update_knowledge_base` tool | A | `agent/simple_agent.py` |
| 3 | `acknowledgement` tool | A | `agent/simple_agent.py` |
| 4 | `wiki_simulator` tool — **ROM-parsed**, returns text + screenshot | A | `agent/wiki.py` (new) |
| 5 | Dynamic system prompt builder | B | `agent/simple_agent.py` |
| 6 | KB injection refreshed every turn | B | `agent/simple_agent.py` |
| 7 | Periodic `Save State` step in core loop | C | `agent/simple_agent.py`, `agent/emulator.py` |
| 8 | Summary injected as **assistant** message (currently user) | D | `agent/simple_agent.py` |
| 9 | **KB Critic** second-LLM feedback pass | D | `agent/kb_critic.py` (new), `agent/simple_agent.py` |
| 10 | Enable navigator | A | `config.py` |

---

## 3. Build Steps

### Step 1 — `KnowledgeBase` (`agent/knowledge_base.py`)

File-backed JSON dictionary acting as Claude's long-term memory. Survives summarization because it lives on disk, independent of `message_history`.

```python
class KnowledgeBase:
    def __init__(self, path="runs/knowledge_base.json"):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._data = json.loads(self.path.read_text()) if self.path.exists() else {}

    def get(self) -> dict: ...
    def update(self, key: str, value: str) -> None:   # upsert + flush to disk
    def delete(self, key: str) -> None:               # used by KB Critic
    def to_prompt_str(self) -> str:                   # markdown-rendered for prompt
    def size(self) -> int:                            # entry count for telemetry
```

**Persistence contract:** every `update`/`delete` writes the full JSON file atomically. Crash-safe across runs.

---

### Step 2 — `update_knowledge_base` tool

```json
{
  "name": "update_knowledge_base",
  "description": "Write or overwrite a key in your persistent long-term memory dictionary. Use this for facts you want to recall many turns from now: gym leader teams, item locations, map exits, NPC dialogue clues, strategic decisions.",
  "input_schema": {
    "type": "object",
    "properties": {
      "key":   {"type": "string"},
      "value": {"type": "string"}
    },
    "required": ["key", "value"]
  }
}
```

**Handler in `process_tool_call`:** call `self.knowledge_base.update(key, value)`, emit `kb_updated` event, return tool result with `{key} := {value}` confirmation **plus the latest screenshot and memory state** (same shape as `acknowledgement` — both are "thinking turns" per `finished.png`).

---

### Step 3 — `acknowledgement` tool

```json
{
  "name": "acknowledgement",
  "description": "Take a turn to think without pressing any buttons. Returns the current screen and memory state so you can reason before acting.",
  "input_schema": {
    "type": "object",
    "properties": {
      "message": {"type": "string", "description": "What you are acknowledging or reasoning about."}
    },
    "required": ["message"]
  }
}
```

**Handler:** no button presses, return fresh screenshot + memory + collision map.

---

### Step 4 — `wiki_simulator` tool (`agent/wiki.py`) — **ROM-parsed**

`finished.png` annotation is explicit: *"This wiki is all parsed directly from the ROM of the game; PyBoy Tools is very good at this task."* Static text dicts and hand-saved PNGs are out.

**Design:**
- `WikiSimulator(emulator)` holds a reference to the live `PyBoy` instance.
- On query, it uses `PyBoy` introspection (`game_wrapper`, `memory`, `tilemap_background`, sprite reads) to extract real game data: map tile layouts, NPC sprite positions, location names from the `MapLocation` enum, item/move/Pokemon names from the `Pokemon`/`Move` IntEnums already defined in `memory_reader.py`.
- Returns **both** a text description **and** a rendered screenshot (PNG → base64), matching the Diglett's Cave 2F example in `finished.png`.

```json
{
  "name": "wiki_simulator",
  "description": "Look up Pokemon Red game data extracted live from the ROM: location maps, Pokemon stats and moves, item effects, NPC layouts. Returns a description plus a rendered preview image when applicable.",
  "input_schema": {
    "type": "object",
    "properties": {
      "query":    {"type": "string", "description": "What to look up. Examples: 'Pallet Town map', 'Charmander moves', 'TM01 effect', 'Diglett's Cave 2F'."},
      "category": {"type": "string", "enum": ["location", "pokemon", "item", "move", "auto"], "default": "auto"}
    },
    "required": ["query"]
  }
}
```

**Implementation sketch:**
- `WikiSimulator._lookup_location(name)` — resolve name → `MapLocation` enum → seek tilemap region → render to PIL Image
- `WikiSimulator._lookup_pokemon(name)` — read base stats from ROM bank, render front sprite
- `WikiSimulator._lookup_move(name)` — pull from ROM move table
- `WikiSimulator._lookup_item(name)` — pull from ROM item table
- `auto` dispatch via fuzzy match against all four indexes

**Fallback if ROM extraction is too deep for v1:** a `WikiSimulator` that returns text from the existing IntEnums in `memory_reader.py` (`MapLocation`, `Pokemon`, `Move`, `ITEM_NAMES`) is acceptable as **Stage 1**; Stage 2 adds live tile/sprite rendering. Flag this as a phased deliverable, not a punt.

---

### Step 5 — Dynamic System Prompt (`build_system_prompt`)

Replace the module-level `SYSTEM_PROMPT` constant with a function called on every API request:

```python
def build_system_prompt(kb: KnowledgeBase) -> str:
    return f"""You are playing Pokemon Red. You control the game via emulator tool calls.

## Long-term Goal
Defeat the Elite Four.

## Your Knowledge Base
{kb.to_prompt_str() or "(empty — add entries with update_knowledge_base as you learn things)"}

## Tools — when to use which
- press_buttons      : every in-game action
- navigator          : when you know a grid coordinate and want auto-pathfinding
- update_knowledge_base : whenever you learn a durable fact worth remembering past summarization
- acknowledgement    : burn a turn to think without acting
- wiki_simulator     : look up game data parsed live from the ROM

## Summarization convention
When you see an assistant message labeled "CONVERSATION HISTORY SUMMARY", that is your own past notes after a context compression. Trust it as ground truth for what happened before.
"""
```

Pass `system=build_system_prompt(self.knowledge_base)` on **every** `client.messages.create` call (main loop AND summarization AND KB critic).

---

### Step 6 — Core Loop Refactor (`SimpleAgent.run`)

Restructure to the 5 stages from `finished.png`:

```
while running and step < num_steps:
    # 1. Compose Prompt
    prompt_messages = self._compose_prompt()       # caches + KB-refreshed system

    # 2. LLM Model
    response = self.client.messages.create(...)

    # 3. Execute Tools
    self._execute_tool_calls(response)

    # 4. Check For Summarization
    if len(self.message_history) >= self.max_history:
        self.summarize_history()
        self.run_kb_critic()                       # Step 9

    # 5. Save State
    if step % save_state_every == 0:
        self._save_state(step)                     # writes runs/states/step_{N}.state
```

`save_state_every` defaults to 5 steps; CLI flag `--save-state-every`. State files go to `runs/states/`.

---

### Step 7 — Emulator `save_state` (`agent/emulator.py`)

Add `Emulator.save_state(path)`:

```python
def save_state(self, path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as f:
        self.pyboy.save_state(f)
```

Symmetric with the existing `load_state` (already wired through `--load-state` CLI arg).

---

### Step 8 — Summary Injected as **Assistant** Message

Current `summarize_history()` builds the post-summary history as:

```python
[{"role": "user", "content": [{"type": "text", "text": "CONVERSATION HISTORY SUMMARY ..."}]}]
```

`finished.png` says: *"inject the summary as the first assistant message and Claude resumes the journey."* Fix:

```python
self.message_history = [
    {"role": "assistant", "content": [
        {"type": "text", "text": f"CONVERSATION HISTORY SUMMARY (replacing {self.max_history} prior messages):\n{summary_text}"}
    ]},
    {"role": "user", "content": [
        {"type": "text", "text": "Current game screenshot:"},
        {"type": "image", "source": {...}},
        {"type": "text", "text": "Continue playing — pick your next action."}
    ]}
]
```

Anthropic API requires history start with `user`, but assistant-leading is allowed when followed immediately by a user turn. Confirm with a smoke test; if rejected, swap to the user-role fallback with an `assistant:` prefix in the text.

---

### Step 9 — KB Critic (`agent/kb_critic.py`)

Second LLM pass invoked **immediately after every summarization**. Inspects the KB JSON and returns structured feedback the agent applies before resuming.

```python
class KnowledgeBaseCritic:
    def __init__(self, client, model=MODEL_NAME):
        self.client = client
        self.model = model

    def review(self, kb: KnowledgeBase) -> list[dict]:
        """Return a list of operations: [{'op': 'delete', 'key': 'x'},
                                          {'op': 'rewrite', 'key': 'y', 'value': '...'}]"""
```

**Critic system prompt outline:**
- "You are reviewing another agent's long-term memory for a Pokemon Red playthrough."
- "Flag stale facts, duplicates, low-signal entries, and overly verbose values."
- "Return ONLY a JSON array of operations: delete | rewrite | merge."

**Wiring in `SimpleAgent`:**

```python
def run_kb_critic(self):
    ops = self.kb_critic.review(self.knowledge_base)
    for op in ops:
        if op["op"] == "delete":
            self.knowledge_base.delete(op["key"])
        elif op["op"] == "rewrite":
            self.knowledge_base.update(op["key"], op["value"])
    self.events.emit("kb_critic_applied", ops=ops)
```

Critic runs on the same `MODEL_NAME` from `config.py`. Add `--disable-kb-critic` flag for cheap runs.

---

### Step 10 — Enable Navigator

`config.py`:

```python
USE_NAVIGATOR = True
```

`navigate_to` schema and handler already exist in `simple_agent.py`. No other change needed.

---

## 4. The Prompt — Final Composition Order

Per `finished.png`, every API call's `system` + `messages` payload contains, in order:

1. **System** (from `build_system_prompt`)
   - Tool usage instructions
   - **Knowledge Base** dump (refreshed every turn)
   - Summarization convention blurb
2. **Tools** (`AVAILABLE_TOOLS` array, 4–5 tools depending on navigator flag)
3. **Messages** (`message_history`)
   - If post-summary: `[assistant: SUMMARY, user: screenshot + "continue"]`
   - Else: full conversation with ephemeral cache markers on the last and third-to-last user turns

---

## 5. File Change Summary

| File | Change | LOC est. |
|------|--------|----------|
| `agent/knowledge_base.py` | **New** — `KnowledgeBase` class | ~60 |
| `agent/wiki.py` | **New** — `WikiSimulator` (Stage 1 text-only, Stage 2 ROM-rendered) | ~150 |
| `agent/kb_critic.py` | **New** — `KnowledgeBaseCritic` | ~80 |
| `agent/emulator.py` | Add `save_state(path)` method | +6 |
| `agent/simple_agent.py` | Add 3 tools, dynamic system prompt, KB wiring, save-state hook, assistant-message summary, KB critic call | ~+150 |
| `config.py` | `USE_NAVIGATOR = True`; optional `SAVE_STATE_EVERY = 5` | +2 |
| `main.py` | Add `--save-state-every`, `--disable-kb-critic` CLI flags | +10 |

---

## 6. Build & Verification Order

1. **Step 1, 2, 3** — KB + `update_knowledge_base` + `acknowledgement`. Smoke test: run 5 steps, confirm `runs/knowledge_base.json` populates and survives a restart.
2. **Step 5, 6** — Dynamic system prompt. Confirm KB string appears in logged prompts.
3. **Step 6, 7 (save state portion)** — Save state every N steps. Confirm `runs/states/step_5.state` exists; reload via `--load-state` resumes exactly.
4. **Step 8** — Assistant-role summary. Force-trigger summarization with `--max-history 6` and verify Anthropic accepts the shape.
5. **Step 9** — KB Critic. Plant 3 obviously bad entries in KB; confirm critic deletes them after the next summarization.
6. **Step 4 Stage 1** — Wiki text lookup. Query `"Charmander"`, confirm move list returns.
7. **Step 4 Stage 2** — Wiki ROM rendering. Query `"Pallet Town"`, confirm a rendered tilemap PNG returns.
8. **Step 10** — Enable navigator. Confirm `navigate_to` appears in `AVAILABLE_TOOLS` and a path executes.
9. **End-to-end:** 50-step run, no exceptions, `events.jsonl` contains: `agent_started`, `tool_call` for every tool type, `summary_completed`, `kb_critic_applied`, `state_saved`.

---

## 7. Out of Scope (for this pass)

- Multi-agent / parallel emulator instances
- Web UI overlay consuming `events.jsonl`
- Reinforcement-learning fine-tune of the agent
- Anything beyond reaching the Elite Four with the four-tool architecture
