import { test } from 'node:test'
import assert from 'node:assert/strict'

import {
  MAX_URL_LENGTH,
  assertPublicDestination,
  assertPublicResolution,
  classifyJinaPayload,
  isNonPublicIpv4,
  isNonPublicIpv6,
  parseIpv6,
  validateFetchUrl,
} from '../lib/policy.js'

function codeOf(error) {
  return error?.code
}

async function rejection(promise) {
  try {
    await promise
  } catch (error) {
    return error
  }
  throw new Error('expected the promise to reject')
}

test('validateFetchUrl accepts http and https URLs', () => {
  assert.equal(validateFetchUrl('https://example.com/a?b=1').hostname, 'example.com')
  assert.equal(validateFetchUrl('http://example.com/').protocol, 'http:')
})

test('validateFetchUrl rejects a missing, non-string, or over-long URL', () => {
  assert.equal(codeOf(rejectionThrown(() => validateFetchUrl(undefined))), 'WEB_INVALID_URL')
  assert.equal(codeOf(rejectionThrown(() => validateFetchUrl('   '))), 'WEB_INVALID_URL')
  assert.equal(codeOf(rejectionThrown(() => validateFetchUrl('not a url'))), 'WEB_INVALID_URL')
  const tooLong = `https://example.com/${'a'.repeat(MAX_URL_LENGTH)}`
  assert.equal(codeOf(rejectionThrown(() => validateFetchUrl(tooLong))), 'WEB_INVALID_URL')
})

test('validateFetchUrl rejects non-http(s) protocols and embedded credentials', () => {
  assert.equal(codeOf(rejectionThrown(() => validateFetchUrl('file:///etc/passwd'))), 'WEB_INVALID_URL')
  assert.equal(codeOf(rejectionThrown(() => validateFetchUrl('ftp://example.com/x'))), 'WEB_INVALID_URL')
  assert.equal(codeOf(rejectionThrown(() => validateFetchUrl('https://user:pw@example.com/'))), 'WEB_INVALID_URL')
})

test('assertPublicDestination accepts public IPv4 and IPv6 literals', () => {
  for (const url of ['https://8.8.8.8/', 'https://93.184.216.34/', 'https://[2606:4700:4700::1111]/']) {
    assert.doesNotThrow(() => assertPublicDestination(validateFetchUrl(url)), url)
  }
})

test('assertPublicDestination refuses private IPv4 ranges', () => {
  const blocked = [
    'http://0.0.0.0/',
    'http://10.1.2.3/',
    'http://100.64.0.1/',
    'http://127.0.0.1/',
    'http://169.254.1.1/',
    'http://172.16.0.1/',
    'http://172.31.255.254/',
    'http://192.0.0.1/',
    'http://192.0.2.5/',
    'http://192.168.1.1/',
    'http://198.18.0.1/',
    'http://198.51.100.7/',
    'http://203.0.113.9/',
    'http://224.0.0.1/',
    'http://255.255.255.255/',
  ]
  for (const url of blocked) {
    assert.equal(codeOf(rejectionThrown(() => assertPublicDestination(validateFetchUrl(url)))), 'WEB_BLOCKED_URL', url)
  }
})

test('assertPublicDestination refuses private IPv6 ranges and mapped private IPv4', () => {
  const blocked = [
    'http://[::]/',
    'http://[::1]/',
    'http://[fc00::1]/',
    'http://[fd12:3456::1]/',
    'http://[fe80::1]/',
    'http://[fec0::1]/',
    'http://[ff02::1]/',
    'http://[100::1]/',
    'http://[64:ff9b::10.0.0.1]/',
    'http://[64:ff9b:1::1]/',
    'http://[::ffff:192.168.0.1]/',
    'http://[2001:db8::1]/',
    'http://[2001::1]/',
    'http://[2002:c0a8:0101::]/',
  ]
  for (const url of blocked) {
    assert.equal(codeOf(rejectionThrown(() => assertPublicDestination(validateFetchUrl(url)))), 'WEB_BLOCKED_URL', url)
  }
})

test('assertPublicDestination judges IPv4-mapped and NAT64 addresses by their embedded IPv4', () => {
  for (const url of ['http://[::ffff:93.184.216.34]/', 'http://[64:ff9b::5db8:d822]/']) {
    assert.doesNotThrow(() => assertPublicDestination(validateFetchUrl(url)), url)
  }
})

test('assertPublicDestination refuses local hostnames and normalizes numeric IPv4 spellings', () => {
  for (const url of ['http://localhost/', 'http://localhost./', 'http://app.localhost/', 'http://svc.local/', 'http://db.internal/', 'http://x.home.arpa/', 'http://wiki.lan/']) {
    assert.equal(codeOf(rejectionThrown(() => assertPublicDestination(validateFetchUrl(url)))), 'WEB_BLOCKED_URL', url)
  }
  // WHATWG URL normalizes decimal IPv4 to 127.0.0.1 before the range check.
  const decimal = validateFetchUrl('http://2130706433/')
  assert.equal(decimal.hostname, '127.0.0.1')
  assert.equal(codeOf(rejectionThrown(() => assertPublicDestination(decimal))), 'WEB_BLOCKED_URL')
})

test('isNonPublicIpv4 classifies public and private addresses', () => {
  assert.equal(isNonPublicIpv4('8.8.8.8'), false)
  assert.equal(isNonPublicIpv4('114.114.114.114'), false)
  assert.equal(isNonPublicIpv4('172.32.0.1'), false)
  assert.equal(isNonPublicIpv4('198.20.0.1'), false)
  assert.equal(isNonPublicIpv4('10.0.0.1'), true)
  assert.equal(isNonPublicIpv4('not-an-address'), true)
})

test('parseIpv6 handles compressed, embedded-IPv4, zoned, and invalid forms', () => {
  assert.deepEqual([...parseIpv6('::')], new Array(16).fill(0))
  assert.deepEqual([...parseIpv6('::1')].slice(0, 15), new Array(15).fill(0))
  assert.equal(parseIpv6('::1')[15], 1)
  assert.equal(parseIpv6('2001:db8::1')[0], 0x20)
  assert.equal(parseIpv6('2001:DB8::1')[2], 0x0d)
  assert.equal(parseIpv6('1:2:3:4:5:6:7:8')[15], 8)
  assert.deepEqual([...parseIpv6('::ffff:192.0.2.1')].slice(10), [0xff, 0xff, 0xc0, 0x00, 0x02, 0x01])
  assert.equal(parseIpv6('fe80::1%en0')[1], 0x80)
  assert.equal(parseIpv6('1:2:3:4:5:6:7'), undefined)
  assert.equal(parseIpv6('1::2::3'), undefined)
  assert.equal(parseIpv6('gggg::1'), undefined)
  assert.equal(parseIpv6('1:2:3:4:5:6:7:8:9'), undefined)
  assert.equal(parseIpv6('::ffff:300.0.2.1'), undefined)
})

test('isNonPublicIpv6 covers the documented non-public prefixes', () => {
  const blocked = ['::', '::1', 'fc00::1', 'fe80::1', 'ff05::1', '100::1', '2001:db8::1', '2001::1', '64:ff9b::10.0.0.1', '2002:c0a8:0101::']
  for (const text of blocked) {
    assert.equal(isNonPublicIpv6(parseIpv6(text)), true, text)
  }
  for (const text of ['2606:4700:4700::1111', '2001:4860:4860::8888', '::ffff:93.184.216.34', '2002:5db8:d822::']) {
    assert.equal(isNonPublicIpv6(parseIpv6(text)), false, text)
  }
})

test('assertPublicResolution refuses any non-public answer and tolerates resolution failure', async () => {
  const privateAnswer = await rejection(assertPublicResolution('example.com', async () => [
    { address: '93.184.216.34', family: 4 },
    { address: '10.0.0.7', family: 4 },
  ]))
  assert.equal(codeOf(privateAnswer), 'WEB_BLOCKED_URL')
  const mappedPrivate = await rejection(assertPublicResolution('example.com', async () => [
    { address: '::ffff:192.168.5.5', family: 6 },
  ]))
  assert.equal(codeOf(mappedPrivate), 'WEB_BLOCKED_URL')
  await assert.doesNotReject(assertPublicResolution('example.com', async () => [{ address: '93.184.216.34', family: 4 }]))
  await assert.doesNotReject(assertPublicResolution('example.com', async () => { throw new Error('ENOTFOUND') }))
  await assert.doesNotReject(assertPublicResolution('93.184.216.34', async () => { throw new Error('must not be called') }))
  await assert.doesNotReject(assertPublicResolution('example.com', undefined))
})

test('classifyJinaPayload keeps the target status and content', () => {
  const mapped = classifyJinaPayload({ code: 200, data: { url: 'https://example.com/x', httpStatus: 404, content: 'gone' } }, 'https://example.com/y')
  assert.deepEqual(mapped, { url: 'https://example.com/x', statusCode: 404, content: 'gone' })
})

test('classifyJinaPayload falls back on unusable envelope fields', () => {
  const mapped = classifyJinaPayload({ data: { url: 'not-a-url', httpStatus: 'oops', content: 'text' } }, 'https://example.com/y')
  assert.deepEqual(mapped, { url: 'https://example.com/y', statusCode: 200, content: 'text' })
  const noContent = rejectionThrown(() => classifyJinaPayload({ data: { content: 5 } }, 'https://example.com/'))
  assert.equal(codeOf(noContent), 'WEB_PROVIDER_ERROR')
  assert.equal(codeOf(rejectionThrown(() => classifyJinaPayload({}, 'https://example.com/'))), 'WEB_PROVIDER_ERROR')
  assert.equal(codeOf(rejectionThrown(() => classifyJinaPayload(null, 'https://example.com/'))), 'WEB_PROVIDER_ERROR')
})

function rejectionThrown(fn) {
  try {
    fn()
  } catch (error) {
    return error
  }
  throw new Error('expected the function to throw')
}
