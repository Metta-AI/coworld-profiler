"""Game config: the token-free `game_config` from the manifest plus runner-injected tokens."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator


class PlayerName(BaseModel):
    name: str = Field(min_length=1)


class PayloadCell(BaseModel):
    cell_id: str = Field(min_length=1)
    target_bytes: int = Field(ge=64, le=4 * 1024 * 1024)
    warmup_ticks: int = Field(ge=0)
    measure_ticks: int = Field(ge=1)


class GameConfig(BaseModel):
    tokens: list[str] = Field(min_length=1, max_length=16)
    players: list[PlayerName] = Field(min_length=1, max_length=16)
    seed: int = Field(ge=0)
    mode: Literal["fixed_tick", "blocking"] = "fixed_tick"
    step_seconds: float = Field(gt=0, le=1.0, default=0.02)
    decision_timeout_seconds: float = Field(gt=0, le=60, default=2.0)
    cells: list[PayloadCell] = Field(min_length=1)
    player_connect_timeout_seconds: float = Field(ge=0, default=180)
    drain_seconds: float = Field(gt=0, le=120, default=15.0)
    clock_probes_per_window: int = Field(ge=0, le=64, default=8)
    resource_sample_seconds: float = Field(gt=0, default=1.0)

    @model_validator(mode="after")
    def _same_slot_count(self) -> GameConfig:
        if len(self.tokens) != len(self.players):
            raise ValueError("tokens and players must have the same length")
        return self

    @property
    def slot_count(self) -> int:
        return len(self.tokens)

    @property
    def total_ticks(self) -> int:
        return sum(cell.warmup_ticks + cell.measure_ticks for cell in self.cells)
