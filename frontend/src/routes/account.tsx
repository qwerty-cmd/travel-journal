import { useId, useState } from "react";
import { Navigate, createFileRoute, useRouter } from "@tanstack/react-router";
import { useQueryClient } from "@tanstack/react-query";
import { ApiError } from "../api/client";
import { useChangePasswordApiV2AuthPasswordPost } from "../api/gen/hooks/useChangePasswordApiV2AuthPasswordPost";
import { useRotateRecoveryCodeApiV2AuthRecoveryCodePost } from "../api/gen/hooks/useRotateRecoveryCodeApiV2AuthRecoveryCodePost";
import { useSignoutAllApiV2AuthSignoutAllPost } from "../api/gen/hooks/useSignoutAllApiV2AuthSignoutAllPost";
import { useSignoutApiV2AuthSignoutPost } from "../api/gen/hooks/useSignoutApiV2AuthSignoutPost";
import { errorText, formatRetry, isOffline, isRateLimited, markSignedIn, markSignedOut, newPasswordError, useMe, useRetryCountdown, useSingleFlight } from "../auth";
import { getCachedMe } from "../localStore";
import { AuthPage } from "../components/AuthPage";
import { Button } from "../components/Button";
import { Dialog, DialogActions } from "../components/Dialog";
import { PasswordField } from "../components/PasswordField";
import { RecoveryCodeStep } from "../components/RecoveryCodeStep";
import { StatusNotice } from "../components/StatusNotice";

// Design feature: `/account` (docs/design/screens/account.md; Entry 29): the signed-in
// user's identity, password change, a new recovery code, sign out and sign out everywhere.
// Design format: AuthPage (title "Account") → H1 → identity card (display name public,
// username private; no edit control) → Password card → Recovery code (Dialog: password,
// then RecoveryCodeStep with "Done"; Escape and the scrim are blocked until the box is
// ticked; "Close without saving" asks first) → Sign out (secondary) and Sign out
// everywhere (danger-secondary, ConfirmDialog). Loading: card skeletons. 401 on `me` →
// /signin?next=/account. Offline: display name from `btj.me`, username "Needs a
// connection", actions answer "You need a connection to manage your account.".
// Every form drops a second submit while one is in flight (useSingleFlight) and disables
// its button while pending, so a double tap sends one request.
// Not here yet: "Your trips" and "My requests" (t-am-fe-discover-trip-detail,
// t-am-fe-join-flow) and the "This phone" queue section.
// APIs called: GET /api/v2/auth/me, POST /api/v2/auth/password, POST
// /api/v2/auth/recovery-code, POST /api/v2/auth/signout, POST /api/v2/auth/signout-all.
// Signing out clears `btj.me` and posts `{type:"signout"}` on `auth`; the offline queue
// is left alone.
export const Route = createFileRoute("/account")({
  component: Account,
});

const OFFLINE = "You need a connection to manage your account.";

function Account() {
  const me = useMe();

  if (me.error?.status === 401) return <Navigate to="/signin" search={{ next: "/account" }} replace />;

  const cached = me.data ?? getCachedMe();
  const offline = !me.data && isOffline(me.error);

  return (
    <AuthPage title="Account">
      <h1>Account</h1>
      {offline ? <StatusNotice tone="offline" title="You're offline." detail={OFFLINE} /> : null}
      {me.isPending ? (
        <div className="auth-card" aria-busy="true">
          <span className="visually-hidden">Loading…</span>
          <div className="auth-skeleton" />
          <div className="auth-skeleton" />
        </div>
      ) : cached ? (
        <section className="auth-card" aria-label="Your account">
          <p className="auth-muted">Display name</p>
          <p>
            <strong>{cached.displayName}</strong>
          </p>
          <p className="auth-muted">Shown on photos you add.</p>
          <p className="auth-muted">Username</p>
          <p>
            <strong>{me.data ? me.data.username : "Needs a connection"}</strong>
          </p>
          <p className="auth-muted">Private. Only you see this.</p>
        </section>
      ) : me.error && !offline ? (
        <StatusNotice tone="danger" title={errorText(me.error, OFFLINE)} />
      ) : null}
      <ChangePassword />
      <NewRecoveryCode displayName={cached?.displayName ?? ""} />
      <SignOut />
    </AuthPage>
  );
}

/** Envelope message for a form, or the offline line; null for a 429 (the countdown notice shows it). */
function FormError({ error, remaining }: { error: unknown; remaining: number }) {
  if (!error) return null;
  if (isRateLimited(error)) return remaining > 0 ? <StatusNotice tone="warning" title={errorText(error, OFFLINE)} detail={formatRetry(remaining)} /> : null;
  return <StatusNotice tone="danger" alert title={errorText(error, OFFLINE)} />;
}

const status = (e: unknown) => (e instanceof ApiError ? e.status : undefined);

function ChangePassword() {
  const queryClient = useQueryClient();
  const change = useChangePasswordApiV2AuthPasswordPost();
  const flight = useSingleFlight();
  const countdown = useRetryCountdown();
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [tried, setTried] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [done, setDone] = useState(false);

  const code = status(error);
  const currentErr = tried && !current ? "Enter your current password." : code === 403 ? errorText(error, "") : undefined;
  const nextErr = (tried ? newPasswordError(next) : undefined) ?? (code === 422 ? errorText(error, "") : undefined);
  const limited = countdown.remaining > 0;

  function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    setTried(true);
    if (!current || newPasswordError(next) || limited) return;
    flight.run((settle) => {
      setError(null);
      setDone(false);
      change.mutate(
        { data: { currentPassword: current, newPassword: next } },
        {
          onSuccess: (account) => {
            // Every session was revoked and this device got a fresh one: still signed in.
            markSignedIn(queryClient, account, false);
            setCurrent("");
            setNext("");
            setTried(false);
            setDone(true);
          },
          onError: (err) => {
            setError(err);
            countdown.startFrom(err);
          },
          onSettled: settle,
        },
      );
    });
  }

  return (
    <section className="auth-section" aria-labelledby="password-heading">
      <h2 id="password-heading">Password</h2>
      <form className="auth-card" noValidate onSubmit={onSubmit}>
        <p className="auth-muted">Changing your password signs you out on all other devices.</p>
        {done ? <StatusNotice tone="success" title="Password changed. Other devices have been signed out." /> : null}
        {code === 403 || code === 422 ? null : <FormError error={error} remaining={countdown.remaining} />}
        <PasswordField label="Current password" name="currentPassword" autoComplete="current-password" value={current} onValueChange={setCurrent} error={currentErr} />
        <PasswordField label="New password" name="newPassword" autoComplete="new-password" isNew value={next} onValueChange={setNext} error={nextErr} />
        <Button type="submit" loading={change.isPending} loadingLabel="Changing password…" disabled={limited}>
          Change password
        </Button>
      </form>
    </section>
  );
}

type Issued = { code: string; displayName: string };

function NewRecoveryCode({ displayName }: { displayName: string }) {
  const queryClient = useQueryClient();
  const rotate = useRotateRecoveryCodeApiV2AuthRecoveryCodePost({ mutation: { gcTime: 0 } });
  const flight = useSingleFlight();
  const countdown = useRetryCountdown();
  const titleId = useId();
  const confirmId = useId();
  const [open, setOpen] = useState(false);
  const [confirming, setConfirming] = useState(false);
  const [password, setPassword] = useState("");
  const [tried, setTried] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [issued, setIssued] = useState<Issued | null>(null);
  const [saved, setSaved] = useState(false);

  function close() {
    setOpen(false);
    setConfirming(false);
    setIssued(null);
    setSaved(false);
    setPassword("");
    setTried(false);
    setError(null);
  }

  const limited = countdown.remaining > 0;
  const passwordErr = tried && !password ? "Enter your password." : status(error) === 403 ? errorText(error, "") : undefined;

  function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    setTried(true);
    if (!password || limited) return;
    flight.run((settle) => {
      setError(null);
      rotate.mutate(
        { data: { password } },
        {
          onSuccess: ({ account, recoveryCode }) => {
            setIssued({ code: recoveryCode, displayName: account.displayName });
            rotate.reset();
            setPassword("");
            markSignedIn(queryClient, account, false);
          },
          onError: (err) => {
            setError(err);
            countdown.startFrom(err);
          },
          onSettled: settle,
        },
      );
    });
  }

  return (
    <section className="auth-section" aria-labelledby="recovery-heading">
      <h2 id="recovery-heading">Recovery code</h2>
      <p>Your recovery code was shown when you created your account. It can't be shown again, but you can replace it with a new one. The old one stops working.</p>
      <div className="auth-row">
        <Button variant="secondary" onClick={() => setOpen(true)}>
          Get a new recovery code
        </Button>
      </div>
      <Dialog open={open && !confirming} onClose={close} titleId={titleId} dismissible={!issued || saved}>
        {issued ? (
          <>
            <RecoveryCodeStep
              code={issued.code}
              displayName={issued.displayName || displayName}
              title="Save your new recovery code"
              body="Your old code no longer works."
              continueLabel="Done"
              onContinue={close}
              onSavedChange={setSaved}
              headingLevel={2}
              titleId={titleId}
            />
            {!saved ? (
              <Button variant="tertiary" onClick={() => setConfirming(true)}>
                Close without saving
              </Button>
            ) : null}
          </>
        ) : (
          <form className="auth-section" noValidate onSubmit={onSubmit}>
            <h2 id={titleId}>Get a new recovery code</h2>
            <FormError error={status(error) === 403 ? null : error} remaining={countdown.remaining} />
            <PasswordField label="Your password" name="password" autoComplete="current-password" value={password} onValueChange={setPassword} error={passwordErr} />
            <DialogActions>
              <Button variant="secondary" onClick={close}>
                Cancel
              </Button>
              <Button type="submit" loading={rotate.isPending} loadingLabel="Getting new code…" disabled={limited}>
                Get new code
              </Button>
            </DialogActions>
          </form>
        )}
      </Dialog>
      <Dialog open={confirming} onClose={() => setConfirming(false)} titleId={confirmId}>
        <h2 id={confirmId}>Close without saving?</h2>
        <p>Your new code won't be shown again.</p>
        <DialogActions>
          <Button variant="secondary" onClick={() => setConfirming(false)} autoFocus>
            Keep it open
          </Button>
          <Button variant="danger" onClick={close}>
            Close without saving
          </Button>
        </DialogActions>
      </Dialog>
    </section>
  );
}

function SignOut() {
  const router = useRouter();
  const queryClient = useQueryClient();
  const signout = useSignoutApiV2AuthSignoutPost();
  const signoutAll = useSignoutAllApiV2AuthSignoutAllPost();
  const flight = useSingleFlight();
  const countdown = useRetryCountdown();
  const confirmId = useId();
  const [confirming, setConfirming] = useState(false);
  const [error, setError] = useState<unknown>(null);

  function finish() {
    markSignedOut(queryClient);
    router.history.push("/");
  }

  function run(all: boolean) {
    setConfirming(false);
    flight.run((settle) => {
      setError(null);
      const handlers = {
        onSuccess: finish,
        onError: (err: ApiError) => {
          // signout-all 401: the session is already gone, which is what was asked for.
          if (err.status === 401) return finish();
          setError(err);
          countdown.startFrom(err);
        },
        onSettled: settle,
      };
      if (all) signoutAll.mutate(undefined, handlers);
      else signout.mutate(undefined, handlers);
    });
  }

  const busy = signout.isPending || signoutAll.isPending;

  return (
    <section className="auth-section" aria-labelledby="signout-heading">
      <h2 id="signout-heading">Sign out</h2>
      <FormError error={error} remaining={countdown.remaining} />
      <div className="auth-row">
        <Button variant="secondary" loading={signout.isPending} loadingLabel="Signing out…" disabled={busy} onClick={() => run(false)}>
          Sign out
        </Button>
        <Button variant="danger-secondary" disabled={busy || countdown.remaining > 0} onClick={() => setConfirming(true)}>
          Sign out everywhere
        </Button>
      </div>
      <Dialog open={confirming} onClose={() => setConfirming(false)} titleId={confirmId}>
        <h2 id={confirmId}>Sign out on every device?</h2>
        <p>Including this one. Anything waiting to send stays on each phone until someone signs in there.</p>
        <DialogActions>
          <Button variant="secondary" onClick={() => setConfirming(false)}>
            Cancel
          </Button>
          <Button onClick={() => run(true)} autoFocus>
            Sign out everywhere
          </Button>
        </DialogActions>
      </Dialog>
    </section>
  );
}
