import { useEffect, useRef, useState } from "react";
import { EyeIcon, EyeOffIcon } from "../icons";
import { codePoints } from "../auth";
import { TextField, type TextFieldProps } from "./TextField";
import "./PasswordField.css";

// Design feature: a password input with show/hide (DESIGN.md §5.4).
// Design format: TextField plus a trailing 48 px toggle ("Show password" /
// "Hide password", eye / eye-off, aria-pressed) that never submits. `isNew` adds the
// 15–128 helper and the live length hint ("7 more characters needed" / "18
// characters", counted in code points). Reverts to hidden whenever its form submits.
// Paste is always allowed. No strength meter.
// APIs called: none.
export interface PasswordFieldProps extends Omit<TextFieldProps, "type" | "trailing" | "hint" | "value" | "onChange"> {
  value: string;
  onValueChange: (value: string) => void;
  isNew?: boolean;
}

export function PasswordField({ value, onValueChange, isNew = false, helper, ...rest }: PasswordFieldProps) {
  const [shown, setShown] = useState(false);
  const wrap = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const form = wrap.current?.closest("form");
    if (!form) return;
    const hide = () => setShown(false);
    form.addEventListener("submit", hide);
    return () => form.removeEventListener("submit", hide);
  }, []);

  const n = codePoints(value);
  const hint = isNew && n > 0 ? (n < 15 ? `${15 - n} more character${15 - n === 1 ? "" : "s"} needed` : `${n} characters`) : undefined;

  return (
    <div ref={wrap}>
      <TextField
        {...rest}
        type={shown ? "text" : "password"}
        value={value}
        onChange={(e) => onValueChange(e.target.value)}
        autoCapitalize="none"
        spellCheck={false}
        helper={helper ?? (isNew ? "15 to 128 characters. A short sentence you'll remember works well." : undefined)}
        hint={hint}
        className="password-field"
        trailing={
          <button
            type="button"
            className="password-field__toggle"
            aria-label={shown ? "Hide password" : "Show password"}
            aria-pressed={shown}
            onClick={() => setShown((s) => !s)}
          >
            {shown ? <EyeOffIcon /> : <EyeIcon />}
          </button>
        }
      />
    </div>
  );
}
