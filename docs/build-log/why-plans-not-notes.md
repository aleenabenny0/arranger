# Why the model emits plans, not notes

The obvious design is: give the model the transcription, ask for MusicXML.
It does not work, and the reasons are worth writing down because every
contributor proposes it again.

**1. Notation is a bad output format for a language model.** MusicXML is
verbose, deeply nested, and full of state that must stay consistent across
hundreds of lines (divisions, key, clef, voice numbering). Models produce
plausible-looking XML with subtly broken state, and the failures surface as
unreadable engraving rather than obvious errors.

**2. You cannot review a note stream.** When an arrangement is wrong, you want
to know *what decision* was wrong. A plan says "dropped the countermelody in
bars 9-16 to fit the left hand under a 9th". A note stream says nothing; you
have to reverse-engineer the intent from what's missing.

**3. Repair needs a small surface.** Fixing a span violation by editing notes
means finding every affected note and keeping voices consistent. Fixing it in
a plan means changing one field. Small edit surfaces make repair loops
converge; large ones make them thrash.

**4. It separates the two kinds of correctness.** Musical judgement is
subjective and the model is good at it. Structural correctness is objective
and the model is unreliable at it. Rendering deterministically means a bad
arrangement is always a *musical* mistake, never a malformed file. That makes
every failure diagnosable.

The cost: the plan schema has to be expressive enough to describe any
arrangement worth making. When something cannot be expressed, the fix is to
extend the schema — not to let the model bypass it "just this once".
