// A labelled form control with room for a hint and an error, all associated
// so a screen reader hears them with the field.
import { cloneElement, useId, type ReactElement } from "react";

interface FieldProps {
  label: string;
  hint?: string;
  error?: string;
  children: ReactElement<Record<string, unknown>>;
}

export function Field({ label, hint, error, children }: FieldProps) {
  const generated = useId();
  const id = (children.props.id as string | undefined) || `f${generated}`;
  const described = [hint ? `${id}-hint` : "", `${id}-error`].filter(Boolean).join(" ");
  const control = cloneElement(children, { id, "aria-describedby": described, "aria-invalid": error ? "true" : undefined });
  return (
    <div className="field">
      <label htmlFor={id}>{label}</label>
      {hint ? <p className="hint" id={`${id}-hint`}>{hint}</p> : null}
      {control}
      <p className="field-error" id={`${id}-error`} role="alert" hidden={!error}>{error || ""}</p>
    </div>
  );
}
