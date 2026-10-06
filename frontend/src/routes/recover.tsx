import { useRef, useState } from "react";
import { Link, Navigate, createFileRoute, useRouter } from "@tanstack/react-router";
import { useQueryClient } from "@tanstack/react-query";
import { useRecoverApiV2AuthRecoverPost } from "../api/gen/hooks/useRecoverApiV2AuthRecoverPost";
import { getMeApiV2AuthMeGetQueryKey } from "../api/gen/hooks/useGetMeApiV2AuthMeGet";
import {
  IOS_LINE,
  errorText,
  formatRetry,
  isIosBrowserTab,
  isOffline,
  isRateLimited,
  markSignedIn,
  newPasswordError,
  safeNext,
  useMe,
  useRetryCountdown,
  useSingleFlight,
  useSlow,
} from "../auth";
import { AuthPage } from "../components/AuthPage";
import { Button } from "../components/Button";
import { PasswordField } from "../components/PasswordField";
import { RecoveryCodeStep } from "../components/RecoveryCodeStep";
import { StatusNotice } from "../components/StatusNotice";
import { TextField } from "../components/TextField";

// Design feature: `/recover` (docs/design/screens/signin.md; Entry 29). A rider who forgot
// their password resets it with the one-time recovery code, then is shown the NEW code once.
// Design format: step 1: AuthPage → H1 "Reset your password" → intro → iOS line → error
// summary → Username (pre-filled from `?username=`) → Recovery code (mono, sent as typed;
// the server normalises) → New password (15–128) → primary lg "Reset password" → muted help.
// 401 → envelope message, code kept, password cleared; 422 → envelope message; 429 →
// warning with the Retry-After countdown, submit disabled. Step 2 replaces the content in
// place: RecoveryCodeStep, "Continue" → `next` or `/`. The new code is held only in this
// component's state; the mutation runs with gcTime 0 and is reset as soon as the code is
// copied out, so it is never in localStorage, IndexedDB, the URL or the Query cache.
// APIs called: POST /api/v2/auth/recover, then GET /api/v2/auth/me on Continue. Success
// writes `btj.me` and posts `{type:"signin"}` on the `auth` BroadcastChannel.
export const Route = createFileRoute("/recover")({
  validateSearch: (s: Record<string, unknown>): { next?: string; username?: string } => ({
    next: typeof s.next === "string" ? s.next : undefined,
    username: typeof s.username === "string" ? s.username : undefined,
  }),
  component: Recover,
});

type Issued = { code: string; displayName: string };

function Recover() {
  const search = Route.useSearch();
  const router = useRouter();
  const queryClient = useQueryClient();
  const me = useMe();
  const recover = useRecoverApiV2AuthRecoverPost({ mutation: { gcTime: 0 } });
  const flight = useSingleFlight();
  const countdown = useRetryCountdown();
  const slow = useSlow(recover.isPending);
  const summary = useRef<HTMLDivElement>(null);

  const [username, setUsername] = useState(search.username ?? "");
  const [recoveryCode, setRecoveryCode] = useState("");
  const [password, setPassword] = useState("");
  const [tried, setTried] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [issued, setIssued] = useState<Issued | null>(null);

  if (issued) {
    return (
      <AuthPage>
        <RecoveryCodeStep
          code={issued.code}
          displayName={issued.displayName}
          lead={<StatusNotice tone="success" title={`Password reset. You're signed in as ${issued.displayName}.`} />}
          title="Save your new recovery code"
          body="Your old code no longer works. You've been signed out on all other devices."
          continueLabel="Continue"
          onContinue={() => {
            void queryClient.invalidateQueries({ queryKey: getMeApiV2AuthMeGetQueryKey() });
            router.history.push(safeNext(search.next));
          }}
        />
      </AuthPage>
    );
  }

  if (me.data) return <Navigate to="/account" replace />;

  const errs = tried
    ? {
        username: username.trim() ? undefined : "Enter your username.",
        code: recoveryCode.trim() ? undefined : "Enter your recovery code.",
        password: newPasswordError(password),
      }
    : {};
  const limited = countdown.remaining > 0;

  function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    setTried(true);
    if (!username.trim() || !recoveryCode.trim() || newPasswordError(password) || limited) return;
    flight.run((done) => {
      setError(null);
      recover.mutate(
        { data: { username: username.trim().toLowerCase(), recoveryCode, newPassword: password } },
        {
          onSuccess: ({ account, recoveryCode: fresh }) => {
            setIssued({ code: fresh, displayName: account.displayName });
            recover.reset();
            markSignedIn(queryClient, account);
          },
          onError: (err) => {
            setError(err);
            countdown.startFrom(err);
            if (err.status === 401) setPassword("");
            setTimeout(() => summary.current?.focus());
          },
          onSettled: done,
        },
      );
    });
  }

  return (
    <AuthPage>
      <h1>Reset your password</h1>
      <p>Enter your username and the recovery code you saved when you created your account.</p>
      {isIosBrowserTab() ? <StatusNotice tone="info" title={IOS_LINE} /> : null}
      {error && isOffline(error) ? (
        <StatusNotice ref={summary} tone="danger" alert title="You need a connection to reset your password." />
      ) : isRateLimited(error) ? (
        limited ? <StatusNotice ref={summary} tone="warning" title={errorText(error, "")} detail={formatRetry(countdown.remaining)} /> : null
      ) : error ? (
        <StatusNotice ref={summary} tone="danger" alert title={errorText(error, "")} />
      ) : null}
      {slow ? <StatusNotice tone="info" title="Waking up the server…" detail="The first visit after a quiet spell can take a little while." /> : null}
      <form noValidate onSubmit={onSubmit}>
        <TextField
          label="Username"
          name="username"
          autoComplete="username"
          autoCapitalize="none"
          spellCheck={false}
          value={username}
          onChange={(e) => setUsername(e.target.value)}
          error={errs.username}
        />
        <TextField
          label="Recovery code"
          name="recoveryCode"
          autoComplete="one-time-code"
          autoCapitalize="none"
          spellCheck={false}
          style={{ fontFamily: "var(--font-family-mono)" }}
          helper="Spaces don't matter."
          value={recoveryCode}
          onChange={(e) => setRecoveryCode(e.target.value)}
          error={errs.code}
        />
        <PasswordField label="New password" name="newPassword" autoComplete="new-password" isNew value={password} onValueChange={setPassword} error={errs.password} />
        <Button type="submit" size="lg" loading={recover.isPending} loadingLabel="Resetting…" disabled={limited}>
          Reset password
        </Button>
      </form>
      <p className="auth-muted">Lost your recovery code too? Ask the person who runs this site to reset your account.</p>
      <div className="auth-links">
        <Link to="/signin" search={{ next: search.next }}>
          Back to sign in
        </Link>
      </div>
    </AuthPage>
  );
}
