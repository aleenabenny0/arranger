"""The playability oracle.

Each rule is a small pure function: (Score, PlayerProfile) -> [Violation].
Pure and independent means each one gets a three-line test, and adding a rule
never risks breaking an existing one.

Rules must be *conservative*. A false HARD violation makes the agent mangle a
passage that was fine, and you will not notice, because you will be looking at
the mangled output and assuming the model was dumb. When unsure, emit STRAIN.
"""

from __future__ import annotations

from ..ir import Note, Score, pitch_name
from ..profile import PlayerProfile
from .hands import assign_hands
from .verdict import Rule, Severity, Verdict, Violation


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
    """Span, per-hand polyphony, and leap feasibility.

    These share a single pass because they all depend on hand assignment, and
    assignment depends on the previous instant. Splitting them into three
    passes would mean assigning hands three times and risking disagreement
    between them.
    """
    out: list[Violation] = []
    prev_centroids: tuple[float | None, float | None] = (None, None)
    # When each hand last actually played a note. A resting hand keeps moving
    # time: if the right hand plays four notes while the left rests, the left
    # has all four notes' worth of time to reach its next position, not just
    # the gap since the most recent note. Measuring from the last onset by
    # *either* hand was the original bug — it charged the left hand 5ms to
    # make a move it had half a second to prepare.
    last_played: list[float | None] = [None, None]

    for t in score.onsets:
        sounding = score.sounding_at(t)
        if not sounding:
            continue
        assignment, centroids = assign_hands(sounding, profile, prev_centroids)
        bar = next((n.bar for n in sounding if n.bar is not None), None)
        active = {assignment.get(i) for i in range(len(sounding))}

        for hand in ("L", "R"):
            pitches = sorted(
                n.pitch for i, n in enumerate(sounding) if assignment.get(i) == hand
            )
            if not pitches:
                continue

            span = pitches[-1] - pitches[0]
            if span > profile.max_span:
                out.append(
                    Violation(
                        rule=Rule.HAND_SPAN, severity=Severity.HARD, time=t, bar=bar,
                        hand=hand, pitches=pitches, measured=span,
                        limit=profile.max_span,
                        message=(
                            f"{hand}H must span {span} semitones "
                            f"({pitch_name(pitches[0])}-{pitch_name(pitches[-1])}); "
                            f"max is {profile.max_span}. Drop an inner voice or "
                            f"move one pitch an octave."
                        ),
                    )
                )
            elif span > profile.comfortable_span:
                out.append(
                    Violation(
                        rule=Rule.HAND_SPAN, severity=Severity.STRAIN, time=t, bar=bar,
                        hand=hand, pitches=pitches, measured=span,
                        limit=profile.comfortable_span,
                        message=f"{hand}H stretch of {span} semitones is reachable but tiring",
                    )
                )

            if len(pitches) > profile.max_notes_per_hand:
                out.append(
                    Violation(
                        rule=Rule.HAND_POLYPHONY, severity=Severity.HARD, time=t,
                        bar=bar, hand=hand, pitches=pitches, measured=len(pitches),
                        limit=profile.max_notes_per_hand,
                        message=(
                            f"{hand}H needs {len(pitches)} fingers, has "
                            f"{profile.max_notes_per_hand}. Thin the voicing."
                        ),
                    )
                )

        # Leap feasibility: how far did each hand have to travel, and was
        # there time to do it? This is the rule that catches arrangements
        # that look fine on the page and are impossible under the fingers.
        for idx, hand in enumerate(("L", "R")):
            if hand not in active:
                continue  # this hand is resting; it isn't going anywhere
            since = last_played[idx]
            before, after = prev_centroids[idx], centroids[idx]
            last_played[idx] = t
            if before is None or after is None or since is None:
                continue  # first appearance of this hand, nothing to compare
            dt = t - since
            displacement = abs(after - before)
            budget = profile.leap_slack + profile.max_leap_rate * dt
            if displacement > budget:
                out.append(
                    Violation(
                        rule=Rule.LEAP_INFEASIBLE, severity=Severity.HARD, time=t,
                        bar=bar, hand=hand, measured=displacement,
                        limit=round(budget, 2),
                        message=(
                            f"{hand}H must move {displacement:.0f} semitones in "
                            f"{dt * 1000:.0f}ms; feasible budget is "
                            f"{budget:.0f}. Sustain the lower note with pedal, "
                            f"or re-voice so the hand stays put."
                        ),
                    )
                )

        prev_centroids = centroids

    return out


def check_total_polyphony(score: Score, profile: PlayerProfile) -> list[Violation]:
    """More simultaneous notes than the player has fingers, in total."""
    out = []
    limit = profile.max_notes_per_hand * 2
    for t in score.onsets:
        sounding = score.sounding_at(t)
        if len(sounding) > limit:
            out.append(
                Violation(
                    rule=Rule.TOTAL_POLYPHONY, severity=Severity.HARD, time=t,
                    bar=next((n.bar for n in sounding if n.bar is not None), None),
                    pitches=sorted(n.pitch for n in sounding),
                    measured=len(sounding), limit=limit,
                    message=f"{len(sounding)} notes sounding at once; only {limit} fingers",
                )
            )
    return out


ALL_RULES = (check_range, check_hands, check_total_polyphony)


def verify(score: Score, profile: PlayerProfile) -> Verdict:
    """Run every rule and produce the verdict.

    `playable` is defined solely by the absence of HARD violations. Strain is
    reported but never blocks: deciding that a tiring passage is acceptable is
    the player's call, not the verifier's.
    """
    violations: list[Violation] = []
    for rule in ALL_RULES:
        violations.extend(rule(score, profile))
    violations.sort(key=lambda v: (v.time, str(v.rule)))

    return Verdict(
        title=score.title,
        profile=profile.name,
        playable=not any(v.severity == Severity.HARD for v in violations),
        violations=violations,
    )
