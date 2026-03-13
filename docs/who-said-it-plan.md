# "Who Said It?" — Game Plan

## What is it?

A game that runs inside Slack. The bot posts a real quote from your team's
Slack history, and everyone guesses who said it by clicking a button.
That's it. No web app. No sign-ups. Just Slack.

## How it works (the player's perspective)

```
 You                           Slack
  │                              │
  │  @centaur play whosaidit     │
  │  ──────────────────────────► │
  │                              │
  │    ┌─────────────────────────────────────┐
  │    │  🎮 Who Said It?                    │
  │    │  Round 1 of 10                      │
  │    │                                     │
  │    │  "I think we should just mass       │
  │    │   migrate everything to Rust        │
  │    │   over the weekend"                 │
  │    │                                     │
  │    │  Who said this?                     │
  │    │                                     │
  │    │  ┌─────────┐  ┌─────────┐          │
  │    │  │  Alice   │  │   Bob   │          │
  │    │  └─────────┘  └─────────┘          │
  │    │  ┌─────────┐  ┌─────────┐          │
  │    │  │ Charlie  │  │  Dana   │          │
  │    │  └─────────┘  └─────────┘          │
  │    │                                     │
  │    │  ⏱️ 20 seconds remaining            │
  │    └─────────────────────────────────────┘
  │                              │
  │  *clicks "Bob"*              │
  │  ──────────────────────────► │
  │                              │
  │    ┌─────────────────────────────────────┐
  │    │  ✅ You picked: Bob                 │
  │    │  Waiting for others...              │
  │    └─────────────────────────────────────┘
  │                              │
  │       ... 20 seconds pass ...│
  │                              │
  │    ┌─────────────────────────────────────┐
  │    │  Round 1 Results                    │
  │    │                                     │
  │    │  ✅ It was Bob!                     │
  │    │                                     │
  │    │  🏆 Scoreboard                      │
  │    │  1. You ········· +100  (100)       │
  │    │  2. Eve ·········  +50  (50)        │
  │    │  3. Frank ········  +0  (0)         │
  │    │                                     │
  │    │  Next round in 5 seconds...         │
  │    └─────────────────────────────────────┘
```

After 10 rounds, the bot posts the final leaderboard:

```
  ┌─────────────────────────────────────┐
  │  🏆 Game Over!                      │
  │                                     │
  │  Final Scores:                      │
  │  🥇 Alice ·········· 820 pts       │
  │  🥈 Bob ············ 650 pts       │
  │  🥉 Charlie ········ 540 pts       │
  │  4. Dana ··········· 310 pts       │
  │  5. Eve ············ 200 pts       │
  │  ...                                │
  │                                     │
  │  Most quotes from: #engineering     │
  │  Hardest question: Round 7 (1/12)   │
  │                                     │
  │  ┌──────────────┐                   │
  │  │  Play Again?  │                  │
  │  └──────────────┘                   │
  └─────────────────────────────────────┘
```

## How it works (under the hood)

```
                    WHAT HAPPENS WHEN SOMEONE STARTS A GAME
                    ════════════════════════════════════════

  Slack                    Slackbot               API                  Slack API
    │                         │                    │                       │
    │  @centaur play          │                    │                       │
    │  whosaidit              │                    │                       │
    │ ──────────────────────► │                    │                       │
    │   (app_mention event)   │                    │                       │
    │                         │  POST /game/start  │                       │
    │                         │ ──────────────────►│                       │
    │                         │                    │                       │
    │                         │                    │  conversations.history│
    │                         │                    │ ─────────────────────►│
    │                         │                    │  (grab ~500 messages  │
    │                         │                    │   from a few channels)│
    │                         │                    │ ◄─────────────────────│
    │                         │                    │                       │
    │                         │                    │  Pick 10 good quotes, │
    │                         │                    │  store in memory      │
    │                         │                    │                       │
    │                         │  {game_id, round1} │                       │
    │                         │ ◄──────────────────│                       │
    │                         │                    │                       │
    │    Post round 1 message │                    │                       │
    │    with buttons         │                    │                       │
    │ ◄───────────────────────│                    │                       │
    │                         │                    │                       │


                    WHAT HAPPENS WHEN SOMEONE CLICKS A BUTTON
                    ══════════════════════════════════════════

  Slack                    Slackbot               API
    │                         │                    │
    │  *user clicks "Bob"*    │                    │
    │ ──────────────────────► │                    │
    │   (block_actions        │                    │
    │    interactive payload) │                    │
    │                         │  POST /game/answer │
    │                         │  {game_id, user,   │
    │                         │   round, pick}     │
    │                         │ ──────────────────►│
    │                         │                    │  Record answer,
    │                         │                    │  calc points
    │                         │  {ack}             │
    │                         │ ◄──────────────────│
    │                         │                    │
    │  Ephemeral message:     │                    │
    │  "You picked Bob ✅"    │                    │
    │ ◄───────────────────────│                    │
    │                         │                    │


                    WHAT HAPPENS WHEN TIME RUNS OUT
                    ═══════════════════════════════

  API (timer fires)           Slack API
    │                            │
    │  chat.update               │
    │  (replace buttons with     │
    │   the answer + scores)     │
    │ ──────────────────────────►│
    │                            │
    │  chat.postMessage          │
    │  (post next round          │
    │   with new buttons)        │
    │ ──────────────────────────►│
    │                            │
```

## What we need to build

There are only 4 pieces:

```
  ┌──────────────────────────────────────────────────┐
  │                                                  │
  │  1. GAME ENGINE             (services/api)       │
  │     New router: /game/*                          │
  │     Keeps game state in memory                   │
  │     Fetches quotes via existing Slack tool        │
  │     Runs round timers                            │
  │     Posts results to Slack                        │
  │                                                  │
  │  2. SLACK INTERACTIONS      (services/slackbot)  │
  │     New webhook: /api/slack/interactions          │
  │     Receives button clicks from Slack            │
  │     Forwards them to the API                     │
  │                                                  │
  │  3. GAME TRIGGER            (services/slackbot)  │
  │     Detect "play whosaidit" in messages          │
  │     Call API to start a game                     │
  │                                                  │
  │  4. SLACK APP CONFIG                             │
  │     Enable interactivity in the manifest         │
  │     Point it at our new webhook                  │
  │                                                  │
  └──────────────────────────────────────────────────┘
```

### Optional (later): Thread viewer leaderboard tab

If we want a persistent leaderboard or game history, we can add a `/game`
page to the thread viewer (services/web) later. But the game itself lives
entirely in Slack.

## Scoring

- Correct answer: **100 points**
- Wrong answer: **0 points**
- No answer (timed out): **0 points**
- Speed bonus: fastest correct answer each round gets **+50 bonus**

We keep it simple. No partial credit, no penalties.

## Quote selection

When a game starts, the API:

1. Picks 3-4 popular channels (e.g. #general, #engineering, #random)
2. Pulls the last ~500 messages from each using the existing Slack tool
3. Filters out:
   - Bot messages
   - Messages shorter than 30 characters
   - Messages that are just links/images
   - Join/leave notifications
4. Picks 10 random quotes from different people
5. For each quote, picks 3 decoy authors (other people from the message pool)
6. Stores all of this in memory for the duration of the game

## Game state

No database tables needed. Games last ~5 minutes and we'll have maybe
1-2 running at a time. We keep everything in a Python dict on the API:

```
games = {
    "game_abc123": {
        "channel": "C042WDDP89Y",
        "thread_ts": "1710300000.000100",
        "rounds": [...],           # 10 pre-loaded rounds
        "current_round": 3,
        "scores": {
            "U123": {"name": "Alice", "points": 250},
            "U456": {"name": "Bob", "points": 100},
        },
        "answers_this_round": {
            "U123": {"pick": "U789", "correct": True},
        },
        "round_message_ts": "...", # so we can update it when time's up
        "state": "playing",        # lobby | playing | finished
    }
}
```

If the API restarts mid-game, the game is lost. That's fine for a fun
team game — just start a new one.

## Files we need to create or change

### New files

```
services/api/api/routers/game.py        # Game engine + API routes
services/api/api/game_state.py          # In-memory game state manager
```

### Files to modify

```
services/api/api/app.py                 # Register the game router
services/slackbot/src/app/api/slack/
  interactions/route.ts                 # New: handle button clicks
services/slackbot/src/lib/bot/bot.ts    # Detect "play whosaidit" trigger
services/slackbot/slack-app-manifest.yml # Enable interactivity
services/nginx/nginx.conf               # Route /api/slack/interactions
```

That's 2 new files and 5 modified files. Small.

## Detailed file breakdown

### 1. `services/api/api/game_state.py` — In-memory state

A simple Python class that holds all active games in a dict. Methods:

- `create_game(channel, thread_ts)` → game_id
- `record_answer(game_id, user_id, user_name, pick)`
- `get_round(game_id)` → current round data
- `advance_round(game_id)` → next round or game over
- `get_scores(game_id)` → sorted leaderboard

### 2. `services/api/api/routers/game.py` — Game API

Routes:

| Route             | What it does                              |
|-------------------|-------------------------------------------|
| POST /game/start  | Fetch quotes, create game, post round 1   |
| POST /game/answer | Record a player's guess                   |
| GET /game/status  | Get current game state (for debugging)    |

The `start` endpoint also kicks off an `asyncio.create_task` that runs
the round timer (20 second countdown → reveal answer → post next round).

### 3. `services/slackbot/.../interactions/route.ts` — Button handler

When someone clicks a button in Slack, Slack sends a POST to our
interactions URL. This handler:

1. Verifies the Slack signature (same as events)
2. Parses the `payload` JSON from the form body
3. Extracts: who clicked, which button, which message
4. Calls `POST /game/answer` on the API
5. Returns an ephemeral "You picked X" response

### 4. `services/slackbot/.../bot.ts` — Trigger detection

In the existing message handler, check if the message contains
"play whosaidit" (or similar trigger phrases). If so, call the API's
`/game/start` endpoint instead of the normal agent flow.

### 5. `slack-app-manifest.yml` — Enable interactivity

Add this to the manifest:

```yaml
settings:
  interactivity:
    is_enabled: true
    request_url: https://svc-ai.paradigm.xyz/api/slack/interactions
```

### 6. `nginx.conf` — Route interactions

Add a location block so `/api/slack/interactions` goes to the slackbot,
similar to how `/api/slack/events` is routed today:

```
location /api/slack/interactions {
    proxy_pass http://slackbot;
}
```

## The round lifecycle

Here's exactly what happens during one round, step by step:

```
  ┌─────────────────────────────────────────────────────────┐
  │                                                         │
  │  1. API posts message to Slack with Block Kit buttons   │
  │     (quote text + 4 name buttons)                       │
  │     Saves the message timestamp (for updating later)    │
  │                                                         │
  │  2. API starts a 20-second timer (asyncio.sleep)        │
  │                                                         │
  │  3. Players click buttons → Slack sends interactive     │
  │     payload → slackbot → API records answers            │
  │     Each player gets an ephemeral "You picked X" ack    │
  │     Players can only answer once per round               │
  │                                                         │
  │  4. Timer fires. API:                                   │
  │     a. Calculates scores for this round                 │
  │     b. Updates the original message via chat.update     │
  │        (replaces buttons with the answer + scoreboard)  │
  │     c. Waits 5 seconds (let people read the results)    │
  │     d. Posts the next round (back to step 1)            │
  │        OR posts final leaderboard if last round         │
  │                                                         │
  └─────────────────────────────────────────────────────────┘
```

## What we're NOT building

- ❌ Web app or separate game UI
- ❌ Database tables
- ❌ User accounts or authentication for the game
- ❌ Room codes or join flows (you're "in" if you click a button)
- ❌ Persistent game history (maybe later)
- ❌ WebSockets or SSE
- ❌ Slash commands (just use @mention)

## Final decisions (implemented)

- **Thread-based** — All rounds in a thread (keeps channel clean)
- **Quote source** — Channel where the game is started (e.g. policy team chat)
- **Trigger** — `@centaur play whosaidit` (exact phrase)
- **5 options** per round, **15 seconds** per round
- **Top 5** scoreboard per round, full list at game over
- **Own quote** — Do nothing (it's funny)
- **Unlimited** concurrent games
- **Strip @mentions** from quote text
- **Play again** — Fresh game with new quotes
