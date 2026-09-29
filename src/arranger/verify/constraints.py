"""The playability oracle.

Each rule is a small pure function: (Score, PlayerProfile) -> [Violation].
Pure and independent means each one gets a three-line test, and adding a rule
never risks breaking an existing one.

Rules must be *conservative*. A false HARD violation makes the agent mangle a
passage that was fine, and you will not notice, because you will be looking at
the mangled output and assuming the model was dumb. When unsure, emit STRAIN.
"""

from __future__ import annotations

from dataclasses import replace

from ..ir import PedalSpan, Score, pitch_name
from ..profile import PlayerProfile
from .solver import PEDAL_CATCH_SECONDS, HandSolution, solve_hands
from .verdict import Rule, Severity, SolverStatus, Verdict, Violation


def check_range(score: Score, profile: PlayerProfile) -> list[Violation]:
    """Notes outside the instrument (or the player's usable range)."""
    out = []
    for n in score.notes:
        if n.pitch < profile.lowest_pitch or n.pitch > profile.highest_pitch:
            out.append(
                Violation(
                    rule=Rule.RANGE,
                    severity=Severity.HARD,
                    time=n.onset,
                    bar=n.bar,
                    pitches=[n.pitch],
                    measured=n.pitch,
                    limit=(
                        profile.lowest_pitch
                        if n.pitch < profile.lowest_pitch
                        else profile.highest_pitch
                    ),
                    message=(
                        f"{pitch_name(n.pitch)} is outside the playable range "
                        f"{pitch_name(profile.lowest_pitch)}-"
                        f"{pitch_name(profile.highest_pitch)}"
                    ),
                )
            )
    return out


def check_hands(score: Score, profile: PlayerProfile) -> list[Violation]:
    """Span, per-hand polyphony, leap feasibility, and the strain rules.

    These share one search because they all depend on which hand plays what,
    and that is decided for the whole phrase at once. See `solver.py` for what
    the search covers and what its three outcomes mean.
    """
    return solve_hands(score, profile).violations


def check_total_polyphony(score: Score, profile: PlayerProfile) -> list[Violation]:
    """More keys held down at once than the player has fingers, in total.

    A note the pedal is sustaining is not a key held down: the finger has left
    it. Counting those made every pedalled arpeggio look like a ten-note chord.
    """
    out = []
    limit = profile.note_limit("L") + profile.note_limit("R")
    for t in score.onsets:
        sounding = score.sounding_at(t)
        if len(sounding) <= limit:
            continue
        held = [
            n for n in sounding
            if n.onset >= t - 1e-6 or not any(
                p.start <= t - PEDAL_CATCH_SECONDS and p.end > t - 1e-6 and p.start < n.offset
                for p in score.pedals
            )
        ]
        if len(held) > limit:
            out.append(
                Violation(
                    rule=Rule.TOTAL_POLYPHONY, severity=Severity.HARD, time=t,
                    bar=next((n.bar for n in held if n.bar is not None), None),
                    pitches=sorted(n.pitch for n in held),
                    measured=len(held), limit=limit,
                    note_ids=[n.id for n in held if n.id],
                    message=f"{len(held)} notes held at once; only {limit} fingers",
                )
            )
    return out


ALL_RULES = (check_range, check_hands, check_total_polyphony)


def with_assumed_pedal(score: Score) -> Score:
    """The same score with the pedal also changed at every barline.

    Most MIDI and MusicXML files carry no pedal data even when the music is
    unplayable without it: a bass note written as held while the same hand
    plays a chord a tenth above. Judged literally that is a HARD finding on
    nearly every Romantic piece. This is the alternative reading, "as a
    pianist would pedal it". It is only ever an explicit option, and callers
    must say which reading a verdict used.

    Pedal marks the score does have are kept. A file with a handful of marks
    is as under-pedalled as one with none, so the assumed spans are added
    alongside rather than only when the list is empty.
    """
    starts: dict[int, float] = {}
    for n in score.notes:
        if n.bar is not None and n.bar not in starts:
            starts[n.bar] = n.onset
    if len(starts) < 1:
        return score
    ordered = sorted(starts.values())
    ends = ordered[1:] + [score.duration()]
    spans = [PedalSpan(a, b) for a, b in zip(ordered, ends, strict=True) if b > a]
    return replace(score, pedals=[*score.pedals, *spans])


def verify(
    score: Score,
    profile: PlayerProfile,
    *,
    time_budget: float | None = None,
    staff_is_binding: bool = True,
    assume_pedal: bool = False,
) -> Verdict:
    """Run every rule and produce the verdict.

    `playable` is defined solely by the absence of HARD violations. Strain is
    reported but never blocks: deciding that a tiring passage is acceptable is
    the player's call, not the verifier's.

    The defaults judge the score literally: staves are hands, and there is
    no pedal unless the score says so. That is right for arrangements this
    system produced. For imported music pass `staff_is_binding=False`, and
    consider `assume_pedal=True`; see `solve_hands` and `with_assumed_pedal`.

    `solver_status` says how far to trust a HARD hand finding. When it is
    UNKNOWN the search was cut short; those findings carry
    `certainty="unproven"` and mean "no way was found", not "no way exists".
    """
    if assume_pedal:
        score = with_assumed_pedal(score)
    kwargs: dict = {"staff_is_binding": staff_is_binding}
    if time_budget is not None:
        kwargs["time_budget"] = time_budget
    solution: HandSolution = solve_hands(score, profile, **kwargs)
    violations: list[Violation] = [
        *check_range(score, profile), *solution.violations, *check_total_polyphony(score, profile),
    ]
    violations.sort(key=lambda v: (v.time, str(v.rule)))

    # Range and total polyphony do not depend on the hand search, so they are
    # proven whatever the search did.
    status = solution.status
    independent_hard = any(
        v.severity == Severity.HARD and v.rule in (Rule.RANGE, Rule.TOTAL_POLYPHONY)
        for v in violations
    )
    if independent_hard and status == SolverStatus.FEASIBLE:
        status = SolverStatus.INFEASIBLE

    return Verdict(
        title=score.title,
        profile=profile.name,
        playable=not any(v.severity == Severity.HARD for v in violations),
        violations=violations,
        solver_status=status,
        hands={
            score.notes[i].id: hand for i, hand in solution.hands.items() if score.notes[i].id
        },
    )
