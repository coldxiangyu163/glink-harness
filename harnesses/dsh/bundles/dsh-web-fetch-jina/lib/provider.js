/**
 * Jina Reader (`https://r.jina.ai`) backed `WebFetchProvider`.
 *
 * Contract (aligned with `@deepseek-ai/dsh-web-fetch-http`):
 *   * one `WebFetchResult` on success, `WebError` for every failure — this module never lets
 *     an error escape as a process-level exception;
 *   * cooperative cancellation through the caller's `AbortSignal`, plus its own deadline
 *     reported as `WEB_FETCH_TIMEOUT`;
 *   * declared-length and streamed byte caps plus a decoded-character cap;
 *   * non-http(s), credentialed, over-long, and non-public destinations are refused before
 *     any request reaches Jina;
 *   * `data.httpStatus` is the target's status, so a target 404 stays a *result* (the seam
 *     separates retrieval failure from resource state).
 *
 * @module @glink-harness/dsh-web-fetch-jina/provider
 */

import { WebError } from '@deepseek-ai/dsh-web'
import { deadline, timeoutOf } from '@deepseek-ai/dsh-timeout'
import { assertPublicDestination, assertPublicResolution, classifyJinaPayload, validateFetchUrl } from './policy.js'

/** Stable id this provider registers under; `web.fetchProvider` names it. */
export const JINA_FETCH_PROVIDER_ID = 'jina'

/** Default `User-Agent`: an explicit product agent, never a browser disguise. */
export const DEFAULT_USER_AGENT = 'glink-harness-dsh-web-fetch-jina/0.1.0'

/** Default Jina Reader base; the target URL travels in the POST body. */
export const DEFAULT_BASE_URL = 'https://r.jina.ai'

/** Default credential reference resolved through the dsh credentials service. */
export const DEFAULT_API_KEY_ENV = 'JINA_API_KEY'

/** Default provider backstop; the model-facing tool budget fires first and wins. */
export const DEFAULT_TIMEOUT_MS = 60_000

/** Maximum response body size in bytes (the JSON envelope must arrive whole). */
export const DEFAULT_MAX_RESPONSE_BYTES = 5_000_000

/** Maximum decoded content length in characters. */
export const DEFAULT_MAX_BODY_CHARS = 100_000

/** Bounded snippet read from a failing Jina response, for the diagnostic only. */
const ERROR_SNIPPET_BYTES = 2048

/** The Jina Reader fetch provider. */
export class JinaFetchProvider {
  /**
   * @param options - resolved collaborators.
   * @param options.resolveOptions - thunk returning the current (volatile) configuration.
   * @param options.resolveApiKey - resolves the credential reference to a key, per call.
   * @param options.fetchImpl - the fetch implementation (the real `fetch` by default).
   * @param options.lookup - `dns.lookup`-compatible resolver; omit to skip the egress check.
   */
  constructor({
    resolveOptions,
    resolveApiKey,
    fetchImpl = globalThis.fetch,
    lookup,
  } = {}) {
    this.resolveOptions = resolveOptions
    this.resolveApiKey = resolveApiKey
    this.fetchImpl = fetchImpl
    this.lookup = lookup
  }

  /** Stable registration id. */
  get id() {
    return JINA_FETCH_PROVIDER_ID
  }

  /**
   * Cheap local availability check: configuration sanity only, because credential presence
   * has an explicit outcome at fetch time ({@link WebError} `WEB_PROVIDER_CREDENTIAL_MISSING`).
   * @returns true always — the provider is registered and structurally usable.
   */
  available() {
    return true
  }

  /**
   * Retrieve one URL through Jina Reader. Never throws a non-{@link WebError} value.
   * @param request - the seam's fetch request.
   * @param signal - optional caller cancellation.
   * @returns the retrieval outcome; target non-2xx responses resolve descriptively.
   */
  async fetch(request, signal) {
    try {
      return await this.retrieveWithPolicy(request, signal)
    } catch (error) {
      if (error instanceof WebError) throw error
      throw new WebError(`Jina web fetch failed: ${describeError(error)}`, 'WEB_PROVIDER_ERROR', { cause: error })
    }
  }

  /** Validate, resolve, and dispatch one retrieval. */
  async retrieveWithPolicy(request, signal) {
    throwIfAborted(signal)
    const options = this.currentOptions()
    const target = validateFetchUrl(request?.url)
    assertPublicDestination(target)
    throwIfAborted(signal)
    const apiKey = await this.apiKey(options, signal)
    throwIfAborted(signal)
    await this.guardResolution(target, signal)
    throwIfAborted(signal)
    // One signal stops both the request and the body read; `timeoutOf` later separates this
    // provider's timeout from caller or outer-deadline cancellation.
    using remaining = deadline(signal, options.timeoutMs, 'WEB_FETCH_TIMEOUT')
    return await this.perform(target, apiKey, options, remaining.signal)
  }

  /** Resolve the current configuration and assert it is usable (a live edit fails as a WebError). */
  currentOptions() {
    let options
    try {
      options = this.resolveOptions()
      assertOptions(options)
    } catch (error) {
      if (error instanceof WebError) throw error
      throw new WebError(`web-fetch-jina configuration is invalid: ${describeError(error)}`, 'WEB_PROVIDER_ERROR', { cause: error })
    }
    return options
  }

  /**
   * Resolve one operation's credential without retaining it. A missing credential is an
   * explicit, actionable failure rather than an anonymous degraded request.
   * @param options - this operation's configuration snapshot.
   * @param signal - caller cancellation.
   * @returns the key.
   */
  async apiKey(options, signal) {
    throwIfAborted(signal)
    let resolved
    try {
      resolved = await this.resolveApiKey(options.apiKeyEnv)
    } catch (error) {
      if (signal?.aborted === true) throw abortError(signal, error)
      throw new WebError(`Jina fetch credential resolution failed: ${describeError(error)}`, 'WEB_PROVIDER_ERROR', { cause: error })
    }
    throwIfAborted(signal)
    if (typeof resolved === 'string' && resolved.length > 0) return resolved
    throw new WebError(
      `Jina fetch has no API key for "${options.apiKeyEnv}"; store it through the credentials`
      + ' service (the dsh credentials file behind ~/.dsh/.credentials.yaml) or export it in the'
      + ' launching environment',
      'WEB_PROVIDER_CREDENTIAL_MISSING',
    )
  }

  /** Refuse a hostname that locally resolves to a non-public address (best effort). */
  async guardResolution(target, signal) {
    try {
      await assertPublicResolution(target.hostname, this.lookup)
    } catch (error) {
      if (error instanceof WebError) throw error
      // A resolver fault must not fail retrieval: Jina resolves independently.
    }
    throwIfAborted(signal)
  }

  /** One POST to Jina Reader, then the shared result mapping. */
  async perform(target, apiKey, options, signal) {
    const endpoint = normalizeBaseUrl(options.baseURL)
    let response
    try {
      response = await this.fetchImpl(endpoint, {
        method: 'POST',
        // Jina answers at this endpoint; a redirect would drop the POST body, so refuse it
        // rather than silently asking for something else.
        redirect: 'error',
        headers: {
          'authorization': `Bearer ${apiKey}`,
          'accept': 'application/json',
          'content-type': 'application/x-www-form-urlencoded',
          'user-agent': options.userAgent,
        },
        body: new URLSearchParams({ url: target.toString() }).toString(),
        ...signal === undefined ? {} : { signal },
      })
    } catch (error) {
      throw translateAbortOrNetwork(error, signal, `Jina reader request to ${endpoint} failed`)
    }

    if (!response.ok) {
      const snippet = await readErrorSnippet(response, signal)
      throw jinaStatusError(response.status, snippet)
    }

    const read = await readBounded(response, options.maxResponseBytes, signal)
    if (read.overLimit) {
      throw new WebError(
        `Jina reader response exceeds the maximum of ${options.maxResponseBytes} bytes`
        + ' (declared or streamed) before its JSON envelope could be parsed',
        'WEB_FETCH_TOO_LARGE',
      )
    }
    let payload
    try {
      payload = JSON.parse(new TextDecoder('utf-8').decode(read.bytes))
    } catch (error) {
      throw new WebError('Jina reader returned a body that is not valid JSON', 'WEB_PROVIDER_ERROR', { cause: error })
    }
    const mapped = classifyJinaPayload(payload, target.toString())
    const truncated = mapped.content.length > options.maxBodyChars
    const content = truncated ? mapped.content.slice(0, options.maxBodyChars) : mapped.content
    return {
      url: mapped.url,
      statusCode: mapped.statusCode,
      body: { kind: 'text', content },
      truncated,
    }
  }
}

/** Assert one resolved configuration carries usable values. Throws a plain Error (boot) or WebError (live edit). */
export function assertOptions(options) {
  if (options === null || typeof options !== 'object') throw configError('configuration must be an object')
  if (typeof options.apiKeyEnv !== 'string' || !/^[A-Za-z_][A-Za-z0-9_]*$/.test(options.apiKeyEnv)) {
    throw configError(`apiKeyEnv must be a POSIX environment-variable name, got ${JSON.stringify(options.apiKeyEnv)}`)
  }
  if (typeof options.baseURL !== 'string' || !URL.canParse(options.baseURL)) {
    throw configError(`baseURL must be an absolute URL, got ${JSON.stringify(options.baseURL)}`)
  }
  const base = new URL(options.baseURL)
  if (base.protocol !== 'http:' && base.protocol !== 'https:') {
    throw configError(`baseURL must use http: or https:, got ${JSON.stringify(base.protocol)}`)
  }
  if (!Number.isFinite(options.timeoutMs) || options.timeoutMs <= 0 || options.timeoutMs > 2_147_483_647) {
    throw configError('timeoutMs must be a positive finite number no greater than 2147483647')
  }
  for (const name of ['maxResponseBytes', 'maxBodyChars']) {
    if (!Number.isFinite(options[name]) || options[name] <= 0) throw configError(`${name} must be a positive finite number`)
  }
  if (typeof options.userAgent !== 'string' || options.userAgent.length === 0) {
    throw configError('userAgent must be a non-empty string')
  }
}

/** One configuration defect, as a plain Error at apply time and a WebError once live. */
function configError(message) {
  return new Error(`web-fetch-jina: ${message}`)
}

/** Normalize the configured base into the POST endpoint. */
function normalizeBaseUrl(baseURL) {
  return baseURL.endsWith('/') ? baseURL : `${baseURL}/`
}

/** Send one request URL to the target status. */
function jinaStatusError(status, snippet) {
  const detail = snippet.length > 0 ? `: ${snippet}` : ''
  if (status === 401 || status === 403) {
    return new WebError(
      `Jina reader rejected the request (HTTP ${status}); check the JINA_API_KEY credential${detail}`,
      'WEB_PROVIDER_ERROR',
    )
  }
  if (status === 402) {
    return new WebError(`Jina reader reported a billing or quota problem (HTTP 402)${detail}`, 'WEB_PROVIDER_ERROR')
  }
  if (status === 429) {
    return new WebError(
      `Jina reader rate limit reached (HTTP 429); retry later or raise the plan quota${detail}`,
      'WEB_PROVIDER_ERROR',
    )
  }
  if (status === 422) {
    return new WebError(`Jina reader could not process the target (HTTP 422)${detail}`, 'WEB_PROVIDER_ERROR')
  }
  return new WebError(`Jina reader returned HTTP ${status}${detail}`, 'WEB_PROVIDER_ERROR')
}

/**
 * Read a response body up to a byte cap. The declared `Content-Length` decides `overLimit`
 * before any read; a stream that grows past the cap is cut short and also reports
 * `overLimit`, because the JSON envelope cannot survive truncation.
 */
async function readBounded(response, maxBytes, signal) {
  const declared = response.headers?.get?.('content-length')
  if (typeof declared === 'string' && declared.length > 0) {
    const length = Number(declared)
    if (Number.isFinite(length) && length > maxBytes) {
      await cancelBody(response)
      return { bytes: new Uint8Array(0), overLimit: true }
    }
  }
  if (response.body === null || response.body === undefined) return { bytes: new Uint8Array(0), overLimit: false }
  const chunks = []
  let total = 0
  let overLimit = false
  const reader = response.body.getReader()
  try {
    for (;;) {
      const { done, value } = await reader.read()
      if (done) break
      const remaining = maxBytes - total
      if (value.byteLength > remaining) {
        if (remaining > 0) {
          chunks.push(value.subarray(0, remaining))
          total += remaining
        }
        overLimit = true
        break
      }
      chunks.push(value)
      total += value.byteLength
    }
  } catch (error) {
    throw translateAbortOrNetwork(error, signal, 'Jina reader response read failed')
  } finally {
    await reader.cancel().catch(() => {})
  }
  const bytes = new Uint8Array(total)
  let offset = 0
  for (const chunk of chunks) {
    bytes.set(chunk, offset)
    offset += chunk.byteLength
  }
  return { bytes, overLimit }
}

/** Read a small diagnostic snippet from a failing response; never fails the diagnostic path. */
async function readErrorSnippet(response, signal) {
  try {
    const read = await readBounded(response, ERROR_SNIPPET_BYTES, signal)
    const text = new TextDecoder('utf-8').decode(read.bytes).trim().replace(/\s+/g, ' ')
    return text.length > 300 ? `${text.slice(0, 300)}…` : text
  } catch (error) {
    if (error instanceof WebError && (error.code === 'WEB_ABORTED' || error.code === 'WEB_FETCH_TIMEOUT')) throw error
    return ''
  }
}

/** Best-effort body cancellation so a refused response never leaks its socket. */
async function cancelBody(response) {
  try {
    await response.body?.cancel()
  } catch {
    // Cleanup only.
  }
}

/**
 * Translate a thrown fetch/stream error by the deadline signal rather than the thrown value:
 * this provider's timeout, a caller abort, or an outer deadline are distinguishable only
 * through the signal that was armed.
 */
function translateAbortOrNetwork(error, signal, context) {
  if (error instanceof WebError) return error
  const timeout = timeoutOf(signal, 'WEB_FETCH_TIMEOUT')
  if (timeout !== undefined) return new WebError('Jina web fetch timed out', 'WEB_FETCH_TIMEOUT', { cause: timeout })
  if (signal?.aborted === true || isAbortError(error)) return abortError(signal, error)
  return new WebError(`${context}: ${describeError(error)}`, 'WEB_PROVIDER_ERROR', { cause: error })
}

/** The seam's abort outcome, carrying the signal's reason when it has one. */
function abortError(signal, cause) {
  const reason = signal?.reason
  return new WebError('web fetch aborted', 'WEB_ABORTED', { cause: reason ?? cause })
}

/** Refuse work whose caller signal already fired. */
function throwIfAborted(signal) {
  if (signal?.aborted === true) throw abortError(signal, signal.reason)
}

/** DOMException/E2E abort detection. */
function isAbortError(error) {
  return error instanceof Error && (error.name === 'AbortError' || error.name === 'TimeoutError')
}

/** One-line error description safe for a tool-visible message. */
function describeError(error) {
  if (error instanceof Error) return error.message.length > 0 ? error.message : error.name
  return String(error)
}
