import { useRef, useState } from "react";
import { Link, Navigate, createFileRoute, useRouter } from "@tanstack/react-router";
import { useQueryClient } from "@tanstack/react-query";
import { ApiError } from "../api/client";
import { useSignupApiV2AuthSignupPost } from "../api/gen/hooks/useSignupApiV2AuthSignupPost";
import { getMeApiV2AuthMeGetQueryKey } from "../api/gen/hooks/useGetMeApiV2AuthMeGet";
import {
  IOS_LINE,
  displayNameError,
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
  usernameError,
} from "../auth";
import { AuthPage } from "../components/AuthPage";
import { Button } from "../components/Button";
import { PasswordField } from "../components/PasswordField";
import { RecoveryCodeStep } from "../components/RecoveryCodeStep";
import { StatusNotice } from "../components/StatusNotice";
import { TextField } from "../components/TextField";

// Design feature: `/signup` in two steps (docs/design/screens/signup.md; Entry 29):
// the account details, then the recovery code shown exactly once.
// Design format: step 1: AuthPage → H1 "Create an account" → intro → iOS line → error
// summary → Display name / Username / Password (client checks mirror the contract:
// display name 1–40 trimmed, username lowercased against ^[a-z0-9][a-z0-9_.-]{2,31}$,
// password 15–128 code points after NFKC; validate on submit, then live) → primary lg
// "Create account" → link to /signin. 409 → Username field error with the envelope
// message plus the "it may have worked" hint; 422 → envelope message in the summary;
// 429 → warning with the Retry-After countdown, submit disabled. Step 2 replaces the
// content in place (no navigation): RecoveryCodeStep, "Continue" → `next` or `/`.
// The recovery code is held only in this component's state. The mutation runs with
// gcTime 0 and is reset as soon as the code is copied out, so it is never in
// localStorage, IndexedDB, the URL or the TanStack Query cache.
// APIs called: POST /api/v2/auth/signup, then GET /api/v2/auth/me on Continue. Success
// writes `btj.me` ({id, displayName}) and posts `{type:"signin"}` on `auth`.
export const Route = createFileRoute("/signup")({
  validateSearch: (s: Record<string, unknown>): { next?: string } => ({
    next: typeof s.next === "string" ? s.next : undefined,
  }),
  component: SignUp,
});

type Issued = { code: string; displayName: string };

function SignUp() {
  const search = Route.useSearch();
  const router = useRouter();
  const queryClient = useQueryClient();
  const me = useMe();
  const signup = useSignupApiV2AuthSignupPost({ mutation: { gcTime: 0 } });
  const flight = useSingleFlight();
  const countdown = useRetryCountdown();
  const slow = useSlow(signup.isPending);
  const summary = useRef<HTMLDivElement>(null);

  const [displayName, setDisplayName] = useState("");
  const [username, setUsername] = useState("");
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
          lead={<StatusNotice tone="success" title={`Account created. You're signed in as ${issued.displayName}.`} />}
          title="Save your recovery code"
          body="If you forget your password, this code is the only way back in. We can't show it again."
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
    ? { displayName: displayNameError(displayName), username: usernameError(username), password: newPasswordError(password) }
    : {};
  const conflict = error instanceof ApiError && error.status === 409;
  const limited = countdown.remaining > 0;

  function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    setTried(true);
    if (displayNameError(displayName) || usernameError(username) || newPasswordError(password) || limited) return;
    flight.run((done) => {
      setError(null);
      signup.mutate(
        { data: { displayName: displayName.trim(), username: username.toLowerCase(), password } },
        {
          onSuccess: ({ account, recoveryCode }) => {
            setIssued({ code: recoveryCode, displayName: account.displayName });
            signup.reset();
            markSignedIn(queryClient, account);
          },
          onError: (err) => {
            setError(err);
            countdown.startFrom(err);
            if (err.status !== 409) setTimeout(() => summary.current?.focus());
          },
          onSettled: done,
        },
      );
    });
  }

  return (
    <AuthPage>
      <h1>Create an account</h1>
      <p>An account lets you add stops and photos to a trip once a leader approves you. You don't need one to follow public trips.</p>
      {isIosBrowserTab() ? <StatusNotice tone="info" title={IOS_LINE} /> : null}
      {!error || conflict ? null : isOffline(error) ? (
        <StatusNotice ref={summary} tone="danger" alert title="You need a connection to create an account." />
      ) : isRateLimited(error) ? (
        limited ? <StatusNotice ref={summary} tone="warning" title={errorText(error, "")} detail={formatRetry(countdown.remaining)} /> : null
      ) : (
        <StatusNotice ref={summary} tone="danger" alert title={errorText(error, "")} />
      )}
      {slow ? <StatusNotice tone="info" title="Waking up the server…" detail="The first visit after a quiet spell can take a little while." /> : null}
      <form noValidate onSubmit={onSubmit}>
        <TextField
          label="Display name"
          name="displayName"
          autoComplete="nickname"
          maxLength={40}
          helper="Shown on photos you add and to trip leaders. You can't change it later."
          value={displayName}
          onChange={(e) => setDisplayName(e.target.value)}
          error={errs.displayName}
        />
        <div className="auth-section">
          <TextField
            label="Username"
            name="username"
            autoComplete="username"
            autoCapitalize="none"
            spellCheck={false}
            helper="Private. Only used to sign in. 3 to 32 characters: letters, numbers, dots, dashes or underscores, starting with a letter or number."
            value={username}
            onChange={(e) => {
              setUsername(e.target.value);
              if (conflict) setError(null);
            }}
            error={errs.username ?? (conflict ? errorText(error, "") : undefined)}
          />
          {conflict ? (
            <p className="auth-muted">
              If you just tried to create this account and the connection dropped, it may have worked. Sign in, then get a new recovery code from
              Account.{" "}
              <Link to="/signin" search={{ username: username.toLowerCase(), next: search.next }}>
                Sign in
              </Link>
            </p>
          ) : null}
        </div>
        <PasswordField label="Password" name="password" autoComplete="new-password" isNew value={password} onValueChange={setPassword} error={errs.password} />
        <Button type="submit" size="lg" loading={signup.isPending} loadingLabel="Creating account…" disabled={limited}>
          Create account
        </Button>
      </form>
      <div className="auth-links">
        <Link to="/signin" search={{ next: search.next }}>
          Already have an account? Sign in
        </Link>
      </div>
    </AuthPage>
  );
}
