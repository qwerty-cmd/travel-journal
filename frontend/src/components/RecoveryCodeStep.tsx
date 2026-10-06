import { useEffect, useId, useState, type ReactNode } from "react";
import { Button } from "./Button";
import { RecoveryCodeBlock } from "./RecoveryCodeBlock";
import "./RecoveryCodeStep.css";

// Design feature: the "save your recovery code" step shared by /signup step 2 and the
// /account rotation dialog (screens/signup.md step 2). Saving the code is deliberate:
// the continue action stays disabled until "I've saved my recovery code" is ticked.
// Design format: optional `lead` (e.g. the success notice) → H1/H2 `title` → `body` →
// RecoveryCodeBlock → muted tips → 48 px checkbox → helper while unticked → primary
// `continueLabel`. While unticked, leaving the page raises the browser's beforeunload prompt.
// The code is a prop held in the caller's component state only.
// APIs called: none.
export interface RecoveryCodeStepProps {
  code: string;
  displayName: string;
  title: string;
  body: string;
  continueLabel: string;
  onContinue: () => void;
  onSavedChange?: (saved: boolean) => void;
  lead?: ReactNode;
  headingLevel?: 1 | 2;
  titleId?: string;
}

export function RecoveryCodeStep({ code, displayName, title, body, continueLabel, onContinue, onSavedChange, lead, headingLevel = 1, titleId }: RecoveryCodeStepProps) {
  const [saved, setSaved] = useState(false);
  const checkId = useId();
  const Heading = headingLevel === 1 ? "h1" : "h2";

  useEffect(() => {
    if (saved) return;
    const warn = (e: BeforeUnloadEvent) => {
      e.preventDefault();
      e.returnValue = "";
    };
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [saved]);

  return (
    <div className="recovery-step">
      {lead}
      <Heading id={titleId}>{title}</Heading>
      <p>{body}</p>
      <RecoveryCodeBlock code={code} displayName={displayName} />
      <ul className="recovery-step__tips">
        <li>Save it in your password manager.</li>
        <li>Or write it down and keep it with your licence.</li>
      </ul>
      <label className="recovery-step__check" htmlFor={checkId}>
        <input
          id={checkId}
          type="checkbox"
          checked={saved}
          onChange={(e) => {
            setSaved(e.target.checked);
            onSavedChange?.(e.target.checked);
          }}
        />
        I've saved my recovery code
      </label>
      <div className="recovery-step__submit">
        {!saved ? <p className="recovery-step__reason">Tick the box once you've saved the code.</p> : null}
        <Button size="lg" disabled={!saved} onClick={onContinue}>
          {continueLabel}
        </Button>
      </div>
    </div>
  );
}
