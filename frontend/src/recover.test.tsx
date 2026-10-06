import "fake-indexeddb/auto";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { RouterProvider, createMemoryHistory, createRouter } from "@tanstack/react-router";
import { routeTree } from "./routeTree.gen";

// t-am-fe-recover: /recover through the real route tree and generated client.

const ME = { id: "u1", username: "wes", displayName: "Wes", createdAt: "2026-10-01T00:00:00Z" };
const OLD = "OLDCODE2ABCDEFGHJKMNPQRSTV";
const NEW = "7K3M9QX2ABCDEFGHJKMNPQRSTV";

type Handler = (init?: RequestInit) => Response | Promise<Response>;
const json = (status: number, body: unknown, headers: Record<string, string> = {}) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json", ...headers } });
const envelope = (status: number, code: string, message: string, headers?: Record<string, string>) =>
  json(status, { error: { code, message } }, headers);

let routes: Record<string, Handler>;
const calls: string[] = [];
const posted: unknown[] = [];
let client: QueryClient;

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
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  );
  return router;
}

const type = (label: string | RegExp, value: string) => fireEvent.change(screen.getByLabelText(label), { target: { value } });
const settle = () => act(() => new Promise((r) => setTimeout(r, 20)));

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
  sessionStorage.clear();
  calls.length = 0;
  posted.length = 0;
  routes = { "GET /api/v2/auth/me": () => envelope(401, "UNAUTHENTICATED", "Sign in to continue.") };
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

async function fill() {
  await screen.findByRole("heading", { name: "Reset your password" });
  type("Username", "Wes");
  type("Recovery code", `${OLD.slice(0, 4)} ${OLD.slice(4)}`);
  type("New password", "a sentence long enough");
}

describe("/signin link", () => {
  test("Forgot your password? goes to /recover carrying the typed username", async () => {
    const router = renderAt("/signin");
    await screen.findByRole("heading", { name: "Sign in" });
    type("Username", "wes");
    fireEvent.click(screen.getByRole("link", { name: /Forgot your password/ }));
    await screen.findByRole("heading", { name: "Reset your password" });
    expect(router.state.location.pathname).toBe("/recover");
    expect((screen.getByLabelText("Username") as HTMLInputElement).value).toBe("wes");
    expect(router.state.location.href).not.toContain("password");
  });
});

describe("/recover", () => {
  test("client rule blocks a short new password", async () => {
    renderAt("/recover");
    await fill();
    type("New password", "short");
    fireEvent.click(screen.getByRole("button", { name: "Reset password" }));
    expect(await screen.findByText("Use at least 15 characters.")).toBeTruthy();
    expect(calls.filter((c) => c.startsWith("POST"))).toEqual([]);
  });

  test("200: sends typed code, shows new code once, signs in, never stores either code", async () => {
    let body: unknown;
    routes["POST /api/v2/auth/recover"] = (init) => {
      body = JSON.parse(String(init?.body));
      routes["GET /api/v2/auth/me"] = () => json(200, ME);
      return json(200, { account: ME, recoveryCode: NEW });
    };
    const router = renderAt("/recover?next=%2Faccount");
    await fill();
    fireEvent.click(screen.getByRole("button", { name: "Reset password" }));

    await screen.findByRole("heading", { name: "Save your new recovery code" });
    expect(body).toEqual({ username: "wes", recoveryCode: `${OLD.slice(0, 4)} ${OLD.slice(4)}`, newPassword: "a sentence long enough" });
    expect(document.querySelector(".recovery-code__code")!.textContent).toBe(NEW);
    const cont = screen.getByRole("button", { name: "Continue" }) as HTMLButtonElement;
    expect(cont.disabled).toBe(true);
    fireEvent.click(screen.getByLabelText("I've saved my recovery code"));
    expect(cont.disabled).toBe(false);

    await settle();
    expect(JSON.parse(localStorage.getItem("btj.me")!)).toEqual({ id: "u1", displayName: "Wes" });
    expect(posted).toEqual([{ channel: "auth", type: "signin" }]);
    const local = Object.keys(localStorage).map((k) => `${k}=${localStorage.getItem(k)}`).join("\n");
    const session = Object.keys(sessionStorage).map((k) => `${k}=${sessionStorage.getItem(k)}`).join("\n");
    const idb = await dumpIndexedDb();
    expect((await indexedDB.databases()).length).toBeGreaterThan(0);
    for (const code of [OLD, NEW]) {
      expect(local).not.toContain(code);
      expect(session).not.toContain(code);
      expect(idb).not.toContain(code);
    }
    // The mutation cache no longer holds the new code (nor the old one).
    const cached = JSON.stringify(client.getMutationCache().getAll().map((m) => [m.state.data, m.state.variables]));
    expect(cached).not.toContain(NEW);
    expect(cached).not.toContain(OLD);

    fireEvent.click(cont);
    await settle();
    expect(router.state.location.pathname).toBe("/account");
  });

  test("a double tap sends one request", async () => {
    let release!: () => void;
    routes["POST /api/v2/auth/recover"] = () => new Promise<Response>((r) => (release = () => r(json(200, { account: ME, recoveryCode: NEW }))));
    renderAt("/recover");
    await fill();
    const btn = screen.getByRole("button", { name: "Reset password" });
    fireEvent.click(btn);
    fireEvent.click(btn);
    await settle();
    expect(calls.filter((c) => c === "POST /api/v2/auth/recover")).toHaveLength(1);
    release();
    await screen.findByRole("heading", { name: "Save your new recovery code" });
    expect(calls.filter((c) => c === "POST /api/v2/auth/recover")).toHaveLength(1);
  });

  test("401: server's generic message, code kept, password cleared", async () => {
    routes["POST /api/v2/auth/recover"] = () => envelope(401, "UNAUTHENTICATED", "Username or recovery code is incorrect.");
    renderAt("/recover");
    await fill();
    fireEvent.click(screen.getByRole("button", { name: "Reset password" }));
    expect(await screen.findByText("Username or recovery code is incorrect.")).toBeTruthy();
    expect((screen.getByLabelText("Recovery code") as HTMLInputElement).value).toBe(`${OLD.slice(0, 4)} ${OLD.slice(4)}`);
    expect((screen.getByLabelText("New password") as HTMLInputElement).value).toBe("");
  });

  test("422 shows the envelope message", async () => {
    routes["POST /api/v2/auth/recover"] = () => envelope(422, "VALIDATION_ERROR", "Password is too common.");
    renderAt("/recover");
    await fill();
    fireEvent.click(screen.getByRole("button", { name: "Reset password" }));
    expect(await screen.findByText("Password is too common.")).toBeTruthy();
  });

  test("429: Retry-After countdown disables submit", async () => {
    routes["POST /api/v2/auth/recover"] = () => envelope(429, "RATE_LIMITED", "Too many attempts.", { "Retry-After": "90" });
    renderAt("/recover");
    await fill();
    fireEvent.click(screen.getByRole("button", { name: "Reset password" }));
    expect(await screen.findByText("Too many attempts.")).toBeTruthy();
    expect(screen.getByText(/Try again in/)).toBeTruthy();
    expect((screen.getByRole("button", { name: "Reset password" }) as HTMLButtonElement).disabled).toBe(true);
  });
});
