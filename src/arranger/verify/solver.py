"""Which hand plays what: a search over the whole phrase, not one instant.

The greedy assigner in `hands.py` decides each onset on its own with a nudge
towards continuity. That is fast and usually right, and it has three failure
modes that all produce *false* HARD findings, the expensive kind:

- it cannot see that giving a note to the other hand now avoids an impossible
  leap two notes later;
- it re-decides held notes at every onset, so a note can change hands while
  it is sounding;
- it knows nothing about the pedal or rolled chords, so a bass note caught by
  the pedal still "occupies" the left hand an octave and a half away.

This solver is a shortest-path search over onsets. A state records where each
hand is, when it last played, and which sounding notes it is still holding.
Costs are compared as (HARD findings, strain, effort), so it finds an
assignment with the fewest HARD findings, and among those the least strain.

**What a result means.** The search space is: each hand takes a contiguous
block of the free notes by pitch, with the hands either side by side or
crossed. Notes with an explicit staff are not free; the arranger chose. Within
that space:

- FEASIBLE is a proof by construction: here is an assignment with no HARD
  finding.
- INFEASIBLE means every assignment was considered and the best still has a
  HARD finding. It is a statement about the model, not about all of human
  technique: the model has no finger substitution, no thumb playing two keys,
  no interleaved hands.
- UNKNOWN means the search was narrowed or ran out of time before finding a
  clean assignment. Nothing was proven. Callers must not present an UNKNOWN
  finding as "this cannot be played".

Zero third-party dependencies, like the rest of `arranger.verify`.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from itertools import combinations

from ..ir import Note, Score, pitch_name
from ..profile import PlayerProfile
from .verdict import Certainty, Rule, Severity, SolverStatus, Violation

HANDS = ("L", "R")

BEAM_WIDTH = 64                 # states kept per onset before the search is "narrowed"
DEFAULT_TIME_BUDGET = 4.0       # seconds; beyond it the search continues greedily
ROLL_WINDOW = 0.15              # rolled notes this close together are one gesture
PEDAL_CATCH_SECONDS = 0.03      # a key must be down this long for the pedal to hold it
WAKE_COST = 3.0                 # effort charged for bringing a resting hand in
SWITCH_EFFORT = 5.0             # effort charged for passing a single line to the other hand
CROSS_EFFORT = 6.0
STAFF_MISMATCH_EFFORT = 2.0     # per note taken by the hand its staff does not suggest
SIDE_EFFORT = 0.1               # per semitone a waking hand starts on the far side of middle C
MIDDLE_C = 60
LEGATO_BREAK_MIN_SECONDS = 0.12
LEGATO_BREAK_MIN_FRACTION = 0.35

# Largest practical stretch between two fingers, in semitones, for a hand
# whose thumb-to-little-finger reach is 15 (after Parncutt et al. 1997). Scaled
# to the player's own max_span before use.
_PAIR_STRETCH = {
    (1, 2): 10, (1, 3): 12, (1, 4): 14, (1, 5): 15,
    (2, 3): 5, (2, 4): 7, (2, 5): 10,
    (3, 4): 4, (3, 5): 7,
    (4, 5): 5,
}
_REFERENCE_SPAN = 15.0


@dataclass
class HandSolution:
    status: SolverStatus
    violations: list[Violation]
    hands: dict[int, str]           # index into score.notes -> "L" / "R"
    phrases: int = 1
    states_explored: int = 0
    narrowed: bool = False
    timed_out: bool = False
    elapsed: float = 0.0


# --- fingers ----------------------------------------------------------------


def chord_fits_fingers(pitches: list[int], fingers: tuple[int, ...], max_span: int, hand: str) -> bool:
    """Can these simultaneous pitches be taken by some choice of the available fingers?

    Fingers run thumb-to-little-finger upward in the right hand and downward in
    the left, so the left hand is solved as the mirror image.
    """
    n = len(pitches)
    if n <= 1:
        return True
    if n > len(fingers):
        return False
    ordered = sorted(pitches) if hand == "R" else sorted((-p for p in pitches))
    scale = max_span / _REFERENCE_SPAN
    for chosen in combinations(sorted(fingers), n):
        ok = True
        for i in range(n):
            for j in range(i + 1, n):
                if ordered[j] - ordered[i] > _PAIR_STRETCH[(chosen[i], chosen[j])] * scale + 1e-9:
                    ok = False
                    break
            if not ok:
                break
        if ok:
            return True
    return False


# --- search state -------------------------------------------------------------


@dataclass(frozen=True)
class _Hand:
    centre: float | None = None       # centroid of what the hand last played / holds
    attacked: float | None = None     # when it last struck a key
    released: float = 0.0             # when the notes it last struck end, as written
    held: tuple[int, ...] = ()        # note indices it is still holding down
    last: tuple[int, ...] = ()        # pitches of its most recent attack


@dataclass
class _State:
    cost: tuple[int, float, float]
    hands: tuple[_Hand, _Hand]
    parent: "_State | None" = None
    assigned: tuple[tuple[int, str], ...] = ()
    found: tuple[Violation, ...] = ()

    def key(self) -> tuple:
        return tuple(
            (h.held, None if h.centre is None else round(h.centre, 3), h.attacked, h.last)
            for h in self.hands
        )


@dataclass
class _Slice:
    time: float
    notes: list[int] = field(default_factory=list)   # indices into score.notes


def _slices(notes: list[Note]) -> list[_Slice]:
    """Group attacks. A rolled chord is one gesture, so its notes share a slice."""
    out: list[_Slice] = []
    for index, n in enumerate(notes):
        if out:
            last = out[-1]
            same_instant = n.onset - last.time <= 1e-6
            same_roll = (
                n.rolled
                and n.onset - last.time <= ROLL_WINDOW
                and all(notes[i].rolled and notes[i].staff == n.staff for i in last.notes)
            )
            if same_instant or same_roll:
                last.notes.append(index)
                continue
        out.append(_Slice(n.onset, [index]))
    return out


def _options(free: list[int], notes: list[Note]) -> list[dict[int, str]]:
    """Ways to divide the free notes: a split by pitch, hands side by side or crossed."""
    ordered = sorted(free, key=lambda i: notes[i].pitch)
    k = len(ordered)
    seen: set[tuple] = set()
    out: list[dict[int, str]] = []
    for crossed in (False, True):
        if crossed and k > 6:
            break
        for j in range(k + 1):
            low, high = ("R", "L") if crossed else ("L", "R")
            choice = {i: (low if pos < j else high) for pos, i in enumerate(ordered)}
            signature = tuple(sorted(choice.items()))
            if signature not in seen:
                seen.add(signature)
                out.append(choice)
    return out or [{}]


# --- the solver -----------------------------------------------------------------


def solve_hands(
    score: Score,
    profile: PlayerProfile,
    *,
    time_budget: float = DEFAULT_TIME_BUDGET,
    beam_width: int = BEAM_WIDTH,
    staff_is_binding: bool = True,
) -> HandSolution:
    """Find the division of notes between the hands with the fewest HARD findings.

    `staff_is_binding` is True for anything this system arranged: the renderer
    chose the staff on purpose, and a bad choice must be reported, not quietly
    rescued. It should be False for imported music. There the staff is only
    where the engraver put the note. Pianists routinely take upper-staff notes
    with the left hand (the chords of a Gymnopedie), and an importer that
    equates "two tracks" with "two hands" is guessing. With False, the staff
    becomes a preference that breaks ties and nothing more.
    """
    started = time.perf_counter()
    notes = score.notes
    if not notes:
        return HandSolution(SolverStatus.FEASIBLE, [], {})

    pedals = score.pedals

    def pedal_holds(note: Note, t: float) -> bool:
        """At time t, is the pedal sustaining this note so the hand may let go?"""
        for span in pedals:
            if span.start > t:
                break
            if span.end > t - 1e-6 and span.start <= t - PEDAL_CATCH_SECONDS and span.start < note.offset:
                return True
        return False

    def pedal_down(t: float) -> bool:
        return any(span.start <= t < span.end for span in pedals)

    span_limit = {h: profile.span_limit(h) for h in HANDS}
    comfort = {h: profile.comfortable_limit(h) for h in HANDS}
    note_limit = {h: profile.note_limit(h) for h in HANDS}
    repeat_gap = 1.0 / profile.max_repeat_rate
    # Long enough for either hand to get anywhere on the keyboard: a new phrase.
    reset_gap = max(0.0, (88 - profile.leap_slack) / profile.max_leap_rate)

    slices = _slices(notes)
    frontier: list[_State] = [_State((0, 0.0, 0.0), (_Hand(), _Hand()))]
    narrowed = timed_out = False
    least_dropped_hard = 10**9
    explored = 0
    phrases = 1
    furthest_offset = 0.0

    for number, sl in enumerate(slices):
        t = sl.time
        if number % 32 == 0 and not timed_out and time.perf_counter() - started > time_budget:
            timed_out = True
        width = 1 if timed_out else beam_width

        silent_for = t - furthest_offset
        fresh_phrase = number > 0 and silent_for >= reset_gap
        if fresh_phrase:
            phrases += 1

        suggested = {i: ("L" if (notes[i].staff or 1) >= 2 else "R") for i in sl.notes if notes[i].staff is not None}
        fixed = suggested if staff_is_binding else {}
        free = [i for i in sl.notes if i not in fixed]
        options = _options(free, notes)
        bar = next((notes[i].bar for i in sl.notes if notes[i].bar is not None), None)
        rolled_here = {i for i in sl.notes if notes[i].rolled}
        pedalled = pedal_down(t)

        best: dict[tuple, _State] = {}
        for state in frontier:
            hands_now = []
            for h in state.hands:
                if fresh_phrase:
                    hands_now.append(_Hand())
                    continue
                held = tuple(
                    i for i in h.held
                    if notes[i].offset > t + 1e-6 and not pedal_holds(notes[i], t)
                )
                if not held and h.attacked is not None and t - h.attacked >= reset_gap:
                    # Idle long enough to be anywhere on the keyboard. Where it
                    # was no longer matters, and forgetting it lets states that
                    # differ only in ancient history merge.
                    hands_now.append(_Hand())
                    continue
                hands_now.append(_Hand(h.centre, h.attacked, h.released, held, h.last))

            for option in options:
                explored += 1
                choice = {**fixed, **option}
                hard = 0
                strain = 0.0
                effort = 0.0
                found: list[Violation] = []
                new_hands: list[_Hand] = []
                ranges: list[tuple[int, int] | None] = []

                for hi, hand in enumerate(HANDS):
                    before = hands_now[hi]
                    mine = [i for i in sl.notes if choice[i] == hand]
                    if not mine:
                        new_hands.append(before)
                        held_pitches = [notes[i].pitch for i in before.held]
                        ranges.append((min(held_pitches), max(held_pitches)) if held_pitches else None)
                        continue

                    sounding = list(before.held) + mine
                    pitches = sorted(notes[i].pitch for i in sounding)
                    ids = [notes[i].id for i in sounding if notes[i].id]
                    span = pitches[-1] - pitches[0]
                    rolling = bool(rolled_here.intersection(mine))
                    limit = span_limit[hand]
                    if rolling:
                        # A rolled chord is played in sequence. The hand pivots,
                        # so it reaches further; with the pedal it can let go of
                        # the bottom and travel, so it reaches much further.
                        limit = span_limit[hand] * 2 if pedalled else span_limit[hand] + 5

                    if span > limit:
                        hard += 1
                        how = "as a rolled chord " if rolling else ""
                        found.append(Violation(
                            rule=Rule.HAND_SPAN, severity=Severity.HARD, time=t, bar=bar, hand=hand,
                            pitches=pitches, measured=span, limit=limit, note_ids=ids,
                            message=(
                                f"{hand}H must span {span} semitones {how}"
                                f"({pitch_name(pitches[0])}-{pitch_name(pitches[-1])}); "
                                f"max is {limit}. Drop an inner voice or move one pitch an octave."
                            ),
                        ))
                    elif span > comfort[hand] and not rolling:
                        strain += 1.0 + 0.1 * (span - comfort[hand])
                        found.append(Violation(
                            rule=Rule.HAND_SPAN, severity=Severity.STRAIN, time=t, bar=bar, hand=hand,
                            pitches=pitches, measured=span, limit=comfort[hand], note_ids=ids,
                            message=f"{hand}H stretch of {span} semitones is reachable but tiring",
                        ))

                    if len(pitches) > note_limit[hand]:
                        hard += 1
                        found.append(Violation(
                            rule=Rule.HAND_POLYPHONY, severity=Severity.HARD, time=t, bar=bar,
                            hand=hand, pitches=pitches, measured=len(pitches),
                            limit=note_limit[hand], note_ids=ids,
                            message=(
                                f"{hand}H needs {len(pitches)} fingers, has "
                                f"{note_limit[hand]}. Thin the voicing."
                            ),
                        ))
                    elif (
                        not rolling and span <= limit and len(pitches) > 1
                        and not chord_fits_fingers(pitches, profile.fingers(hand), span_limit[hand], hand)
                    ):
                        strain += 1.5
                        found.append(Violation(
                            rule=Rule.FINGER_STRETCH, severity=Severity.STRAIN, time=t, bar=bar,
                            hand=hand, pitches=pitches, measured=span, limit=span_limit[hand],
                            note_ids=ids,
                            message=(
                                f"{hand}H chord fits the hand's reach but not between the "
                                f"fingers available ({', '.join(map(str, profile.fingers(hand)))})"
                            ),
                        ))

                    centre = sum(pitches) / len(pitches)
                    if before.centre is not None and before.attacked is not None:
                        # Leap feasibility: how far did the hand have to travel,
                        # and was there time? A resting hand keeps its moving
                        # time: measured from when *this hand* last played.
                        dt = t - before.attacked
                        move = abs(centre - before.centre)
                        budget = profile.leap_slack + profile.max_leap_rate * dt
                        effort += move
                        if move > budget:
                            hard += 1
                            found.append(Violation(
                                rule=Rule.LEAP_INFEASIBLE, severity=Severity.HARD, time=t, bar=bar,
                                hand=hand, pitches=pitches, measured=move, limit=round(budget, 2),
                                note_ids=ids,
                                message=(
                                    f"{hand}H must move {move:.0f} semitones in "
                                    f"{dt * 1000:.0f}ms; feasible budget is {budget:.0f}. "
                                    f"Sustain the lower note with pedal, or re-voice so the hand stays put."
                                ),
                            ))
                        else:
                            # The move fits if the hand leaves early enough.
                            # Without pedal, leaving early clips the last note.
                            needed = max(0.0, move - profile.leap_slack) / profile.max_leap_rate
                            free_time = max(0.0, t - before.released)
                            early = needed - free_time
                            written = max(before.released - before.attacked, 1e-6)
                            if (
                                early > max(LEGATO_BREAK_MIN_SECONDS, LEGATO_BREAK_MIN_FRACTION * written)
                                and not pedal_down(max(before.attacked, t - needed))
                            ):
                                strain += 0.5
                                found.append(Violation(
                                    rule=Rule.LEGATO_BREAK, severity=Severity.STRAIN, time=t, bar=bar,
                                    hand=hand, pitches=pitches, measured=round(early * 1000),
                                    limit=round(LEGATO_BREAK_MIN_SECONDS * 1000), note_ids=ids,
                                    message=(
                                        f"{hand}H must let go about {early * 1000:.0f}ms early to reach "
                                        f"this position; use pedal or accept a break in the line"
                                    ),
                                ))
                        # One key, struck again faster than this player repeats.
                        if dt < repeat_gap - 1e-9:
                            again = sorted(set(before.last).intersection(notes[i].pitch for i in mine))
                            if again:
                                strain += 0.5
                                found.append(Violation(
                                    rule=Rule.FAST_REPETITION, severity=Severity.STRAIN, time=t,
                                    bar=bar, hand=hand, pitches=again,
                                    measured=round(1.0 / dt, 1), limit=profile.max_repeat_rate,
                                    note_ids=[notes[i].id for i in mine if notes[i].id and notes[i].pitch in again],
                                    message=(
                                        f"{hand}H repeats {pitch_name(again[0])} at "
                                        f"{1.0 / dt:.0f} notes per second; comfortable is "
                                        f"{profile.max_repeat_rate:.0f}"
                                    ),
                                ))
                    else:
                        effort += WAKE_COST
                        # A fresh hand has a home side. Without this, a piece
                        # that starts low is as happy in the right hand as the
                        # left, and the whole assignment comes out mirrored.
                        wrong_side = centre - MIDDLE_C if hand == "L" else MIDDLE_C - centre
                        effort += SIDE_EFFORT * max(0.0, wrong_side)

                    ends = max(notes[i].offset for i in mine)
                    new_hands.append(_Hand(
                        centre, t, ends, tuple(sorted(sounding)),
                        tuple(sorted(notes[i].pitch for i in mine)),
                    ))
                    ranges.append((pitches[0], pitches[-1]))

                # A single line passed to the other hand for no reason is the
                # M2 Fur Elise failure: phantom leaps from a melody relabelled
                # back and forth. Effort is the last tie-break, so this never
                # outweighs a HARD finding or strain: a real rescue still wins.
                if not staff_is_binding:
                    effort += STAFF_MISMATCH_EFFORT * sum(
                        1 for i, hand in suggested.items() if choice[i] != hand
                    )
                attackers = [hi for hi, hand in enumerate(HANDS) if any(choice[i] == hand for i in sl.notes)]
                if len(attackers) == 1:
                    mine_before = hands_now[attackers[0]].attacked
                    other_before = hands_now[1 - attackers[0]].attacked
                    if other_before is not None and (mine_before is None or other_before > mine_before):
                        effort += SWITCH_EFFORT

                # Hands in crossed order one after the other, not at once. No
                # finding, but not the natural way round either.
                lc, rc = new_hands[0].centre, new_hands[1].centre
                if lc is not None and rc is not None and lc > rc and len(attackers) == 1:
                    effort += CROSS_EFFORT

                left, right = ranges
                if left is not None and right is not None and left[0] > right[1]:
                    strain += 1.0
                    effort += CROSS_EFFORT
                    found.append(Violation(
                        rule=Rule.HAND_CROSSING, severity=Severity.STRAIN, time=t, bar=bar,
                        pitches=[left[0], right[1]], measured=left[0] - right[1], limit=0,
                        note_ids=[notes[i].id for i in sl.notes if notes[i].id],
                        message="the left hand plays above the right here",
                    ))

                cost = (state.cost[0] + hard, state.cost[1] + strain, state.cost[2] + effort)
                candidate = _State(
                    cost, (new_hands[0], new_hands[1]), state,
                    tuple(sorted(choice.items())), tuple(found),
                )
                key = candidate.key()
                incumbent = best.get(key)
                if incumbent is None or cost < incumbent.cost:
                    best[key] = candidate

        frontier = sorted(best.values(), key=lambda s: s.cost)
        if len(frontier) > width:
            # Dropping states gives up the guarantee that the best was found,
            # but not entirely. HARD counts only grow along a path, so a
            # dropped state with h HARD findings could never have finished
            # below h. Remember the smallest h ever dropped: if the final
            # answer is no worse than that, nothing dropped could have beaten it.
            narrowed = True
            least_dropped_hard = min(least_dropped_hard, frontier[width].cost[0])
            frontier = frontier[:width]
        furthest_offset = max(furthest_offset, max(notes[i].offset for i in sl.notes))

    final = min(frontier, key=lambda s: s.cost)
    hands: dict[int, str] = {}
    violations: list[Violation] = []
    node: _State | None = final
    while node is not None:
        hands.update(dict(node.assigned))
        violations.extend(node.found)
        node = node.parent

    if final.cost[0] == 0:
        status = SolverStatus.FEASIBLE
    elif timed_out or least_dropped_hard < final.cost[0]:
        # A discarded branch was doing better than where we ended up. It might
        # have stayed better. Nothing is proven.
        status = SolverStatus.UNKNOWN
    else:
        status = SolverStatus.INFEASIBLE

    if status == SolverStatus.UNKNOWN:
        for v in violations:
            if v.severity == Severity.HARD:
                v.certainty = Certainty.UNPROVEN

    violations.sort(key=lambda v: (v.time, str(v.rule)))
    return HandSolution(
        status=status, violations=violations, hands=hands, phrases=phrases,
        states_explored=explored, narrowed=narrowed, timed_out=timed_out,
        elapsed=time.perf_counter() - started,
    )
