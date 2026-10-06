import type { ReactNode } from "react";
import { useNavigate, useRouter } from "@tanstack/react-router";
import { ArrowLeftIcon } from "../icons";
import "./AuthPage.css";

// Design feature: the page frame of /signin, /signup and /account (screens/signin.md,
// signup.md, account.md: "Frame: phone 390, form column max 480").
// Design format: TopBar (56 px, surface/card) with a 48 px back IconButton (previous
// page, or `/` when there is none) and an optional title, then a <main> column capped
// at size/form/max with page padding 16 and gap 16.
// APIs called: none.
export function AuthPage({ title, children }: { title?: string; children: ReactNode }) {
  const router = useRouter();
  const navigate = useNavigate();
  return (
    <>
      <header className="auth-topbar">
        <button
          type="button"
          className="auth-topbar__back"
          aria-label="Back"
          onClick={() => (window.history.length > 1 ? router.history.back() : navigate({ to: "/" }))}
        >
          <ArrowLeftIcon />
        </button>
        {title ? <span className="auth-topbar__title">{title}</span> : null}
      </header>
      <main className="auth-page">{children}</main>
    </>
  );
}
