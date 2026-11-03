# Audio transcription

`arranger.adapters.audio` turns an audio file into a `Score` of detected notes.
It is an importer, on the same footing as the MIDI importer: it produces the
*source* `Score` that a person corrects and the arranger then reduces. It has
nothing to do with `ArrangementPlan`, and no LLM is involved.

Treat its output as a draft. On clean synthetic audio it found every note in
every test (recall 1.0), but in a two-hand texture it already invents some
(precision 0.76). No real recording has been measured here. The numbers below
say how far each claim reaches and where it stops.

Every figure in this document was measured on one machine (described under
Cost) with the code as it stands. Figures marked *test* are asserted or printed
by `tests/test_audio_transcription.py -s`; figures marked *one-off* came from
throwaway scripts that are not checked in, so they can be re-derived but not
re-run with one command. Anything not measured says so.

## What the model is

- **Model:** Spotify's Basic Pitch, the ICASSP 2022 model (`icassp_2022/nmp`),
  a small CNN over a harmonic constant-Q transform (the whole ONNX file is
  230 KB). It was trained to be instrument-agnostic, not piano-specific.
  Paper: Bittner, Bosch, Rubinstein, Meseguer-Brocal, Ewert, *A Lightweight
  Instrument-Agnostic Model for Polyphonic Note Transcription and Multipitch
  Estimation*, ICASSP 2022.
- **Licence:** Apache License 2.0, Copyright 2022 Spotify AB.
  <https://github.com/spotify/basic-pitch>. The weights file is used
  unmodified. The inference windowing (`_run_model` in `adapters/audio.py`) and
  the note creation (`adapters/_basic_pitch_notes.py`) are ports of
  `basic_pitch/inference.py` and `basic_pitch/note_creation.py` from
  basic-pitch 0.4.0, and carry that attribution in their docstrings.
- **File used:** `basic_pitch/saved_models/icassp_2022/nmp.onnx` from the
  basic-pitch 0.4.0 wheel, 230,444 bytes, sha256
  `2c3c1d144bfa61ad236e92e169c13535c880469a12a047d4e73451f2c059a0ec`. Every
  result reports the hash of the file it actually ran (`model_sha256`); a
  different file still runs but adds a warning.
- **Runtime:** ONNX Runtime, CPU provider, one thread, sequential execution.
  **No GPU is needed or used.** The `basic_pitch` Python package is never
  imported: its modules import TensorFlow, librosa and scipy at the top. The
  package is located with `importlib.util.find_spec` and only the model file
  is read.

Model signature, read from the file with onnxruntime rather than assumed:

| | name | shape | used as |
|---|---|---|---|
| input | `serving_default_input_2:0` | `[batch, 43844, 1]` float32 | 2 s minus one hop of 22050 Hz mono audio |
| output | `StatefulPartitionedCall:1` | `[batch, 172, 88]` | note posterior (A0..C8) |
| output | `StatefulPartitionedCall:2` | `[batch, 172, 88]` | onset posterior |
| output | `StatefulPartitionedCall:0` | `[batch, 172, 264]` | contour (3 bins/semitone), **not evaluated** |

The contour head is identified by its 264 bins. The note and onset heads have
identical shapes, so they are told apart by name: `:1` is note and `:2` is
onset, the mapping hard-coded in upstream's `Model.predict`. A model whose
outputs do not fit this is refused with `AudioUnavailable`, not guessed at. A
test checks the mapping behaviourally: on one sustained note the note head
stays above 0.3 for over 100 frames while the onset head does so for fewer
than 15, peaking at the note's start.

## Install

```
pip install "numpy>=2.0" "onnxruntime>=1.20" "soundfile>=0.13" "soxr>=0.5"
pip install --no-deps "basic-pitch>=0.4"
```

The second command needs `--no-deps`. basic-pitch 0.4.0 declares
`tensorflow<2.15.1,>=2.4.1` for Python >= 3.11 on Windows and Linux, and
TensorFlow 2.15 publishes no wheels for Python 3.12 or later, so a plain
install cannot resolve there. For the same reason **`pip install ".[audio]"`
is not expected to work on Python >= 3.12** even though the extra lists the
right packages; use the two commands above. On Python 3.11 a plain install
should resolve but would pull in TensorFlow, which this project does not use;
`--no-deps` is still the right call. (Both statements are read off
basic-pitch's package metadata. Neither was tested by running pip here.)

A deployment that would rather ship only the model file can set
`ARRANGER_BASIC_PITCH_ONNX=/path/to/nmp.onnx`; basic-pitch then need not be
installed at all (*test*: a copy of the file, with `basic_pitch` made
unfindable, transcribes the scale). Keep its LICENSE and NOTICE with the file.

The environment everything here ran in: Python 3.13, Windows 11, numpy 2.5.3,
onnxruntime 1.30.0, soundfile 0.14.0 (libsndfile 1.2.2), soxr 1.1.0,
basic-pitch 0.4.0. basic-pitch was already installed (with `--no-deps`) when
this work started; the install commands above were not re-run. What was
checked: tensorflow, librosa, scipy, pretty_midi, mir_eval, resampy and torch
are all absent from that venv, and all 48 tests pass, so nothing here depends
on them.

`audio_support()` reports what a given process can do:

```python
from arranger.adapters.audio import audio_support
audio_support()
# AudioSupport(available=True, missing=[], model_path='...nmp.onnx',
#   detail='numpy 2.5.3; soundfile 0.14.0; soxr 1.1.0; onnxruntime 1.30.0.
#           libsndfile 1.2.2 decodes: WAV, FLAC, OGG, MP3, AIFF. model: ...')
```

Importing the module never fails for want of these packages; everything
third-party is imported inside functions. If something is missing,
`transcribe_audio` raises `AudioUnavailable` (`code = "audio_unavailable"`)
naming it. There is no demo mode and no canned output.

## API

```python
from arranger.adapters.audio import (
    transcribe_audio, probe_audio, audio_support,
    TranscriptionOptions, TranscriptionResult, AudioInfo, AudioSupport,
    AudioUnavailable, TranscriptionCancelled,
)

result = transcribe_audio(
    data,                                  # bytes of the uploaded file
    filename="take3.flac",                 # messages and title only; never trusted
    options=TranscriptionOptions(),        # thresholds, pitch range, melodia, tempo
    limits=DEFAULT_LIMITS,
    progress=lambda fraction, stage: ...,  # non-decreasing 0..1
    should_cancel=lambda: False,           # polled between 2 s windows
)
result.score                    # Score, source_format="audio", notes a0, a1, ...
result.overall_confidence       # duration-weighted mean of note confidence
result.low_confidence_note_ids  # notes with confidence < 0.5
result.tempo_bpm                # None when no steady pulse was found
result.warnings                 # also copied to result.score.warnings
result.max_polyphony            # most notes sounding at once in the result
result.info                     # AudioInfo: format, subtype, channels, sample_rate, frames, ...
result.model, result.model_sha256
```

`probe_audio(data, filename=..., limits=...)` does the validation alone and
returns the `AudioInfo` without decoding a sample; an upload handler can call
it to reject a file before queueing a job.

Stages reported to `progress`: `decode` (0-0.10), `inference` (0.10-0.88, one
call per window), `notes`, `tempo`, `done` (1.0). `should_cancel` is polled
before every window, during decoding and inside the note-creation loops;
`TranscriptionCancelled` is deliberately *not* a `ScoreImportError`, because a
cancelled job says nothing about the file.

Errors are the shared importer types from `arranger.limits`:
`LimitExceeded`, `MalformedFile`, `UnsupportedFormat`, `EmptyScore`, plus
`AudioUnavailable`. Each has a user-safe `public` message and a `detail` for
logs.

## Containers

Decoding is libsndfile through `soundfile`, from memory. What this build was
found to decode, checked at runtime and by round-trip tests (encode a
synthesised scale, transcribe, compare with the WAV result):

| container | detected here | round-trip test |
|---|---|---|
| WAV, 16-bit PCM and 32-bit float | yes | yes |
| WAVEX | yes | yes (16-bit PCM) |
| FLAC | yes | yes |
| OGG/Vorbis | yes | yes |
| MP3 | yes (libsndfile >= 1.1 with mpg123) | yes |
| AIFF | yes | yes |

Other sample formats inside these containers (24-bit, ADPCM, Opus-in-OGG, ...)
go through the same libsndfile call but were not tested.

MP3 support depends on the libsndfile build. `audio_support().detail` lists
what the running process has, and the MP3 tests skip with a reason where it is
absent. **M4A/AAC, WMA and Opus-in-WebM are not supported**: libsndfile does
not read them and no ffmpeg fallback is built. An `.m4a` upload gets an
`UnsupportedFormat` that says to convert it.

Files are judged by content. The extension is ignored: a PNG named `.wav` is
`UnsupportedFormat`, a WAV named `.png` is transcribed (*test*, both). This
libsndfile build lists 26 formats; six are accepted (WAV, WAVEX, FLAC, OGG,
MP3, AIFF). The rest (AU, VOC, Sound Designer II, ...) are refused with
`UnsupportedFormat` to keep the parsing surface small (*test*: AU and W64).
RF64 and W64 are among the refused: they exist for files over 4 GB, which the
40 MiB cap rules out anyway.

Audio is downmixed to mono by averaging channels and resampled to 22050 Hz
with soxr (`HQ`), streaming, in blocks of 262,144 samples.

## Limits

From `ImportLimits`, enforced in this order, each before the more expensive
step after it:

1. `max_audio_bytes` (40 MiB): checked on `len(data)` before any library is
   touched.
2. Container, channels (1-16), sample rate (4-384 kHz): from the header.
3. `max_audio_seconds` (12 minutes): from the declared frame count, before any
   sample is decoded.
4. The same duration limit again while decoding, because a header can
   understate the length. Decoding stops and raises `LimitExceeded`; it does
   not truncate silently.
5. `max_notes` on the result.

Cut-off files are the awkward case, because libsndfile opens most of them,
reports the shortened length as if it were the real one, and decodes what is
there. Left alone that hands the user half a transcription with no
explanation, so each container gets the check its format allows:

| half a file of... | what happens | how it is caught | source |
|---|---|---|---|
| WAV, WAVEX | `MalformedFile` "cut short" | `data` chunk size against bytes present | test |
| AIFF | `MalformedFile` "cut short" | `SSND` chunk size against bytes present | test |
| FLAC | `MalformedFile` "damaged" | the FLAC decoder loses sync on the first read | one-off |
| FLAC, 600 bytes zeroed mid-file | `MalformedFile` "damaged" | same; the header probe passes | test |
| OGG | transcribed (3 of 8 notes) **with a warning** | last Ogg page lacks the end-of-stream flag | test; numbers one-off |
| MP3 cut at 60% | transcribed (5 of 8 notes, 2.6 s of 4.7 s) **with a warning** | decoded length over 1% short of the declared length | test; numbers one-off |

OGG is a warning rather than an error because some legitimate stream captures
never write the end-of-stream flag. A complete 12 minute OGG written by
libsndfile is not flagged (*one-off*), nor is a multi-page one in the tests.
An MP3 with no length header, cut off, would not be noticed: libsndfile then
measures the length by scanning the same truncated data. That case was not
constructed or tested.

## How notes are made

Faithful to upstream, with the constants it uses: 22050 Hz, hop 256, windows of
43844 samples, 30 frames of overlap (hop 36164 samples), a front pad of 3840
zeros, 15 frames dropped from each side of each window's output before
concatenating. Note creation is `output_to_notes_polyphonic`: onsets inferred
from frame differences, strict local-maximum peak picking, backwards-in-time
forward tracking with an 11-frame energy tolerance, then the melodia pass over
the residual. Frame times use upstream's `model_frames_to_time`. numpy only; no
scipy, librosa or pretty_midi.

A test holds the port to account: the upstream function is transcribed line
for line into the test file, and the two must agree exactly, note for note, on
real model output for three clips (polyphonic, noisy, and one that exercises
the melodia pass).

Defaults: `onset_threshold=0.5`, `frame_threshold=0.3` (upstream's),
`min_note_ms=58` (upstream's default is 127.7; shorter here so fast passages
survive, at the price of more short spurious notes), piano range 21-108.

Two departures from upstream:

- **End of file.** Upstream trims the unwrapped output to
  `floor(seconds * 86)` frames. Unwrapped frames are 11.55 ms apart, not
  1/86 s, so the cut falls 0.67% before the end of the audio: 0.3 s of a 45 s
  clip, 4.8 s of a 12 minute one. This adapter keeps every frame that starts
  inside the audio. Verified: in the 45 s test clip the final note begins after
  upstream's cut; with upstream trimming the same posteriors yield one note
  fewer.
- **No pitch bends.** The contour output is not requested from the model.

Velocity is `round(127 * mean note posterior)`, clipped to 1-127, as upstream
does it. It is **not** loudness. *One-off*, two-hand texture: chord notes
synthesised at velocity 70 came back at 86-102 (mean 94) and melody notes
synthesised at 100 came back at 82-102 (mean 95). Do not read dynamics from
it.

## Confidence

Per note, from the model's posteriors:

```
note_mean  = mean over the note's frames of the note posterior at its pitch
onset_peak = max over frames [start-1, start+1] of the RAW onset posterior at its pitch
confidence = clip(0.6 * note_mean + 0.4 * onset_peak, 0, 1)
```

`onset_peak` uses the model's own onset head, not the frame-difference onsets
that peak picking also uses, so a note that exists only because the frame
posterior jumped scores low on that term. A note with no onset evidence at all
cannot exceed 0.6. *One-off*: the 18 notes the melodia pass produced across
three test clips scored 0.23-0.43.

`overall_confidence` is the mean of note confidence weighted by note duration.
`low_confidence_note_ids` lists notes below 0.5. The 0.6/0.4 weights are a
judgement, not a fit; the number is a triage aid, not a calibrated probability.

What was measured about it (synthetic audio):

| situation | confidence | source |
|---|---|---|
| true notes, clean scale | 0.85-0.86 | one-off |
| true notes, two-hand texture | 0.77-0.88 | one-off |
| same E4 clean / 0 dB SNR / -10 dB SNR white noise | 0.85 / 0.67 / 0.55 | test |
| true notes, scale with a truncated harmonic series | 0.83-0.85 | one-off |
| ghost notes from that timbre | 0.23-0.50, all below every true note | test |
| false positives in the two-hand texture | 0.23-0.65; 7 of 9 are **above** 0.5 | test |
| spurious notes in pure white noise, 36 clips | highest 0.525; highest overall 0.45 | one-off |

So the score orders notes usefully and falls with noise, but the 0.5 line does
not separate right from wrong. In the two-hand texture every false positive
scored below every true note (0.65 against 0.77), yet only 2 of the 9 fell
under 0.5 and into `low_confidence_note_ids`. One white-noise note in 36 clips
scored above 0.5.

Warnings are attached when: overall confidence < 0.5; more than 6 notes sound
at once (dense polyphony is where this model is weakest); more than 0.1% of
samples are at full scale (clipping); RMS below -46 dBFS; the file ended early;
no tempo was found; the model file is not the tested one.

## Tempo

An honest, simple estimator: the onset posterior summed over pitch gives an
onset-strength envelope; its autocorrelation (mean removed, lag 0 normalised to
1, corrected for shrinking overlap) is searched between 50 and 200 bpm with a
mild log-normal preference for ~110 bpm to choose between a tempo and its half
or double, then refined by parabolic interpolation.

```
tempo_confidence = clip((acf[peak] - median(acf over searched lags)) / (1 - median), 0, 1)
```

It measures how periodic the onsets are. It does not know the metre, does not
find the downbeat, does not follow rubato or tempo changes, and can be off by a
factor of two. Below 0.5, or with fewer than 6 onsets or under 4 s of audio,
`tempo_bpm` is `None`: the `Score` is then built on a placeholder
`Timeline.constant(120)` and a warning says bar lines are not meaningful until
the user sets a tempo. Bar 1 always starts at 0 s; there is no pickup
detection. *One-off*, on the test clips: the 120 bpm scale gives 120.0 bpm at
confidence 0.72; the 100 bpm two-hand texture gives 100.0 at 0.59, not far
above the cut-off; 14 randomly spaced onsets give 0.14 and no tempo. Music with
tempo changes, swing or rubato was not tried. Whether the half/double
preference picks the felt tempo on real music was not measured.

## Measured accuracy

The table is printed by `tests/test_audio_transcription.py -s` (*test*). The
audio is rendered by `tests/audio_synth.py`: an additive synthesiser, 16
harmonics at 1/k^1.5, exponential decay, 6 ms attack, perfectly tuned, dry,
noiseless. A detected
note counts as correct when the pitch is exact and the onset is within 50 ms of
an unmatched true note. Offsets and velocities are not scored.

| clip | notes | precision | recall | F1 | worst onset error |
|---|---|---|---|---|---|
| C major scale, monophonic, 4.7 s | 8 | 1.000 | 1.000 | 1.000 | 10.3 ms |
| chromatic run, monophonic, 45.0 s | 90 | 1.000 | 1.000 | 1.000 | 13.7 ms |
| melody over sustained triads, 10.4 s | 28 | 0.757 | 1.000 | 0.862 | 26.8 ms |
| scale with harmonics cut off at 6 partials (provoked) | 8 | 0.571 | 1.000 | 0.727 | 10.3 ms |

Reading them:

- The two-hand result is the informative one: 37 notes reported for 28 played.
  Nothing was missed. Of the nine extra notes, three are chord tones that were
  still sounding, reported as struck again (A3 and C4 at 4.5 s, D4 at 6.9 s);
  the other six are notes nobody played, one octave above a sounding chord tone
  (C4 over C3 three times, F4 over F3, G4 over G3, E4 over E3). Eight of the
  nine begin within 50 ms of a real melody or chord onset. Both kinds should be
  expected over any sustained accompaniment, and an octave ghost in the middle
  register is exactly the kind of error a reducer would mistake for a voice.
- The provoked case shows sensitivity to timbre. A harmonic series that stops
  dead after 4-6 partials makes the model report the top partial or the octave
  as a separate note. No acoustic instrument does that, but some synthesisers
  and heavily filtered recordings come close.
- Onset timing shows no drift over 45 s. *One-off*: mean onset error is
  -2.8 ms over the first ten notes and -0.8 ms over the last ten; over all 90
  it is +1.3 ms with a standard deviation of 5.3 ms. (The test asserts the two
  means are within 15 ms.) Frames are 11.6 ms apart, so this is frame-level
  accuracy on sharp synthetic attacks; soft real attacks were not measured.
- Stereo, 48 kHz (resampled), float WAV, FLAC, OGG, MP3, AIFF and WAVEX
  versions of the scale give the same notes on the same frames as the mono
  22.05 kHz WAV (*test*: onsets within one frame, confidence within 0.05).
  *One-off*: the onset difference was 0 ms in every case and confidences
  differed by at most 0.024 (OGG), 0.013 (MP3), 0.005 otherwise.
- Two runs on the same bytes in one process give identical notes, confidences
  and tempo (*test*). Identity across machines or onnxruntime versions was not
  tested.
- Silence raises `EmptyScore` (*test*). White noise does **not** come back
  empty. *One-off*, 36 four-second clips at three levels: one was empty, the
  rest held 1-10 spurious notes (mean 4.0 over all 36). All 35 carried the
  low-overall-confidence warning and none got a tempo, but a caller that
  ignores warnings will see notes.
- The scale scaled down to an RMS of -60 dBFS (float WAV) gives the same eight
  notes as at its normal -12 dBFS, so the model is largely level-invariant on
  noiseless input. The quiet warning is still given, because in a real
  recording that quiet the noise floor is the problem.

**These numbers are a ceiling, not an estimate.** Synthetic, dry, in-tune,
noiseless audio is the easiest input there is. Real recordings will do worse,
and nothing here measures by how much, because no real recording of any kind
has been put through this code. The list below is therefore *not measured*: it
is what is generally known about frame-level transcription models, and what
the two-hand result above already hints at. Expect, specifically:

- **Reverb and sustain pedal** smear note ends and hide re-articulations:
  durations become unreliable, repeated notes merge.
- **Dense polyphony** (more than about six notes at once): missing inner voices,
  octave and fifth ghosts. The model's authors report much lower note-level
  scores on real piano recordings than the figures above (see the paper); those
  results were not reproduced here.
- **Non-piano timbres, vocals:** vibrato and slides split one note into several
  or bend it to the wrong semitone (pitch bends are not implemented).
- **Drums and percussion** are not pitched and come out as spurious notes.
- **Band or full-mix recordings:** every instrument lands in one undifferentiated
  note list. There is no source separation (see below).
- **Detuned instruments** (a piano a quarter-tone flat) can land on the wrong
  semitone throughout.

The output needs a human to correct it before it is arranged. `melodic_recall`
in the arranger is measured against the source `Score`; if the source is a
wrong transcription, a faithful arrangement of it is faithfully wrong.

## Cost

Measured on this machine: Intel Core i5-11320H (4 cores / 8 threads, laptop),
16 GB RAM, Windows 11, Python 3.13, onnxruntime 1.30 CPU, **one inference
thread**. Inputs were synthesised polyphonic textures, 44.1 kHz stereo. Peak
RSS is the process's peak working set (`GetProcessMemoryInfo`) in a fresh
process that imports the adapter, reads the file and transcribes it once; the
figure includes the file bytes themselves.

Two runs each, with the code as it stands and the default limits (*one-off*):

| input | file | notes out | wall time | per audio minute | peak RSS |
|---|---|---|---|---|---|
| 59.5 s FLAC | 4.6 MB | 410 | 1.60, 1.71 s | 1.6-1.7 s | 100 MB |
| 179.5 s FLAC | 13.9 MB | 1281 | 4.55, 4.86 s | 1.5-1.6 s | 123 MB |
| 719.5 s OGG | 7.9 MB | 5351 | 20.4, 19.1 s | 1.6-1.7 s | 217-224 MB |
| 719.5 s MP3 | 9.7 MB | 5099 | 19.0, 17.4 s | 1.5-1.6 s | 217 MB |

That is 1.5-1.7 s of one core per minute of audio, 35-41x real time. Earlier
runs of the same material during development ranged from 1.4 to 1.9; this is a
laptop and it throttles. Model inference is 72-88% of the time, decoding and
resampling 9-26% (largest for the short FLAC), note creation and tempo about
3%. CPU time was within 4% of wall time in every run: the work is
single-threaded by design, so that results are reproducible and a server can
run one transcription per core. More threads were not tried. Before
transcription the process sits at 26-35 MB including the file bytes; cold
start measured once at 0.22 s to import the four libraries and 0.05 s to load
the model, once per process.

Memory grows with duration. By arithmetic, not measurement: the mono 22 kHz
signal is 5.3 MB per minute, the two posterior matrices 3.7 MB per minute, and
note creation makes one working copy of one of them. It is bounded by the
12 minute limit. The largest file measured was 13.9 MB; a 40 MiB upload at the
limit would add its own size. As an estimate from these figures: budget 300 MB
per concurrent transcription. An earlier version of this code that kept a
padded copy of the audio and a list of per-window outputs peaked at 424 MB on
a 12 minute file, which is why it no longer does. No GPU.

Not measured: Linux or the deployment container, ARM, concurrent jobs in one
process, and anything about real recordings.

## Not implemented

- **Source separation.** Not built; no code in this repository separates
  instruments. It would matter for band recordings: the transcriber receives
  the full mix, so bass, guitar, vocals and drums all become notes in one list,
  and drums become wrong notes. Separating stems first and transcribing the
  pitched ones individually would give cleaner input and would let a user pick
  "the vocal is the melody". Demucs (MIT licence) is the candidate. It needs
  PyTorch and roughly 2-4 GB of RAM, and is far slower on a CPU than the
  transcription is. Those figures are general knowledge about Demucs; it is not
  installed here (torch is absent from the venv) and nothing about it was
  measured. Whether separation artefacts would help or hurt this model on real
  band recordings is also unknown. The old `demucs` entry was removed from the
  `audio` extra because nothing used it.
- **Pitch bends / vibrato**, as above.
- **Instrument, hand or voice assignment.** Every note is track 0, staff unset.
- **Key, metre, downbeat and pickup detection.** The timeline is a constant
  tempo in 4/4 from 0 s. Enharmonic spelling is left to the engraver, as the
  project conventions require.
- **Pedal detection.**
- **M4A/AAC/WMA input**, or any ffmpeg fallback.
- **An API route, job queue or UI.** This is the adapter only. A caller should
  run it off the request thread and wire `progress` / `should_cancel` to its
  job record.
- **A real-recording benchmark.** `evals/corpus/` is symbolic. Until
  transcription is scored against aligned ground truth such as MAESTRO or a
  public-domain equivalent, accuracy on real audio is unknown.

## Tests

```
python -m pytest tests/test_audio_transcription.py -q -s
```

48 tests, about 9 s here, none skipped, no assets and no network. `-s` prints
the measured precision/recall/F1. Model tests skip with an explicit reason when
`audio_support().available` is false. Because a run of skips is green, set
`ARRANGER_REQUIRE_AUDIO=1` wherever the extra is supposed to be installed: the
first test then fails if it is not. Four tests need no audio library at all:
that one, the size limit, the support report, and a subprocess that imports
the adapter with numpy, soundfile, soxr, onnxruntime and basic_pitch all
blocked.

CI does not run this file today. `.github/workflows/ci.yml` installs
`.[api,dev]` and runs a fixed list of test scripts; it installs none of the
audio packages. Until someone adds the two install commands and this file to
that workflow, these tests run only where a developer runs them.
