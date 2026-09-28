/**
 * The fetch client every generated call in `gen/` goes through, wired in by
 * `importPath` in `kubb.config.ts`. Started from Kubb's bundled
 * `.kubb/fetch.ts` (v4.39.3) and changed in two places:
 *
 * 1. `Content-Type: application/json` is sent on JSON bodies only. A `FormData`
 *    body gets no Content-Type from here, so the browser can add the multipart
 *    boundary itself.
 * 2. Every failure throws `ApiError`. `envelope` is set only when the body is a
 *    parseable `ErrorEnvelope`; the offline queue classifies on
 *    `envelope.error.code` and retries anything without an envelope
 *    (docs/api-contract.md, Error envelope). An `AbortError` is re-thrown as is.
 */
import type { ErrorEnvelope } from './gen/types/ErrorEnvelope'

export type RequestCredentials = 'omit' | 'same-origin' | 'include'

export type RequestConfig<TData = unknown> = {
  baseURL?: string
  url?: string
  method?: 'GET' | 'PUT' | 'PATCH' | 'POST' | 'DELETE' | 'OPTIONS' | 'HEAD'
  params?: unknown
  data?: TData | FormData
  responseType?: 'arraybuffer' | 'blob' | 'document' | 'json' | 'text' | 'stream'
  signal?: AbortSignal
  headers?: [string, string][] | Record<string, string>
  credentials?: RequestCredentials
}

export type ResponseConfig<TData = unknown> = {
  data: TData
  status: number
  statusText: string
  headers: Headers
}

/**
 * The one error type this client throws.
 * - `envelope` set: the server answered with an `ErrorEnvelope`; classify by `envelope.error.code`.
 * - `envelope` undefined, `status` set: a non-2xx without an envelope (e.g. a 502 HTML page).
 * - both undefined: the request never got a response (network failure).
 */
export class ApiError extends Error {
  readonly status?: number
  readonly envelope?: ErrorEnvelope

  constructor(message: string, status?: number, envelope?: ErrorEnvelope, options?: ErrorOptions) {
    super(message, options)
    this.name = 'ApiError'
    this.status = status
    this.envelope = envelope
  }
}

// Generated code passes the operation's documented error bodies as `_TError`;
// what is actually thrown is always ApiError, so that is what hooks see as `error`.
export type ResponseErrorConfig<_TError = unknown> = ApiError

export type Client = <TData, _TError = unknown, TVariables = unknown>(config: RequestConfig<TVariables>) => Promise<ResponseConfig<TData>>

let _config: Partial<RequestConfig> = {}

export const getConfig = () => _config

export const setConfig = (config: Partial<RequestConfig>) => {
  _config = config
  return getConfig()
}

export const mergeConfig = <T extends RequestConfig>(...configs: Array<Partial<T>>): Partial<T> => {
  return configs.reduce<Partial<T>>((merged, config) => {
    return {
      ...merged,
      ...config,
      headers: {
        ...(Array.isArray(merged.headers) ? Object.fromEntries(merged.headers) : merged.headers),
        ...(Array.isArray(config.headers) ? Object.fromEntries(config.headers) : config.headers),
      },
    }
  }, {})
}

const isAbort = (e: unknown) => (e as { name?: unknown } | null)?.name === 'AbortError'

function parseEnvelope(text: string): ErrorEnvelope | undefined {
  let body: unknown
  try {
    body = JSON.parse(text)
  } catch {
    return undefined
  }
  const error = (body as { error?: { code?: unknown } } | null)?.error
  return typeof error?.code === 'string' ? (body as ErrorEnvelope) : undefined
}

const request = async <TData, _TError = unknown, TVariables = unknown>(paramsConfig: RequestConfig<TVariables>): Promise<ResponseConfig<TData>> => {
  const normalizedParams = new URLSearchParams()

  const config = mergeConfig(getConfig(), paramsConfig)

  Object.entries(config.params || {}).forEach(([key, value]) => {
    if (value !== undefined) {
      normalizedParams.append(key, value === null ? 'null' : value.toString())
    }
  })

  let targetUrl = [config.baseURL, config.url].filter(Boolean).join('')

  if (config.params) {
    targetUrl += `?${normalizedParams}`
  }

  const isForm = config.data instanceof FormData
  const isJson = config.data !== undefined && !isForm

  let response: Response
  try {
    response = await globalThis.fetch(targetUrl, {
      credentials: config.credentials || 'same-origin',
      method: config.method?.toUpperCase(),
      body: isForm ? (config.data as FormData) : isJson ? JSON.stringify(config.data) : undefined,
      signal: config.signal,
      headers: isJson ? { 'Content-Type': 'application/json', ...(config.headers as Record<string, string>) } : config.headers,
    })
  } catch (e) {
    if (isAbort(e)) throw e
    throw new ApiError(e instanceof Error ? e.message : 'Network request failed', undefined, undefined, { cause: e })
  }

  // 304 stays a success, as in the bundled client (data `{}` below).
  if (!response.ok && response.status !== 304) {
    let text = ''
    try {
      text = await response.text()
    } catch (e) {
      if (isAbort(e)) throw e
    }
    const envelope = parseEnvelope(text)
    throw new ApiError(envelope ? envelope.error.message : `HTTP ${response.status} ${response.statusText}`.trim(), response.status, envelope)
  }

  const data = [204, 205, 304].includes(response.status) || !response.body ? {} : await response.json()

  return {
    data: data as TData,
    status: response.status,
    statusText: response.statusText,
    headers: response.headers as Headers,
  }
}

request.getConfig = getConfig
request.setConfig = setConfig

export default request
