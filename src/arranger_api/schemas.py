"""Pydantic request/response schemas for the Arranger API."""

from __future__ import annotations

import json
from dataclasses import asdict
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from arranger.agent import RunResult
from arranger.fidelity import Fidelity
from arranger.ir import Note, Score
from arranger.plan import ArrangementPlan
from arranger.profile import PlayerProfile
from arranger.verify import Verdict


class ApiModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class NoteIn(ApiModel):
    pitch: int = Field(ge=0, le=127)
    onset: float = Field(ge=0)
    duration: float = Field(gt=0)
    staff: int | None = Field(default=None, ge=1, le=2)
    bar: int | None = Field(default=None, ge=1)
    voice: int = Field(default=1, ge=1)


class ScoreIn(ApiModel):
    title: str = "untitled"
    tempo_bpm: float = Field(default=100.0, gt=0)
    notes: list[NoteIn] = Field(min_length=1)


class PlayerProfileIn(ApiModel):
    name: str = "default"
    instrument: str = "piano"
    lowest_pitch: int = Field(default=21, ge=0, le=127)
    highest_pitch: int = Field(default=108, ge=0, le=127)
    max_span: int = Field(default=12, ge=1, le=36)
    comfortable_span: int = Field(default=9, ge=1, le=36)
    max_notes_per_hand: int = Field(default=5, ge=1, le=5)
    max_leap_rate: float = Field(default=70.0, gt=0)
    leap_slack: int = Field(default=5, ge=0)
    skill_level: int = Field(default=5, ge=1, le=10)


class SectionIn(ApiModel):
    start_bar: int = Field(ge=1)
    end_bar: int = Field(ge=1)
    lh_pattern: Literal[
        "block", "pedal_tone", "broken_octave", "arpeggio", "alberti", "walking"
    ] = "block"
    melody_shift: int = Field(default=0, ge=-24, le=24)
    lh_octave: int = Field(default=3, ge=0, le=6)
    lh_voices: int = Field(default=3, ge=0, le=5)
    roll_wide_chords: bool = False
    melody_fold_window: int = Field(default=0, ge=0, le=24)
    label: str = ""


class ReductionIn(ApiModel):
    kind: Literal[
        "doubling",
        "inner_voice",
        "bass_movement",
        "harmonic_colour",
        "countermelody",
    ]
    start_bar: int = Field(ge=1)
    end_bar: int = Field(ge=1)
    rationale: str = ""


class ArrangementPlanIn(ApiModel):
    title: str = "untitled"
    target_skill: int = Field(default=5, ge=1, le=10)
    sections: list[SectionIn] = Field(min_length=1)
    reductions: list[ReductionIn] = Field(default_factory=list)
    pedal_bars: list[int] = Field(default_factory=list)
    notes: str = ""


class VerifyRequest(ApiModel):
    score: ScoreIn
    profile: PlayerProfileIn


class RenderRequest(ApiModel):
    source: ScoreIn
    plan: ArrangementPlanIn


class RenderVerifyRequest(ApiModel):
    source: ScoreIn
    profile: PlayerProfileIn
    plan: ArrangementPlanIn


class ArrangeRequest(ApiModel):
    source: ScoreIn
    profile: PlayerProfileIn
    max_attempts: int = Field(default=4, ge=1, le=12)
    countdown: bool = True


class RegisterRequest(ApiModel):
    email: str
    password: str = Field(min_length=8)
    display_name: str = ""


class LoginRequest(ApiModel):
    email: str
    password: str


class UserResponse(ApiModel):
    user: dict


class PlanCreateRequest(ApiModel):
    score_id: str
    plan: ArrangementPlanIn


class PersistentRenderVerifyRequest(ApiModel):
    score_id: str
    profile_id: str
    plan_id: str


class PersistentArrangeRequest(ApiModel):
    score_id: str
    profile_id: str
    max_attempts: int = Field(default=4, ge=1, le=12)
    countdown: bool = True


class ScoreOut(ApiModel):
    title: str
    tempo_bpm: float
    notes: list[NoteIn]


class FidelityOut(ApiModel):
    melodic_recall: float
    harmonic_coverage: float
    accompaniment: float
    score: float


class RenderResponse(ApiModel):
    score: ScoreOut


class RenderVerifyResponse(ApiModel):
    arranged: ScoreOut
    verdict: dict
    fidelity: FidelityOut


class ArrangeResponse(ApiModel):
    result: dict


class RecordResponse(ApiModel):
    record: dict


class RecordsResponse(ApiModel):
    records: list[dict]


def to_score(data: ScoreIn) -> Score:
    return Score(
        notes=[
            Note(
                pitch=n.pitch,
                onset=n.onset,
                duration=n.duration,
                staff=n.staff,
                bar=n.bar,
                voice=n.voice,
            )
            for n in data.notes
        ],
        tempo_bpm=data.tempo_bpm,
        title=data.title,
    )


def to_profile(data: PlayerProfileIn) -> PlayerProfile:
    profile = PlayerProfile(**data.model_dump())
    if problems := profile.validate():
        raise ValueError("; ".join(problems))
    return profile


def to_plan(data: ArrangementPlanIn) -> ArrangementPlan:
    plan = ArrangementPlan.from_dict(data.model_dump())
    if problems := plan.validate():
        raise ValueError("; ".join(problems))
    return plan


def score_to_dict(score: Score) -> dict:
    return {
        "title": score.title,
        "tempo_bpm": score.tempo_bpm,
        "notes": [
            {
                "pitch": n.pitch,
                "onset": n.onset,
                "duration": n.duration,
                "staff": n.staff,
                "bar": n.bar,
                "voice": n.voice,
            }
            for n in score.notes
        ],
    }


def verdict_to_dict(verdict: Verdict) -> dict:
    return json.loads(verdict.to_json())


def fidelity_to_dict(fidelity: Fidelity) -> dict:
    data = asdict(fidelity)
    data["score"] = fidelity.score()
    return data


def run_result_to_dict(result: RunResult) -> dict:
    return json.loads(result.to_json())
