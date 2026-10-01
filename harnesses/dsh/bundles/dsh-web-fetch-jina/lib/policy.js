/**
 * Retrieval policy for the Jina Reader fetch provider: URL validation, public-destination
 * enforcement, and the mapping from Jina's JSON envelope to the `ctx.web` fetch-result shape.
 *
 * The provider never connects to the target itself — Jina does — but an internal or
 * non-public destination must not leave the process either: the URL is the payload we hand
 * to a third party, so it is validated and refused here before any request is sent.
 * @module @glink-harness/dsh-web-fetch-jina/policy
 */

import { isIP } from 'node:net'
import { WebError } from '@deepseek-ai/dsh-web'

/** URL security limit, identical to the shipped `dsh-web-fetch-http` limit. */
export const MAX_URL_LENGTH = 2048

/** Hostnames that never name a public destination, matched after lowercasing. */
const BLOCKED_HOST_NAMES = new Set(['localhost'])

/** Hostname suffixes that never name a public destination. */
const BLOCKED_HOST_SUFFIXES = ['.localhost', '.local', '.internal', '.home.arpa', '.lan', '.intranet', '.corp']

/**
 * Validate one caller-supplied URL for the request to Jina.
 *
 * Accepts only absolute `http:`/`https:` URLs without embedded credentials. The WHATWG URL
 * parser normalizes alternative IPv4 spellings (decimal/octal/hex) to dotted-quad first, so
 * the literal checks in {@link assertPublicDestination} cannot be bypassed by `2130706433`.
 *
 * @param raw - the request URL, as the model or a direct service caller supplied it.
 * @returns the parsed URL.
 * @throws {WebError} `WEB_INVALID_URL` for a missing, over-long, credentialed, or non-http(s) URL.
 */
export function validateFetchUrl(raw) {
  if (typeof raw !== 'string' || raw.trim().length === 0) {
    throw new WebError('web fetch URL must be a non-empty string', 'WEB_INVALID_URL')
  }
  if (raw.length > MAX_URL_LENGTH) {
    throw new WebError(`web fetch URL exceeds the maximum of ${MAX_URL_LENGTH} characters`, 'WEB_INVALID_URL')
  }
  let url
  try {
    url = new URL(raw)
  } catch (error) {
    throw new WebError(`web fetch URL is not a valid absolute URL: ${JSON.stringify(raw)}`, 'WEB_INVALID_URL', { cause: error })
  }
  if (url.protocol !== 'http:' && url.protocol !== 'https:') {
    throw new WebError(`web fetch supports only http: and https: URLs, got ${JSON.stringify(url.protocol)}`, 'WEB_INVALID_URL')
  }
  if (url.username.length > 0 || url.password.length > 0) {
    throw new WebError('web fetch URL must not embed credentials', 'WEB_INVALID_URL')
  }
  if (url.hostname.length === 0) {
    throw new WebError('web fetch URL must name a host', 'WEB_INVALID_URL')
  }
  return url
}

/**
 * Refuse a URL whose destination is not a public internet host, before it is ever sent to
 * Jina. IP literals are checked by range; hostnames are checked against the reserved name
 * sets that always mean "this machine or this network". A hostname that only *resolves*
 * to a private address is refused by {@link assertPublicResolution}.
 *
 * @param url - a URL accepted by {@link validateFetchUrl}.
 * @throws {WebError} `WEB_BLOCKED_URL` when the destination is not public.
 */
export function assertPublicDestination(url) {
  const literal = bareHostname(url.hostname)
  const family = isIP(literal)
  if (family === 4) {
    if (isNonPublicIpv4(literal)) throw blockedUrl(url, 'a non-public IPv4 address')
    return
  }
  if (family === 6) {
    const bytes = parseIpv6(literal)
    if (bytes === undefined || isNonPublicIpv6(bytes)) throw blockedUrl(url, 'a non-public IPv6 address')
    return
  }
  const name = literal.toLowerCase().replace(/\.$/, '')
  if (BLOCKED_HOST_NAMES.has(name) || BLOCKED_HOST_SUFFIXES.some(suffix => name.endsWith(suffix))) {
    throw blockedUrl(url, 'a private or local hostname')
  }
}

/**
 * Best-effort resolution guard: when a local resolver is available, resolve the target once
 * and refuse the request if any answer is non-public. The provider does not connect with
 * these addresses (Jina does), so this is purely an egress check; a resolution failure is
 * *not* fatal — Jina may resolve a name this sandbox cannot, and the literal/name checks
 * above already ran.
 *
 * @param hostname - the URL hostname (an IP literal is already checked and skipped here).
 * @param lookup - Node-style `dns.lookup`-compatible resolver returning `{address, family}[]`.
 * @throws {WebError} `WEB_BLOCKED_URL` when any resolved address is non-public.
 */
export async function assertPublicResolution(hostname, lookup) {
  const literal = bareHostname(hostname)
  if (isIP(literal) !== 0 || lookup === undefined) return
  let records
  try {
    records = await lookup(literal, { all: true, verbatim: true })
  } catch {
    // Unresolvable here (offline resolver, split-horizon mismatch): let Jina decide. The
    // request is still bounded by the literal/name policy above.
    return
  }
  if (!Array.isArray(records)) return
  for (const record of records) {
    const address = record?.address
    if (typeof address !== 'string' || address.length === 0) continue
    const family = record.family === 4 || record.family === 6 ? record.family : isIP(address)
    if (family === 4 && isNonPublicIpv4(address)) {
      throw new WebError(`web fetch blocked: ${literal} resolves to the non-public address ${address}`, 'WEB_BLOCKED_URL')
    }
    if (family === 6) {
      const bytes = parseIpv6(address)
      if (bytes === undefined || isNonPublicIpv6(bytes)) {
        throw new WebError(`web fetch blocked: ${literal} resolves to the non-public address ${address}`, 'WEB_BLOCKED_URL')
      }
    }
  }
}

/**
 * Map Jina Reader's JSON envelope to the seam's fetch-result facts.
 *
 * Jina answers `200` for target failures too and reports the target's own status in
 * `data.httpStatus` (verified against `r.jina.ai` directly for both a 200 and a 404 target),
 * so retrieval success and resource status stay separate exactly as the seam requires.
 *
 * @param payload - the parsed JSON body.
 * @param fallbackUrl - the requested URL, used when the envelope carries no usable `data.url`.
 * @returns `{ url, statusCode, content }`.
 * @throws {WebError} `WEB_PROVIDER_ERROR` when the envelope does not carry text content.
 */
export function classifyJinaPayload(payload, fallbackUrl) {
  if (payload === null || typeof payload !== 'object' || Array.isArray(payload)) {
    throw new WebError('Jina reader response is not a JSON object', 'WEB_PROVIDER_ERROR')
  }
  const data = payload.data
  if (data === null || typeof data !== 'object' || Array.isArray(data)) {
    throw new WebError('Jina reader response carries no data object', 'WEB_PROVIDER_ERROR')
  }
  if (typeof data.content !== 'string') {
    throw new WebError('Jina reader response carries no text content', 'WEB_PROVIDER_ERROR')
  }
  const statusCode = Number.isInteger(data.httpStatus) && data.httpStatus >= 100 && data.httpStatus <= 599
    ? data.httpStatus
    : 200
  let url = fallbackUrl
  if (typeof data.url === 'string' && data.url.length > 0) {
    try {
      const parsed = new URL(data.url)
      if (parsed.protocol === 'http:' || parsed.protocol === 'https:') url = parsed.toString()
    } catch {
      // A malformed echo must not invalidate an otherwise usable retrieval.
    }
  }
  return { url, statusCode, content: data.content }
}

/** Strip the brackets a WHATWG URL hostname keeps around an IPv6 literal. */
function bareHostname(hostname) {
  return hostname.startsWith('[') && hostname.endsWith(']') ? hostname.slice(1, -1) : hostname
}

/** Build the shared refusal for one non-public destination. */
function blockedUrl(url, reason) {
  return new WebError(`web fetch blocked: ${url.hostname} is ${reason}`, 'WEB_BLOCKED_URL')
}

/**
 * Whether a dotted-quad IPv4 address belongs to a range that is not a public internet host.
 * An unparseable address counts as non-public.
 * @param address - dotted-quad text.
 * @returns true when the address must not be requested.
 */
export function isNonPublicIpv4(address) {
  const parts = String(address).split('.')
  if (parts.length !== 4) return true
  const octets = []
  for (const part of parts) {
    if (!/^\d{1,3}$/.test(part)) return true
    const value = Number(part)
    if (value > 255) return true
    octets.push(value)
  }
  const [a, b, c] = octets
  if (a === 0 || a === 10 || a === 127) return true
  if (a === 100 && b >= 64 && b <= 127) return true
  if (a === 169 && b === 254) return true
  if (a === 172 && b >= 16 && b <= 31) return true
  if (a === 192 && b === 0 && (c === 0 || c === 2)) return true
  if (a === 192 && b === 88 && c === 99) return true
  if (a === 192 && b === 168) return true
  if (a === 198 && (b === 18 || b === 19)) return true
  if (a === 198 && b === 51 && c === 100) return true
  if (a === 203 && b === 0 && c === 113) return true
  if (a >= 224) return true
  return false
}

/**
 * Whether a 16-byte IPv6 address belongs to a range that is not a public internet host.
 * IPv4-mapped, IPv4-compatible, and NAT64 addresses are judged by their embedded IPv4
 * address, so a mapped public v4 answer from a resolver is not refused.
 * @param bytes - the 16 address bytes.
 * @returns true when the address must not be requested.
 */
export function isNonPublicIpv6(bytes) {
  if (bytes.length !== 16) return true
  if (allZero(bytes, 0, 12)) {
    // ::/96 — unspecified, loopback (::1), or IPv4-compatible: judge the embedded address.
    return isNonPublicIpv4FromBytes(bytes, 12)
  }
  if (allZero(bytes, 0, 10) && bytes[10] === 0xff && bytes[11] === 0xff) {
    return isNonPublicIpv4FromBytes(bytes, 12)
  }
  if (bytes[0] === 0x00 && bytes[1] === 0x64 && bytes[2] === 0xff && bytes[3] === 0x9b) {
    // 64:ff9b::/96 (well-known NAT64) and 64:ff9b:1::/48 (local-use NAT64).
    if (allZero(bytes, 4, 12)) return isNonPublicIpv4FromBytes(bytes, 12)
    if (bytes[4] === 0x00 && bytes[5] === 0x01 && allZero(bytes, 6, 16)) return true
    return true
  }
  if ((bytes[0] & 0xfe) === 0xfc) return true // fc00::/7 unique local
  if (bytes[0] === 0xfe && (bytes[1] & 0xc0) === 0x80) return true // fe80::/10 link local
  if (bytes[0] === 0xfe && (bytes[1] & 0xc0) === 0xc0) return true // fec0::/10 site local (deprecated)
  if (bytes[0] === 0xff) return true // ff00::/8 multicast
  if (bytes[0] === 0x01 && allZero(bytes, 1, 4)) return true // 100::/64 discard-only
  if (bytes[0] === 0x20 && bytes[1] === 0x01 && bytes[2] === 0x0d && bytes[3] === 0xb8) return true // 2001:db8::/32
  if (bytes[0] === 0x20 && bytes[1] === 0x01 && allZero(bytes, 2, 4)) return true // 2001::/32 Teredo
  if (bytes[0] === 0x20 && bytes[1] === 0x02) return isNonPublicIpv4FromBytes(bytes, 2) // 2002::/16 6to4
  if (bytes[0] === 0x00) return true // other ::/8
  return false
}

/**
 * Parse one IPv6 literal (with optional zone, embedded dotted-quad allowed) into bytes.
 * @param text - the literal, without brackets.
 * @returns 16 bytes, or `undefined` when the text is not a valid IPv6 address.
 */
export function parseIpv6(text) {
  let value = String(text)
  const zone = value.indexOf('%')
  if (zone !== -1) value = value.slice(0, zone)
  if (value.length === 0 || !/^[0-9a-fA-F:.]+$/.test(value)) return undefined
  const doubleColon = value.indexOf('::')
  if (doubleColon !== value.lastIndexOf('::')) return undefined
  let headText = value
  let tailText = ''
  if (doubleColon !== -1) {
    headText = value.slice(0, doubleColon)
    tailText = value.slice(doubleColon + 2)
  }
  const head = parseIpv6Groups(headText)
  const tail = parseIpv6Groups(tailText)
  if (head === undefined || tail === undefined) return undefined
  let groups
  if (doubleColon === -1) {
    if (head.length !== 8) return undefined
    groups = head
  } else {
    if (head.length + tail.length > 7) return undefined
    groups = [...head, ...new Array(8 - head.length - tail.length).fill(0), ...tail]
  }
  const bytes = new Uint8Array(16)
  for (let index = 0; index < 8; index += 1) {
    bytes[index * 2] = (groups[index] >> 8) & 0xff
    bytes[index * 2 + 1] = groups[index] & 0xff
  }
  return bytes
}

/** Parse one side of an IPv6 literal into 16-bit group values; the dotted-quad tail counts as two. */
function parseIpv6Groups(part) {
  if (part === '') return []
  const raw = part.split(':')
  const groups = []
  for (let index = 0; index < raw.length; index += 1) {
    const group = raw[index]
    if (group.includes('.')) {
      if (index !== raw.length - 1) return undefined
      const octets = group.split('.')
      if (octets.length !== 4) return undefined
      const values = []
      for (const octet of octets) {
        if (!/^\d{1,3}$/.test(octet)) return undefined
        const value = Number(octet)
        if (value > 255) return undefined
        values.push(value)
      }
      groups.push((values[0] << 8) | values[1], (values[2] << 8) | values[3])
    } else {
      if (!/^[0-9a-fA-F]{1,4}$/.test(group)) return undefined
      groups.push(parseInt(group, 16))
    }
  }
  return groups
}

/** Judge an embedded IPv4 address starting at a byte offset. */
function isNonPublicIpv4FromBytes(bytes, offset) {
  return isNonPublicIpv4(`${bytes[offset]}.${bytes[offset + 1]}.${bytes[offset + 2]}.${bytes[offset + 3]}`)
}

/** Whether every byte in `[from, to)` is zero. */
function allZero(bytes, from, to) {
  for (let index = from; index < to; index += 1) {
    if (bytes[index] !== 0) return false
  }
  return true
}
