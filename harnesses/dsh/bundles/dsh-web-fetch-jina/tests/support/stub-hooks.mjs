/** Module-resolution hook mapping the dsh seam specifiers to this suite's stubs. */
const STUBS = new Map([
  ['@deepseek-ai/dsh-web', new URL('./stubs/dsh-web.mjs', import.meta.url).href],
  ['@deepseek-ai/dsh-timeout', new URL('./stubs/dsh-timeout.mjs', import.meta.url).href],
  ['@deepseek-ai/dsh-credentials', new URL('./stubs/dsh-credentials.mjs', import.meta.url).href],
  ['@deepseek-ai/dsh-launch-environment', new URL('./stubs/dsh-launch-environment.mjs', import.meta.url).href],
  ['@deepseek-ai/schemastery', new URL('./stubs/schemastery.mjs', import.meta.url).href],
])

export async function resolve(specifier, context, next) {
  const stub = STUBS.get(specifier)
  if (stub !== undefined) return { url: stub, shortCircuit: true }
  return next(specifier, context)
}
