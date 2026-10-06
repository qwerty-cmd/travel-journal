import { useEffect, useState } from "react";
import { DownloadIcon } from "../icons";
import { Button } from "./Button";
import "./RecoveryCodeBlock.css";

// Design feature: shows a just-issued recovery code (DESIGN.md §5.13). The code
// lives only in the caller's component state: nothing here writes it to storage.
// Design format: surface/sunken block, the code in mono groups of 4 (the spaces are
// CSS gaps between spans, so selecting and copying yields the exact code), then
// "Copy code" (secondary md, inline "Copied" for 3 s, or the clipboard-blocked
// fallback line) and "Download as text file" (tertiary; the file carries the display
// name, never the username).
// APIs called: none.
export const RECOVERY_FILE_NAME = "bike-trip-journal-recovery-code.txt";

export function RecoveryCodeBlock({ code, displayName }: { code: string; displayName: string }) {
  const [copy, setCopy] = useState<"idle" | "copied" | "failed">("idle");

  useEffect(() => {
    if (copy !== "copied") return;
    const id = setTimeout(() => setCopy("idle"), 3000);
    return () => clearTimeout(id);
  }, [copy]);

  const groups = code.match(/.{1,4}/g) ?? [code];

  async function onCopy() {
    try {
      await navigator.clipboard.writeText(code);
      setCopy("copied");
    } catch {
      setCopy("failed");
    }
  }

  function onDownload() {
    const text = `Bike Trip Journal recovery code for ${displayName}\n\n${code}\n\nUse this code with your username on the sign-in page if you forget your password. It works once.\n`;
    const url = URL.createObjectURL(new Blob([text], { type: "text/plain" }));
    const a = document.createElement("a");
    a.href = url;
    a.download = RECOVERY_FILE_NAME;
    a.click();
    URL.revokeObjectURL(url);
  }

  return (
    <div className="recovery-code">
      <p className="recovery-code__code">
        {groups.map((g, i) => (
          <span key={i}>{g}</span>
        ))}
      </p>
      <div className="recovery-code__actions">
        <Button variant="secondary" onClick={onCopy}>
          Copy code
        </Button>
        <span role="status" className="recovery-code__feedback">
          {copy === "copied" ? "Copied" : ""}
        </span>
        <Button variant="tertiary" leadingIcon={<DownloadIcon />} onClick={onDownload}>
          Download as text file
        </Button>
      </div>
      {copy === "failed" ? <p className="recovery-code__failed">Couldn't copy. Select the code and copy it, or download it.</p> : null}
    </div>
  );
}
