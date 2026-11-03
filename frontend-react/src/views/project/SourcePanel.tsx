// Inspect and correct the source. Values are read on submit; nothing
// re-renders the panel while typing.
import { useState, type FormEvent } from "react";
import { ApiError, client, fetchText } from "../../api/client";
import type { Selection, Source } from "../../api/types";
import { Field } from "../../components/Field";
import { Notation } from "../../components/Notation";
import { formatClock, keyName, pitchName } from "../../lib/format";
import { useToast } from "../../lib/toast";

interface SourcePanelProps {
  projectId: string;
  source: Source | null;
  onSaved: () => Promise<void>;
  signal: AbortSignal;
}

export function Facts({ pairs }: { pairs: [string, unknown][] }) {
  return (
    <dl className="facts">
      {pairs.filter(([, v]) => v !== null && v !== undefined && v !== "").map(([term, value]) => (
        <div key={term}><dt>{term}</dt><dd>{String(value)}</dd></div>
      ))}
    </dl>
  );
}

export function SourcePanel({ projectId, source, onSaved, signal }: SourcePanelProps) {
  const { toast, announce } = useToast();
  const selection: Selection = (source && source.selection) || {};
  const [melody, setMelody] = useState<string>(selection.melody_track === null || selection.melody_track === undefined ? "" : String(selection.melody_track));
  const [ignored, setIgnored] = useState<number[]>(selection.ignored_tracks || []);
  const [transpose, setTranspose] = useState(String(selection.transpose || 0));
  const [tempo, setTempo] = useState(selection.tempo_bpm ? String(selection.tempo_bpm) : "");
  const [meterTop, setMeterTop] = useState(selection.meter ? String(selection.meter[0]) : "");
  const [meterBottom, setMeterBottom] = useState(selection.meter ? String(selection.meter[1]) : "");
  const [removals, setRemovals] = useState<string[]>([]);
  const [formError, setFormError] = useState("");
  const [saving, setSaving] = useState(false);
  const [xml, setXml] = useState<string | null>(null);
  const [showing, setShowing] = useState(false);

  if (!source) return <p>This piece has no source.</p>;
  const info = source.inspection;
  const isAudio = source.kind === "audio";
  const tracks = info.tracks || [];
  const lowIds = (info.confidence && info.confidence.low_confidence_note_ids) || [];

  async function save(event: FormEvent) {
    event.preventDefault();
    setFormError("");
    const next: Selection = {
      melody_track: melody === "" ? null : Number(melody),
      ignored_tracks: ignored,
      transpose: Number(transpose),
      edits: [...(selection.edits || []), ...removals.map((id) => ({ op: "delete", note_id: id }))],
    };
    if (tempo) next.tempo_bpm = Number(tempo);
    if (meterTop && meterBottom) next.meter = [Number(meterTop), Number(meterBottom)];
    setSaving(true);
    try {
      await client.POST("/projects/{project_id}/sources", { params: { path: { project_id: projectId } }, body: { based_on: source!.id, selection: next as Record<string, unknown> } });
      toast("Corrections saved as a new revision of the source.", "success");
      await onSaved();
    } catch (error) {
      const message = (error as Error).message;
      setFormError(message);
      announce(message, { urgent: true });
    } finally {
      setSaving(false);
    }
  }

  async function showNotation() {
    setShowing(true);
    try {
      setXml(await fetchText(`/projects/${projectId}/sources/${source!.id}/musicxml`, signal));
    } catch (error) {
      if (!(error instanceof ApiError && error.aborted)) toast((error as Error).message, "error");
    } finally {
      setShowing(false);
    }
  }

  const meter = info.meter ? `${info.meter[0]}/${info.meter[1]}${info.meter_changes ? `, ${info.meter_changes} change(s)` : ""}` : "unknown";
  const tempoText = `${Math.round(info.tempo_bpm || 0)} beats per minute${info.tempo_changes ? `, ${info.tempo_changes} change(s)` : ""}`;

  return (
    <>
      <h2>What was imported</h2>
      <Facts pairs={[
        ["File", `${source.filename || "upload"} (${source.kind})`], ["Length", `${info.bar_count} bars, ${formatClock(info.duration_seconds || 0)}`],
        ["Notes", info.note_count], ["Key", keyName(info.key)], ["Meter", meter], ["Tempo", tempoText],
        ["Difficulty as written", info.difficulty ? `${info.difficulty.level} of 10 (${info.difficulty.label})` : ""],
      ]} />
      {info.as_written ? (
        <>
          <h3>How the original sits under an average hand</h3>
          <ul>
            <li>{`Read literally, without pedal: ${info.as_written.without_pedal.headline}.`}</li>
            <li>{`With the pedal changed each bar: ${info.as_written.with_pedal_each_bar.headline}.`}</li>
          </ul>
        </>
      ) : null}
      {info.transcription ? (
        <div className="notice" role="note">
          <strong>This came from a recording. </strong>{info.transcription.note}
          <p>{`Model confidence ${Math.round((info.transcription.overall_confidence || 0) * 100)}%. ${info.confidence ? info.confidence.note : ""}`}</p>
        </div>
      ) : null}
      {(info.warnings || []).length ? (
        <>
          <h3>Things the import could not keep exactly</h3>
          <ul className="warnings">{(info.warnings || []).map((w) => <li key={w}>{w}</li>)}</ul>
        </>
      ) : null}

      <h2>Correct the import</h2>
      <form className="form" noValidate onSubmit={save}>
        {tracks.length > 1 ? (
          <fieldset>
            <legend>Which part is the tune?</legend>
            <p className="hint">Arranger guesses, but you can hear it. Choose the part with the melody, and leave out any part you do not want.</p>
            <div className="table-wrap" tabIndex={0} role="region" aria-label="Table, scroll sideways if it is cut off">
              <table>
                <thead><tr>{["Part", "Notes", "Range", "Looks like a tune", "Melody", "Leave out"].map((t) => <th key={t} scope="col">{t}</th>)}</tr></thead>
                <tbody>
                  {tracks.map((t) => {
                    const name = t.name || `part ${t.index + 1}`;
                    return (
                      <tr key={t.index}>
                        <th scope="row">{t.name || `Part ${t.index + 1}`}</th>
                        <td>{String(t.note_count)}</td>
                        <td>{t.lowest_pitch !== null && t.highest_pitch !== null ? `${pitchName(t.lowest_pitch)} to ${pitchName(t.highest_pitch)}` : ""}</td>
                        <td>{t.index === info.suggested_melody_track ? "Likely the tune" : t.melody_likelihood !== null ? `${Math.round(t.melody_likelihood * 100)}%` : ""}</td>
                        <td><input type="radio" name="melody-track" value={String(t.index)} aria-label={`Use ${name} as the melody`} checked={melody === String(t.index)} onChange={() => setMelody(String(t.index))} /></td>
                        <td><input type="checkbox" value={String(t.index)} aria-label={`Leave out ${name}`} checked={ignored.includes(t.index)}
                          onChange={(e) => setIgnored(e.target.checked ? [...ignored, t.index] : ignored.filter((i) => i !== t.index))} /></td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
            <div className="inline-choice">
              <input type="radio" name="melody-track" value="" id="melody-auto" checked={melody === ""} onChange={() => setMelody("")} />
              <label htmlFor="melody-auto">Let Arranger find the melody</label>
            </div>
          </fieldset>
        ) : null}
        <Field label="Transpose the whole piece" hint="For example into a key with fewer sharps or flats.">
          <select value={transpose} onChange={(e) => setTranspose(e.target.value)}>
            {Array.from({ length: 13 }, (_, i) => i - 6).map((n) => (
              <option key={n} value={String(n)}>{n === 0 ? "No change" : `${n > 0 ? "Up" : "Down"} ${Math.abs(n)} semitone${Math.abs(n) === 1 ? "" : "s"}`}</option>
            ))}
          </select>
        </Field>
        {isAudio || !info.meter ? (
          <fieldset>
            <legend>Tempo and meter you hear</legend>
            <p className="hint">A recording has no barlines. Set these so the bars land where you hear them.</p>
            <Field label="Beats per minute"><input type="number" min="20" max="400" step="1" value={tempo} onChange={(e) => setTempo(e.target.value)} /></Field>
            <Field label="Beats in a bar"><input type="number" min="1" max="32" value={meterTop} onChange={(e) => setMeterTop(e.target.value)} /></Field>
            <Field label="Beat unit">
              <select value={meterBottom} onChange={(e) => setMeterBottom(e.target.value)}>
                {["", "2", "4", "8", "16"].map((v) => <option key={v} value={v}>{v || "Keep"}</option>)}
              </select>
            </Field>
          </fieldset>
        ) : null}
        {lowIds.length ? (
          <fieldset>
            <legend>{`Notes the model was unsure about (${lowIds.length})`}</legend>
            <p className="hint">Listen in the Arrangement step's player, then tick any that are wrong.</p>
            <ul className="checklist">
              {lowIds.slice(0, 200).map((id) => (
                <li key={id}>
                  <input type="checkbox" id={`rm-${id}`} checked={removals.includes(id)}
                    onChange={(e) => setRemovals(e.target.checked ? [...removals, id] : removals.filter((r) => r !== id))} />
                  <label htmlFor={`rm-${id}`}>{` Remove note ${id}`}</label>
                </li>
              ))}
            </ul>
          </fieldset>
        ) : null}
        <p className="form-error" role="alert" hidden={!formError}>{formError}</p>
        <button type="submit" className="button button-primary" disabled={saving}>Save corrections</button>
      </form>

      <h2>Look at it</h2>
      <button type="button" className="button" disabled={showing} onClick={() => void showNotation()}>Show the source as notation</button>
      <div>{xml ? <Notation label={`Notation of the source, ${info.bar_count} bars`} musicxml={xml} /> : null}</div>
    </>
  );
}
