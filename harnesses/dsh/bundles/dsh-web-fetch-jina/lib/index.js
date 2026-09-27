/**
 * Plugin entry for the Jina Reader fetch provider bundle.
 *
 * Registers one `ctx.web` fetch provider (`id: jina`) and resolves its credential per
 * operation through the dsh credentials seam, so a rotated key reaches the next fetch
 * without a configuration edit and no secret ever enters a configuration file.
 * The bundle's `cordis.patch.yml` inserts this row and points `web.fetchProvider` at it.
 *
 * @module @pi-glink/dsh-web-fetch-jina
 */

import { lookup } from 'node:dns'
import z from '@deepseek-ai/schemastery'
import { credentialRef } from '@deepseek-ai/dsh-credentials'
import { launchEnvironmentOf } from '@deepseek-ai/dsh-launch-environment'
import {
  DEFAULT_API_KEY_ENV,
  DEFAULT_BASE_URL,
  DEFAULT_MAX_BODY_CHARS,
  DEFAULT_MAX_RESPONSE_BYTES,
  DEFAULT_TIMEOUT_MS,
  DEFAULT_USER_AGENT,
  JINA_FETCH_PROVIDER_ID,
  JinaFetchProvider,
  assertOptions,
} from './provider.js'

export {
  DEFAULT_API_KEY_ENV,
  DEFAULT_BASE_URL,
  DEFAULT_MAX_BODY_CHARS,
  DEFAULT_MAX_RESPONSE_BYTES,
  DEFAULT_TIMEOUT_MS,
  DEFAULT_USER_AGENT,
  JINA_FETCH_PROVIDER_ID,
  JinaFetchProvider,
} from './provider.js'

/** Cordis plugin name used by loader diagnostics. */
export const name = 'web-fetch-jina'

/** The web seam this provider registers into. */
export const inject = ['web']

/**
 * Plugin config. Credential values are referenced, never carried: `apiKeyEnv` names the
 * credential the credentials seam resolves for each fetch. (dsh 0.1.5-rc.3 schemastery has
 * no `volatile()` field kind; the ACP profile is startup-reload, so `apply` reads the
 * validated config directly.)
 */
export const Config = z.object({
  /** Credential reference resolved for each fetch; defaults to `JINA_API_KEY`. */
  apiKeyEnv: z.string().role('credential-ref').default(DEFAULT_API_KEY_ENV),
  /** Jina Reader base; the target URL travels in the POST body. */
  baseURL: z.string().default(DEFAULT_BASE_URL),
  /** Provider backstop in milliseconds; the model-facing tool budget fires first. */
  timeoutMs: z.number().default(DEFAULT_TIMEOUT_MS),
  /** Maximum response body size in bytes. */
  maxResponseBytes: z.number().default(DEFAULT_MAX_RESPONSE_BYTES),
  /** Maximum decoded content length in characters. */
  maxBodyChars: z.number().default(DEFAULT_MAX_BODY_CHARS),
  /** `User-Agent` header sent to Jina. */
  userAgent: z.string().default(DEFAULT_USER_AGENT),
})

/**
 * Read the current resolution of every field into one snapshot. One fetch never mixes two
 * configurations: the snapshot is taken once at the operation's entry.
 * @param config - the validated plugin config.
 * @returns the resolved options.
 */
function snapshot(config) {
  return {
    apiKeyEnv: config.apiKeyEnv ?? DEFAULT_API_KEY_ENV,
    baseURL: config.baseURL ?? DEFAULT_BASE_URL,
    timeoutMs: config.timeoutMs ?? DEFAULT_TIMEOUT_MS,
    maxResponseBytes: config.maxResponseBytes ?? DEFAULT_MAX_RESPONSE_BYTES,
    maxBodyChars: config.maxBodyChars ?? DEFAULT_MAX_BODY_CHARS,
    userAgent: config.userAgent ?? DEFAULT_USER_AGENT,
  }
}

/**
 * Resolve one fetch's key from the credentials seam, falling back to the launch environment
 * when the composition carries no credentials service (a bare test composition).
 * @param ctx - plugin context.
 * @param ref - the credential reference name.
 * @returns the key value, or `undefined` when the reference is unset.
 */
async function resolveApiKey(ctx, ref) {
  const reference = credentialRef(ref)
  const credentials = ctx.get('credentials')
  if (credentials !== undefined) {
    const hit = await credentials.resolve(reference)
    if (hit !== undefined && hit.value.length > 0) return hit.value
    return undefined
  }
  const ambient = launchEnvironmentOf(ctx).get(ref)?.value
  return ambient !== undefined && ambient.length > 0 ? ambient : undefined
}

/**
 * Record one successful retrieval as a durable session event, the same pattern
 * `dsh-web-search-deepseek` uses for its own provider dispatch. The record is secret-free
 * and audit-only: it is the evidence that a run's `web_fetch` was served by this provider.
 * @param ctx - plugin context.
 * @param record - the retrieval facts.
 */
function recordRetrieval(ctx, record) {
  ctx.get('agents')?.currentInitiator()?.session.append('web/jina-fetch-request', record)
}

/** Register the Jina fetch provider with `ctx.web`. */
export function apply(ctx, config) {
  // Fail loudly at composition time on a nonsensical configuration, before any fetch.
  assertOptions(snapshot(config))
  ctx.web.registerFetchProvider(new JinaFetchProvider({
    resolveOptions: () => snapshot(config),
    resolveApiKey: ref => resolveApiKey(ctx, ref),
    lookup,
    recordRetrieval: record => recordRetrieval(ctx, record),
  }))
}
