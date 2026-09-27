/**
 * Stub of `@deepseek-ai/dsh-timeout`: `deadline` fuses an upstream signal with a timer whose
 * abort reason is a `TimeoutReason`, and `timeoutOf` recovers that reason for classification.
 * Mirrors the shipped package's observable contract (`packages/util/timeout/src/index.ts`).
 */
export class TimeoutReason extends Error {
  constructor(code, timeoutMs) {
    super(`${code} after ${timeoutMs}ms`)
    this.name = 'TimeoutReason'
    this.code = code
    this.timeoutMs = timeoutMs
  }
}

export function deadline(signal, timeoutMs, code) {
  const controller = new AbortController()
  if (signal?.aborted === true) controller.abort(signal.reason)
  const forward = () => controller.abort(signal.reason)
  signal?.addEventListener?.('abort', forward, { once: true })
  const timer = setTimeout(() => controller.abort(new TimeoutReason(code, timeoutMs)), timeoutMs)
  timer.unref?.()
  return {
    signal: controller.signal,
    [Symbol.dispose]() {
      clearTimeout(timer)
      signal?.removeEventListener?.('abort', forward)
    },
  }
}

export function timeoutOf(value, code) {
  const reason = value?.reason
  if (reason instanceof TimeoutReason && (code === undefined || reason.code === code)) return reason
  return undefined
}
