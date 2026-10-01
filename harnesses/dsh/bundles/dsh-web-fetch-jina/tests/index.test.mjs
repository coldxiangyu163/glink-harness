import { test } from 'node:test'
import assert from 'node:assert/strict'

import { name, inject, apply, Config } from '../lib/index.js'

/** A resolved plugin config, as dsh 0.1.5-rc.3 schemastery hands it to `apply`. */
function config(overrides = {}) {
  return {
    apiKeyEnv: 'JINA_API_KEY',
    baseURL: 'https://r.jina.ai',
    timeoutMs: 5_000,
    maxResponseBytes: 1_000_000,
    maxBodyChars: 1_000,
    userAgent: 'glink-harness-test/0.0.1',
    ...overrides,
  }
}

function envelope(content = 'page body') {
  return JSON.stringify({ code: 200, data: { url: 'https://93.184.216.34/', httpStatus: 200, content } })
}

/**
 * Apply the plugin with the global fetch replaced, so nothing in this suite can reach the
 * network even if a test regresses into dispatching. The provider captures the fake at
 * construction, exactly as the real one captures `globalThis.fetch`.
 */
async function withFakeFetch(fakeFetch, run) {
  const original = globalThis.fetch
  globalThis.fetch = fakeFetch
  try {
    return await run()
  } finally {
    globalThis.fetch = original
  }
}

/** Build a plugin context and apply the plugin; returns the registered provider and session events. */
function applyPlugin({ credentials, initiator, config: overrides } = {}) {
  const events = []
  let registered
  const ctx = {
    get(key) {
      if (key === 'credentials') return credentials
      if (key === 'agents') return initiator === undefined ? undefined : { currentInitiator: () => initiator }
      return undefined
    },
    web: {
      registerFetchProvider(provider) {
        registered = provider
      },
    },
  }
  apply(ctx, config(overrides))
  return { provider: registered, events }
}

test('the plugin declares its role and config surface', () => {
  assert.equal(name, 'web-fetch-jina')
  assert.deepEqual([...inject], ['web'])
  assert.equal(typeof Config, 'object')
})

test('apply registers the jina provider and resolves JINA_API_KEY through the credentials service', async () => {
  const seen = []
  const credentials = {
    async resolve(ref) {
      seen.push(ref)
      return ref === 'JINA_API_KEY' ? { value: 'credential-service-key' } : undefined
    },
  }
  const calls = []
  await withFakeFetch(async (url, init) => {
    calls.push({ url, init })
    return new Response(envelope(), { status: 200, headers: { 'content-type': 'application/json' } })
  }, async () => {
    const { provider } = applyPlugin({ credentials })
    assert.equal(provider.id, 'jina')
    const result = await provider.fetch({ url: 'https://93.184.216.34/' })
    assert.equal(result.statusCode, 200)
  })
  assert.deepEqual(seen, ['JINA_API_KEY'])
  assert.equal(calls[0].init.headers.authorization, 'Bearer credential-service-key')
})

test('apply falls back to the launch environment when no credentials service is mounted', async () => {
  const previous = process.env.JINA_API_KEY
  process.env.JINA_API_KEY = 'ambient-key'
  try {
    const calls = []
    await withFakeFetch(async (url, init) => {
      calls.push({ url, init })
      return new Response(envelope(), { status: 200, headers: { 'content-type': 'application/json' } })
    }, async () => {
      const { provider } = applyPlugin()
      await provider.fetch({ url: 'https://93.184.216.34/' })
    })
    assert.equal(calls[0].init.headers.authorization, 'Bearer ambient-key')
  } finally {
    if (previous === undefined) delete process.env.JINA_API_KEY
    else process.env.JINA_API_KEY = previous
  }
})

test('a missing credential fails the fetch explicitly with the configured reference name', async () => {
  const previous = process.env.JINA_API_KEY
  delete process.env.JINA_API_KEY
  try {
    let dispatched = 0
    const error = await withFakeFetch(async () => {
      dispatched += 1
      return new Response(envelope(), { status: 200, headers: { 'content-type': 'application/json' } })
    }, async () => {
      const { provider } = applyPlugin({ credentials: { async resolve() { return undefined } }, config: { apiKeyEnv: 'JINA_TEST_MISSING_KEY' } })
      return provider.fetch({ url: 'https://93.184.216.34/' }).then(
        () => undefined,
        caught => caught,
      )
    })
    assert.equal(dispatched, 0)
    assert.equal(error.code, 'WEB_PROVIDER_CREDENTIAL_MISSING')
    assert.match(error.message, /JINA_TEST_MISSING_KEY/)
  } finally {
    if (previous !== undefined) process.env.JINA_API_KEY = previous
  }
})

/**
 * dsh 0.1.5-rc.3 refuses to resume a session whose log holds an event type outside its
 * built-in vocabulary unless the envelope carries `ignorable: true`, and `session.append()`
 * has no way to set that marker. One out-of-tree event therefore made every later
 * `session/resume` of the conversation fail with -32603 (run 71779b32e35a). The bundle
 * must leave the durable session log to dsh's own vocabulary.
 */
test('a successful retrieval writes nothing into the durable session log', async () => {
  const appended = []
  const initiator = { session: { append: (type, data) => appended.push({ type, data }) } }
  const result = await withFakeFetch(
    async () => new Response(envelope('page body'), { status: 200, headers: { 'content-type': 'application/json' } }),
    async () => {
      const { provider } = applyPlugin({ credentials: { async resolve() { return { value: 'key' } } }, initiator })
      return provider.fetch({ url: 'https://93.184.216.34/' })
    },
  )
  assert.equal(result.statusCode, 200)
  assert.deepEqual(appended, [])
})

test('an invalid configuration fails at apply time instead of at the first fetch', () => {
  const ctx = { get: () => undefined, web: { registerFetchProvider() {} } }
  assert.throws(() => apply(ctx, config({ timeoutMs: 0 })), /timeoutMs/)
  assert.throws(() => apply(ctx, config({ apiKeyEnv: 'not a name' })), /apiKeyEnv/)
  assert.throws(() => apply(ctx, config({ baseURL: 'ftp://reader.example/' })), /baseURL/)
})
