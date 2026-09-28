import { afterEach, beforeEach, describe, expect, expectTypeOf, test, vi } from "vitest";
import request, { ApiError } from "./client";
import type { ResponseErrorConfig } from "./client";
import type { ErrorCode } from "./gen/types/ErrorCode";
import type { StopCreate } from "./gen/types/StopCreate";
import { createStopApiTripsSlugStopsPost } from "./gen/clients/createStopApiTripsSlugStopsPost";
import type { useCreateStopApiTripsSlugStopsPost } from "./gen/hooks/useCreateStopApiTripsSlugStopsPost";

// Written from the task's acceptance criteria and the api-contract retry rule:
// the offline queue retries anything without an ErrorEnvelope and classifies
// everything else by `envelope.error.code`. So every failure must be an
// ApiError, and "has an envelope" must be exactly "body is JSON with a string
// error.code" — never a guess.

const fetchMock = vi.fn<typeof fetch>();

beforeEach(() => {
  fetchMock.mockReset();
  vi.stubGlobal("fetch", fetchMock);
});
afterEach(() => {
  vi.unstubAllGlobals();
});

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

/** Headers the client passed to fetch, normalised whatever shape they took. */
function sentHeaders(): Headers {
  const init = fetchMock.mock.calls[0]?.[1];
  return new Headers(init?.headers);
}

async function thrown(p: Promise<unknown>): Promise<unknown> {
  try {
    await p;
  } catch (e) {
    return e;
  }
  throw new Error("expected the request to throw, it resolved");
}

async function thrownApiError(p: Promise<unknown>): Promise<ApiError> {
  const e = await thrown(p);
  expect(e).toBeInstanceOf(ApiError);
  expect(e).not.toBeInstanceOf(SyntaxError);
  return e as ApiError;
}

const stop: StopCreate = {
  id: "0b6f6a2e-4a0e-4f8e-9b8a-1c2d3e4f5a6b",
  name: "Alice Springs",
  lat: -23.698,
  lng: 133.8807,
  locationSource: "gps",
  arrivedAt: "2026-09-29T10:00:00+09:30",
};

describe("request headers and body (AC 3)", () => {
  test("JSON POST sends application/json and a JSON-stringified body", async () => {
    fetchMock.mockResolvedValue(jsonResponse(201, { ok: true }));

    await request({ method: "POST", url: "/api/x", data: { a: 1 } });

    expect(sentHeaders().get("content-type")).toBe("application/json");
    expect(fetchMock.mock.calls[0]?.[1]?.body).toBe(JSON.stringify({ a: 1 }));
  });

  test("FormData POST sets no Content-Type and passes the FormData through", async () => {
    fetchMock.mockResolvedValue(jsonResponse(201, { ok: true }));
    const form = new FormData();
    form.append("file", new Blob(["x"], { type: "image/jpeg" }), "a.jpg");

    await request({ method: "POST", url: "/api/x", data: form });

    expect(sentHeaders().has("content-type")).toBe(false);
    expect(fetchMock.mock.calls[0]?.[1]?.body).toBe(form);
  });

  test("no body sends no JSON Content-Type", async () => {
    fetchMock.mockResolvedValue(jsonResponse(200, []));

    await request({ method: "GET", url: "/api/x" });

    expect(sentHeaders().get("content-type")).not.toBe("application/json");
    expect(fetchMock.mock.calls[0]?.[1]?.body ?? undefined).toBeUndefined();
  });
});

describe("2xx responses (AC 4)", () => {
  test("200 JSON body is returned as data with status", async () => {
    fetchMock.mockResolvedValue(jsonResponse(200, { id: "t1" }));

    const res = await request({ method: "GET", url: "/api/x" });

    expect(res.status).toBe(200);
    expect(res.data).toEqual({ id: "t1" });
  });

  test.each([204, 205, 304])("%i with no body resolves to {}", async (status) => {
    fetchMock.mockResolvedValue(new Response(null, { status }));

    const res = await request({ method: "GET", url: "/api/x" });

    expect(res.status).toBe(status);
    expect(res.data).toEqual({});
  });

  test("200 with a null body resolves to {}", async () => {
    fetchMock.mockResolvedValue(new Response(null, { status: 200 }));

    const res = await request({ method: "GET", url: "/api/x" });

    expect(res.data).toEqual({});
  });
});

describe("non-2xx with an ErrorEnvelope (AC 5)", () => {
  test("409 CONFLICT through a generated client: code readable without a cast", async () => {
    const envelope = { error: { code: "CONFLICT", message: "That stop id belongs to another trip." } };
    fetchMock.mockResolvedValue(jsonResponse(409, envelope));

    const err = await thrownApiError(createStopApiTripsSlugStopsPost({ slug: "rider-slug", data: stop }));

    // Compile-time: envelope.error.code is typed ErrorCode, no cast needed.
    const code: ErrorCode | undefined = err.envelope?.error.code;
    expect(code).toBe("CONFLICT");
    expect(err.status).toBe(409);
    expect(err.envelope).toEqual(envelope);
    expect(err.message).toBe(envelope.error.message);
    // The generated client really went through the stubbed fetch with a JSON body.
    expect(sentHeaders().get("content-type")).toBe("application/json");
    expect(JSON.parse(String(fetchMock.mock.calls[0]?.[1]?.body))).toEqual(stop);
  });

  test("422 VALIDATION_ERROR", async () => {
    const envelope = { error: { code: "VALIDATION_ERROR", message: "arrivedAt needs a UTC offset." } };
    fetchMock.mockResolvedValue(jsonResponse(422, envelope));

    const err = await thrownApiError(request({ method: "POST", url: "/api/x", data: {} }));

    expect(err.status).toBe(422);
    expect(err.envelope?.error.code).toBe("VALIDATION_ERROR");
    expect(err.message).toBe("arrivedAt needs a UTC offset.");
  });

  test("500 INTERNAL_ERROR keeps its envelope (retryable by code, not by absence)", async () => {
    const envelope = { error: { code: "INTERNAL_ERROR", message: "Something went wrong." } };
    fetchMock.mockResolvedValue(jsonResponse(500, envelope));

    const err = await thrownApiError(request({ method: "GET", url: "/api/x" }));

    expect(err.status).toBe(500);
    expect(err.envelope?.error.code).toBe("INTERNAL_ERROR");
  });
});

describe("non-2xx without an envelope (AC 6)", () => {
  test("502 text/html from an intermediary", async () => {
    fetchMock.mockResolvedValue(
      new Response("<html><body>502 Bad Gateway</body></html>", {
        status: 502,
        statusText: "Bad Gateway",
        headers: { "Content-Type": "text/html" },
      }),
    );

    const err = await thrownApiError(request({ method: "GET", url: "/api/x" }));

    expect(err.status).toBe(502);
    expect(err.envelope).toBeUndefined();
    expect(err.message).not.toBe("");
  });

  test("500 JSON that is not envelope-shaped (FastAPI default detail)", async () => {
    fetchMock.mockResolvedValue(jsonResponse(500, { detail: "Internal Server Error" }));

    const err = await thrownApiError(request({ method: "GET", url: "/api/x" }));

    expect(err.status).toBe(500);
    expect(err.envelope).toBeUndefined();
  });

  test.each([
    ["invalid JSON", '{"error": {"code": "CONFL'],
    ["JSON null", "null"],
    ["error.code not a string", JSON.stringify({ error: { code: 42, message: "x" } })],
    ["error not an object", JSON.stringify({ error: "CONFLICT" })],
    ["empty body", ""],
  ])("503 with %s -> no envelope, no raw SyntaxError", async (_label, body) => {
    fetchMock.mockResolvedValue(
      new Response(body, { status: 503, headers: { "Content-Type": "application/json" } }),
    );

    const err = await thrownApiError(request({ method: "GET", url: "/api/x" }));

    expect(err.status).toBe(503);
    expect(err.envelope).toBeUndefined();
  });
});

describe("fetch rejections (AC 7)", () => {
  test("network failure (TypeError) -> ApiError with no status and no envelope", async () => {
    const netErr = new TypeError("Failed to fetch");
    fetchMock.mockRejectedValue(netErr);

    const err = await thrownApiError(request({ method: "POST", url: "/api/x", data: { a: 1 } }));

    expect(err.status).toBeUndefined();
    expect(err.envelope).toBeUndefined();
    expect(err.cause).toBe(netErr);
  });

  test("AbortError is re-thrown unchanged, not wrapped", async () => {
    const abortErr = new DOMException("The operation was aborted.", "AbortError");
    fetchMock.mockRejectedValue(abortErr);

    const e = await thrown(request({ method: "GET", url: "/api/x", signal: new AbortController().signal }));

    expect(e).toBe(abortErr);
    expect(e).not.toBeInstanceOf(ApiError);
  });
});

describe("envelope vs no-envelope is always distinguishable (AC 10)", () => {
  test("every envelope case has a string code; every no-envelope case has envelope undefined", async () => {
    const envelopeCases: Array<() => Response> = [
      () => jsonResponse(409, { error: { code: "CONFLICT", message: "c" } }),
      () => jsonResponse(422, { error: { code: "VALIDATION_ERROR", message: "v" } }),
    ];
    const noEnvelopeCases: Array<() => Response | Promise<never>> = [
      () => new Response("<html>502</html>", { status: 502, headers: { "Content-Type": "text/html" } }),
      () => jsonResponse(500, { detail: "x" }),
      () => Promise.reject(new TypeError("Failed to fetch")),
    ];

    for (const make of envelopeCases) {
      fetchMock.mockImplementationOnce(async () => make());
      const err = await thrownApiError(request({ method: "GET", url: "/api/x" }));
      expect(typeof err.envelope?.error.code).toBe("string");
    }
    for (const make of noEnvelopeCases) {
      fetchMock.mockImplementationOnce(async () => make());
      const err = await thrownApiError(request({ method: "GET", url: "/api/x" }));
      expect(err.envelope).toBeUndefined();
    }
  });
});

describe("types (AC 8)", () => {
  test("ResponseErrorConfig<T> is ApiError, so a hook's error is what is thrown", () => {
    expectTypeOf<ResponseErrorConfig<{ detail: string }>>().toEqualTypeOf<ApiError>();
    type HookError = ReturnType<typeof useCreateStopApiTripsSlugStopsPost>["error"];
    expectTypeOf<HookError>().toEqualTypeOf<ApiError | null>();
  });
});
