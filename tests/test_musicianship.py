"""Musician-style analysis tests."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from arranger.ir import Note, Score  # noqa: E402
from arranger.musicianship import (  # noqa: E402
    analyze_note_importance,
    guidance_for_context,
    reduction_priority,
)


def layered_source() -> Score:
    return Score(
        notes=[
            Note(48, 0.0, 1.0, bar=1),
            Note(60, 0.0, 1.0, bar=1),
            Note(64, 0.0, 1.0, bar=1),
            Note(67, 0.0, 1.0, bar=1),
            Note(72, 0.0, 1.0, bar=1),
            Note(84, 0.5, 0.4, bar=1),
        ],
        tempo_bpm=90,
        title="layered",
    )


def test_note_importance_protects_melody_and_bass():
    importance = analyze_note_importance(layered_source())
    melody = [item for item in importance if item.layer == "melody"]
    bass = [item for item in importance if item.layer == "bass"]

    assert melody
    assert bass
    assert min(item.score for item in melody) >= 0.9
    assert min(item.score for item in bass) >= 0.85


def test_reduction_priority_starts_with_optional_material():
    removable = reduction_priority(layered_source())
    assert removable[0].layer in {"doubling", "color", "inner_voice"}
    assert removable[0].score < 0.6


def test_guidance_retrieval_returns_relevant_arranging_principles():
    guidance = guidance_for_context("dense texture melody bass reduction")
    titles = [item.title for item in guidance]
    assert "Protect the melody first" in titles
    assert any("bass" in item.tags for item in guidance)


if __name__ == "__main__":
    tests = [(k, v) for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"  PASS  {name}")
        except AssertionError as exc:
            failed += 1
            print(f"  FAIL  {name}  {exc}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    raise SystemExit(1 if failed else 0)
