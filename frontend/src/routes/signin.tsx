import { useEffect, useRef, useState } from "react";
import { Link, createFileRoute, useRouter } from "@tanstack/react-router";
import { useQueryClient } from "@tanstack/react-query";
import { useSigninApiV2AuthSigninPost } from "../api/gen/hooks/useSigninApiV2AuthSigninPost";
import {
  IOS_LINE,
  errorText,
  formatRetry,
  isIosBrowserTab,
  isOffline,
  isRateLimited,
  markSignedIn,
  safeNext,
  useMe,
  useRetryCountdown,
  useSingleFlight,
  useSlow,
} from "../auth";
import { AuthPage } from "../components/AuthPage";
import { Button } from "../components/Button";
import { PasswordField } from "../components/PasswordField";
import { StatusNotice } from "../components/StatusNotice";
import { TextField } from "../components/TextField";

// Design feature: `/signin` (docs/design/screens/signin.md; decision-log Entry 29).
// Username + password is the only way to get write access. Needs a connection.
// Design format: AuthPage → H1 "Sign in" → `?next=` context notice → iOS line →
// error summary (focus moves there) → Username → Password → primary lg "Sign in"
// ("Signing in…") → link to /signup carrying `next`. 401 shows the envelope message
// ("Username or password is incorrect.") and clears the password; 429 (rate limit or
// lockout, never described as "locked") shows the envelope message plus the
// Retry-After countdown and disables submit until it ends. Already signed in →
// redirect to `next` (same-origin paths only) or `/`.
// APIs called: POST /api/v2/auth/signin, GET /api/v2/auth/me (useMe). Success writes
// `btj.me` and posts `{type:"signin"}` on the `auth` BroadcastChannel.
export const Route = createFileRoute("/signin")({
  validateSearch: (s: Record<string, unknown>): { next?: string; username?: string } => ({
    next: typeof s.next === "string" ? s.next : undefined,
    username: typeof s.username === "string" ? s.username : undefined,
  }),
  component: SignIn,
});

function SignIn() {
  const search = Route.useSearch();
  const next = safeNext(search.next);
  const router = useRouter();
  const queryClient = useQueryClient();
  const me = useMe();
  const signin = useSigninApiV2AuthSigninPost();
  const flight = useSingleFlight();
  const countdown = useRetryCountdown();
  const slow = useSlow(signin.isPending);
  const summary = useRef<HTMLDivElement>(null);

  const [username, setUsername] = useState(search.username ?? "");
  const [password, setPassword] = useState("");
  const [tried, setTried] = useState(false);
  const [error, setError] = useState<unknown>(null);

  // Already signed in: go on to `next` (a plain path, so history rather than a typed route).
  const alreadyIn = Boolean(me.data) && !signin.isSuccess;
  useEffect(() => {
    if (alreadyIn) router.history.replace(next);
  }, [alreadyIn, next, router]);
  if (alreadyIn) return null;

  const usernameErr = tried && !username.trim() ? "Enter your username." : undefined;
  const passwordErr = tried && !password ? "Enter your password." : undefined;
  const limited = countdown.remaining > 0;

  function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    setTried(true);
    if (!username.trim() || !password || limited) return;
    flight.run((done) => {
      setError(null);
      signin.mutate(
        { data: { username, password } },
        {
          onSuccess: (account) => {
            markSignedIn(queryClient, account);
            router.history.push(next);
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
      <h1>Sign in</h1>
      {search.next ? <StatusNotice tone="info" title="Sign in to add stops and send anything waiting on this phone." /> : null}
      {isIosBrowserTab() ? <StatusNotice tone="info" title={IOS_LINE} /> : null}
      {error && isOffline(error) ? (
        <StatusNotice ref={summary} tone="danger" alert title="You need a connection to sign in. Anything you've added is still saved on this phone." />
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
          error={usernameErr}
        />
        <PasswordField label="Password" name="password" autoComplete="current-password" value={password} onValueChange={setPassword} error={passwordErr} />
        <Button type="submit" size="lg" loading={signin.isPending} loadingLabel="Signing in…" disabled={limited}>
          Sign in
        </Button>
      </form>
      <div className="auth-links">
        <Link to="/signup" search={{ next: search.next }}>
          New here? Create an account
        </Link>
      </div>
    </AuthPage>
  );
}
