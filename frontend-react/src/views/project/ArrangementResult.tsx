// One arrangement: the verdict, what changed, playback, downloads, the score,
// the findings, and the bars as a timeline.
import { useRef, useState } from "react";
import { ApiError, client, downloadUrl, watchJob } from "../../api/client";
import type { Arrangement, ArrangementDetail, Catalog, Finding } from "../../api/types";
import { Notation, type NotationHandle } from "../../components/Notation";
import { PlayerControls } from "../../components/PlayerControls";
import { Timeline } from "../../components/Timeline";
import { formatClock, formatDate } from "../../lib/format";
import type { Player } from "../../lib/player";
import { useToast } from "../../lib/toast";
import { FIDELITY_LABELS, RULE_NAMES } from "./constants";

export function FindingsBadge({ summary }: { summary: Arrangement["summary"]["findings"] }) {
  const icon = { passes: "✓", findings: "!", unresolved: "?" }[summary.status] || "i";
  return (
    <p className={`verdict verdict-${summary.status}`} data-testid="verdict" data-status={summary.status}>
      <span className="verdict-icon" aria-hidden="true">{icon}</span>
      <strong>{summary.headline}</strong>
    </p>
  );
}

export function handName(hand: Finding["hand"]): string {
  return hand === "L" ? "Left" : hand === "R" ? "Right" : "Both";
}

interface DownloadsProps { projectId: string; arrangementId: string; catalog: Catalog; signal: AbortSignal }

export function Downloads({ projectId, arrangementId, catalog, signal }: DownloadsProps) {
  const { toast } = useToast();
  const [busy, setBusy] = useState<Record<string, string>>({});
  const pdfAvailable = Boolean(catalog.capabilities.export_pdf && catalog.capabilities.export_pdf.available);

  async function download(kind: "midi" | "musicxml" | "pdf", label: string) {
    setBusy((b) => ({ ...b, [kind]: `Download ${label}` }));
    try {
      const { data } = await client.POST("/projects/{project_id}/arrangements/{arrangement_id}/exports/{kind}", {
        params: { path: { project_id: projectId, arrangement_id: arrangementId, kind } },
      });
      let result = data as { job?: { id: string }; artifact?: { id: string } };
      if (result.job) {
        setBusy((b) => ({ ...b, [kind]: "Engraving…" }));
        const job = await watchJob(result.job.id, { signal, onUpdate: (j) => setBusy((b) => ({ ...b, [kind]: `Engraving… ${Math.round(j.progress * 100)}%` })) });
        if (job.status !== "succeeded") throw new Error(job.error ? job.error.message : "Engraving was cancelled.");
        result = { artifact: { id: (job.result as { artifact_id: string }).artifact_id } };
      }
      window.location.assign(downloadUrl(result.artifact!.id));
    } catch (error) {
      if (!(error instanceof ApiError && error.aborted)) toast((error as Error).message, "error");
    } finally {
      setBusy((b) => { const next = { ...b }; delete next[kind]; return next; });
    }
  }

  return (
    <div className="toolbar" role="group" aria-label="Downloads">
      {([["midi", "MIDI"], ["musicxml", "MusicXML"], ["pdf", "Printable PDF"]] as const).map(([kind, label]) => (
        kind === "pdf" && !pdfAvailable
          ? <span key={kind} className="muted">PDF engraving is not installed on this server.</span>
          : <button key={kind} type="button" className="button" data-testid={`download-${kind}`} disabled={kind in busy} onClick={() => void download(kind, label)}>{busy[kind] || `Download ${label}`}</button>
      ))}
    </div>
  );
}

interface ResultProps {
  projectId: string;
  detail: ArrangementDetail;
  sourceRevision: number | string;
  musicxml: string | null;
  player: Player;
  playerVersion: number;
  currentBar: number;
  catalog: Catalog;
  signal: AbortSignal;
}

export function ArrangementResult({ projectId, detail, sourceRevision, musicxml, player, playerVersion, currentBar, catalog, signal }: ResultProps) {
  const notation = useRef<NotationHandle>(null);
  const arrangement = detail.arrangement;
  const summary = arrangement.summary;
  const verdict = arrangement.verdict;
  const findings = verdict.violations || [];
  const fidelity = summary.fidelity || {};

  return (
    <>
      <h2>{`Arrangement ${arrangement.revision}${arrangement.label ? `: ${arrangement.label}` : ""}`}</h2>
      <p className="muted">{`Made ${formatDate(arrangement.created_at)} from source revision ${sourceRevision} · engine ${arrangement.algorithm_version}${arrangement.model ? ` · ${arrangement.model}` : ""}`}</p>
      <div className="cards">
        <article className="card">
          <h3>Can you play it?</h3>
          <FindingsBadge summary={summary.findings} />
          <p>{summary.findings.detail}</p>
          <p className="muted">{`${summary.findings.hard} beyond your limits · ${summary.findings.strain} stretches`}</p>
        </article>
        <article className="card">
          <h3>Is it still the piece?</h3>
          <p><strong data-testid="fidelity-score">{`Fidelity score ${Math.round((fidelity.score || 0) * 100)}%`}</strong></p>
          <p className="muted">How much of the tune, rhythm, harmony and bass can still be heard. It is not a count of notes: what was left out is listed below.</p>
          <ul className="meters">
            {FIDELITY_LABELS.map(([key, label]) => (
              <li key={key}>
                <span>{label}</span>
                <meter min={0} max={1} low={0.6} high={0.85} optimum={1} value={fidelity[key] ?? 0} aria-label={label} />
                <span>{`${Math.round((fidelity[key] ?? 0) * 100)}%`}</span>
              </li>
            ))}
          </ul>
        </article>
        <article className="card">
          <h3>How hard is it?</h3>
          <p><strong>{`${summary.difficulty.level} of 10`}</strong>{` (${summary.difficulty.label})`}</p>
          <p className="muted">{summary.difficulty.note}</p>
          {summary.tempo_scale < 1 ? <p>{`Suggested tempo: ${Math.round(summary.tempo_bpm)} beats per minute (${Math.round(summary.tempo_scale * 100)}% of the original).`}</p> : null}
        </article>
      </div>
      <h3>What was changed</h3>
      <ul>{(arrangement.report.sentences || []).map((s, i) => <li key={i}>{s}</li>)}</ul>
      <h3>Listen</h3>
      <PlayerControls player={player} version={playerVersion} />
      <Timeline bars={detail.playback.bars || []} findings={findings} currentBar={currentBar} onSelect={(tile) => {
        player.seek(tile.start);
        const first = findings.find((f) => f.bar === tile.bar && (f.note_ids || []).length);
        if (first) notation.current?.reveal(first);
      }} />
      <h3>Download</h3>
      <Downloads projectId={projectId} arrangementId={arrangement.id} catalog={catalog} signal={signal} />
      <h3>Score</h3>
      <Notation ref={notation} label={`Notation of arrangement ${arrangement.revision}`} musicxml={musicxml} findings={findings} />
      <h3>{`Findings (${findings.length})`}</h3>
      {findings.length ? (
        <div className="table-wrap" tabIndex={0} role="region" aria-label="Table, scroll sideways if it is cut off" data-testid="findings">
          <table>
            <caption className="visually-hidden">Playability findings, most serious first</caption>
            <thead><tr>{["Kind", "Where", "Hand", "What", "Detail", "Score"].map((t) => <th key={t} scope="col">{t}</th>)}</tr></thead>
            <tbody>
              {findings.map((f, i) => (
                <tr key={i}>
                  <td><span className={`pill pill-${f.severity}`}>{f.severity === "hard" ? "Beyond your limits" : "A stretch"}</span></td>
                  <td>{f.bar ? `Bar ${f.bar}` : formatClock(f.time)}</td>
                  <td>{handName(f.hand)}</td>
                  <td>{RULE_NAMES[f.rule] || f.rule}</td>
                  <td>{f.message + (f.certainty === "unproven" ? " (not proven: the search was cut short)" : "")}</td>
                  <td>{(f.note_ids || []).length ? <button type="button" className="button button-small" aria-label={`Show bar ${f.bar} in the score`} onClick={() => notation.current?.reveal(f)}>Show</button> : null}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : <p data-testid="findings-none">Nothing in this arrangement is outside the limits you set.</p>}
      {verdict.violations_truncated ? <p className="muted">{`${verdict.violations_truncated} more findings are not listed.`}</p> : null}
    </>
  );
}
