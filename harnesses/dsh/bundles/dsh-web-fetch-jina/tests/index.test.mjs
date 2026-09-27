import { test } from 'node:test'
import assert from 'node:assert/strict'

import { name, inject, apply, Config } from '../lib/index.js'

/** A resolved plugin config, standing in for the schemastery volatile fields. */
function config(overrides = {}) {
  const values = {
    apiKeyEnv: 'JINA_API_KEY',
    baseURL: 'https://r.jina.ai',
    timeoutMs: 5_000,
    maxResponseBytes: 1_000_000,
    maxBodyChars: 1_000,
    userAgent: 'pi-glink-test/0.0.1',
    ...overrides,
  }
  return Object.fromEntries(Object.entries(values).map(([key, value]) => [key, { get: () => value }]))
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
function applyPlugin({ credentials, initiator } = {}) {
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
  apply(ctx, config())
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
      const { provider } = applyPlugin({ credentials: { async resolve() { return undefined } } })
      return provider.fetch({ url: 'https://93.184.216.34/' }).then(
        () => undefined,
        caught => caught,
      )
    })
    assert.equal(dispatched, 0)
    assert.equal(error.code, 'WEB_PROVIDER_CREDENTIAL_MISSING')
    assert.match(error.message, /JINA_API_KEY/)
  } finally {
    if (previous !== undefined) process.env.JINA_API_KEY = previous
  }
})

test('one successful retrieval appends the audit event with the provider facts', async () => {
  const initiator = { session: { append: () => {} } }
  const events = []
  initiator.session.append = (type, data) => events.push({ type, data })
  await withFakeFetch(
    async () => new Response(envelope('audited body'), { status: 200, headers: { 'content-type': 'application/json' } }),
    async () => {
      const { provider } = applyPlugin({ credentials: { async resolve() { return { value: 'key' } } }, initiator })
      await provider.fetch({ url: 'https://93.184.216.34/' })
    },
  )
  assert.equal(events.length, 1)
  assert.equal(events[0].type, 'web/jina-fetch-request')
  assert.deepEqual(events[0].data, {
    url: 'https://93.184.216.34/',
    endpoint: 'https://r.jina.ai/',
    statusCode: 200,
    truncated: false,
    chars: 'audited body'.length,
  })
})

test('an invalid configuration fails at apply time instead of at the first fetch', () => {
  const ctx = { get: () => undefined, web: { registerFetchProvider() {} } }
  assert.throws(() => apply(ctx, config({ timeoutMs: 0 })), /timeoutMs/)
  assert.throws(() => apply(ctx, config({ apiKeyEnv: 'not a name' })), /apiKeyEnv/)
  assert.throws(() => apply(ctx, config({ baseURL: 'ftp://reader.example/' })), /baseURL/)
})
