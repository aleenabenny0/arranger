"""Pydantic request/response schemas for the Arranger API."""

from __future__ import annotations

import json
from dataclasses import asdict
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from arranger.adapters.score_json import score_from_dict
from arranger.adapters.score_json import score_to_dict as score_json_to_dict
from arranger.agent import RunResult
from arranger.fidelity import Fidelity
from arranger.ir import Score
from arranger.limits import (
    MAX_BAR_NUMBER,
    MAX_LABEL_CHARS,
    MAX_PLAN_REDUCTIONS,
    MAX_PLAN_SECTIONS,
    MAX_PLAN_TEXT_CHARS,
    MAX_SCORE_NOTES,
)
from arranger.plan import ArrangementPlan
from arranger.profile import PlayerProfile
from arranger.verify import Verdict

# Every inbound value is bounded. The shared ceilings come from
# `arranger.limits` so the importers, plan validation and this API agree; the
# rest are physical limits of the thing being described.
MAX_ONSET_SECONDS = 6 * 60 * 60
MAX_NOTE_DURATION_SECONDS = 600.0
MIN_TEMPO_BPM = 5.0
MAX_TEMPO_BPM = 1000.0
MAX_VOICES = 64
MAX_ATTEMPTS = 8
MAX_ID_CHARS = 64
MAX_PASSWORD_CHARS = 1024  # policy (auth.password_problems) is stricter; this bounds hashing work
MAX_TOKEN_CHARS = 256

MAX_SPAN_SEMITONES = 24  # arranger.profile.PlayerProfile.validate's ceiling

BarNumber = Annotated[int, Field(ge=1, le=MAX_BAR_NUMBER)]
Finger = Annotated[int, Field(ge=1, le=5)]
RecordId = Annotated[str, Field(min_length=1, max_length=MAX_ID_CHARS, pattern=r"^[A-Za-z0-9_-]+$")]


class ApiModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class NoteIn(ApiModel):
    pitch: int = Field(ge=0, le=127)
    onset: float = Field(ge=0, le=MAX_ONSET_SECONDS, allow_inf_nan=False)
    duration: float = Field(gt=0, le=MAX_NOTE_DURATION_SECONDS, allow_inf_nan=False)
    staff: int | None = Field(default=None, ge=1, le=2)
    bar: BarNumber | None = None
    voice: int = Field(default=1, ge=1, le=MAX_VOICES)


class ScoreIn(ApiModel):
    title: str = Field(default="untitled", max_length=MAX_LABEL_CHARS)
    tempo_bpm: float = Field(
        default=100.0, ge=MIN_TEMPO_BPM, le=MAX_TEMPO_BPM, allow_inf_nan=False
    )
    notes: list[NoteIn] = Field(min_length=1, max_length=MAX_SCORE_NOTES)


class PlayerProfileIn(ApiModel):
    name: str = Field(default="default", max_length=80)
    instrument: str = Field(default="piano", max_length=40)
    lowest_pitch: int = Field(default=21, ge=0, le=127)
    highest_pitch: int = Field(default=108, ge=0, le=127)
    max_span: int = Field(default=12, ge=1, le=MAX_SPAN_SEMITONES)
    comfortable_span: int = Field(default=9, ge=1, le=MAX_SPAN_SEMITONES)
    max_notes_per_hand: int = Field(default=5, ge=1, le=5)
    max_leap_rate: float = Field(default=70.0, gt=0, le=1000, allow_inf_nan=False)
    leap_slack: int = Field(default=5, ge=0, le=MAX_SPAN_SEMITONES)
    skill_level: int = Field(default=5, ge=1, le=10)
    # Mirrors arranger.profile.PlayerProfile; keep the two in step. Saved
    # profiles are read back with PlayerProfile.from_dict, not this model.
    left_max_span: int | None = Field(default=None, ge=1, le=MAX_SPAN_SEMITONES)
    right_max_span: int | None = Field(default=None, ge=1, le=MAX_SPAN_SEMITONES)
    left_comfortable_span: int | None = Field(default=None, ge=1, le=MAX_SPAN_SEMITONES)
    right_comfortable_span: int | None = Field(default=None, ge=1, le=MAX_SPAN_SEMITONES)
    left_fingers: list[Finger] = Field(default_factory=lambda: [1, 2, 3, 4, 5], min_length=1, max_length=5)
    right_fingers: list[Finger] = Field(default_factory=lambda: [1, 2, 3, 4, 5], min_length=1, max_length=5)
    max_repeat_rate: float = Field(default=8.0, ge=0.5, le=30, allow_inf_nan=False)


class SectionIn(ApiModel):
    start_bar: BarNumber
    end_bar: BarNumber
    lh_pattern: Literal[
        "block",
        "pedal_tone",
        "broken_octave",
        "arpeggio",
        "alberti",
        "walking",
        "stride",
        "broken_tenth",
    ] = "block"
    melody_shift: int = Field(default=0, ge=-24, le=24)
    lh_octave: int = Field(default=3, ge=0, le=6)
    lh_voices: int = Field(default=3, ge=0, le=5)
    roll_wide_chords: bool = False
    melody_fold_window: int = Field(default=0, ge=0, le=24)
    label: str = Field(default="", max_length=MAX_LABEL_CHARS)
    # Mirrors arranger.plan.Section; keep the two in step.
    voicing: Literal["root", "smooth"] = "root"
    bass: Literal["root", "source"] = "root"
    harmonic_rhythm: Literal["bar", "detected"] = "bar"


class ReductionIn(ApiModel):
    kind: Literal[
        "doubling",
        "inner_voice",
        "bass_movement",
        "harmonic_colour",
        "countermelody",
    ]
    start_bar: BarNumber
    end_bar: BarNumber
    rationale: str = Field(default="", max_length=500)


class ArrangementPlanIn(ApiModel):
    title: str = Field(default="untitled", max_length=MAX_LABEL_CHARS)
    target_skill: int = Field(default=5, ge=1, le=10)
    sections: list[SectionIn] = Field(min_length=1, max_length=MAX_PLAN_SECTIONS)
    reductions: list[ReductionIn] = Field(default_factory=list, max_length=MAX_PLAN_REDUCTIONS)
    pedal_bars: list[BarNumber] = Field(default_factory=list, max_length=MAX_BAR_NUMBER)
    notes: str = Field(default="", max_length=MAX_PLAN_TEXT_CHARS)
    tempo_scale: float = Field(default=1.0, ge=0.4, le=1.0, allow_inf_nan=False)


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
    max_attempts: int = Field(default=4, ge=1, le=MAX_ATTEMPTS)
    countdown: bool = True


def _normalize_email(value: str) -> str:
    value = value.strip().lower()
    local, _, domain = value.rpartition("@")
    if not local or not domain or "." not in domain or any(ch.isspace() for ch in value):
        raise ValueError("Enter a valid email address.")
    return value


class _EmailModel(ApiModel):
    email: str = Field(min_length=3, max_length=254)

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value: str) -> str:
        return _normalize_email(value)


class RegisterRequest(_EmailModel):
    password: str = Field(min_length=1, max_length=MAX_PASSWORD_CHARS)
    display_name: str = Field(default="", max_length=80)


class LoginRequest(_EmailModel):
    password: str = Field(min_length=1, max_length=MAX_PASSWORD_CHARS)


class PasswordResetRequest(_EmailModel):
    pass


class PasswordResetConfirmRequest(ApiModel):
    token: str = Field(min_length=16, max_length=MAX_TOKEN_CHARS)
    password: str = Field(min_length=1, max_length=MAX_PASSWORD_CHARS)


class PasswordChangeRequest(ApiModel):
    current_password: str = Field(min_length=1, max_length=MAX_PASSWORD_CHARS)
    new_password: str = Field(min_length=1, max_length=MAX_PASSWORD_CHARS)


class EmailVerifyRequest(ApiModel):
    token: str = Field(min_length=16, max_length=MAX_TOKEN_CHARS)


class UserResponse(ApiModel):
    user: dict


class SessionsResponse(ApiModel):
    sessions: list[dict]


class PlanCreateRequest(ApiModel):
    score_id: RecordId
    plan: ArrangementPlanIn


class PersistentRenderVerifyRequest(ApiModel):
    score_id: RecordId
    profile_id: RecordId
    plan_id: RecordId


class PersistentArrangeRequest(ApiModel):
    score_id: RecordId
    profile_id: RecordId
    max_attempts: int = Field(default=4, ge=1, le=MAX_ATTEMPTS)
    countdown: bool = True


class FeedbackCreateRequest(ApiModel):
    label: Literal["accepted", "rejected", "edited"]
    candidate_ranking_id: RecordId | None = None
    arrangement_id: RecordId | None = None
    edited_plan: ArrangementPlanIn | None = None
    notes: str = Field(default="", max_length=1000)


class NoteOut(ApiModel):
    """A rendered note. Output is produced by the engine, so it is typed, not bounded."""

    pitch: int
    onset: float
    duration: float
    staff: int | None = None
    bar: int | None = None
    voice: int = 1


class ScoreOut(ApiModel):
    title: str
    tempo_bpm: float
    notes: list[NoteOut]


class FidelityOut(ApiModel):
    # Mirrors arranger.fidelity.Fidelity; keep the two in step.
    melodic_recall: float
    harmonic_coverage: float
    accompaniment: float
    rhythm: float = 1.0
    contour: float = 1.0
    bass: float = 1.0
    score: float


class RenderResponse(ApiModel):
    score: ScoreOut


class RenderVerifyResponse(ApiModel):
    arranged: ScoreOut
    verdict: dict
    fidelity: FidelityOut


class ArrangeResponse(ApiModel):
    result: dict


class PlanResponse(ApiModel):
    plan: dict
    verdict: dict
    fidelity: FidelityOut


class PlanAnalysisResponse(ApiModel):
    analysis: dict


class RecordResponse(ApiModel):
    record: dict


class RecordsResponse(ApiModel):
    records: list[dict]


def to_score(data: ScoreIn) -> Score:
    """A request-body score, through the same validating adapter as stored ones."""
    return score_from_dict(data.model_dump(exclude_none=True), max_notes=MAX_SCORE_NOTES)


def score_from_payload(payload: object) -> Score:
    """A stored score record back into a `Score`.

    Stored payloads are not `ScoreIn`-shaped: importers save richer scores
    (timeline, tracks, pedals). `score_from_dict` validates and bounds them and
    raises only `ScoreImportError`s, which `errors.domain_error` knows how to show.
    """
    return score_from_dict(payload, max_notes=MAX_SCORE_NOTES)


def stored_score_dict(score: Score) -> dict:
    """The persisted form of a score: the adapter's full, versioned shape."""
    return score_json_to_dict(score)


def profile_from_payload(payload: object) -> PlayerProfile:
    """A profile from parsed JSON (request or stored). `from_dict` rejects unknown
    keys and wrong types and runs `validate()`, raising ValueError."""
    return PlayerProfile.from_dict(payload)


def to_profile(data: PlayerProfileIn) -> PlayerProfile:
    return profile_from_payload(data.model_dump())


def stored_profile_dict(profile: PlayerProfile) -> dict:
    return profile.to_dict()


def plan_from_payload(payload: object) -> ArrangementPlan:
    """A plan from parsed JSON (request or stored), strictly validated."""
    plan = ArrangementPlan.from_dict(payload)
    if problems := plan.validate():
        raise ValueError("; ".join(problems))
    return plan


def to_plan(data: ArrangementPlanIn) -> ArrangementPlan:
    return plan_from_payload(data.model_dump())


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
