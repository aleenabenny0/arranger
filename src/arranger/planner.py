"""Deterministic first-pass arrangement planning.

The agent can repair a plan, but it should not have to invent the obvious
structure from nothing. This module builds a conservative, fully deterministic
first draft from measurable score features, then chooses the best safe variant
using the same render/verify/fidelity objective as the agent loop.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from .fidelity import measure
from .ir import Score
from .musicianship import profile_fit_weight
from .plan import (
    ArrangementPlan,
    LHPattern,
    Reduction,
    ReductionKind,
    Section,
    patterns_for_skill,
)
from .profile import PlayerProfile
from .render import RenderError, detect_chords, extract_melody, last_bar, render
from .verify import verify

FIDELITY_FLOOR = 0.88
FIDELITY_WEIGHT = 60.0
MAX_SECTIONS = 10


@dataclass(frozen=True)
class BarFeatures:
    bar: int
    note_count: int
    onset_count: int
    max_simultaneous: int
    min_onset_gap: float | None
    chord_root: int | None
    chord_quality: str | None
    melody_low: int | None
    melody_high: int | None
    melody_leap: int

    @property
    def melody_span(self) -> int:
        if self.melody_low is None or self.melody_high is None:
            return 0
        return self.melody_high - self.melody_low

    @property
    def density(self) -> float:
        return self.note_count / max(1, self.onset_count)


@dataclass(frozen=True)
class PlannerOptions:
    max_sections: int = MAX_SECTIONS
    fidelity_floor: float = FIDELITY_FLOOR


@dataclass(frozen=True)
class Region:
    start_bar: int
    end_bar: int
    features: tuple[BarFeatures, ...]
    boundary_strength: float = 0.0

    @property
    def bar_count(self) -> int:
        return self.end_bar - self.start_bar + 1

    @property
    def note_count(self) -> int:
        return sum(feature.note_count for feature in self.features)

    @property
    def average_density(self) -> float:
        if not self.features:
            return 0.0
        return sum(feature.density for feature in self.features) / len(self.features)

    @property
    def max_density(self) -> float:
        return max((feature.density for feature in self.features), default=0.0)

    @property
    def max_simultaneous(self) -> int:
        return max((feature.max_simultaneous for feature in self.features), default=0)

    @property
    def shortest_onset_gap(self) -> float | None:
        gaps = [
            feature.min_onset_gap
            for feature in self.features
            if feature.min_onset_gap is not None
        ]
        return min(gaps, default=None)

    @property
    def max_melody_leap(self) -> int:
        return max((feature.melody_leap for feature in self.features), default=0)

    @property
    def melody_span(self) -> int:
        lows = [
            feature.melody_low
            for feature in self.features
            if feature.melody_low is not None
        ]
        highs = [
            feature.melody_high
            for feature in self.features
            if feature.melody_high is not None
        ]
        if not lows or not highs:
            return 0
        return max(highs) - min(lows)

    @property
    def root_changes(self) -> int:
        roots = [feature.chord_root for feature in self.features]
        return sum(
            1
            for previous, current in zip(roots, roots[1:], strict=False)
            if previous != current
        )


@dataclass(frozen=True)
class SectionCandidate:
    pattern: LHPattern
    voices: int
    melody_fold_window: int
    label: str


@dataclass(frozen=True)
class SectionDifficulty:
    score: float
    rank: str
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class SectionRole:
    role: str
    energy: str
    protected_layers: tuple[str, ...]


@dataclass(frozen=True)
class CandidateRankingRow:
    region_index: int
    start_bar: int
    end_bar: int
    pattern: str
    voices: int
    melody_fold_window: int
    difficulty_score: float
    difficulty_rank: str
    energy: str
    role: str
    note_count: int
    average_density: float
    max_simultaneous: int
    root_changes: int
    melody_span: int
    max_melody_leap: int
    tempo_bpm: float
    profile_fit: float
    verifier_cost: float
    planner_penalty: float
    chosen: bool

    def to_dict(self) -> dict:
        return {
            "region_index": self.region_index,
            "start_bar": self.start_bar,
            "end_bar": self.end_bar,
            "pattern": self.pattern,
            "voices": self.voices,
            "melody_fold_window": self.melody_fold_window,
            "difficulty_score": self.difficulty_score,
            "difficulty_rank": self.difficulty_rank,
            "energy": self.energy,
            "role": self.role,
            "note_count": self.note_count,
            "average_density": self.average_density,
            "max_simultaneous": self.max_simultaneous,
            "root_changes": self.root_changes,
            "melody_span": self.melody_span,
            "max_melody_leap": self.max_melody_leap,
            "tempo_bpm": self.tempo_bpm,
            "profile_fit": self.profile_fit,
            "verifier_cost": self.verifier_cost,
            "planner_penalty": self.planner_penalty,
            "chosen": self.chosen,
        }


def analyze_score(source: Score) -> list[BarFeatures]:
    """Extract bar-level facts the deterministic planner can act on."""
    end = last_bar(source)
    melody = extract_melody(source)
    chords = detect_chords(source, melody)
    features: list[BarFeatures] = []

    for bar in range(1, end + 1):
        notes = [n for n in source.notes if n.bar == bar]
        melody_notes = [n for n in melody if n.bar == bar]
        note_onsets = sorted({n.onset for n in notes})
        onset_gaps = [
            current - previous
            for previous, current in zip(note_onsets, note_onsets[1:], strict=False)
        ]
        notes_by_onset = {
            onset: [n for n in notes if n.onset == onset]
            for onset in note_onsets
        }
        ordered_melody = sorted(melody_notes, key=lambda n: n.onset)
        melody_leaps = [
            abs(current.pitch - previous.pitch)
            for previous, current in zip(
                ordered_melody, ordered_melody[1:], strict=False
            )
        ]
        chord = chords.get(bar)
        features.append(
            BarFeatures(
                bar=bar,
                note_count=len(notes),
                onset_count=len({n.onset for n in notes}),
                max_simultaneous=max(
                    (len(group) for group in notes_by_onset.values()),
                    default=0,
                ),
                min_onset_gap=min(onset_gaps, default=None),
                chord_root=chord[0] if chord else None,
                chord_quality=chord[1] if chord else None,
                melody_low=min((n.pitch for n in melody_notes), default=None),
                melody_high=max((n.pitch for n in melody_notes), default=None),
                melody_leap=max(melody_leaps, default=0),
            )
        )
    return features


def _density_band(feature: BarFeatures) -> int:
    if feature.note_count >= 10 or feature.density >= 4:
        return 2
    if feature.note_count >= 6 or feature.density >= 2.5:
        return 1
    return 0


def _melody_band(feature: BarFeatures, profile: PlayerProfile) -> int:
    if feature.melody_span > profile.max_span:
        return 2
    if feature.melody_span > profile.comfortable_span:
        return 1
    return 0


def _boundary_strength(
    previous: BarFeatures,
    current: BarFeatures,
    profile: PlayerProfile,
) -> float:
    score = 0.0
    if previous.chord_root != current.chord_root:
        score += 2.0
    if previous.chord_quality != current.chord_quality:
        score += 0.75
    if _density_band(previous) != _density_band(current):
        score += 1.25
    if _melody_band(previous, profile) != _melody_band(current, profile):
        score += 1.25
    if bool(previous.note_count) != bool(current.note_count):
        score += 1.0
    return score


def _fold_window_for_span(melody_span: int, profile: PlayerProfile, tempo_bpm: float) -> int:
    if melody_span > profile.max_span:
        return min(max(profile.max_span, 12), 16)
    if melody_span > profile.comfortable_span and (
        profile.skill_level <= 3 or tempo_bpm >= 126
    ):
        return min(max(profile.comfortable_span + 2, 9), 14)
    return 0


def _clamp(value: float, low: float, high: float) -> float:
    return min(max(value, low), high)


def _difficulty_rank(score: float) -> str:
    if score >= 8:
        return "extreme"
    if score >= 6:
        return "demanding"
    if score >= 4:
        return "challenging"
    if score >= 2:
        return "moderate"
    return "easy"


def score_region_difficulty(
    region: Region,
    profile: PlayerProfile,
    tempo_bpm: float,
) -> SectionDifficulty:
    """Estimate practical playing demand for a region on a 0-10 scale."""
    tempo_factor = _clamp((tempo_bpm - 72.0) / 72.0, 0.0, 1.5)
    event_rate = region.average_density * (tempo_bpm / 60.0)
    rhythm_pressure = _clamp(event_rate / 8.0, 0.0, 1.6)
    if region.shortest_onset_gap is not None:
        rhythm_pressure += _clamp((0.18 - region.shortest_onset_gap) / 0.18, 0.0, 1.0)

    texture_pressure = _clamp(region.average_density / 4.0, 0.0, 1.5)
    simultaneity_pressure = _clamp(
        region.max_simultaneous / max(1, profile.max_notes_per_hand),
        0.0,
        2.0,
    )
    span_pressure = _clamp(
        region.melody_span / max(1, profile.comfortable_span),
        0.0,
        2.0,
    )
    leap_pressure = _clamp(
        region.max_melody_leap / max(1, profile.leap_slack + profile.comfortable_span),
        0.0,
        2.0,
    )
    harmony_pressure = _clamp(region.root_changes / max(1, region.bar_count - 1), 0.0, 1.0)

    raw_score = (
        1.8 * texture_pressure
        + 1.7 * simultaneity_pressure
        + 1.6 * span_pressure
        + 1.4 * leap_pressure
        + 1.2 * rhythm_pressure
        + 0.8 * tempo_factor
        + 0.5 * harmony_pressure
    )
    score = round(_clamp(raw_score, 0.0, 10.0), 1)

    reasons: list[str] = []
    if region.max_simultaneous > profile.max_notes_per_hand:
        reasons.append("thick simultaneities")
    elif region.average_density >= 3:
        reasons.append("dense texture")
    if region.melody_span > profile.max_span:
        reasons.append("melody exceeds max span")
    elif region.melody_span > profile.comfortable_span:
        reasons.append("wide melody span")
    if region.max_melody_leap > profile.comfortable_span:
        reasons.append("large melody leaps")
    if tempo_factor >= 0.75 or rhythm_pressure >= 1.0:
        reasons.append("fast note activity")
    if region.root_changes:
        reasons.append("harmonic movement")
    if not reasons:
        reasons.append("low physical demand")

    return SectionDifficulty(score, _difficulty_rank(score), tuple(reasons))


def rank_regions_by_difficulty(
    source: Score,
    profile: PlayerProfile,
    options: PlannerOptions | None = None,
) -> list[tuple[Region, SectionDifficulty]]:
    """Return detected regions from hardest to easiest."""
    return sorted(
        (
            (region, score_region_difficulty(region, profile, source.tempo_bpm))
            for region in detect_regions(source, profile, options)
        ),
        key=lambda item: item[1].score,
        reverse=True,
    )


def classify_section_role(
    region: Region,
    difficulty: SectionDifficulty,
    index: int,
    total_regions: int,
) -> SectionRole:
    """Give the region a practical arranging role label."""
    if index == 0 and total_regions > 1 and difficulty.score < 4:
        role = "intro/verse support"
    elif index == total_regions - 1 and total_regions > 2 and difficulty.score < 4:
        role = "outro/tag support"
    elif difficulty.score >= 7:
        role = "climax reduction"
    elif region.root_changes and difficulty.score >= 4:
        role = "bridge movement"
    elif difficulty.score >= 5:
        role = "chorus support"
    elif region.average_density < 2.2:
        role = "verse support"
    else:
        role = "main support"

    if difficulty.score >= 7:
        energy = "high"
    elif difficulty.score >= 4:
        energy = "medium"
    else:
        energy = "low"

    protected = ["melody", "bass", "chord identity"]
    if region.root_changes:
        protected.append("harmonic rhythm")
    if difficulty.score >= 6:
        protected.append("rhythmic drive")

    return SectionRole(role, energy, tuple(protected))


def detect_regions(
    source: Score,
    profile: PlayerProfile,
    options: PlannerOptions | None = None,
) -> list[Region]:
    """Split the source into musically distinct regions before choosing patterns."""
    options = options or PlannerOptions()
    features = analyze_score(source)
    return _regions_from_features(features, profile, options.max_sections)


def _regions_from_features(
    features: list[BarFeatures],
    profile: PlayerProfile,
    max_sections: int,
) -> list[Region]:
    if not features:
        return []

    scored_boundaries = [
        (index, _boundary_strength(features[index - 1], features[index], profile))
        for index in range(1, len(features))
    ]
    strong_boundaries = [
        (index, strength)
        for index, strength in scored_boundaries
        if strength >= 2.0
    ]
    strong_boundaries.sort(key=lambda item: (-item[1], item[0]))
    kept = sorted(strong_boundaries[: max(0, max_sections - 1)])

    regions: list[Region] = []
    start_index = 0
    for boundary_index, strength in kept:
        region_features = tuple(features[start_index:boundary_index])
        regions.append(
            Region(
                region_features[0].bar,
                region_features[-1].bar,
                region_features,
                boundary_strength=strength,
            )
        )
        start_index = boundary_index

    region_features = tuple(features[start_index:])
    regions.append(
        Region(
            region_features[0].bar,
            region_features[-1].bar,
            region_features,
        )
    )
    return regions


def _bar_decision(
    feature: BarFeatures,
    profile: PlayerProfile,
    tempo_bpm: float,
) -> tuple[LHPattern, int, int]:
    wide_melody = feature.melody_span > profile.max_span
    strained_melody = feature.melody_span > profile.comfortable_span
    dense = feature.density >= 4 or feature.note_count >= 10
    fast = tempo_bpm >= 126
    beginner = profile.skill_level <= 3

    fold = _fold_window_for_span(feature.melody_span, profile, tempo_bpm)

    if beginner or wide_melody or (dense and fast):
        pattern = LHPattern.PEDAL_TONE
        voices = 1
    elif dense or fast or strained_melody:
        pattern = LHPattern.BLOCK
        voices = 2
    elif profile.skill_level >= 7:
        pattern = LHPattern.WALKING
        voices = 3
    else:
        pattern = LHPattern.BLOCK
        voices = 2

    return pattern, voices, fold


def _safest_candidate(
    region: Region,
    profile: PlayerProfile,
    tempo_bpm: float,
) -> SectionCandidate:
    fold = _fold_window_for_span(region.melody_span, profile, tempo_bpm)
    dense = region.average_density >= 4 or region.note_count >= 10 * region.bar_count
    fast = tempo_bpm >= 126
    wide = region.melody_span > profile.max_span
    difficulty = score_region_difficulty(region, profile, tempo_bpm)

    if profile.skill_level <= 3 or wide or difficulty.score >= 7 or (dense and fast):
        return SectionCandidate(LHPattern.PEDAL_TONE, 1, fold, "safe root support")
    if dense or fast or difficulty.score >= 5 or region.melody_span > profile.comfortable_span:
        return SectionCandidate(LHPattern.BLOCK, 2, fold, "compact harmony")
    if profile.skill_level >= 7:
        return SectionCandidate(LHPattern.WALKING, 3, fold, "active bass line")
    return SectionCandidate(LHPattern.BLOCK, 2, fold, "compact harmony")


def _dedupe_candidates(candidates: list[SectionCandidate]) -> list[SectionCandidate]:
    unique: list[SectionCandidate] = []
    seen: set[tuple[LHPattern, int, int]] = set()
    for candidate in candidates:
        key = (candidate.pattern, candidate.voices, candidate.melody_fold_window)
        if key not in seen:
            unique.append(candidate)
            seen.add(key)
    return unique


def _region_candidates(
    region: Region,
    profile: PlayerProfile,
    tempo_bpm: float,
) -> list[SectionCandidate]:
    fold = _fold_window_for_span(region.melody_span, profile, tempo_bpm)
    dense = region.average_density >= 4 or region.max_density >= 4
    busy = region.average_density >= 2.5 or region.note_count >= 6 * region.bar_count
    fast = tempo_bpm >= 126
    wide = region.melody_span > profile.max_span
    strained = region.melody_span > profile.comfortable_span
    beginner = profile.skill_level <= 3
    difficulty = score_region_difficulty(region, profile, tempo_bpm)

    candidates = [_safest_candidate(region, profile, tempo_bpm)]
    if beginner or wide or difficulty.score >= 8:
        candidates.extend(
            [
                SectionCandidate(LHPattern.PEDAL_TONE, 1, fold, "safe root support"),
                SectionCandidate(LHPattern.BLOCK, 1, fold, "single-voice harmony"),
            ]
        )
        return _dedupe_candidates(candidates)

    candidates.extend(
        [
            SectionCandidate(LHPattern.BLOCK, 2, fold, "compact harmony"),
            SectionCandidate(LHPattern.PEDAL_TONE, 1, fold, "safe root support"),
            SectionCandidate(LHPattern.BLOCK, 1, fold, "single-voice harmony"),
        ]
    )

    if profile.skill_level >= 5 and not (dense and fast) and not strained and difficulty.score < 6.5:
        candidates.append(SectionCandidate(LHPattern.ARPEGGIO, 2, fold, "light arpeggio"))
    if profile.skill_level >= 6 and not dense and not (fast and busy) and difficulty.score < 5.5:
        candidates.append(SectionCandidate(LHPattern.ALBERTI, 2, fold, "alberti motion"))
    if profile.skill_level >= 7 and not dense and not strained and difficulty.score < 6.0:
        voices = 3 if region.root_changes or region.bar_count >= 2 else 2
        candidates.append(SectionCandidate(LHPattern.WALKING, voices, fold, "walking bass"))
    if profile.skill_level >= 8 and tempo_bpm <= 112 and not busy and difficulty.score < 4.0:
        candidates.append(
            SectionCandidate(LHPattern.BROKEN_OCTAVE, 1, fold, "broken octave pulse")
        )
    # Tempo gates the two widest figures. A broken tenth is rolled, so it needs
    # time to sound; stride crosses up to two octaves every beat, so the hand has
    # to be able to travel that far in one beat. The verifier still has the last
    # word on every candidate: these gates only avoid rendering hopeless ones.
    if (
        profile.skill_level >= 7 and tempo_bpm <= BROKEN_TENTH_MAX_BPM
        and not dense and not strained and difficulty.score < 5.0
    ):
        candidates.append(SectionCandidate(LHPattern.BROKEN_TENTH, 2, fold, "rolled broken tenths"))
    if (
        profile.skill_level >= 7 and not dense and not strained and difficulty.score < 5.5
        and stride_is_reachable(profile, tempo_bpm)
    ):
        candidates.append(SectionCandidate(LHPattern.STRIDE, 3, fold, "stride bass and chord"))

    # The skill ceiling is not a preference. Whatever the rules above offered,
    # nothing beyond the level of this player reaches the scorer.
    allowed = set(patterns_for_skill(profile.skill_level))
    return _dedupe_candidates([c for c in candidates if c.pattern in allowed])


BROKEN_TENTH_MAX_BPM = 84.0
STRIDE_LEAP_SEMITONES = 24      # bass note to after-beat chord and back, at its widest


def stride_is_reachable(profile: PlayerProfile, tempo_bpm: float) -> bool:
    """Can this hand cover a stride leap inside one beat at this tempo?"""
    beat_seconds = 60.0 / max(tempo_bpm, 1.0)
    reach = profile.max_leap_rate * beat_seconds + profile.leap_slack
    return reach >= STRIDE_LEAP_SEMITONES


def _candidate_penalty(
    candidate: SectionCandidate,
    region: Region,
    profile: PlayerProfile,
    tempo_bpm: float,
) -> float:
    dense = region.average_density >= 4 or region.max_density >= 4
    fast = tempo_bpm >= 126
    difficulty = score_region_difficulty(region, profile, tempo_bpm)
    expressive = {
        LHPattern.WALKING: -0.35,
        LHPattern.STRIDE: -0.30,
        LHPattern.ARPEGGIO: -0.25,
        LHPattern.ALBERTI: -0.20,
        LHPattern.BROKEN_TENTH: -0.15,
        LHPattern.BROKEN_OCTAVE: -0.10,
        LHPattern.BLOCK: 0.0,
        LHPattern.PEDAL_TONE: 0.12,
    }[candidate.pattern]
    moving = {
        LHPattern.WALKING, LHPattern.ARPEGGIO, LHPattern.ALBERTI, LHPattern.BROKEN_OCTAVE,
        LHPattern.STRIDE, LHPattern.BROKEN_TENTH,
    }
    risk = 0.0
    if difficulty.score >= 6 and candidate.pattern in moving:
        risk += (difficulty.score - 5.0) * 0.25
    if difficulty.score <= 3 and profile.skill_level >= 6:
        expressive -= 0.08
    if dense and candidate.pattern in moving:
        risk += 0.45
    if fast and candidate.pattern in {LHPattern.ALBERTI, LHPattern.BROKEN_OCTAVE, LHPattern.STRIDE}:
        risk += 0.3
    if candidate.voices > 2 and profile.skill_level < 7:
        risk += 0.5
    return expressive + risk


def _section_from_candidate(
    region: Region,
    candidate: SectionCandidate,
    profile: PlayerProfile,
    tempo_bpm: float,
) -> Section:
    difficulty = score_region_difficulty(region, profile, tempo_bpm)
    role = classify_section_role(region, difficulty, 0, 1)
    return Section(
        start_bar=region.start_bar,
        end_bar=region.end_bar,
        lh_pattern=candidate.pattern,
        lh_voices=candidate.voices,
        melody_fold_window=candidate.melody_fold_window,
        label=(
            f"bars {region.start_bar}-{region.end_bar} "
            f"{role.energy} {difficulty.rank} {difficulty.score:.1f}: "
            f"{role.role}, {candidate.label}"
        ),
    )


def _sections_from_choices(
    regions: list[Region],
    choices: list[SectionCandidate],
    profile: PlayerProfile,
    tempo_bpm: float,
) -> list[Section]:
    sections: list[Section] = []
    total = len(regions)
    for index, (region, candidate) in enumerate(zip(regions, choices, strict=False)):
        section = _section_from_candidate(region, candidate, profile, tempo_bpm)
        difficulty = score_region_difficulty(region, profile, tempo_bpm)
        role = classify_section_role(region, difficulty, index, total)
        section.label = (
            f"bars {region.start_bar}-{region.end_bar} "
            f"{role.energy} {difficulty.rank} {difficulty.score:.1f}: "
            f"{role.role}, {candidate.label}"
        )
        sections.append(section)
    return sections


def _plan_from_sections(
    source: Score,
    profile: PlayerProfile,
    sections: list[Section],
    notes: str,
) -> ArrangementPlan:
    return ArrangementPlan(
        title=f"{source.title} deterministic draft",
        target_skill=profile.skill_level,
        sections=sections,
        reductions=_reductions_for_sections(sections),
        notes=notes,
    )


def _score_region_candidate(
    source: Score,
    profile: PlayerProfile,
    regions: list[Region],
    choices: list[SectionCandidate],
    index: int,
    candidate: SectionCandidate,
) -> float:
    trial_choices = [*choices]
    trial_choices[index] = candidate
    sections = _sections_from_choices(regions, trial_choices, profile, source.tempo_bpm)
    plan = _plan_from_sections(
        source,
        profile,
        sections,
        "Per-section candidate scored by renderer, verifier, and fidelity.",
    )
    return _plan_cost(source, profile, plan)


def _choose_region_candidates(
    source: Score,
    profile: PlayerProfile,
    regions: list[Region],
    stop: Callable[[], bool] | None = None,
) -> list[SectionCandidate]:
    """Pick a figure per region by rendering and checking each candidate.

    Every trial renders and verifies the whole piece, so this loop is what the
    planner costs. `stop` is asked before each trial; once it says yes, the
    regions not yet decided keep their safest figure, which is a complete plan.
    """
    choices = [
        _safest_candidate(region, profile, source.tempo_bpm)
        for region in regions
    ]

    for index, region in enumerate(regions):
        best = choices[index]
        best_key: tuple[float, float] | None = None
        for candidate in _region_candidates(region, profile, source.tempo_bpm):
            if stop is not None and stop():
                choices[index] = best
                return choices
            try:
                cost = _score_region_candidate(
                    source, profile, regions, choices, index, candidate
                )
            except RenderError:
                continue
            key = (
                cost,
                _candidate_penalty(candidate, region, profile, source.tempo_bpm),
            )
            if best_key is None or key < best_key:
                best = candidate
                best_key = key
        choices[index] = best

    return choices


def candidate_ranking_rows(
    source: Score,
    profile: PlayerProfile,
    options: PlannerOptions | None = None,
) -> list[CandidateRankingRow]:
    """Export candidate evaluations in a shape suitable for future ML training."""
    options = options or PlannerOptions()
    regions = detect_regions(source, profile, options)
    choices = _choose_region_candidates(source, profile, regions)
    rows: list[CandidateRankingRow] = []

    for index, region in enumerate(regions):
        difficulty = score_region_difficulty(region, profile, source.tempo_bpm)
        role = classify_section_role(region, difficulty, index, len(regions))
        for candidate in _region_candidates(region, profile, source.tempo_bpm):
            try:
                verifier_cost = _score_region_candidate(
                    source, profile, regions, choices, index, candidate
                )
            except RenderError:
                continue
            penalty = _candidate_penalty(candidate, region, profile, source.tempo_bpm)
            rows.append(
                CandidateRankingRow(
                    region_index=index,
                    start_bar=region.start_bar,
                    end_bar=region.end_bar,
                    pattern=candidate.pattern.value,
                    voices=candidate.voices,
                    melody_fold_window=candidate.melody_fold_window,
                    difficulty_score=difficulty.score,
                    difficulty_rank=difficulty.rank,
                    energy=role.energy,
                    role=role.role,
                    note_count=region.note_count,
                    average_density=round(region.average_density, 3),
                    max_simultaneous=region.max_simultaneous,
                    root_changes=region.root_changes,
                    melody_span=region.melody_span,
                    max_melody_leap=region.max_melody_leap,
                    tempo_bpm=source.tempo_bpm,
                    profile_fit=profile_fit_weight(profile),
                    verifier_cost=round(verifier_cost, 3),
                    planner_penalty=round(penalty, 3),
                    chosen=(
                        candidate.pattern == choices[index].pattern
                        and candidate.voices == choices[index].voices
                        and candidate.melody_fold_window
                        == choices[index].melody_fold_window
                    ),
                )
            )
    return rows


def _make_sections(
    features: list[BarFeatures],
    profile: PlayerProfile,
    tempo_bpm: float,
    max_sections: int,
) -> list[Section]:
    if not features:
        return []
    regions = _regions_from_features(features, profile, max_sections)
    choices = [_safest_candidate(region, profile, tempo_bpm) for region in regions]
    return _sections_from_choices(regions, choices, profile, tempo_bpm)


def _reductions_for_sections(sections: list[Section]) -> list[Reduction]:
    reductions: list[Reduction] = []
    for section in sections:
        if section.lh_voices <= 2:
            reductions.append(
                Reduction(
                    ReductionKind.INNER_VOICE,
                    section.start_bar,
                    section.end_bar,
                    "Thin left-hand harmony to keep the texture playable.",
                )
            )
        if section.lh_pattern == LHPattern.PEDAL_TONE:
            reductions.append(
                Reduction(
                    ReductionKind.BASS_MOVEMENT,
                    section.start_bar,
                    section.end_bar,
                    "Use held roots where movement would create hand leaps.",
                )
            )
    return reductions


def _plan_cost(source: Score, profile: PlayerProfile, plan: ArrangementPlan) -> float:
    arranged = render(plan, source)
    verdict = verify(arranged, profile)
    fidelity = measure(source, arranged)
    shortfall = max(0.0, FIDELITY_FLOOR - fidelity.score())
    return len(verdict.hard) + FIDELITY_WEIGHT * shortfall


def _candidate_plans(
    source: Score,
    profile: PlayerProfile,
    options: PlannerOptions,
    stop: Callable[[], bool] | None = None,
) -> list[ArrangementPlan]:
    regions = detect_regions(source, profile, options)
    choices = _choose_region_candidates(source, profile, regions, stop)
    sections = _sections_from_choices(regions, choices, profile, source.tempo_bpm)
    base = _plan_from_sections(
        source,
        profile,
        sections,
        (
            "Deterministic draft from boundary-scored regions and per-section "
            "candidate scoring."
        ),
    )

    variants = [base]
    end = last_bar(source)
    fold = _fold_window_for_span(
        max((region.melody_span for region in regions), default=0),
        profile,
        source.tempo_bpm,
    )
    for pattern, voices in (
        (LHPattern.PEDAL_TONE, 1),
        (LHPattern.BLOCK, 1),
        (LHPattern.BLOCK, 2),
    ):
        sections = [
            Section(
                1,
                end,
                pattern,
                lh_voices=voices,
                melody_fold_window=fold,
                label="whole piece fallback",
            )
        ]
        variants.append(
            _plan_from_sections(
                source,
                profile,
                sections,
                "Conservative whole-piece fallback candidate scored by the verifier.",
            )
        )
    return variants


def create_initial_plan(
    source: Score,
    profile: PlayerProfile,
    options: PlannerOptions | None = None,
    *,
    stop: Callable[[], bool] | None = None,
) -> ArrangementPlan:
    """Create the best deterministic first draft for a score/profile pair.

    `stop` bounds the work: it is polled before every candidate trial, and when
    it returns true the planner finishes with what it has. The result is always
    a complete, valid plan; stopping early only makes it more conservative.
    """
    options = options or PlannerOptions()
    best_plan: ArrangementPlan | None = None
    best_cost: float | None = None

    for position, plan in enumerate(_candidate_plans(source, profile, options, stop)):
        if position and best_plan is not None and stop is not None and stop():
            break
        try:
            cost = _plan_cost(source, profile, plan)
        except RenderError:
            continue
        if best_cost is None or cost < best_cost:
            best_plan = plan
            best_cost = cost

    if best_plan is None:
        end = last_bar(source)
        best_plan = ArrangementPlan(
            title=f"{source.title} deterministic draft",
            target_skill=profile.skill_level,
            sections=[
                Section(
                    1,
                    end,
                    LHPattern.PEDAL_TONE,
                    lh_voices=1,
                    melody_fold_window=min(max(profile.max_span, 12), 16),
                )
            ],
            notes="Fallback deterministic draft.",
        )

    return best_plan


def deterministic_plan(
    source: Score,
    profile: PlayerProfile,
    options: PlannerOptions | None = None,
    *,
    stop: Callable[[], bool] | None = None,
) -> ArrangementPlan:
    """Alias with a plain name for callers and tests."""
    return create_initial_plan(source, profile, options, stop=stop)
