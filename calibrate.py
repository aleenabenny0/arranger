"""Measure your hands and write profiles/me.json.

Run it, sit at the piano, answer five questions:

    python3 calibrate.py

Every question is something you check by putting your hand on keys. Don't
guess. A wrong number here means every arrangement afterwards is wrong, and
you won't be able to tell which part is broken.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from arranger.profile import PlayerProfile  # noqa: E402


def ask(question: str, options: list[tuple[str, object]]) -> object:
    """Show a numbered menu, return the chosen value."""
    print(f"\n{question}")
    for i, (label, _) in enumerate(options, 1):
        print(f"  {i}. {label}")
    while True:
        raw = input("  > ").strip()
        if raw.isdigit() and 1 <= int(raw) <= len(options):
            return options[int(raw) - 1][1]
        print(f"  Type a number from 1 to {len(options)}.")


print("""
CALIBRATION
Go sit at the piano. This takes about five minutes.
""")

# --- 1. Maximum stretch --------------------------------------------------
# This is the widest chord you can physically hold, all notes down at once.
print("""
TEST 1 — your maximum stretch

Put your thumb on middle C. Stretch your pinky as far right as it goes and
press a key. Both notes down at the same time. Push a little; this is your
limit, not your comfort zone.
""")
max_span = ask("Highest key you can press while holding middle C:", [
    ("The B just below the next C  (a 7th)", 11),
    ("The next C up                (an octave)", 12),
    ("The D above that             (a 9th)", 14),
    ("The E above that             (a 10th)", 16),
    ("Higher than that", 17),
])

# --- 2. Comfortable stretch ---------------------------------------------
print("""
TEST 2 — your comfortable stretch

Same setup, but now: how wide can you go and still hold it through a whole
song without your hand aching?
""")
comfortable = ask("Comfortable reach from middle C:", [
    ("A 5th  (the G above)", 7),
    ("A 6th  (the A above)", 9),
    ("A 7th  (the B above)", 11),
    ("An octave (the next C)", 12),
])
comfortable = min(comfortable, max_span)

# --- 3. Fingers ---------------------------------------------------------
print("""
TEST 3 — notes at once

Almost everyone answers 5. Answer lower only if an injury or a small hand
means you genuinely can't get five fingers down on adjacent-ish keys.
""")
fingers = ask("Notes one hand can play simultaneously:", [
    ("5 — normal", 5),
    ("4", 4),
    ("3", 3),
])

# --- 4. Hand speed ------------------------------------------------------
# Turned into semitones-per-second inside the checker.
print("""
TEST 4 — how fast your hand relocates

Play a low C with your left hand. Then jump two octaves up and play another C
cleanly — no fumbling, no landing on the wrong key. Repeat it a few times and
notice how fast you can go before it gets sloppy.
""")
leap = ask("That two-octave jump, cleanly, takes about:", [
    ("A full second — I need to look", 25.0),
    ("Half a second — steady but careful", 50.0),
    ("A quarter second — pretty quick", 95.0),
    ("Faster; I can do this in tempo", 140.0),
])

# --- 5. Level -----------------------------------------------------------
print("""
TEST 5 — your level

Be honest. Aiming too high produces arrangements you'll abandon.
""")
skill = ask("Roughly where are you?", [
    ("Beginner — reading notes is still slow", 2),
    ("Advancing — easy pop songs, simple classical", 4),
    ("Intermediate — Für Elise, most pop sheet music", 6),
    ("Advanced — Chopin nocturnes, jazz voicings", 8),
])

# --- Build and save -----------------------------------------------------
profile = PlayerProfile(
    name="me",
    max_span=max_span,
    comfortable_span=comfortable,
    max_notes_per_hand=fingers,
    max_leap_rate=leap,
    leap_slack=5,
    skill_level=skill,
)

problems = profile.validate()
if problems:
    print("\nSomething's inconsistent:", "; ".join(problems))
    raise SystemExit(1)

out = Path(__file__).resolve().parent / "profiles" / "me.json"
out.parent.mkdir(exist_ok=True)
profile.save(out)

print(f"""
Saved to {out}

  Max stretch        {profile.max_span} semitones
  Comfortable        {profile.comfortable_span} semitones
  Fingers per hand   {profile.max_notes_per_hand}
  Hand speed         {profile.max_leap_rate:.0f} semitones/second
  Level              {profile.skill_level}/10

NEXT: check these numbers against real music.

Find sheet music for something you can already play, and run the checker on
it. It should say PLAYABLE. If it doesn't, your numbers are too tight — come
back and redo the test that's being flagged.

Do this before writing any more code. An untested ruler measures nothing.
""")
