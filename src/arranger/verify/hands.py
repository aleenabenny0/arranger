"""Assign sounding notes to hands.

You cannot check hand span until you know which notes are in which hand, and
the arrangement often does not say. So this has to be inferred.

v1 (this file) is greedy: at each instant, pick the pitch split that is
feasible and moves the hands least from where they just were. It is fast,
has no dependencies, and is right the large majority of the time.

v2 (milestone 3) replaces this with OR-Tools CP-SAT solving the whole piece at
once, jointly with finger assignment. That is the version that produces
*infeasibility certificates* — proof that no assignment exists, which is a far
stronger signal for the repair loop than "greedy couldn't find one".

Keep this module's signature stable so v2 is a drop-in swap. The rest of the
codebase should not know or care which solver answered.
"""

from __future__ import annotations

from ..ir import Note
from ..profile import PlayerProfile

Assignment = dict[int, str]  # index into the sounding list -> "L" or "R"

# Price of bringing a resting hand into play, in "semitones of travel".
# Tuned empirically against Fur Elise; see the M2 build-log entry.
IDLE_HAND_COST = 24.0


def _centroid(pitches: list[int]) -> float | None:
    return sum(pitches) / len(pitches) if pitches else None


def assign_hands(
    sounding: list[Note],
    profile: PlayerProfile,
    prev: tuple[float | None, float | None] = (None, None),
) -> tuple[Assignment, tuple[float | None, float | None]]:
    """Split simultaneously-sounding notes between hands.

    Returns the assignment and the resulting (left_centroid, right_centroid),
    which the caller threads into the next instant to measure hand travel.

    If any note carries an explicit staff, staff wins outright — the arranger
    made a deliberate choice and the verifier's job is to check that choice,
    not to overrule it and hide the mistake.
    """
    if not sounding:
        return {}, prev

    if any(n.staff is not None for n in sounding):
        assignment = {
            i: ("L" if (n.staff or 1) >= 2 else "R") for i, n in enumerate(sounding)
        }
        return assignment, _centroids(sounding, assignment, prev)

    order = sorted(range(len(sounding)), key=lambda i: sounding[i].pitch)
    prev_l, prev_r = prev

    best: tuple[float, Assignment] | None = None

    # Split point k: the k lowest notes go left, the rest go right.
    # k = 0 means everything in the right hand; k = n means everything left.
    for k in range(len(order) + 1):
        left_idx, right_idx = order[:k], order[k:]
        left_p = [sounding[i].pitch for i in left_idx]
        right_p = [sounding[i].pitch for i in right_idx]

        # Cost is a penalty, not a hard filter. If nothing is feasible we still
        # return the least-bad option so the constraint checker can report a
        # real violation with real numbers, instead of the verifier crashing.
        cost = 0.0
        for pitches in (left_p, right_p):
            if not pitches:
                continue
            span = max(pitches) - min(pitches)
            if span > profile.max_span:
                cost += 1000 * (span - profile.max_span)
            elif span > profile.comfortable_span:
                cost += 5 * (span - profile.comfortable_span)
            if len(pitches) > profile.max_notes_per_hand:
                cost += 1000 * (len(pitches) - profile.max_notes_per_hand)

        # Continuity: hands prefer to stay where they are.
        #
        # An idle hand must be charged something, or it is always the cheapest
        # option. Without this, a lone melody note at pitch 75 costs |75-76|=1
        # to keep in the right hand but 0 to hand to the idle left, so a single
        # melodic line alternates between hands forever — and the phantom
        # motion then shows up as impossible leaps when the other hand really
        # does enter. See docs/build-log/m2-first-real-music.md.
        #
        # IDLE_HAND_COST is the price of waking a resting hand. It has to
        # exceed the distance a hand would sensibly travel within a phrase,
        # but stay below the cost of a genuinely impossible stretch.
        lc, rc = _centroid(left_p), _centroid(right_p)
        for pos, prev_pos in ((lc, prev_l), (rc, prev_r)):
            if pos is None:
                continue  # this hand rests; it is not moving, so no cost
            if prev_pos is None:
                cost += IDLE_HAND_COST
            else:
                cost += abs(pos - prev_pos)
        # Hands do not cross in v1. Real pianists cross constantly; this is a
        # known limitation, listed in docs/build-log/limitations.md.
        if lc is not None and rc is not None and lc > rc:
            cost += 500

        assignment = {i: "L" for i in left_idx} | {i: "R" for i in right_idx}
        if best is None or cost < best[0]:
            best = (cost, assignment)

    assert best is not None
    return best[1], _centroids(sounding, best[1], prev)


def _centroids(
    sounding: list[Note],
    assignment: Assignment,
    prev: tuple[float | None, float | None] = (None, None),
) -> tuple[float | None, float | None]:
    """Where each hand ends up.

    A hand with nothing to play does not cease to exist — it stays where it
    was. Carrying the previous position forward is what lets the leap rule
    see "the left hand rested for 80ms, then had to be two octaves away".
    Returning None there instead would silently skip the check.
    """
    left = [n.pitch for i, n in enumerate(sounding) if assignment.get(i) == "L"]
    right = [n.pitch for i, n in enumerate(sounding) if assignment.get(i) == "R"]
    return (_centroid(left) or prev[0], _centroid(right) or prev[1])
