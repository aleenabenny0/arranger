// Describe your hands: a preset, numbers, fingers, or a guided measurement.
import { useEffect, useState } from "react";
import { client } from "../../api/client";
import type { components } from "../../api/schema";
import type { Catalog } from "../../api/types";

type CalibrationIn = components["schemas"]["CalibrationIn"];
import { Dialog } from "../../components/Dialog";
import { Field } from "../../components/Field";
import { useToast } from "../../lib/toast";
import { FINGERS, fingersOf, PROFILE_FIELDS, type Profile } from "./constants";

interface HandsPanelProps {
  catalog: Catalog;
  profile: Profile;
  preset: string;
  onChange: (profile: Profile, preset: string) => void;
}

function draftsOf(profile: Profile): Record<string, string> {
  return Object.fromEntries(PROFILE_FIELDS.map((f) => [f.key, profile[f.key] === undefined ? "" : String(profile[f.key])]));
}

interface CalibrateProps { catalog: Catalog; basePreset: string; onClose: () => void; onProfile: (profile: Profile) => void }

export function CalibrateDialog({ catalog, basePreset, onClose, onProfile }: CalibrateProps) {
  const steps = catalog.calibration_steps || [];
  const [index, setIndex] = useState(0);
  const [value, setValue] = useState("");
  const [error, setError] = useState("");
  const [answers, setAnswers] = useState<CalibrationIn>({ base_preset: basePreset || "intermediate", name: "My hands" });
  const [busy, setBusy] = useState(false);

  useEffect(() => { if (!steps.length) onClose(); }, [steps.length, onClose]);
  if (!steps.length) return null;
  const step = steps[index];

  async function finish(final: CalibrationIn) {
    setBusy(true);
    try {
      const { data } = await client.POST("/catalog/calibrate", { body: final });
      onProfile((data as { profile: Profile }).profile);
    } catch (failure) {
      setIndex(steps.length - 1);
      setError((failure as Error).message);
    } finally {
      setBusy(false);
    }
  }

  function advance(useAnswer: boolean) {
    const next: CalibrationIn = { ...answers };
    if (useAnswer) {
      const number = Number(value);
      if (!value || !Number.isFinite(number) || number <= 0) { setError("Enter a number, or skip this one."); return; }
      (next as Record<string, unknown>)[step.field] = number;
      setAnswers(next);
    }
    setError(""); setValue("");
    if (index + 1 < steps.length) { setIndex(index + 1); return; }
    void finish(next);
  }

  return (
    <Dialog title="Measure your hands" onClose={onClose} actions={
      <>
        <button type="button" className="button" onClick={onClose}>Cancel</button>
        <button type="button" className="button" disabled={busy} onClick={() => advance(false)}>Skip this one</button>
        <button type="button" className="button button-primary" disabled={busy} onClick={() => advance(true)}>{index === steps.length - 1 ? "Finish" : "Next"}</button>
      </>
    }>
      <div className="form">
        <p className="muted" aria-live="polite">{`Question ${index + 1} of ${steps.length}`}</p>
        <p>{step.ask}</p>
        <p className="hint">{step.hint || ""}</p>
        <Field label="Your answer" error={error}>
          <input type="number" step="any" inputMode="decimal" autoFocus value={value} onChange={(e) => setValue(e.target.value)} />
        </Field>
      </div>
    </Dialog>
  );
}

export function HandsPanel({ catalog, profile, preset, onChange }: HandsPanelProps) {
  const { toast, announce } = useToast();
  const [drafts, setDrafts] = useState(() => draftsOf(profile));
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [presetHint, setPresetHint] = useState("");
  const [measuring, setMeasuring] = useState(false);

  useEffect(() => { setDrafts(draftsOf(profile)); }, [profile]);

  function choosePreset(id: string) {
    const chosen = catalog.presets.find((p) => p.id === id);
    if (!chosen) { onChange(profile, id); return; }
    setPresetHint(chosen.description);
    setErrors({});
    onChange({ ...chosen.profile }, id);
  }

  function edit(key: string, raw: string, min: number, max: number) {
    setDrafts((current) => ({ ...current, [key]: raw }));
    const value = Number(raw);
    if (raw.trim() === "" || !Number.isFinite(value) || value < min || value > max) {
      setErrors((current) => ({ ...current, [key]: `Enter a number from ${min} to ${max}.` }));
      return;
    }
    setErrors((current) => ({ ...current, [key]: "" }));
    if (profile[key] !== value) onChange({ ...profile, [key]: value }, "");
  }

  function toggleFinger(key: string, finger: number, checked: boolean) {
    const current = fingersOf(profile, key);
    const chosen = FINGERS.filter((f) => (f === finger ? checked : current.includes(f)));
    if (!chosen.length) { announce("Keep at least one finger."); return; }
    onChange({ ...profile, [key]: chosen }, "");
  }

  return (
    <>
      <h2>Describe your hands</h2>
      <p>“Playable” depends on who is playing. These numbers are what Arranger checks every note against.</p>
      <Field label="Start from">
        <select data-testid="profile-preset" value={preset} onChange={(e) => choosePreset(e.target.value)}>
          <option value="">Custom</option>
          {catalog.presets.map((p) => <option key={p.id} value={p.id}>{p.id.replace(/_/g, " ")}</option>)}
        </select>
      </Field>
      <p className="hint" aria-live="polite">{presetHint}</p>
      <button type="button" className="button" onClick={() => setMeasuring(true)}>Measure my hands step by step</button>
      <div className="form grid-2">
        {PROFILE_FIELDS.map((f) => (
          <Field key={f.key} label={f.label} hint={f.hint || undefined} error={errors[f.key] || ""}>
            <input type="number" min={f.min} max={f.max} step={f.key === "max_leap_rate" ? 5 : 1} data-testid={`profile-${f.key}`}
              value={drafts[f.key] ?? ""} onChange={(e) => edit(f.key, e.target.value, f.min, f.max)} />
          </Field>
        ))}
      </div>
      {([["left_fingers", "Left hand"], ["right_fingers", "Right hand"]] as const).map(([key, label]) => (
        <fieldset key={key}>
          <legend>{`${label}: fingers you can use`}</legend>
          <div className="inline-choice">
            {FINGERS.map((finger) => {
              const id = `${key}-${finger}`;
              return (
                <span key={id}>
                  <input type="checkbox" id={id} checked={fingersOf(profile, key).includes(finger)} onChange={(e) => toggleFinger(key, finger, e.target.checked)} />
                  <label htmlFor={id}>{finger === 1 ? "1 (thumb)" : String(finger)}</label>
                </span>
              );
            })}
          </div>
        </fieldset>
      ))}
      {measuring ? (
        <CalibrateDialog catalog={catalog} basePreset={preset} onClose={() => setMeasuring(false)} onProfile={(next) => {
          setMeasuring(false);
          setErrors({});
          onChange(next, "");
          toast("Your hand profile was updated from your measurements.", "success");
        }} />
      ) : null}
    </>
  );
}
