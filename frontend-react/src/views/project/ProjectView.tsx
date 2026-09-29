// One piece: inspect and correct the source, describe your hands, arrange,
// review the result, compare revisions, download.
//
// Server results are tied to the revision they were made from: an arrangement
// made from an older source or a different hand profile says so instead of
// passing as current.
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ApiError, client, fetchText, watchJob, type Job } from "../../api/client";
import type { ArrangementDetail, Catalog, Playback, ProjectDetail } from "../../api/types";
import { Tabs } from "../../components/Tabs";
import { setTitle, useFocusHeading } from "../../lib/focus";
import { newIdempotencyKey } from "../../lib/format";
import { Player } from "../../lib/player";
import { go } from "../../lib/route";
import { useSession } from "../../lib/session";
import { useToast } from "../../lib/toast";
import { AdvancedPanel } from "./AdvancedPanel";
import { ArrangementResult } from "./ArrangementResult";
import type { Profile } from "./constants";
import { HandsPanel } from "./HandsPanel";
import { RevisionsPanel } from "./RevisionsPanel";
import { SourcePanel } from "./SourcePanel";

const EMPTY_CATALOG: Catalog = { presets: [], capabilities: {}, calibration_steps: [], limits: { max_upload_bytes: 40 * 1024 * 1024 } };

interface JobBox {
  label: string;
  progress: number;
  stage: string;
  id: string | null;
  cancelling: boolean;
  failed?: { id: string; message: string };
  error?: string;
}

export function staleReasons(data: ProjectDetail | null, arrangementSourceId: string | null, profile: Profile, profileAtLastArrange: string | null): string[] {
  if (!data || !arrangementSourceId) return [];
  const reasons: string[] = [];
  if (data.project.current_source_id !== arrangementSourceId) reasons.push("the source has been corrected since");
  if (profileAtLastArrange && JSON.stringify(profile) !== profileAtLastArrange) reasons.push("your hand profile has changed since");
  return reasons;
}

export function ProjectView({ projectId }: { projectId: string }) {
  const { catalog: sessionCatalog, setUser } = useSession();
  const { toast } = useToast();
  const catalog = sessionCatalog || EMPTY_CATALOG;
  const [data, setData] = useState<ProjectDetail | null>(null);
  const [error, setError] = useState<ApiError | null>(null);
  const [profile, setProfile] = useState<Profile>({});
  const [preset, setPreset] = useState("");
  const [profileAtLastArrange, setProfileAtLastArrange] = useState<string | null>(null);
  const [detail, setDetail] = useState<ArrangementDetail | null>(null);
  const [xml, setXml] = useState<string | null>(null);
  const [tab, setTab] = useState("source");
  const [job, setJob] = useState<JobBox | null>(null);
  const [useModel, setUseModel] = useState(false);
  const [playerVersion, setPlayerVersion] = useState(0);
  const [currentBar, setCurrentBar] = useState(1);
  const player = useMemo(() => new Player(), []);
  const controller = useRef(new AbortController());
  const profileRef = useRef(profile);
  profileRef.current = profile;
  const activeJob = useRef<string | null>(null);
  useFocusHeading(data !== null || error !== null, `project-${projectId}`);

  useEffect(() => {
    const current = new AbortController();
    controller.current = current;
    const unsubscribe = player.subscribe((state) => setCurrentBar(state.bar));
    return () => { current.abort(); unsubscribe(); player.dispose(); };
  }, [player]);

  const fetchDetail = useCallback(async () => {
    const { data: body } = await client.GET("/projects/{project_id}", { params: { path: { project_id: projectId } }, signal: controller.current.signal });
    const next = body as unknown as ProjectDetail;
    setData(next);
    setTitle(next.project.title);
    return next;
  }, [projectId]);

  const openArrangement = useCallback(async (id: string) => {
    const { data: body } = await client.GET("/projects/{project_id}/arrangements/{arrangement_id}", {
      params: { path: { project_id: projectId, arrangement_id: id } }, signal: controller.current.signal,
    });
    const next = body as unknown as ArrangementDetail;
    player.clear();
    player.load("result", next.playback);
    try {
      const { data: src } = await client.GET("/projects/{project_id}/sources/{source_id}", {
        params: { path: { project_id: projectId, source_id: next.arrangement.source_id } }, signal: controller.current.signal,
      });
      const playback = (src as { playback?: Playback }).playback;
      if (playback) player.load("source", playback);
    } catch { /* the source revision may have been removed; the result still plays */ }
    player.use("result");
    setDetail(next);
    setXml(null);
    setPlayerVersion((v) => v + 1);
    try {
      setXml(await fetchText(`/projects/${projectId}/arrangements/${id}/musicxml`, controller.current.signal));
    } catch (failure) {
      if (!(failure instanceof ApiError && failure.aborted)) toast((failure as Error).message, "error");
    }
  }, [projectId, player, toast]);

  const runJob = useCallback(async (body: Record<string, unknown> & { jobId?: string }, label: string) => {
    if (activeJob.current) return;
    setJob({ label, progress: 0, stage: "Starting…", id: null, cancelling: false });
    try {
      let jobId = body.jobId;
      if (!jobId) {
        const { data: started } = await client.POST("/jobs/arrange", {
          body: { project_id: projectId, profile: profileRef.current as never, use_model: Boolean(body.use_model), plan: (body.plan as never) ?? null, label: (body.label as string) || "" },
          params: { header: { "Idempotency-Key": newIdempotencyKey() } },
          signal: controller.current.signal,
        });
        jobId = (started as { job: { id: string } }).job.id;
      }
      activeJob.current = jobId;
      setJob((current) => current && { ...current, id: jobId! });
      const finished: Job = await watchJob(jobId, { signal: controller.current.signal, onUpdate: (j) => {
        setJob((current) => current && { ...current, progress: j.progress, stage: `${j.stage} (${Math.round(j.progress * 100)}%)` });
      } });
      if (finished.status === "succeeded") {
        setProfileAtLastArrange(JSON.stringify(profileRef.current));
        await fetchDetail();
        await openArrangement((finished.result as { arrangement_id: string }).arrangement_id);
        toast("Arrangement ready.", "success");
        setJob(null);
      } else if (finished.status === "cancelled") {
        toast("Cancelled. Nothing was saved.", "info");
        setJob(null);
      } else {
        setJob({ label, progress: 0, stage: "", id: finished.id, cancelling: false, failed: { id: finished.id, message: finished.error ? finished.error.message : "The arrangement failed." } });
      }
    } catch (failure) {
      if (!(failure instanceof ApiError && failure.aborted)) setJob({ label, progress: 0, stage: "", id: null, cancelling: false, error: (failure as Error).message });
    } finally {
      activeJob.current = null;
    }
  }, [projectId, fetchDetail, openArrangement, toast]);

  useEffect(() => {
    let cancelled = false;
    fetchDetail().then(async (loaded) => {
      if (cancelled) return;
      const intermediate = catalog.presets.find((p) => p.id === "intermediate");
      const initial = loaded.project.profile || (intermediate ? intermediate.profile : {});
      setProfile({ ...initial });
      setProfileAtLastArrange(loaded.project.profile ? JSON.stringify(loaded.project.profile) : null);
      setTab(loaded.project.current_arrangement_id ? "arrange" : "source");
      if (loaded.project.current_arrangement_id) await openArrangement(loaded.project.current_arrangement_id);
      const running = (loaded.jobs || []).find((j) => j.kind === "arrange");
      if (running && !cancelled) { setTab("arrange"); void runJob({ jobId: running.id }, "Arranging"); }
    }).catch((failure: ApiError) => {
      if (cancelled || failure.aborted) return;
      if (failure.status === 401) { setUser(null); go("/login"); return; }
      setError(failure);
    });
    return () => { cancelled = true; };
    // The catalog is static for the page; loading once per project is intended.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [projectId, fetchDetail, openArrangement, runJob, setUser]);

  async function cancelJob() {
    if (!job || !job.id) return;
    setJob({ ...job, cancelling: true, stage: "Cancelling…" });
    try { await client.POST("/jobs/{job_id}/cancel", { params: { path: { job_id: job.id } } }); }
    catch (failure) { toast((failure as Error).message, "error"); }
  }

  async function retryJob(failedId: string, label: string) {
    try {
      const { data: retried } = await client.POST("/jobs/{job_id}/retry", { params: { path: { job_id: failedId } } });
      void runJob({ jobId: (retried as { job: { id: string } }).job.id }, label);
    } catch (failure) {
      toast((failure as Error).message, "error");
    }
  }

  if (error) {
    return (
      <>
        <h1>Something went wrong</h1>
        <p role="alert">{error.message}</p>
        <p><a href="#/library">Back to your pieces</a></p>
      </>
    );
  }
  if (!data) return <><h1>Piece</h1><p role="status">Loading…</p></>;

  const arrangement = detail ? detail.arrangement : null;
  const reasons = staleReasons(data, arrangement ? arrangement.source_id : null, profile, profileAtLastArrange);
  const modelAvailable = Boolean(catalog.capabilities.model_repair && catalog.capabilities.model_repair.available);
  const sourceRevision = arrangement ? (data.sources.find((s) => s.id === arrangement.source_id) || {}).revision || "?" : "?";

  return (
    <>
      <p><a href="#/library">← Your pieces</a></p>
      <h1 data-testid="project-title">{data.project.title}</h1>
      <p className="muted">{[data.project.composer, data.source ? `source revision ${data.source.revision}` : ""].filter(Boolean).join(" · ")}</p>
      <Tabs label="Steps" active={tab} onChange={(id) => { setTab(id); player.pause(); }} items={[
        { id: "source", label: "1. Source", content: (
          <SourcePanel key={data.source ? data.source.id : "none"} projectId={projectId} source={data.source} signal={controller.current.signal}
            onSaved={async () => { await fetchDetail(); }} />
        ) },
        { id: "hands", label: "2. Your hands", content: (
          <HandsPanel catalog={catalog} profile={profile} preset={preset} onChange={(next, nextPreset) => { setProfile(next); setPreset(nextPreset); }} />
        ) },
        { id: "arrange", label: "3. Arrangement", content: (
          <>
            <h2>Make an arrangement</h2>
            <p>Arranger keeps the tune, fits a left hand to your level, and checks every note against your hands.</p>
            {modelAvailable ? (
              <div className="inline-choice">
                <input type="checkbox" id="use-model" checked={useModel} onChange={(e) => setUseModel(e.target.checked)} />
                <label htmlFor="use-model">Also let an AI model try to improve it. A text summary of the piece is sent to Anthropic; your file is not.</label>
              </div>
            ) : null}
            <button type="button" className="button button-primary" data-testid="arrange-button" disabled={Boolean(job && !job.failed && !job.error)}
              onClick={() => void runJob({ use_model: useModel }, "Arranging")}>Arrange for my hands</button>
            <div className="progress-box" data-testid="job-status" hidden={!job}>
              {job && job.failed ? (
                <>
                  <p className="form-error" role="alert">{job.failed.message}</p>
                  <button type="button" className="button" onClick={() => void retryJob(job.failed!.id, job.label)}>Try again</button>
                </>
              ) : job && job.error ? (
                <p className="form-error" role="alert">{job.error}</p>
              ) : job ? (
                <>
                  <h3>{job.label}</h3>
                  <progress max="1" value={job.progress} aria-label={job.label} />
                  <p role="status">{job.stage}</p>
                  <button type="button" className="button" disabled={job.cancelling || !job.id} onClick={() => void cancelJob()}>Cancel</button>
                </>
              ) : null}
            </div>
            <div className="notice" role="status" data-testid="stale-notice" hidden={!reasons.length}>
              {reasons.length ? <><strong>This arrangement is out of date: </strong>{`${reasons.join(" and ")}. Arrange again to see the effect.`}</> : null}
            </div>
            <div data-testid="arrangement-result">
              {detail ? (
                <ArrangementResult key={detail.arrangement.id} projectId={projectId} detail={detail} sourceRevision={sourceRevision} musicxml={xml}
                  player={player} playerVersion={playerVersion} currentBar={currentBar} catalog={catalog} signal={controller.current.signal} />
              ) : null}
            </div>
          </>
        ) },
        { id: "revisions", label: "Revisions", content: (
          <RevisionsPanel key={(data.arrangements || []).map((a) => a.id).join(",")} projectId={projectId} items={data.arrangements || []}
            onOpen={(id) => { setTab("arrange"); void openArrangement(id); }} />
        ) },
        { id: "advanced", label: "Advanced", content: (
          <AdvancedPanel key={arrangement ? arrangement.id : "none"} arrangement={arrangement} playback={detail ? detail.playback : null} profile={profile}
            onEvaluate={(plan) => { setTab("arrange"); void runJob({ plan, label: "edited plan" }, "Evaluating your plan"); }} />
        ) },
      ]} />
    </>
  );
}
