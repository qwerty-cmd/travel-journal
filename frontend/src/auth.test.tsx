// The root route mounts the real QueueNotice, which reads the queue's IndexedDB.
import "fake-indexeddb/auto";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { RouterProvider, createMemoryHistory, createRouter } from "@tanstack/react-router";
import { routeTree } from "./routeTree.gen";
import { formatRetry, safeNext } from "./auth";

// t-am-fe-auth-screens: /signup, /signin and /account through the real route tree
// and generated client, with only globalThis.fetch and BroadcastChannel mocked.

const ME = { id: "u1", username: "wes", displayName: "Wes", createdAt: "2026-10-01T00:00:00Z" };
const CODE = "7K3M9QX2ABCDEFGHJKMNPQRSTV";

type Handler = (init?: RequestInit) => Response | Promise<Response>;
const json = (status: number, body: unknown, headers: Record<string, string> = {}) =>
  new Response(status === 204 ? null : JSON.stringify(body), { status, headers: { "Content-Type": "application/json", ...headers } });
const envelope = (status: number, code: string, message: string, headers?: Record<string, string>) =>
  json(status, { error: { code, message } }, headers);
const unauth = () => envelope(401, "UNAUTHENTICATED", "Sign in to continue.");

let routes: Record<string, Handler>;
const calls: string[] = [];
const posted: unknown[] = [];

class FakeChannel {
  constructor(readonly name: string) {}
  postMessage(m: unknown) {
    posted.push({ channel: this.name, ...(m as object) });
  }
  close() {}
  addEventListener() {}
  removeEventListener() {}
}

function renderAt(path: string) {
  const router = createRouter({ routeTree, history: createMemoryHistory({ initialEntries: [path] }) });
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  );
  return router;
}

const type = (label: string | RegExp, value: string) => fireEvent.change(screen.getByLabelText(label), { target: { value } });
const settle = () => act(() => new Promise((r) => setTimeout(r, 20)));

/** Every value in every IndexedDB database, serialised. */
async function dumpIndexedDb(): Promise<string> {
  const out: string[] = [];
  for (const info of await indexedDB.databases()) {
    const db = await new Promise<IDBDatabase>((res, rej) => {
      const req = indexedDB.open(info.name!);
      req.onsuccess = () => res(req.result);
      req.onerror = () => rej(req.error);
    });
    for (const store of Array.from(db.objectStoreNames)) {
      const all = await new Promise<unknown[]>((res, rej) => {
        const req = db.transaction(store).objectStore(store).getAll();
        req.onsuccess = () => res(req.result);
        req.onerror = () => rej(req.error);
      });
      out.push(store, JSON.stringify(all));
    }
    db.close();
  }
  return out.join("\n");
}

beforeEach(() => {
  localStorage.clear();
  calls.length = 0;
  posted.length = 0;
  routes = { "GET /api/v2/auth/me": unauth };
  vi.stubGlobal("BroadcastChannel", FakeChannel);
  vi.stubGlobal("scrollTo", () => {});
  vi.stubGlobal("fetch", async (input: RequestInfo | URL, init?: RequestInit) => {
    const key = `${init?.method ?? "GET"} ${String(input)}`;
    calls.push(key);
    const h = routes[key];
    if (!h) throw new Error(`unexpected ${key}`);
    return h(init);
  });
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

describe("/signup", () => {
  test("client rules block the request: short password, bad username", async () => {
    renderAt("/signup");
    await screen.findByRole("heading", { name: "Create an account" });
    type("Display name", "Wes");
    type("Username", "-x");
    type("Password", "too short");
    fireEvent.click(screen.getByRole("button", { name: "Create account" }));
    expect(await screen.findByText("Use at least 15 characters.")).toBeTruthy();
    expect(screen.getByText(/Use 3 to 32 letters/)).toBeTruthy();
    expect(calls.filter((c) => c.startsWith("POST"))).toEqual([]);
  });

  test("201: code shown once, gated Continue, never stored; btj.me is {id, displayName}; auth broadcast", async () => {
    let body: unknown;
    routes["POST /api/v2/auth/signup"] = (init) => {
      body = JSON.parse(String(init?.body));
      return json(201, { account: ME, recoveryCode: CODE });
    };
    renderAt("/signup");
    await screen.findByRole("heading", { name: "Create an account" });
    type("Display name", "  Wes ");
    type("Username", "Wes");
    type("Password", "a sentence long enough");
    fireEvent.click(screen.getByRole("button", { name: "Create account" }));

    await screen.findByRole("heading", { name: "Save your recovery code" });
    expect(body).toEqual({ displayName: "Wes", username: "wes", password: "a sentence long enough" });
    expect(document.querySelector(".recovery-code__code")!.textContent).toBe(CODE);
    const cont = screen.getByRole("button", { name: "Continue" }) as HTMLButtonElement;
    expect(cont.disabled).toBe(true);
    expect(screen.getByText("Tick the box once you've saved the code.")).toBeTruthy();
    fireEvent.click(screen.getByLabelText("I've saved my recovery code"));
    expect(cont.disabled).toBe(false);

    await settle();
    expect(JSON.parse(localStorage.getItem("btj.me")!)).toEqual({ id: "u1", displayName: "Wes" });
    const local = Object.keys(localStorage).map((k) => `${k}=${localStorage.getItem(k)}`).join("\n");
    expect(local).not.toContain(CODE);
    expect(sessionStorage.length).toBe(0);
    expect((await indexedDB.databases()).length).toBeGreaterThan(0); // the queue DB is open, so the scan is real
    expect(await dumpIndexedDb()).not.toContain(CODE);
    expect(posted).toEqual([{ channel: "auth", type: "signin" }]);
  });

  test("409: envelope message on the Username field plus the may-have-worked hint", async () => {
    routes["POST /api/v2/auth/signup"] = () => envelope(409, "CONFLICT", "That username is taken.");
    renderAt("/signup");
    await screen.findByRole("heading", { name: "Create an account" });
    type("Display name", "Wes");
    type("Username", "wes");
    type("Password", "a sentence long enough");
    fireEvent.click(screen.getByRole("button", { name: "Create account" }));
    expect(await screen.findByText("That username is taken.")).toBeTruthy();
    expect(screen.getByLabelText("Username").getAttribute("aria-invalid")).toBe("true");
    expect(screen.getByText(/it may have worked/)).toBeTruthy();
  });

  test("422: envelope message verbatim in the summary", async () => {
    routes["POST /api/v2/auth/signup"] = () => envelope(422, "VALIDATION_ERROR", "Display name has a control character.");
    renderAt("/signup");
    await screen.findByRole("heading", { name: "Create an account" });
    type("Display name", "Wes");
    type("Username", "wes");
    type("Password", "a sentence long enough");
    fireEvent.click(screen.getByRole("button", { name: "Create account" }));
    expect((await screen.findByRole("alert")).textContent).toContain("Display name has a control character.");
  });
});

describe("/signin", () => {
  test("401: envelope message, password cleared, username kept", async () => {
    routes["POST /api/v2/auth/signin"] = () => envelope(401, "UNAUTHENTICATED", "Username or password is incorrect.");
    renderAt("/signin");
    await screen.findByRole("heading", { name: "Sign in" });
    type("Username", "wes");
    type("Password", "wrong password here");
    fireEvent.click(screen.getByRole("button", { name: "Sign in" }));
    expect((await screen.findByRole("alert")).textContent).toContain("Username or password is incorrect.");
    expect((screen.getByLabelText("Password") as HTMLInputElement).value).toBe("");
    expect((screen.getByLabelText("Username") as HTMLInputElement).value).toBe("wes");
  });

  test("429: message plus the Retry-After countdown; submit disabled", async () => {
    routes["POST /api/v2/auth/signin"] = () => envelope(429, "RATE_LIMITED", "Too many attempts.", { "Retry-After": "120" });
    renderAt("/signin");
    await screen.findByRole("heading", { name: "Sign in" });
    type("Username", "wes");
    type("Password", "a sentence long enough");
    fireEvent.click(screen.getByRole("button", { name: "Sign in" }));
    expect(await screen.findByText("Too many attempts.")).toBeTruthy();
    expect(screen.getByText("Try again in 2 minutes.")).toBeTruthy();
    expect((screen.getByRole("button", { name: "Sign in" }) as HTMLButtonElement).disabled).toBe(true);
  });

  test("200: btj.me written without the username, auth broadcast, navigates to next", async () => {
    routes["POST /api/v2/auth/signin"] = () => json(200, ME);
    const router = renderAt("/signin?next=%2Faccount");
    await screen.findByRole("heading", { name: "Sign in" });
    routes["GET /api/v2/auth/me"] = () => json(200, ME);
    type("Username", "wes");
    type("Password", "a sentence long enough");
    fireEvent.click(screen.getByRole("button", { name: "Sign in" }));
    await waitFor(() => expect(router.state.location.pathname).toBe("/account"));
    expect(JSON.parse(localStorage.getItem("btj.me")!)).toEqual({ id: "u1", displayName: "Wes" });
    expect(posted).toEqual([{ channel: "auth", type: "signin" }]);
  });

  test("?next= off-origin falls back to /", async () => {
    routes["GET /api/v2/auth/me"] = () => json(200, ME);
    const router = renderAt("/signin?next=https%3A%2F%2Fexample.com");
    await waitFor(() => expect(router.state.location.pathname).toBe("/"));
  });
});

describe("/account", () => {
  beforeEach(() => {
    routes["GET /api/v2/auth/me"] = () => json(200, ME);
  });

  test("401 on me redirects to /signin?next=/account", async () => {
    routes["GET /api/v2/auth/me"] = unauth;
    const router = renderAt("/account");
    await waitFor(() => expect(router.state.location.pathname).toBe("/signin"));
    expect(router.state.location.search).toEqual({ next: "/account" });
  });

  test("change password: a double tap sends one request", async () => {
    let release!: () => void;
    routes["POST /api/v2/auth/password"] = () => new Promise<Response>((r) => (release = () => r(json(200, ME))));
    renderAt("/account");
    await screen.findByText("wes");
    type("Current password", "the old sentence here");
    type("New password", "the new sentence here");
    const btn = screen.getByRole("button", { name: "Change password" });
    fireEvent.click(btn);
    fireEvent.click(btn);
    await settle();
    expect(calls.filter((c) => c === "POST /api/v2/auth/password")).toHaveLength(1);
    expect((document.querySelector('button[aria-busy="true"]') as HTMLButtonElement).disabled).toBe(true);
    release();
    expect(await screen.findByText("Password changed. Other devices have been signed out.")).toBeTruthy();
  });

  test("change password 403: field error on Current password, still signed in", async () => {
    routes["POST /api/v2/auth/password"] = () => envelope(403, "FORBIDDEN", "Your current password is incorrect.");
    renderAt("/account");
    await screen.findByText("wes");
    type("Current password", "the old sentence here");
    type("New password", "the new sentence here");
    fireEvent.click(screen.getByRole("button", { name: "Change password" }));
    expect(await screen.findByText("Your current password is incorrect.")).toBeTruthy();
    expect(screen.getByLabelText("Current password").getAttribute("aria-invalid")).toBe("true");
    expect(localStorage.getItem("btj.me")).not.toBeNull();
  });

  test("sign out clears btj.me, broadcasts, goes to /", async () => {
    routes["POST /api/v2/auth/signout"] = () => json(204, null);
    const router = renderAt("/account");
    await screen.findByText("wes");
    expect(localStorage.getItem("btj.me")).not.toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Sign out" }));
    await waitFor(() => expect(router.state.location.pathname).toBe("/"));
    expect(localStorage.getItem("btj.me")).toBeNull();
    expect(posted).toEqual([{ channel: "auth", type: "signout" }]);
  });
});

test("formatRetry follows the DESIGN.md §5.6 rounding", () => {
  expect(formatRetry(45)).toBe("Try again in 45 seconds.");
  expect(formatRetry(61)).toBe("Try again in 2 minutes.");
  expect(formatRetry(7200)).toBe("Try again in 2 hours.");
  expect(formatRetry(7201)).toBe("Try again in 3 hours.");
});

// --- gap coverage (test-writer) ----------------------------------------------

const STACK = /\bat \w+ \(|\.tsx?:\d+|Traceback|ApiError|TypeError/;
const password = () => screen.getByRole("button", { name: "Change password" });

describe("btj.me cache", () => {
  const FULL = { ...ME, extra: "x", email: "a@b.c" };

  test("holds exactly {id, displayName} even when /me returns more", async () => {
    routes["GET /api/v2/auth/me"] = () => json(200, FULL);
    renderAt("/account");
    await screen.findByText("wes");
    await settle();
    expect(JSON.parse(localStorage.getItem("btj.me")!)).toEqual({ id: "u1", displayName: "Wes" });
  });

  test("cleared when /me returns 401", async () => {
    localStorage.setItem("btj.me", JSON.stringify({ id: "u1", displayName: "Wes" }));
    renderAt("/account"); // default route: /me 401
    await waitFor(() => expect(localStorage.getItem("btj.me")).toBeNull());
  });

  test("cleared on sign out everywhere, which broadcasts signout", async () => {
    routes["GET /api/v2/auth/me"] = () => json(200, ME);
    routes["POST /api/v2/auth/signout-all"] = () => json(204, null);
    const router = renderAt("/account");
    await screen.findByText("wes");
    expect(localStorage.getItem("btj.me")).not.toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Sign out everywhere" }));
    const confirm = (await screen.findAllByRole("button", { name: "Sign out everywhere" })).pop()!;
    fireEvent.click(confirm);
    await waitFor(() => expect(router.state.location.pathname).toBe("/"));
    expect(localStorage.getItem("btj.me")).toBeNull();
    expect(posted).toEqual([{ channel: "auth", type: "signout" }]);
  });
});

describe("/account recovery code rotation", () => {
  test("new code is in no storage and gone from the DOM after 'I saved it'", async () => {
    routes["GET /api/v2/auth/me"] = () => json(200, ME);
    routes["POST /api/v2/auth/recovery-code"] = () => json(200, { account: ME, recoveryCode: CODE });
    renderAt("/account");
    await screen.findByText("wes");
    fireEvent.click(screen.getByRole("button", { name: "Get a new recovery code" }));
    type("Your password", "the old sentence here");
    fireEvent.click(screen.getByRole("button", { name: "Get new code" }));
    await screen.findByRole("heading", { name: "Save your new recovery code" });
    expect(document.body.textContent).toContain(CODE);
    await settle();
    const local = Object.keys(localStorage).map((k) => `${k}=${localStorage.getItem(k)}`).join("\n");
    expect(local).not.toContain(CODE);
    expect(sessionStorage.length).toBe(0);
    expect(await dumpIndexedDb()).not.toContain(CODE);
    expect(posted).toEqual([]); // a rotation is not a sign-in

    fireEvent.click(screen.getByLabelText("I've saved my recovery code"));
    fireEvent.click(screen.getByRole("button", { name: "Done" }));
    await settle();
    expect(document.body.textContent).not.toContain(CODE);
    expect(document.querySelector(".recovery-code__code")).toBeNull();
  });
});

describe("429 countdown", () => {
  test("signin: submit stays disabled until Retry-After elapses, then re-enables", async () => {
    routes["POST /api/v2/auth/signin"] = () => envelope(429, "RATE_LIMITED", "Too many attempts.", { "Retry-After": "1" });
    renderAt("/signin");
    await screen.findByRole("heading", { name: "Sign in" });
    type("Username", "wes");
    type("Password", "a sentence long enough");
    fireEvent.click(screen.getByRole("button", { name: "Sign in" }));
    await screen.findByText("Try again in 1 second.");
    const btn = screen.getByRole("button", { name: "Sign in" }) as HTMLButtonElement;
    expect(btn.disabled).toBe(true);
    fireEvent.click(btn);
    expect(calls.filter((c) => c === "POST /api/v2/auth/signin")).toHaveLength(1);
    await waitFor(() => expect((screen.getByRole("button", { name: "Sign in" }) as HTMLButtonElement).disabled).toBe(false), { timeout: 3000 });
    expect(screen.queryByText("Too many attempts.")).toBeNull();
  });

  test("signup: Retry-After drives the countdown and disables submit", async () => {
    routes["POST /api/v2/auth/signup"] = () => envelope(429, "RATE_LIMITED", "Too many sign-ups.", { "Retry-After": "90" });
    renderAt("/signup");
    await screen.findByRole("heading", { name: "Create an account" });
    type("Display name", "Wes");
    type("Username", "wes");
    type("Password", "a sentence long enough");
    fireEvent.click(screen.getByRole("button", { name: "Create account" }));
    expect(await screen.findByText("Too many sign-ups.")).toBeTruthy();
    expect(screen.getByText("Try again in 2 minutes.")).toBeTruthy();
    expect((screen.getByRole("button", { name: "Create account" }) as HTMLButtonElement).disabled).toBe(true);
  });
});

describe("error display: server message, never a raw stack", () => {
  test("signin 401 with a non-envelope body shows a generic line", async () => {
    routes["POST /api/v2/auth/signin"] = () => new Response("Traceback (most recent call last): boom", { status: 500 });
    renderAt("/signin");
    await screen.findByRole("heading", { name: "Sign in" });
    type("Username", "wes");
    type("Password", "a sentence long enough");
    fireEvent.click(screen.getByRole("button", { name: "Sign in" }));
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain("Something went wrong");
    expect(document.body.textContent).not.toMatch(STACK);
  });

  test("signup 409 shows no stack text", async () => {
    routes["POST /api/v2/auth/signup"] = () => envelope(409, "CONFLICT", "That username is taken.");
    renderAt("/signup");
    await screen.findByRole("heading", { name: "Create an account" });
    type("Display name", "Wes");
    type("Username", "wes");
    type("Password", "a sentence long enough");
    fireEvent.click(screen.getByRole("button", { name: "Create account" }));
    await screen.findByText("That username is taken.");
    expect(document.body.textContent).not.toMatch(STACK);
  });

  test("password 422: envelope message on New password, no stack", async () => {
    routes["GET /api/v2/auth/me"] = () => json(200, ME);
    routes["POST /api/v2/auth/password"] = () => envelope(422, "VALIDATION_ERROR", "That password is too common.");
    renderAt("/account");
    await screen.findByText("wes");
    type("Current password", "the old sentence here");
    type("New password", "the new sentence here");
    fireEvent.click(password());
    expect(await screen.findByText("That password is too common.")).toBeTruthy();
    expect(screen.getByLabelText("New password").getAttribute("aria-invalid")).toBe("true");
    expect(document.body.textContent).not.toMatch(STACK);
  });

  test("password 403 shows the envelope message", async () => {
    routes["GET /api/v2/auth/me"] = () => json(200, ME);
    routes["POST /api/v2/auth/password"] = () => envelope(403, "FORBIDDEN", "Your current password is incorrect.");
    renderAt("/account");
    await screen.findByText("wes");
    type("Current password", "the old sentence here");
    type("New password", "the new sentence here");
    fireEvent.click(password());
    await screen.findByText("Your current password is incorrect.");
    expect(document.body.textContent).not.toMatch(STACK);
  });
});

describe("auth broadcast", () => {
  test("a password change does not post on the auth channel", async () => {
    routes["GET /api/v2/auth/me"] = () => json(200, ME);
    routes["POST /api/v2/auth/password"] = () => json(200, ME);
    renderAt("/account");
    await screen.findByText("wes");
    type("Current password", "the old sentence here");
    type("New password", "the new sentence here");
    fireEvent.click(password());
    await screen.findByText("Password changed. Other devices have been signed out.");
    expect(posted).toEqual([]);
  });
});

describe("safeNext (open redirect)", () => {
  test.each([
    ["https://evil.example", "/"],
    ["http://evil.example/x", "/"],
    ["//evil.example", "/"],
    ["/\\evil.example", "/"],
    ["javascript:alert(1)", "/"],
    ["data:text/html,x", "/"],
    ["", "/"],
    [undefined, "/"],
    [42, "/"],
    ["/account", "/account"],
    ["/trips/abc?x=1", "/trips/abc?x=1"],
  ])("%j -> %j", (input, out) => {
    expect(safeNext(input)).toBe(out);
  });

  test("/signin?next=javascript: and //evil land on /", async () => {
    routes["GET /api/v2/auth/me"] = () => json(200, ME);
    const router = renderAt("/signin?next=%2F%2Fevil.example");
    await waitFor(() => expect(router.state.location.pathname).toBe("/"));
  });
});

describe("autocomplete values (signin.md / signup.md)", () => {
  const ac = (l: string) => screen.getByLabelText(l).getAttribute("autocomplete");

  test("signin", async () => {
    renderAt("/signin");
    await screen.findByRole("heading", { name: "Sign in" });
    expect(ac("Username")).toBe("username");
    expect(ac("Password")).toBe("current-password");
  });

  test("signup", async () => {
    renderAt("/signup");
    await screen.findByRole("heading", { name: "Create an account" });
    expect(ac("Display name")).toBe("nickname");
    expect(ac("Username")).toBe("username");
    expect(ac("Password")).toBe("new-password");
  });

  test("account", async () => {
    routes["GET /api/v2/auth/me"] = () => json(200, ME);
    renderAt("/account");
    await screen.findByText("wes");
    expect(ac("Current password")).toBe("current-password");
    expect(ac("New password")).toBe("new-password");
    expect(document.querySelector('[autocomplete="off"]')).toBeNull();
  });
});
