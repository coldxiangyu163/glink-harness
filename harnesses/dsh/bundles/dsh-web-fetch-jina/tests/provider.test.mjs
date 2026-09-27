import { test } from 'node:test'
import assert from 'node:assert/strict'

import { JinaFetchProvider, JINA_FETCH_PROVIDER_ID } from '../lib/provider.js'
import { WebError } from '@deepseek-ai/dsh-web'

const BASE_OPTIONS = {
  apiKeyEnv: 'JINA_API_KEY',
  baseURL: 'https://r.jina.ai',
  timeoutMs: 5_000,
  maxResponseBytes: 1_000_000,
  maxBodyChars: 1_000,
  userAgent: 'pi-glink-test/0.0.1',
}

function options(overrides = {}) {
  return { ...BASE_OPTIONS, ...overrides }
}

function envelope(overrides = {}) {
  return {
    code: 200,
    status: 20000,
    data: {
      url: 'https://example.com/',
      httpStatus: 200,
      content: '# Example Domain\n\nBody',
      ...overrides,
    },
  }
}

function jsonResponse(payload, { status = 200, headers = {} } = {}) {
  return new Response(typeof payload === 'string' ? payload : JSON.stringify(payload), {
    status,
    headers: { 'content-type': 'application/json', ...headers },
  })
}

function makeProvider({ fetchImpl, lookup, resolveApiKey, resolveOptions, recordRetrieval } = {}) {
  const calls = []
  const provider = new JinaFetchProvider({
    resolveOptions: resolveOptions ?? (() => options()),
    resolveApiKey: resolveApiKey ?? (async () => 'test-key'),
    fetchImpl: fetchImpl ?? (async (url, init) => {
      calls.push({ url, init })
      return jsonResponse(envelope())
    }),
    lookup,
    recordRetrieval,
  })
  return { provider, calls }
}

async function rejection(promise) {
  try {
    await promise
  } catch (error) {
    return error
  }
  throw new Error('expected the promise to reject')
}

test('the provider registers under the jina id and is available', () => {
  const { provider } = makeProvider()
  assert.equal(provider.id, JINA_FETCH_PROVIDER_ID)
  assert.equal(provider.id, 'jina')
  assert.equal(provider.available(), true)
})

test('a successful fetch returns the seam result shape and posts the target to Jina', async () => {
  const { provider, calls } = makeProvider()
  const result = await provider.fetch({ url: 'https://example.com/a?b=1' })
  assert.deepEqual(result, {
    url: 'https://example.com/',
    statusCode: 200,
    body: { kind: 'text', content: '# Example Domain\n\nBody' },
    truncated: false,
  })
  assert.equal(calls.length, 1)
  assert.equal(calls[0].url, 'https://r.jina.ai/')
  assert.equal(calls[0].init.method, 'POST')
  assert.equal(calls[0].init.redirect, 'error')
  assert.equal(calls[0].init.body, 'url=https%3A%2F%2Fexample.com%2Fa%3Fb%3D1')
  assert.equal(calls[0].init.headers.authorization, 'Bearer test-key')
  assert.equal(calls[0].init.headers.accept, 'application/json')
  assert.equal(calls[0].init.headers['content-type'], 'application/x-www-form-urlencoded')
  assert.equal(calls[0].init.headers['user-agent'], 'pi-glink-test/0.0.1')
})

test('a base URL with a path keeps its path and gains one trailing slash', async () => {
  const { provider, calls } = makeProvider({ resolveOptions: () => options({ baseURL: 'https://reader.internal.example/api' }) })
  await provider.fetch({ url: 'https://example.com/' })
  assert.equal(calls[0].url, 'https://reader.internal.example/api/')
})

test('a target 404 stays a result, not an error (Jina reports the target status)', async () => {
  const { provider } = makeProvider({
    fetchImpl: async () => jsonResponse(envelope({ httpStatus: 404, content: 'not found' })),
  })
  const result = await provider.fetch({ url: 'https://example.com/missing' })
  assert.equal(result.statusCode, 404)
  assert.equal(result.body.content, 'not found')
  assert.equal(result.truncated, false)
})

test('a PDF target yields extracted text through the same result path', async () => {
  const pdfUrl = 'https://fi.mofcom.gov.cn/x.pdf?fileName=Negtive%20list.pdf'
  const pdfText = '— 1 —\n\n# 外商投资准入特别管理措施（负面清单）（2024 年版）\n\n说明\n'
  const calls = []
  const { provider } = makeProvider({
    fetchImpl: async (url, init) => {
      calls.push({ url, init })
      return jsonResponse(envelope({ url: pdfUrl, content: pdfText }))
    },
  })
  const result = await provider.fetch({ url: pdfUrl })
  assert.equal(result.statusCode, 200)
  assert.equal(result.body.kind, 'text')
  assert.match(result.body.content, /外商投资准入特别管理措施/)
  assert.equal(calls[0].init.body, `url=${encodeURIComponent(pdfUrl)}`)
})

test('the character cap truncates the decoded content and flags it', async () => {
  const content = 'x'.repeat(1_500)
  const { provider } = makeProvider({ fetchImpl: async () => jsonResponse(envelope({ content })) })
  const result = await provider.fetch({ url: 'https://example.com/' })
  assert.equal(result.truncated, true)
  assert.equal(result.body.content.length, 1_000)
})

test('a declared body over the byte cap fails as WEB_FETCH_TOO_LARGE', async () => {
  const { provider } = makeProvider({
    fetchImpl: async () => jsonResponse(envelope(), { headers: { 'content-length': '9999999' } }),
  })
  const error = await rejection(provider.fetch({ url: 'https://example.com/' }))
  assert.ok(error instanceof WebError)
  assert.equal(error.code, 'WEB_FETCH_TOO_LARGE')
})

test('a streamed body over the byte cap fails as WEB_FETCH_TOO_LARGE', async () => {
  const { provider } = makeProvider({
    fetchImpl: async () => jsonResponse(envelope({ content: 'y'.repeat(2_000_000) })),
    resolveOptions: () => options({ maxResponseBytes: 4_096 }),
  })
  const error = await rejection(provider.fetch({ url: 'https://example.com/' }))
  assert.equal(error.code, 'WEB_FETCH_TOO_LARGE')
})

test('a body that is not JSON fails as WEB_PROVIDER_ERROR', async () => {
  const { provider } = makeProvider({ fetchImpl: async () => jsonResponse('not json at all') })
  const error = await rejection(provider.fetch({ url: 'https://example.com/' }))
  assert.equal(error.code, 'WEB_PROVIDER_ERROR')
})

test('an envelope without content fails as WEB_PROVIDER_ERROR', async () => {
  const { provider } = makeProvider({ fetchImpl: async () => jsonResponse({ code: 200, data: { httpStatus: 200 } }) })
  const error = await rejection(provider.fetch({ url: 'https://example.com/' }))
  assert.equal(error.code, 'WEB_PROVIDER_ERROR')
})

test('provider HTTP failures map to WebError with an actionable message', async () => {
  const cases = [
    [401, /401/, /JINA_API_KEY/],
    [403, /403/, /JINA_API_KEY/],
    [402, /402/, /quota/i],
    [429, /429/, /rate limit/i],
    [422, /422/, /process/i],
    [500, /500/, /HTTP 500/],
    [503, /503/, /HTTP 503/],
  ]
  for (const [status, statusPattern, advicePattern] of cases) {
    const { provider } = makeProvider({
      fetchImpl: async () => new Response(`upstream failure ${status}`, { status, headers: { 'content-type': 'text/plain' } }),
    })
    const error = await rejection(provider.fetch({ url: 'https://example.com/' }))
    assert.ok(error instanceof WebError, `status ${status}`)
    assert.equal(error.code, 'WEB_PROVIDER_ERROR', `status ${status}`)
    assert.match(error.message, statusPattern)
    assert.match(error.message, advicePattern)
  }
})

test('a 429 keeps the response snippet bounded and sanitized', async () => {
  const { provider } = makeProvider({
    fetchImpl: async () => new Response(`rate limited\n   ${'z'.repeat(5_000)}`, { status: 429 }),
  })
  const error = await rejection(provider.fetch({ url: 'https://example.com/' }))
  assert.ok(error.message.length < 600)
  assert.match(error.message, /z+…$/)
})

test('the provider deadline reports WEB_FETCH_TIMEOUT', async () => {
  const hanging = (url, init) => new Promise((resolve, reject) => {
    const fail = () => reject(Object.assign(new Error('aborted'), { name: 'AbortError' }))
    if (init?.signal?.aborted === true) fail()
    else init?.signal?.addEventListener?.('abort', fail, { once: true })
  })
  const { provider } = makeProvider({ fetchImpl: hanging, resolveOptions: () => options({ timeoutMs: 25 }) })
  const error = await rejection(provider.fetch({ url: 'https://example.com/' }))
  assert.equal(error.code, 'WEB_FETCH_TIMEOUT')
})

test('caller cancellation reports WEB_ABORTED and stops the request', async () => {
  let sawSignal
  const hanging = (url, init) => new Promise((resolve, reject) => {
    sawSignal = init?.signal
    const fail = () => reject(Object.assign(new Error('aborted'), { name: 'AbortError' }))
    if (init?.signal?.aborted === true) fail()
    else init?.signal?.addEventListener?.('abort', fail, { once: true })
  })
  const { provider } = makeProvider({ fetchImpl: hanging })
  const controller = new AbortController()
  const pending = provider.fetch({ url: 'https://example.com/' }, controller.signal)
  await new Promise(resolve => setTimeout(resolve, 5))
  controller.abort()
  const error = await rejection(pending)
  assert.equal(error.code, 'WEB_ABORTED')
  assert.equal(sawSignal.aborted, true)
})

test('an already-aborted caller signal never dispatches a request', async () => {
  let called = 0
  const { provider } = makeProvider({ fetchImpl: async () => { called += 1; return jsonResponse(envelope()) } })
  const controller = new AbortController()
  controller.abort()
  const error = await rejection(provider.fetch({ url: 'https://example.com/' }, controller.signal))
  assert.equal(error.code, 'WEB_ABORTED')
  assert.equal(called, 0)
})

test('a missing credential fails as WEB_PROVIDER_CREDENTIAL_MISSING before dispatch', async () => {
  let called = 0
  const { provider } = makeProvider({
    resolveApiKey: async () => undefined,
    fetchImpl: async () => { called += 1; return jsonResponse(envelope()) },
  })
  const error = await rejection(provider.fetch({ url: 'https://example.com/' }))
  assert.equal(error.code, 'WEB_PROVIDER_CREDENTIAL_MISSING')
  assert.match(error.message, /JINA_API_KEY/)
  assert.equal(called, 0)
})

test('credential resolution failure is a WebError, never a raw rejection', async () => {
  const { provider } = makeProvider({ resolveApiKey: async () => { throw new Error('credentials store offline') } })
  const error = await rejection(provider.fetch({ url: 'https://example.com/' }))
  assert.equal(error.code, 'WEB_PROVIDER_ERROR')
})

test('non-http(s) and non-public destinations are refused before any request reaches Jina', async () => {
  const blocked = [
    ['file:///etc/passwd', 'WEB_INVALID_URL'],
    ['ftp://example.com/x', 'WEB_INVALID_URL'],
    ['http://127.0.0.1/', 'WEB_BLOCKED_URL'],
    ['http://10.1.2.3/', 'WEB_BLOCKED_URL'],
    ['http://[::1]/', 'WEB_BLOCKED_URL'],
    ['http://localhost/', 'WEB_BLOCKED_URL'],
    ['http://service.internal/', 'WEB_BLOCKED_URL'],
  ]
  for (const [url, expected] of blocked) {
    let called = 0
    const { provider } = makeProvider({ fetchImpl: async () => { called += 1; return jsonResponse(envelope()) } })
    const error = await rejection(provider.fetch({ url }))
    assert.equal(error.code, expected, url)
    assert.equal(called, 0, url)
  }
})

test('a hostname that resolves to a private address is refused, and a resolver failure is tolerated', async () => {
  let called = 0
  const { provider: blockedProvider } = makeProvider({
    lookup: async () => [{ address: '10.9.8.7', family: 4 }],
    fetchImpl: async () => { called += 1; return jsonResponse(envelope()) },
  })
  const error = await rejection(blockedProvider.fetch({ url: 'https://example.com/' }))
  assert.equal(error.code, 'WEB_BLOCKED_URL')
  assert.equal(called, 0)

  const { provider: tolerantProvider } = makeProvider({
    lookup: async () => { throw new Error('EAI_AGAIN') },
    fetchImpl: async () => jsonResponse(envelope()),
  })
  const result = await tolerantProvider.fetch({ url: 'https://example.com/' })
  assert.equal(result.statusCode, 200)
})

test('an unexpected transport failure becomes a WebError, not the original TypeError', async () => {
  const { provider } = makeProvider({ fetchImpl: async () => { throw new TypeError('fetch is not a function') } })
  const error = await rejection(provider.fetch({ url: 'https://example.com/' }))
  assert.ok(error instanceof WebError)
  assert.equal(error.code, 'WEB_PROVIDER_ERROR')
  assert.match(error.message, /fetch is not a function/)
})

test('an invalid live configuration fails as WEB_PROVIDER_ERROR instead of throwing raw', async () => {
  const { provider } = makeProvider({ resolveOptions: () => options({ timeoutMs: 0 }) })
  const error = await rejection(provider.fetch({ url: 'https://example.com/' }))
  assert.equal(error.code, 'WEB_PROVIDER_ERROR')
  assert.match(error.message, /timeoutMs/)
})

test('one successful retrieval is recorded once, and a throwing audit hook cannot fail it', async () => {
  const records = []
  const { provider } = makeProvider({ recordRetrieval: record => records.push(record) })
  await provider.fetch({ url: 'https://example.com/' })
  assert.deepEqual(records, [{
    url: 'https://example.com/',
    endpoint: 'https://r.jina.ai/',
    statusCode: 200,
    truncated: false,
    chars: '# Example Domain\n\nBody'.length,
  }])

  const { provider: throwing } = makeProvider({ recordRetrieval: () => { throw new Error('session append failed') } })
  const result = await throwing.fetch({ url: 'https://example.com/' })
  assert.equal(result.statusCode, 200)
})
