"""In-memory game state for Who Said It? — no database."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any


@dataclass
class RoundData:
    """One round: quote + 5 options (1 correct, 4 decoys)."""

    quote_id: str
    quote_text: str
    correct_user_id: str
    correct_user_name: str
    options: list[dict[str, Any]]  # [{user_id, name}, ...] 5 total


@dataclass
class PlayerScore:
    """Player in a game with their score."""

    user_id: str
    name: str
    points: int = 0


@dataclass
class GameState:
    """One active game."""

    game_id: str
    channel_id: str
    thread_ts: str
    rounds: list[RoundData]
    current_round_idx: int
    scores: dict[str, PlayerScore]
    answers_this_round: dict[str, dict[str, Any]]
    round_message_ts: str | None
    state: str  # lobby | playing | finished
    round_started_at: float | None = None


def _generate_game_id() -> str:
    return f"game_{uuid.uuid4().hex[:12]}"


class GameStore:
    """In-memory store for active games."""

    def __init__(self) -> None:
        self._games: dict[str, GameState] = {}

    def create_game(
        self,
        channel_id: str,
        thread_ts: str,
        rounds: list[RoundData],
    ) -> str:
        game_id = _generate_game_id()
        self._games[game_id] = GameState(
            game_id=game_id,
            channel_id=channel_id,
            thread_ts=thread_ts,
            rounds=rounds,
            current_round_idx=0,
            scores={},
            answers_this_round={},
            round_message_ts=None,
            state="playing",
        )
        return game_id

    def get_game(self, game_id: str) -> GameState | None:
        return self._games.get(game_id)

    def get_current_round(self, game_id: str) -> RoundData | None:
        g = self._games.get(game_id)
        if not g or g.current_round_idx >= len(g.rounds):
            return None
        return g.rounds[g.current_round_idx]

    def record_answer(
        self,
        game_id: str,
        user_id: str,
        user_name: str,
        pick_user_id: str,
        answered_at: float,
    ) -> bool:
        """Record a player's answer. Returns False if already answered or round over."""
        g = self._games.get(game_id)
        if not g or g.state != "playing":
            return False
        if user_id in g.answers_this_round:
            return False

        round_data = self.get_current_round(game_id)
        if not round_data:
            return False

        correct = pick_user_id == round_data.correct_user_id
        g.answers_this_round[user_id] = {
            "pick_user_id": pick_user_id,
            "correct": correct,
            "answered_at": answered_at,
        }

        if user_id not in g.scores:
            g.scores[user_id] = PlayerScore(user_id=user_id, name=user_name)

        return True

    def finalize_round(self, game_id: str) -> dict[str, Any] | None:
        """Calculate scores for current round, advance to next. Returns round result."""
        g = self._games.get(game_id)
        if not g or g.current_round_idx >= len(g.rounds):
            return None

        round_data = g.rounds[g.current_round_idx]
        correct_user_id = round_data.correct_user_id

        # Score: 100 for correct, +50 speed bonus for fastest correct
        CORRECT_POINTS = 100
        SPEED_BONUS = 50

        correct_answers = [
            (uid, data["answered_at"])
            for uid, data in g.answers_this_round.items()
            if data["correct"]
        ]
        correct_answers.sort(key=lambda x: x[1])

        for user_id, _ in correct_answers:
            g.scores[user_id].points += CORRECT_POINTS
        if correct_answers:
            g.scores[correct_answers[0][0]].points += SPEED_BONUS

        result = {
            "correct_user_id": correct_user_id,
            "correct_user_name": round_data.correct_user_name,
            "answers": dict(g.answers_this_round),
            "scores": {uid: s.points for uid, s in g.scores.items()},
        }

        g.answers_this_round.clear()
        g.current_round_idx += 1

        if g.current_round_idx >= len(g.rounds):
            g.state = "finished"

        return result

    def get_scores_sorted(self, game_id: str) -> list[PlayerScore]:
        g = self._games.get(game_id)
        if not g:
            return []
        return sorted(g.scores.values(), key=lambda p: -p.points)

    def get_top_n(self, game_id: str, n: int = 5) -> list[PlayerScore]:
        return self.get_scores_sorted(game_id)[:n]

    def set_round_message_ts(self, game_id: str, ts: str) -> None:
        g = self._games.get(game_id)
        if g:
            g.round_message_ts = ts

    def is_finished(self, game_id: str) -> bool:
        g = self._games.get(game_id)
        return g is not None and g.state == "finished"

    def cleanup(self, game_id: str) -> None:
        """Remove a finished game from memory."""
        self._games.pop(game_id, None)


game_store = GameStore()
