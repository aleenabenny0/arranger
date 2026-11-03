"""The product workflow over HTTP: import, correct, arrange, review, export, manage.

Jobs are run inline with `job_runner.run_once()` so the tests are deterministic;
`test_background_workers_pick_up_jobs` covers the threaded path once.
"""

import io
import json
import sys
import time
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from conftest import PASSWORD, build_settings, csrf_headers, make_client, register  # noqa: E402

from arranger.adapters.lilypond import LilyPondEngraver  # noqa: E402
from arranger.adapters.midi_writer import write_midi  # noqa: E402
from arranger.adapters.musicxml_writer import write_musicxml, write_mxl  # noqa: E402
from arranger.engine import Cancelled  # noqa: E402
from arranger.io import read_midi_bytes  # noqa: E402
from arranger.ir import Note, Score, TrackInfo  # noqa: E402
from arranger.timeline import KeyChange, MeterChange, TempoChange, Timeline  # noqa: E402
from arranger_api.jobs import JobFailed  # noqa: E402

HAS_LILYPOND = LilyPondEngraver().status().available
PROFILE = {"name": "me", "max_span": 12, "comfortable_span": 9, "skill_level": 5, "max_leap_rate": 70.0}


def piece(bars: int = 8, title: str = "Test Waltz") -> Score:
    timeline = Timeline([TempoChange(0, 120)], [MeterChange(1, 4, 4)], [KeyChange(0, 0)])
    notes = []
    roots = [48, 53, 55, 48]
    for bar in range(bars):
        for k in range(4):
            beat = bar * 4 + k
            notes.append(Note(72 + (k + bar) % 5, beat * 0.5, 0.5, staff=1, bar=bar + 1, beat=float(beat),
                              beats=1.0, track=0, velocity=96))
            notes.append(Note(roots[bar % 4] + (0, 7, 4, 7)[k], beat * 0.5, 0.5, staff=2, bar=bar + 1,
                              beat=float(beat), beats=1.0, track=1, velocity=60))
    return Score(notes=notes, tempo_bpm=120, title=title, timeline=timeline,
                 tracks=[TrackInfo(0, "Melody"), TrackInfo(1, "Accompaniment")])


MIDI = write_midi(piece())


@pytest.fixture
def api(tmp_path):
    settings = build_settings(sqlite_path=str(tmp_path / "app.db"), artifact_dir=str(tmp_path / "files"),
                              job_workers=0, quota_active_jobs=3)
    with make_client(settings=settings) as client:
        client.settings = settings
        yield client


def upload(api, data=MIDI, filename="test_waltz.mid", **params):
    return api.post("/projects/import", params={"filename": filename, **params}, content=data,
                    headers={**csrf_headers(api), "Content-Type": "application/octet-stream"})


def run_jobs(api, limit=10):
    ran = []
    for _ in range(limit):
        job = api.app.state.job_runner.run_once()
        if job is None:
            break
        ran.append(job)
    return ran


def arrange(api, project_id, **body):
    response = api.post(f"/projects/{project_id}/arrangements", json={"profile": PROFILE, **body},
                        headers=csrf_headers(api))
    assert response.status_code == 202, response.text
    job_id = response.json()["job"]["id"]
    run_jobs(api)
    job = api.get(f"/jobs/{job_id}").json()["job"]
    assert job["status"] == "succeeded", job
    return job["result"]["arrangement_id"]


# --- the journey ---------------------------------------------------------------------


def test_upload_inspect_correct_arrange_review_export_and_reopen(api, tmp_path):
    register(api)

    # Upload: a real MIDI file as the request body.
    created = upload(api)
    assert created.status_code == 201, created.text
    project = created.json()["project"]
    inspection = created.json()["inspection"]
    assert project["title"] == "test waltz" and project["source_kind"] == "midi"
    assert inspection["bar_count"] == 8 and inspection["meter"] == [4, 4]
    assert [t["name"] for t in inspection["tracks"]] == ["Right hand", "Left hand"]
    assert inspection["suggested_melody_track"] == 0
    project_id = project["id"]

    # Correct: choose the melody part and transpose. A new revision, not an edit.
    revised = api.post(f"/projects/{project_id}/sources",
                       json={"based_on": project["current_source_id"],
                             "selection": {"melody_track": 0, "transpose": -2}},
                       headers=csrf_headers(api))
    assert revised.status_code == 201, revised.text
    assert revised.json()["source"]["revision"] == 2
    detail = api.get(f"/projects/{project_id}").json()
    assert [s["revision"] for s in detail["sources"]] == [2, 1]
    assert detail["project"]["current_source_id"] == revised.json()["source"]["id"]

    # Arrange: a job, run by the real engine.
    arrangement_id = arrange(api, project_id, label="first try")
    full = api.get(f"/projects/{project_id}/arrangements/{arrangement_id}").json()
    record = full["arrangement"]
    assert record["revision"] == 1 and record["label"] == "first try" and record["accepted"]
    assert record["summary"]["findings"]["headline"] == "Passes modeled constraints"
    assert record["summary"]["fidelity"]["score"] >= 0.88
    assert record["algorithm_version"] and record["origin"] in ("deterministic", "local_repair")
    assert record["profile"]["max_span"] == 12 and record["plan"]["sections"]
    assert any("Transposed down 2" in s for s in record["report"]["sentences"])
    assert len(full["playback"]["notes"]) > 32 and full["playback"]["bars"][0] == [1, 0.0]
    assert {a["kind"] for a in full["artifacts"]} == {"midi", "musicxml"}

    # Preview and downloads are the same music.
    preview = api.get(f"/projects/{project_id}/arrangements/{arrangement_id}/musicxml")
    assert preview.status_code == 200 and b"<score-partwise" in preview.content
    exported = api.post(f"/projects/{project_id}/arrangements/{arrangement_id}/exports/midi", headers=csrf_headers(api))
    artifact = exported.json()["artifact"]
    download = api.get(f"/artifacts/{artifact['id']}/download")
    assert download.status_code == 200 and download.headers["content-type"] == "audio/midi"
    assert "attachment" in download.headers["content-disposition"]
    assert download.headers["cache-control"] == "no-store"
    back = read_midi_bytes(download.content)
    assert len(back.notes) == len(full["playback"]["notes"])
    assert min(n.pitch for n in back.notes if n.staff == 1) >= 60 - 2

    mxl = api.post(f"/projects/{project_id}/arrangements/{arrangement_id}/exports/mxl", headers=csrf_headers(api))
    assert zipfile.is_zipfile(io.BytesIO(api.get(f"/artifacts/{mxl.json()['artifact']['id']}/download").content))

    # Revise: an edited plan becomes revision 2; revision 1 is untouched.
    plan = record["plan"]
    for section in plan["sections"]:
        section["lh_pattern"] = "alberti"
    second_id = arrange(api, project_id, plan=plan, label="alberti")
    second = api.get(f"/projects/{project_id}/arrangements/{second_id}").json()["arrangement"]
    assert second["revision"] == 2 and second["origin"] == "user"
    compare = api.get(f"/projects/{project_id}/compare", params={"a": arrangement_id, "b": second_id}).json()
    assert compare["same_source"] and any("lh pattern" in c and "alberti" in c for c in compare["plan_changes"])
    assert api.get(f"/projects/{project_id}/arrangements/{arrangement_id}").json()["arrangement"]["plan"] != plan

    # Library: search, rename.
    assert api.get("/projects", params={"q": "WALTZ"}).json()["total"] == 1
    assert api.get("/projects", params={"q": "100%_nothing"}).json()["total"] == 0
    renamed = api.patch(f"/projects/{project_id}", json={"title": "Valse <b>triste</b>"}, headers=csrf_headers(api))
    assert renamed.json()["project"]["title"] == "Valse <b>triste</b>", "stored as text; the frontend never renders it as HTML"

    # Restart: a new process over the same database and files.
    cookies = dict(api.cookies)
    with make_client(settings=api.settings) as reopened:
        reopened.cookies.update(cookies)
        again = reopened.get(f"/projects/{project_id}").json()
        assert again["project"]["title"] == "Valse <b>triste</b>"
        assert [a["revision"] for a in again["arrangements"]] == [2, 1]
        assert reopened.get(f"/artifacts/{artifact['id']}/download").content == download.content


@pytest.mark.skipif(not HAS_LILYPOND, reason="LilyPond is not installed; PDF engraving cannot be exercised")
def test_pdf_export_is_a_real_engraved_document(api):
    register(api)
    project_id = upload(api).json()["project"]["id"]
    arrangement_id = arrange(api, project_id)
    started = api.post(f"/projects/{project_id}/arrangements/{arrangement_id}/exports/pdf", headers=csrf_headers(api))
    assert started.status_code == 202
    job_id = started.json()["job"]["id"]
    # Asking again while it is queued returns the same job, not a second engraving.
    again = api.post(f"/projects/{project_id}/arrangements/{arrangement_id}/exports/pdf", headers=csrf_headers(api))
    assert again.json()["job"]["id"] == job_id
    run_jobs(api)
    job = api.get(f"/jobs/{job_id}").json()["job"]
    assert job["status"] == "succeeded", job
    pdf = api.get(f"/artifacts/{job['result']['artifact_id']}/download")
    assert pdf.headers["content-type"] == "application/pdf" and pdf.content.startswith(b"%PDF")
    assert len(pdf.content) > 5_000
    ready = api.post(f"/projects/{project_id}/arrangements/{arrangement_id}/exports/pdf", headers=csrf_headers(api))
    assert ready.status_code == 200 and ready.json()["artifact"]["id"] == job["result"]["artifact_id"]


def test_pdf_export_says_so_when_the_engraver_is_missing(api):
    register(api)
    project_id = upload(api).json()["project"]["id"]
    arrangement_id = arrange(api, project_id)

    class Missing:
        def status(self):
            return type("S", (), {"available": False, "version": None})()

    api.app.state.engraver = Missing()
    response = api.post(f"/projects/{project_id}/arrangements/{arrangement_id}/exports/pdf", headers=csrf_headers(api))
    assert response.status_code == 501 and response.json()["detail"]["error"] == "engraver_unavailable"


def test_musicxml_and_compressed_musicxml_import(api):
    register(api)
    xml, _ = write_musicxml(piece(title="From XML"))
    mxl, _ = write_mxl(piece(title="From MXL"))
    first = upload(api, xml, "a.musicxml")
    second = upload(api, mxl, "b.mxl", title="Renamed on upload")
    assert first.status_code == 201 and first.json()["project"]["source_kind"] == "musicxml"
    assert first.json()["project"]["title"] == "From XML"
    assert second.json()["project"]["title"] == "Renamed on upload"
    assert second.json()["inspection"]["key"] == {"fifths": 0, "mode": "major", "estimated": False}


# --- bad uploads ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "data, filename, status, code",
    [(b"", "empty.mid", 400, "empty_upload"),
     (b"%PDF-1.7 definitely not music", "score.pdf", 415, "unsupported_format"),
     (b"MThd" + b"\x00" * 3, "short.mid", 415, "unsupported_format"),
     (b"MThd\x00\x00\x00\x06\x00\x01\x00\x01\xe7\x28MTrk\x00\x00\x00\x04\x00\xff\x2f\x00", "smpte.mid", 415, "unsupported_midi"),
     (b'<?xml version="1.0"?><!DOCTYPE score-partwise [<!ENTITY x "y">]><score-partwise/>', "evil.musicxml",
      400, "unsafe_content")],
    ids=["empty", "pdf", "truncated-midi", "smpte", "entity-declaration"],
)
def test_bad_uploads_get_a_typed_error_and_leave_nothing_behind(api, data, filename, status, code):
    register(api)
    response = upload(api, data, filename)
    assert response.status_code == status, response.text
    assert response.json()["detail"]["error"] == code
    assert "Traceback" not in response.text and "arranger" not in response.json()["detail"]["detail"].lower()
    assert api.get("/projects").json()["total"] == 0
    assert api.get("/account/usage").json()["storage_bytes"]["used"] == 0


def test_upload_requires_a_session_and_a_csrf_token(api):
    assert api.post("/projects/import", content=MIDI).status_code == 401
    register(api)
    assert api.post("/projects/import", content=MIDI).status_code == 403
    assert upload(api).status_code == 201


def test_oversized_upload_is_refused_by_the_body_limit(tmp_path):
    settings = build_settings(sqlite_path=str(tmp_path / "a.db"), artifact_dir=str(tmp_path / "f"),
                              job_workers=0, max_upload_bytes=2_000)
    with make_client(settings=settings) as api:
        register(api)
        assert upload(api, MIDI + b"\x00" * 4_000).status_code == 413


# --- isolation ---------------------------------------------------------------------------------


def test_another_user_cannot_see_or_touch_anything(api):
    register(api, "owner@example.com")
    project_id = upload(api).json()["project"]["id"]
    arrangement_id = arrange(api, project_id)
    detail = api.get(f"/projects/{project_id}").json()
    source_id = detail["project"]["current_source_id"]
    artifact_id = detail["artifacts"][0]["id"]
    job_id = api.get("/jobs").json()["jobs"][0]["id"]

    with make_client(app=api.app) as intruder:
        register(intruder, "intruder@example.com")
        own_project = upload(intruder).json()["project"]
        headers = csrf_headers(intruder)
        attempts = [
            intruder.get(f"/projects/{project_id}"),
            intruder.patch(f"/projects/{project_id}", json={"title": "mine now"}, headers=headers),
            intruder.delete(f"/projects/{project_id}", headers=headers),
            intruder.get(f"/projects/{project_id}/sources/{source_id}"),
            intruder.get(f"/projects/{project_id}/sources/{source_id}/musicxml"),
            intruder.post(f"/projects/{project_id}/sources", json={"based_on": source_id, "selection": {}}, headers=headers),
            intruder.post(f"/projects/{project_id}/arrangements", json={"profile": PROFILE}, headers=headers),
            intruder.get(f"/projects/{project_id}/arrangements/{arrangement_id}"),
            intruder.get(f"/projects/{project_id}/arrangements/{arrangement_id}/musicxml"),
            intruder.patch(f"/projects/{project_id}/arrangements/{arrangement_id}", json={"label": "x"}, headers=headers),
            intruder.post(f"/projects/{project_id}/arrangements/{arrangement_id}/exports/midi", headers=headers),
            intruder.get(f"/projects/{project_id}/compare", params={"a": arrangement_id, "b": arrangement_id}),
            intruder.get(f"/jobs/{job_id}"),
            intruder.post(f"/jobs/{job_id}/cancel", headers=headers),
            intruder.post(f"/jobs/{job_id}/retry", headers=headers),
            intruder.get(f"/artifacts/{artifact_id}/download"),
            # Own project, someone else's records: relationships are checked too.
            intruder.get(f"/projects/{own_project['id']}/sources/{source_id}"),
            intruder.get(f"/projects/{own_project['id']}/arrangements/{arrangement_id}"),
            intruder.post(f"/projects/{own_project['id']}/arrangements",
                          json={"profile": PROFILE, "source_id": source_id}, headers=headers),
            intruder.post(f"/projects/{own_project['id']}/sources",
                          json={"based_on": source_id, "selection": {}}, headers=headers),
        ]
        assert [r.status_code for r in attempts] == [404] * len(attempts)
        assert intruder.get("/projects").json()["total"] == 1
        assert [j["project_id"] for j in intruder.get("/jobs").json()["jobs"]] == []

    assert api.get(f"/projects/{project_id}").json()["project"]["title"] == "test waltz"


def test_a_source_from_one_project_cannot_be_used_in_another(api):
    register(api)
    first = upload(api).json()["project"]
    second = upload(api, filename="other.mid").json()["project"]
    response = api.post(f"/projects/{second['id']}/arrangements",
                        json={"profile": PROFILE, "source_id": first["current_source_id"]}, headers=csrf_headers(api))
    assert response.status_code == 404


# --- jobs --------------------------------------------------------------------------------------------


def test_the_same_idempotency_key_starts_one_job(api):
    register(api)
    project_id = upload(api).json()["project"]["id"]
    headers = {**csrf_headers(api), "Idempotency-Key": "click-0001-abcdef"}
    first = api.post(f"/projects/{project_id}/arrangements", json={"profile": PROFILE}, headers=headers).json()
    second = api.post(f"/projects/{project_id}/arrangements", json={"profile": PROFILE}, headers=headers).json()
    assert first["created"] and not second["created"] and first["job"]["id"] == second["job"]["id"]
    assert len(api.get("/jobs").json()["jobs"]) == 1
    bad = api.post(f"/projects/{project_id}/arrangements", json={"profile": PROFILE},
                   headers={**csrf_headers(api), "Idempotency-Key": "no spaces allowed"})
    assert bad.status_code == 400


def test_a_queued_job_can_be_cancelled_and_then_never_runs(api):
    register(api)
    project_id = upload(api).json()["project"]["id"]
    job_id = api.post(f"/projects/{project_id}/arrangements", json={"profile": PROFILE},
                      headers=csrf_headers(api)).json()["job"]["id"]
    cancelled = api.post(f"/jobs/{job_id}/cancel", headers=csrf_headers(api)).json()["job"]
    assert cancelled["status"] == "cancelled"
    assert run_jobs(api) == []
    assert api.get(f"/projects/{project_id}").json()["arrangements"] == []
    retried = api.post(f"/jobs/{job_id}/retry", headers=csrf_headers(api)).json()["job"]
    assert retried["status"] == "queued"
    assert [j["status"] for j in [api.get(f"/jobs/{job_id}").json()["job"]]] == ["queued"]
    run_jobs(api)
    assert api.get(f"/jobs/{job_id}").json()["job"]["status"] == "succeeded"


def test_a_running_job_stops_when_asked(api):
    register(api)
    project_id = upload(api).json()["project"]["id"]
    runner = api.app.state.job_runner
    seen = {}

    def slow(ctx):
        ctx.progress(0.3, "Working")
        seen["visible"] = api.get(f"/jobs/{ctx.job['id']}").json()["job"]
        api.post(f"/jobs/{ctx.job['id']}/cancel", headers=csrf_headers(api))
        for _ in range(200):
            if ctx.should_cancel():
                raise Cancelled()
            time.sleep(0.02)
        return {"finished": True}

    runner.handlers = {**runner.handlers, "arrange": slow}
    job_id = api.post(f"/projects/{project_id}/arrangements", json={"profile": PROFILE},
                      headers=csrf_headers(api)).json()["job"]["id"]
    started = time.monotonic()
    run_jobs(api)
    assert time.monotonic() - started < 3, "cancellation must not wait for the work to finish"
    assert seen["visible"]["status"] == "running" and seen["visible"]["stage"] == "Working"
    assert seen["visible"]["progress"] == 0.3
    assert api.get(f"/jobs/{job_id}").json()["job"]["status"] == "cancelled"


def test_failures_show_a_safe_message_and_crashes_are_retried_then_failed(api):
    register(api)
    project_id = upload(api).json()["project"]["id"]
    runner = api.app.state.job_runner
    calls = {"n": 0}

    def explode(ctx):
        calls["n"] += 1
        raise RuntimeError(r"secret path C:\\srv\\arranger\\db.sqlite and SELECT * FROM users")

    runner.handlers = {**runner.handlers, "arrange": explode}
    job_id = api.post(f"/projects/{project_id}/arrangements", json={"profile": PROFILE},
                      headers=csrf_headers(api)).json()["job"]["id"]
    run_jobs(api)
    waiting = api.get(f"/jobs/{job_id}").json()["job"]
    assert waiting["status"] == "queued" and waiting["stage"] == "Waiting to retry" and waiting["attempts"] == 1
    # The retry is delayed; bring it forward rather than sleeping.
    with api.app.state.database.connection() as conn:
        conn.execute("UPDATE jobs SET run_after = '2000-01-01T00:00:00+00:00'")
        conn.commit()
    run_jobs(api)
    failed = api.get(f"/jobs/{job_id}").json()["job"]
    assert calls["n"] == 2 and failed["status"] == "failed"
    assert failed["error"] == {"code": "internal_error", "message": "Something went wrong while processing this."}
    assert "secret" not in json.dumps(failed) and "SELECT" not in json.dumps(failed)

    def refuse(ctx):
        raise JobFailed("not_possible", "This piece cannot be arranged that way.")

    runner.handlers = {**runner.handlers, "arrange": refuse}
    second = api.post(f"/projects/{project_id}/arrangements", json={"profile": PROFILE},
                      headers=csrf_headers(api)).json()["job"]["id"]
    run_jobs(api)
    told = api.get(f"/jobs/{second}").json()["job"]
    assert told["status"] == "failed" and told["attempts"] == 1, "a deliberate failure is not retried"
    assert told["error"]["message"] == "This piece cannot be arranged that way."


def test_a_job_whose_worker_died_is_recovered_then_given_up_on(api):
    register(api)
    project_id = upload(api).json()["project"]["id"]
    job_id = api.post(f"/projects/{project_id}/arrangements", json={"profile": PROFILE},
                      headers=csrf_headers(api)).json()["job"]["id"]
    database = api.app.state.database
    from arranger_api.storage import Storage
    from arranger_api.storage.workspace import Workspace

    def claim_and_die():
        with database.connection() as conn:
            ws = Workspace(Storage(conn, dialect=database.dialect))
            assert ws.claim_job("dead-worker", 120)["id"] == job_id
            conn.execute("UPDATE jobs SET lease_expires_at = '2000-01-01T00:00:00+00:00'")
            conn.commit()

    claim_and_die()
    assert api.get(f"/jobs/{job_id}").json()["job"]["status"] == "running"
    assert api.app.state.job_runner.housekeeping()["requeued"] == 1
    claim_and_die()
    assert api.app.state.job_runner.housekeeping()["failed"] == 1
    lost = api.get(f"/jobs/{job_id}").json()["job"]
    assert lost["status"] == "failed" and lost["error"]["code"] == "worker_lost"


def test_two_workers_never_run_the_same_job(api):
    import threading

    register(api)
    project_id = upload(api).json()["project"]["id"]
    for _ in range(3):
        api.post(f"/projects/{project_id}/arrangements", json={"profile": PROFILE}, headers=csrf_headers(api))
    ran: list[str] = []
    lock = threading.Lock()
    runner = api.app.state.job_runner

    def handler(ctx):
        with lock:
            ran.append(ctx.job["id"])
        time.sleep(0.05)
        return {}

    runner.handlers = {"arrange": handler}
    threads = [threading.Thread(target=lambda i=i: [runner.run_once(f"w{i}") for _ in range(4)]) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(ran) == sorted(set(ran)) and len(ran) == 3


def test_background_workers_pick_up_jobs(tmp_path):
    settings = build_settings(sqlite_path=str(tmp_path / "w.db"), artifact_dir=str(tmp_path / "f"), job_workers=1)
    with make_client(settings=settings) as api:
        register(api)
        project_id = upload(api).json()["project"]["id"]
        job_id = api.post(f"/projects/{project_id}/arrangements", json={"profile": PROFILE},
                          headers=csrf_headers(api)).json()["job"]["id"]
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            job = api.get(f"/jobs/{job_id}").json()["job"]
            if job["status"] in ("succeeded", "failed"):
                break
            time.sleep(0.1)
        assert job["status"] == "succeeded", job


# --- quotas ------------------------------------------------------------------------------------------------


def test_quotas_bound_projects_storage_and_concurrent_jobs(tmp_path):
    settings = build_settings(sqlite_path=str(tmp_path / "q.db"), artifact_dir=str(tmp_path / "f"), job_workers=0,
                              quota_projects=2, quota_active_jobs=1, quota_storage_bytes=len(MIDI) * 2 + 10)
    with make_client(settings=settings) as api:
        register(api)
        first = upload(api).json()["project"]["id"]
        assert upload(api).status_code == 201
        assert upload(api).json()["detail"]["error"] == "project_quota"

        body, headers = {"profile": PROFILE}, csrf_headers(api)
        assert api.post(f"/projects/{first}/arrangements", json=body, headers=headers).status_code == 202
        busy = api.post(f"/projects/{first}/arrangements", json=body, headers=headers)
        assert busy.status_code == 429 and busy.json()["detail"]["error"] == "too_many_active_jobs"

        run_jobs(api)   # the arrangement's own files do not fit in what is left
        job = api.get("/jobs").json()["jobs"][0]
        assert job["status"] == "failed" and job["error"]["code"] == "storage_quota"
        usage = api.get("/account/usage").json()
        assert usage["projects"] == {"used": 2, "limit": 2}
        assert usage["storage_bytes"]["used"] <= usage["storage_bytes"]["limit"]


# --- account -------------------------------------------------------------------------------------------------


def test_account_export_contains_the_data_and_the_files_but_no_secrets(api):
    register(api)
    project_id = upload(api).json()["project"]["id"]
    arrange(api, project_id)
    response = api.get("/account/export")
    assert response.status_code == 200 and response.headers["content-type"] == "application/zip"
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        names = archive.namelist()
        account = json.loads(archive.read("account.json"))
        midi_names = [n for n in names if n.endswith(".mid")]
        assert any(read_midi_bytes(archive.read(n)).notes for n in midi_names)
    assert account["user"]["email"] == "user@example.com" and len(account["projects"]) == 1
    assert len(account["project_arrangements"]) == 1 and len([n for n in names if n.startswith("files/")]) == 3
    text = json.dumps(account)
    for secret in ("password_hash", "token_hash", "csrf_token_hash", "argon2", "storage_key"):
        assert secret not in text


def test_account_deletion_needs_the_password_and_removes_everything(api, tmp_path):
    register(api)
    project_id = upload(api).json()["project"]["id"]
    arrange(api, project_id)
    files = [p for p in (tmp_path / "files").rglob("*") if p.is_file()]
    assert len(files) == 3

    headers = csrf_headers(api)
    assert api.request("DELETE", "/account", json={"password": "wrong-password-0", "confirm": "DELETE"},
                       headers=headers).status_code == 403
    assert api.request("DELETE", "/account", json={"password": PASSWORD, "confirm": "yes"},
                       headers=headers).status_code == 400
    assert api.get("/projects").json()["total"] == 1

    done = api.request("DELETE", "/account", json={"password": PASSWORD, "confirm": "DELETE"}, headers=headers)
    assert done.status_code == 200 and done.json() == {"deleted": True, "files_removed": 3, "files_total": 3}
    assert [p for p in (tmp_path / "files").rglob("*") if p.is_file()] == []
    assert api.get("/auth/me").status_code == 401
    assert api.post("/auth/login", json={"email": "user@example.com", "password": PASSWORD}).status_code == 401
    with api.app.state.database.connection() as conn:
        for table in ("users", "projects", "project_sources", "project_arrangements", "artifacts", "jobs", "sessions"):
            assert conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"] == 0, table
        conn.rollback()


def test_deleting_a_project_removes_its_files(api, tmp_path):
    register(api)
    project_id = upload(api).json()["project"]["id"]
    arrange(api, project_id)
    deleted = api.delete(f"/projects/{project_id}", headers=csrf_headers(api))
    assert deleted.json() == {"deleted": True, "files_removed": 3}
    assert [p for p in (tmp_path / "files").rglob("*") if p.is_file()] == []
    assert api.get(f"/projects/{project_id}").status_code == 404


# --- catalog and launch details ---------------------------------------------------------------------------------


def test_catalog_reports_what_this_server_can_really_do(api):
    catalog = api.get("/catalog").json()
    assert {p["id"] for p in catalog["presets"]} >= {"beginner", "intermediate", "advanced", "small_hands"}
    assert catalog["capabilities"]["export_pdf"]["available"] is HAS_LILYPOND
    assert catalog["capabilities"]["model_repair"]["available"] is False
    assert [s["field"] for s in catalog["calibration_steps"]][0] == "right_comfortable_white_keys"
    assert {p["id"]: p["min_skill"] for p in catalog["patterns"]}["stride"] == 7


def test_guided_calibration_builds_a_profile_and_refuses_nonsense(api):
    good = api.post("/catalog/calibrate", json={"right_max_white_keys": 9, "left_max_white_keys": 8,
                                                "two_octave_leap_seconds": 0.5, "right_fingers": [1, 2, 3, 5]})
    profile = good.json()["profile"]
    assert (profile["right_max_span"], profile["left_max_span"], profile["max_span"]) == (14, 12, 12)
    assert profile["max_leap_rate"] == 38.0 and profile["right_fingers"] == [1, 2, 3, 5]
    assert api.post("/catalog/calibrate", json={"base_preset": "wizard"}).status_code == 400
    assert api.post("/catalog/calibrate", json={"right_max_white_keys": 40}).status_code == 422


def test_legal_config_never_invents_an_operator(api):
    legal = api.get("/legal/config").json()
    assert set(legal["operator"].values()) == {""}, "unset details are reported as unset"
    assert legal["analytics"] is False and legal["billing"] is False
    assert [c["name"] for c in legal["cookies"]] == ["arranger_session", "arranger_csrf"]
    assert all(c["essential"] for c in legal["cookies"])
    assert "not sent to any third party" in legal["audio_processing"]
    assert [p["name"] for p in legal["processors"]] == ["Database and file hosting"]


# --- audio ------------------------------------------------------------------------------------------------------------


def test_audio_upload_is_transcribed_by_the_real_model_into_an_editable_project(api):
    from arranger.adapters.audio import audio_support

    if not audio_support().available:
        pytest.skip(f"audio extra not installed: {audio_support().detail}")
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from audio_synth import wav_bytes

    played = [(60, 0.2, 0.4, 90), (64, 0.7, 0.4, 90), (67, 1.2, 0.4, 90), (72, 1.7, 0.6, 90)]
    wav = wav_bytes(played)
    register(api)
    queued = upload(api, wav, "c-major.wav", title="My recording")
    assert queued.status_code == 202, queued.text
    job_id = queued.json()["job"]["id"]
    assert api.get("/projects").json()["total"] == 0, "nothing exists until transcription finishes"
    run_jobs(api)
    job = api.get(f"/jobs/{job_id}").json()["job"]
    assert job["status"] == "succeeded", job
    detail = api.get(f"/projects/{job['result']['project_id']}").json()
    assert detail["project"]["title"] == "My recording" and detail["project"]["source_kind"] == "audio"
    inspection = detail["source"]["inspection"]
    assert inspection["transcription"]["model"].startswith("basic-pitch")
    assert "Check it before arranging" in inspection["transcription"]["note"]
    assert 0 < inspection["confidence"]["mean"] <= 1
    source = api.get(f"/projects/{detail['project']['id']}/sources/{detail['source']['id']}").json()
    heard = sorted({n[0] for n in source["playback"]["notes"]})
    assert set(heard) >= {60, 64, 67, 72}, heard

    # The correction step: remove a note the user says is wrong, then arrange.
    wrong = source["playback"]["notes"][0][5]
    fixed = api.post(f"/projects/{detail['project']['id']}/sources",
                     json={"based_on": detail["source"]["id"],
                           "selection": {"edits": [{"op": "delete", "note_id": wrong}], "tempo_bpm": 120}},
                     headers=csrf_headers(api))
    assert fixed.status_code == 201, fixed.text
    assert fixed.json()["source"]["inspection"]["note_count"] == len(source["playback"]["notes"]) - 1
    assert "transcription" in fixed.json()["source"]["inspection"]


def test_ready_fails_when_files_cannot_be_stored_and_recovers(api):
    assert api.get("/ready").json()["artifacts"] == "ok"

    class Broken:
        def healthy(self):
            raise OSError("disk gone")

    working = api.app.state.artifacts
    api.app.state.artifacts = Broken()
    api.app.state.artifact_health = (0.0, False)
    down = api.get("/ready")
    assert down.status_code == 503
    assert down.json()["detail"]["error"] == "artifact_store_unavailable"
    assert "disk gone" not in down.text
    # The process itself is still alive: liveness and readiness are different questions.
    assert api.get("/health").status_code == 200

    api.app.state.artifacts = working
    assert api.get("/ready").status_code == 200, "an unhealthy result is not cached"

