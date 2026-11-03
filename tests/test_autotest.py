"""The self-tester: snippets, invariants, contests, baselines and discovery, offline."""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from arranger.adapters.midi_writer import write_midi  # noqa: E402
from arranger.adapters.musicxml_writer import write_musicxml  # noqa: E402
from arranger.adapters.mutopia import BASE_URL, MutopiaCatalogue  # noqa: E402
from arranger.application import autotest  # noqa: E402
from arranger.application.autotest import (  # noqa: E402
    CaseResult,
    CaseSpec,
    Contest,
    baseline_from_results,
    candidates_for_pool,
    check_invariants,
    compare_with_baseline,
    contest,
    discover_mutopia,
    load_profiles,
    read_case,
    render_report,
    snippet,
    snippet_windows,
    test_piece as run_piece,
)
from arranger.engine import arrange_deterministic  # noqa: E402
from arranger.evaluate import Contestant  # noqa: E402
from arranger.ir import Note, PedalSpan, Score  # noqa: E402
from arranger.plan import LHPattern, Section  # noqa: E402
from arranger.profile import PRESETS  # noqa: E402
from arranger.timeline import MeterChange, TempoChange, Timeline  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures" / "mutopia"
PROFILE = PRESETS["intermediate"]


def build(rows, *, bpm=120.0, meter=(4, 4), tempos=(), pedals=(), title="t") -> Score:
    timeline = Timeline([TempoChange(0, bpm), *tempos], [MeterChange(1, *meter)])
    notes = []
    for i, (pitch, beat, beats) in enumerate(rows):
        onset = timeline.seconds_at(beat)
        notes.append(Note(pitch=pitch, onset=onset, duration=timeline.seconds_at(beat + beats) - onset,
                          bar=timeline.bar_at(beat), id=f"s{i}", beat=beat, beats=beats))
    return Score(notes=notes, tempo_bpm=bpm, timeline=timeline, title=title, pedals=list(pedals))


def piece(bars: int = 8, **kwargs) -> Score:
    rows = []
    for bar in range(bars):
        root = (48, 53, 55, 48)[bar % 4]
        beat = bar * 4
        rows += [(root, beat, 4), (root + 4, beat, 4), (root + 7, beat, 4)]
        rows += [(root + 24 + (q * 2), beat + q, 1) for q in range(4)]
    return build(rows, title=f"{bars} bars", **kwargs)


# --- snippets -------------------------------------------------------------------


def test_snippet_cuts_bars_and_restarts_the_clock():
    source = piece(8, pedals=[PedalSpan(0.0, 7.0)])
    cut = snippet(source, 3, 4)
    assert cut.notes and {n.bar for n in cut.notes} == {1, 2}
    assert min(n.onset for n in cut.notes) == 0.0 and min(n.beat for n in cut.notes) == 0.0
    assert len(cut.notes) == 2 * 7
    assert cut.timeline is not None and cut.timeline.initial_bpm == 120.0
    assert cut.pedals == [PedalSpan(0.0, 3.0)], "the pedal span is clipped to the window and shifted"
    assert cut.title.endswith("bars 3-4")
    whole = snippet(source, 1, 8)
    assert len(whole.notes) == len(source.notes)


def test_snippet_carries_tempo_changes_inside_the_window():
    source = piece(8, tempos=[TempoChange(12.0, 60.0)])   # slows down at bar 4
    cut = snippet(source, 3, 5)
    assert cut.timeline.tempos == (TempoChange(0.0, 120.0), TempoChange(4.0, 60.0))
    later = snippet(source, 6, 7)
    assert later.timeline.tempos == (TempoChange(0.0, 60.0),) and later.tempo_bpm == 60.0


def test_snippet_without_a_timeline_uses_the_notes_bars():
    notes = [Note(pitch=60 + b, onset=b * 2.0, duration=1.0, bar=b) for b in range(1, 7)]
    cut = snippet(Score(notes=notes, tempo_bpm=100), 4, 5)
    assert [(n.pitch, n.bar, n.onset) for n in cut.notes] == [(64, 1, 0.0), (65, 2, 2.0)]
    with pytest.raises(ValueError):
        snippet(Score(notes=notes), 0, 3)


def test_snippet_windows_are_evenly_spaced_and_absent_on_short_pieces():
    # Never more windows than fit without overlap, spread from the first bar to the last.
    assert snippet_windows(piece(48), bars=16, max_count=3) == [(1, 16), (17, 32), (33, 48)]
    assert snippet_windows(piece(64), bars=16, max_count=3) == [(1, 16), (25, 40), (49, 64)]
    assert snippet_windows(piece(40), bars=16, max_count=3) == [(1, 16), (25, 40)]
    assert snippet_windows(piece(20), bars=16, max_count=3) == [(1, 16)]
    assert snippet_windows(piece(16), bars=16) == []
    assert snippet_windows(piece(40), bars=0) == [] and snippet_windows(piece(40), max_count=0) == []


# --- reading, invariants, contests -------------------------------------------------


def test_read_case_sniffs_midi_and_musicxml():
    source = piece(4)
    assert len(read_case(write_midi(source), "x.mid").notes) == len(source.notes)
    xml, _warnings = write_musicxml(source)
    assert len(read_case(xml, "x.musicxml").notes) == len(source.notes)


def test_invariants_hold_on_a_well_formed_piece():
    source = piece(6)
    candidate = arrange_deterministic(source, PROFILE)
    found = check_invariants(source, candidate, PROFILE, reproducibility=True)
    names = [i.name for i in found]
    assert names == ["midi_round_trip", "score_json_round_trip", "plan_valid_for_source",
                     "musicxml_round_trip", "verify_deterministic", "melody_kept", "engine_reproducible"]
    assert all(i.ok for i in found), [(i.name, i.detail) for i in found if not i.ok]


def test_a_failed_invariant_names_what_went_wrong():
    source = piece(4)
    candidate = arrange_deterministic(source, PROFILE)
    # A plan that leaves the last bar without a left hand: the validation
    # invariant must catch it and say which bar.
    candidate.plan.sections = [Section(1, 3, LHPattern.BLOCK)]
    found = {i.name: i for i in check_invariants(source, candidate, PROFILE)}
    assert not found["plan_valid_for_source"].ok and "not covered" in found["plan_valid_for_source"].detail
    assert "4" in found["plan_valid_for_source"].detail


def test_musicxml_round_trip_tolerates_only_declared_merges(monkeypatch):
    from arranger.adapters.musicxml_writer import write_musicxml as real_write

    source = piece(4)
    candidate = arrange_deterministic(source, PROFILE)
    short = Score(notes=candidate.arranged.notes[:-1], tempo_bpm=source.tempo_bpm,
                  timeline=source.timeline, title="one note short")

    def one_short_undeclared(score):
        xml, warnings = real_write(short)
        return xml, warnings

    def one_short_declared(score):
        xml, warnings = real_write(short)
        return xml, [*warnings, "1 repeated note(s) fell on the same position as a neighbour of the "
                                "same pitch after quantisation and were merged into it."]

    monkeypatch.setattr(autotest, "write_musicxml", one_short_undeclared)
    found = {i.name: i for i in check_invariants(source, candidate, PROFILE)}
    assert not found["musicxml_round_trip"].ok and "1 missing, 0 declared" in found["musicxml_round_trip"].detail

    monkeypatch.setattr(autotest, "write_musicxml", one_short_declared)
    found = {i.name: i for i in check_invariants(source, candidate, PROFILE)}
    assert found["musicxml_round_trip"].ok and "1 missing, 1 declared" in found["musicxml_round_trip"].detail
    assert autotest.declared_merges(["2 repeated note(s) fell on the same position as x", "other"]) == 2


def test_contest_is_never_lost_by_the_engine():
    source = piece(8)
    held, engine = contest(source, PROFILE)
    assert held.profile == PROFILE.name and held.on_quality in ("win", "tie")
    assert held.engine.cost <= held.baseline.cost and engine.plan is not None
    assert held.model is None and not held.engine_lost


def test_test_piece_covers_the_whole_and_its_snippets():
    source = piece(40)
    spec = CaseSpec(id="forty", title="Forty bars", origin="file")
    result = run_piece(spec, write_midi(source), [PROFILE], snippet_bars=16, max_snippets=2, deadline_seconds=60)
    assert result.error is None and result.notes == len(source.notes) and result.bars == 40
    assert result.spec.sha256 and len(result.contests) == 1
    assert len(result.snippets) == 2 and all(s.contests for s in result.snippets)
    assert all("engine_reproducible" in [i.name for i in s.invariants] for s in result.snippets)
    assert result.ok, result.failures()
    assert result.to_dict()["snippets"][0]["spec"]["id"].startswith("forty#bars-")


def test_a_refused_file_is_recorded_not_failed():
    spec = CaseSpec(id="junk", title="junk", origin="file")
    result = run_piece(spec, b"MThd" + b"\x00" * 3, [PROFILE])
    assert result.error and result.error.startswith("refused:") and result.refused
    assert result.ok and result.contests == []


# --- baselines ----------------------------------------------------------------------


def run_fixture() -> list[CaseResult]:
    spec = CaseSpec(id="eight", title="Eight bars", origin="file")
    return [run_piece(spec, write_midi(piece(8)), [PROFILE], snippet_bars=0)]


def test_a_run_matches_its_own_baseline():
    results = run_fixture()
    baseline = baseline_from_results(results)
    assert baseline["algorithm_version"] and results[0].spec.sha256 in baseline["cases"]
    assert compare_with_baseline(results, baseline) == []
    assert compare_with_baseline(results, {"cases": {}}) == [], "an unknown case is not a regression"


def test_an_unchanged_result_is_never_a_regression_whatever_its_decimals():
    # The first CI run flagged half the corpus: the comparison rounded the
    # recorded fidelity to three places and the fresh one to four, so 0.9226
    # against its own record (0.923) read as a fall. Same number, same answer.
    results = run_fixture()
    held = results[0].contests[0]
    for value in (0.9226, 0.9224, 0.925, 0.9999, 0.88):
        held.engine.fidelity = value
        baseline = baseline_from_results(results)
        assert compare_with_baseline(results, baseline) == [], value


def test_regressions_are_named():
    results = run_fixture()
    baseline = baseline_from_results(results)
    entry = baseline["cases"][results[0].spec.sha256]["contests"][PROFILE.name]
    entry["engine_cost"] = -1.0
    assert any("engine cost" in r for r in compare_with_baseline(results, baseline))
    entry["engine_cost"] = results[0].contests[0].engine.cost
    entry["engine_strain"] = -1
    assert any("engine quality fell" in r for r in compare_with_baseline(results, baseline))
    entry["engine_strain"] = results[0].contests[0].engine.strain
    entry["on_quality"] = "win"
    results[0].contests[0].on_quality = "tie"
    assert any("now has a tie" in r for r in compare_with_baseline(results, baseline))
    baseline["cases"][results[0].spec.sha256]["invariants"]["melody_kept"] = True
    results[0].invariants[0].ok = False
    results[0].invariants[0].name = "melody_kept"
    assert any("held before and fails now" in r for r in compare_with_baseline(results, baseline))


# --- report ------------------------------------------------------------------------------


def test_report_lists_failures_regressions_and_candidates():
    results = run_fixture()
    text = render_report(results, [], title="Test run")
    assert text.startswith("# Test run") and "| eight |" in text and "never lost" in text
    results[0].contests[0].on_quality = "loss"
    results[0].contests.append(Contest("hard", Contestant(2.0, 2, 3, 0.9, 1.0, 1, False, 0.1, "block/v1/f0"),
                                       Contestant(1.0, 1, 3, 0.9, 1.0, 1, False, 0.1, "x"), "win", "win"))
    text = render_report(results, ["eight (intermediate): engine cost 1.00 was 0.00"])
    assert "## Failures" in text and "lost to brute force" in text
    assert "## Regressions against the baseline" in text
    assert "## Resists brute force" in text and candidates_for_pool(results)[0]["brute_force_cost"] == 2.0


# --- discovery ----------------------------------------------------------------------------


class FakeSite:
    def __init__(self):
        self.urls = []

    def fetch(self, url, *, max_bytes, timeout):
        self.urls.append(url)
        if url.endswith(".mid"):
            return write_midi(piece(6))
        if "startat=0" in url:
            return (FIXTURES / "search-piano-page1.html").read_bytes()
        return (FIXTURES / "search-empty.html").read_bytes()

    def client(self):
        return MutopiaCatalogue(self.fetch, sleep=lambda s: None)


def test_discovery_downloads_permitted_pieces_once_and_keeps_their_provenance(tmp_path):
    site = FakeSite()
    cases = discover_mutopia(site.client(), page=0, limit=3, cache_dir=tmp_path)
    assert len(cases) == 3
    spec, data = cases[0]
    assert spec.origin == "discovered" and spec.license == "Public Domain" and spec.source_url.startswith(BASE_URL)
    assert spec.id.startswith("mutopia-2247") and data.startswith(b"MThd")
    index = json.loads((tmp_path / "index.json").read_text(encoding="utf-8"))
    assert spec.id in index and index[spec.id]["sha256"] == spec.sha256
    assert (tmp_path / f"{spec.id}.mid").exists()
    downloads = [u for u in site.urls if u.endswith(".mid")]
    assert len(downloads) == 3
    again = discover_mutopia(site.client(), page=0, limit=3, cache_dir=tmp_path)
    assert [s.id for s, _ in again] == [s.id for s, _ in cases]
    assert len([u for u in site.urls if u.endswith(".mid")]) == 3, "cached files are not fetched again"


def test_discovery_skips_unlicensed_pieces_and_wraps_past_the_end(tmp_path):
    class Site(FakeSite):
        def fetch(self, url, *, max_bytes, timeout):
            if "startat=0" in url:
                page = (FIXTURES / "search-piano-page1.html").read_text(encoding="utf-8")
                return page.replace("legal.html#publicdomain", "legal.html#ccancsa", 2).encode("utf-8")
            return super().fetch(url, max_bytes=max_bytes, timeout=timeout)

    site = Site()
    said = []
    cases = discover_mutopia(site.client(), page=500, limit=2, cache_dir=tmp_path, log=said.append)
    assert any("past the end" in line for line in said)
    assert any("not permitted" in line for line in said)
    assert len(cases) == 2 and all(s.license != "" for s, _ in cases)


def test_profiles_come_from_presets_or_files():
    assert load_profiles(["preset:beginner"])[0].name == "beginner"
    assert load_profiles([str(Path("profiles") / "me.json")])[0].name == "me"
    with pytest.raises(ValueError):
        load_profiles(["preset:virtuoso"])


def test_main_runs_files_writes_reports_and_updates_a_baseline(tmp_path):
    path = tmp_path / "eight.mid"
    path.write_bytes(write_midi(piece(8)))
    report, numbers, baseline = tmp_path / "r.md", tmp_path / "r.json", tmp_path / "baseline.json"
    args = ["--files", str(path), "--snippets", "0", "--deadline", "60", "--quiet",
            "--report", str(report), "--json", str(numbers), "--baseline", str(baseline)]
    assert autotest.main([*args, "--update-baseline"]) == 0
    assert baseline.exists() and report.read_text(encoding="utf-8").startswith("# Autotest")
    data = json.loads(numbers.read_text(encoding="utf-8"))
    assert data["regressions"] == [] and data["results"][0]["spec"]["origin"] == "file"
    assert autotest.main(args) == 0, "the same run against its own baseline has no regressions"


def test_updating_the_baseline_merges_into_the_existing_one(tmp_path):
    first, second = tmp_path / "a.mid", tmp_path / "b.mid"
    first.write_bytes(write_midi(piece(8)))
    second.write_bytes(write_midi(piece(12)))
    baseline = tmp_path / "baseline.json"
    common = ["--snippets", "0", "--deadline", "60", "--quiet", "--baseline", str(baseline), "--update-baseline"]
    assert autotest.main(["--files", str(first), *common]) == 0
    assert autotest.main(["--files", str(second), *common]) == 0
    recorded = json.loads(baseline.read_text(encoding="utf-8"))
    assert len(recorded["cases"]) == 2, "the second run kept the first run's case"
    assert autotest.main(["--files", str(first), str(second), "--snippets", "0", "--deadline", "60",
                          "--quiet", "--baseline", str(baseline)]) == 0


def test_main_fails_the_run_on_a_regression(tmp_path):
    path = tmp_path / "eight.mid"
    path.write_bytes(write_midi(piece(8)))
    baseline = tmp_path / "baseline.json"
    args = ["--files", str(path), "--snippets", "0", "--deadline", "60", "--quiet", "--baseline", str(baseline)]
    assert autotest.main([*args, "--update-baseline"]) == 0
    recorded = json.loads(baseline.read_text(encoding="utf-8"))
    for entry in recorded["cases"].values():
        for held in entry["contests"].values():
            held["engine_cost"] = -1.0
    baseline.write_text(json.dumps(recorded), encoding="utf-8")
    assert autotest.main(args) == 1


def test_main_with_nothing_to_test_says_so(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(autotest, "CORPUS_DIR", tmp_path / "missing")
    assert autotest.main(["--quiet"]) == 2
    assert "nothing to test" in capsys.readouterr().err
