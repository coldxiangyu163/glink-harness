/**
 * Stub of `@deepseek-ai/schemastery` covering only the schema chain the plugin builds at
 * module load (`z.object`, `z.string()/z.number()` with `.role().default().volatile()`).
 * Tests pass their own resolved config objects, so no validation/default behavior is needed.
 */
function field() {
  const self = {
    role: () => self,
    default: () => self,
    volatile: () => self,
    get: () => undefined,
  }
  return self
}

export default {
  object: shape => ({ shape }),
  string: field,
  number: field,
}
