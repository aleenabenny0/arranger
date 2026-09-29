// Every arrangement kept with what it was made from, and a comparison of two.
import { useState } from "react";
import { client } from "../../api/client";
import type { ArrangementRow, Comparison, ComparisonSide } from "../../api/types";
import { Field } from "../../components/Field";
import { formatDate } from "../../lib/format";
import { useToast } from "../../lib/toast";

interface RevisionsPanelProps {
  projectId: string;
  items: ArrangementRow[];
  onOpen: (id: string) => void;
}

export function comparisonLine(r: ComparisonSide): string {
  return `Arrangement ${r.revision}: ${r.accepted ? "passes" : `${r.hard} beyond limits`}, ${r.strain} stretches, ${Math.round(r.fidelity * 100)}% kept, difficulty ${r.difficulty}.`;
}

export function RevisionsPanel({ projectId, items, onOpen }: RevisionsPanelProps) {
  const { toast } = useToast();
  const [labels, setLabels] = useState<Record<string, string>>({});
  const [a, setA] = useState(items.length > 1 ? items[1].id : items[0]?.id || "");
  const [b, setB] = useState(items[0]?.id || "");
  const [diff, setDiff] = useState<Comparison | null>(null);

  if (!items.length) return <><h2>Revisions</h2><p>No arrangements yet.</p></>;

  async function saveLabel(item: ArrangementRow) {
    const label = labels[item.id] ?? item.label ?? "";
    try {
      await client.PATCH("/projects/{project_id}/arrangements/{arrangement_id}", { params: { path: { project_id: projectId, arrangement_id: item.id } }, body: { label } });
      toast("Label saved.", "success");
    } catch (error) {
      toast((error as Error).message, "error");
    }
  }

  async function compare() {
    try {
      const { data } = await client.GET("/projects/{project_id}/compare", { params: { path: { project_id: projectId }, query: { a, b } } });
      setDiff(data as unknown as Comparison);
    } catch (error) {
      toast((error as Error).message, "error");
    }
  }

  const option = (item: ArrangementRow) => <option key={item.id} value={item.id}>{`Arrangement ${item.revision}${item.label ? `: ${item.label}` : ""}`}</option>;

  return (
    <>
      <h2>Revisions</h2>
      <p>Every arrangement is kept with the source, hand profile and engine version it was made from.</p>
      <div className="table-wrap" tabIndex={0} role="region" aria-label="Table, scroll sideways if it is cut off" data-testid="revisions">
        <table>
          <thead><tr>{["No.", "Label", "Made", "Playable", "Kept", "Difficulty", "Open"].map((t) => <th key={t} scope="col">{t}</th>)}</tr></thead>
          <tbody>
            {items.map((item) => (
              <tr key={item.id}>
                <th scope="row">{String(item.revision)}</th>
                <td>
                  <input type="text" value={labels[item.id] ?? item.label ?? ""} maxLength={120} aria-label={`Label for arrangement ${item.revision}`}
                    onChange={(e) => setLabels({ ...labels, [item.id]: e.target.value })} onBlur={() => { if (item.id in labels && labels[item.id] !== (item.label || "")) void saveLabel(item); }} />
                </td>
                <td>{formatDate(item.created_at)}</td>
                <td>{item.accepted ? "Passes" : `${item.n_hard} beyond limits`}</td>
                <td>{`${Math.round(item.fidelity_score * 100)}%`}</td>
                <td>{String(item.difficulty)}</td>
                <td><button type="button" className="button button-small" aria-label={`Open arrangement ${item.revision}`} onClick={() => onOpen(item.id)}>Open</button></td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {items.length > 1 ? (
        <div className="form">
          <h3>Compare two</h3>
          <Field label="First"><select value={a} onChange={(e) => setA(e.target.value)}>{items.map(option)}</select></Field>
          <Field label="Second"><select value={b} onChange={(e) => setB(e.target.value)}>{items.map(option)}</select></Field>
          <button type="button" className="button" onClick={() => void compare()}>Compare</button>
          <div role="status">
            {diff ? (
              <>
                <ul><li>{comparisonLine(diff.a)}</li><li>{comparisonLine(diff.b)}</li></ul>
                <h4>What differs</h4>
                <ul>{[...diff.profile_changes.map((c) => `Hands: ${c}`), ...diff.plan_changes].map((t, i) => <li key={i}>{t}</li>)}</ul>
                {!diff.profile_changes.length && !diff.plan_changes.length ? <p>The plans and hand profiles are the same.</p> : null}
                {diff.same_source ? null : <p>They were made from different revisions of the source.</p>}
              </>
            ) : null}
          </div>
        </div>
      ) : null}
    </>
  );
}
