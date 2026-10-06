import { useId, type InputHTMLAttributes, type ReactNode } from "react";
import { AlertTriangleIcon } from "../icons";
import "./TextField.css";

// Design feature: a labelled text input (DESIGN.md §5.3).
// Design format: label (body 600) → optional helper (small, muted) → input → error
// line (small, danger, alert-triangle, "Error:" for screen readers). An error sets
// aria-invalid; aria-describedby points at the helper, hint and error. `trailing`
// is an optional slot inside the input row (PasswordField's toggle); `hint` sits
// below the input (PasswordField's live length).
// APIs called: none.
export interface TextFieldProps extends Omit<InputHTMLAttributes<HTMLInputElement>, "id"> {
  label: string;
  helper?: ReactNode;
  hint?: ReactNode;
  error?: string;
  trailing?: ReactNode;
}

export function TextField({ label, helper, hint, error, trailing, className, ...input }: TextFieldProps) {
  const id = useId();
  const describedBy = [helper && `${id}-helper`, hint && `${id}-hint`, error && `${id}-error`].filter(Boolean).join(" ");
  return (
    <div className={["field", className].filter(Boolean).join(" ")}>
      <label className="field__label" htmlFor={id}>
        {label}
      </label>
      {helper ? (
        <p className="field__helper" id={`${id}-helper`}>
          {helper}
        </p>
      ) : null}
      <div className="field__row">
        <input
          id={id}
          className={["field__input", error && "field__input--error"].filter(Boolean).join(" ")}
          aria-invalid={error ? true : undefined}
          aria-describedby={describedBy || undefined}
          {...input}
        />
        {trailing}
      </div>
      {hint ? (
        <p className="field__helper" id={`${id}-hint`} aria-live="polite">
          {hint}
        </p>
      ) : null}
      {error ? (
        <p className="field__error" id={`${id}-error`}>
          <AlertTriangleIcon size="sm" />
          <span className="visually-hidden">Error: </span>
          {error}
        </p>
      ) : null}
    </div>
  );
}
