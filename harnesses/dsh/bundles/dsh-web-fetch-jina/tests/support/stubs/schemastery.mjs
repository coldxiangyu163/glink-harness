/**
 * Stub of `@deepseek-ai/schemastery` covering only the schema chain the plugin builds at
 * module load (`z.object`, `z.string()/z.number()` with `.role().default()`).
 *
 * The chain deliberately has NO `.volatile()`: the pinned dsh 0.1.5-rc.3 ships schemastery
 * 3.18.2, which has no volatile field kind (that API arrived with 0.1.7). Calling it here
 * makes the bundle tests red instead of the image boot — the failure actually observed when
 * an earlier revision of this bundle used it.
 */
function field() {
  const self = {
    role: () => self,
    default: () => self,
  }
  return self
}

export default {
  object: shape => ({ shape }),
  string: field,
  number: field,
}
