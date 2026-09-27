/**
 * Stub of `@deepseek-ai/dsh-launch-environment`: the launcher's immutable environment
 * snapshot, falling back to `process.env` when the composition carries no snapshot service.
 */
export function launchEnvironmentOf(ctx) {
  const snapshot = ctx?.get?.('launchEnvironment')
  if (snapshot !== undefined) return snapshot
  return {
    get(name) {
      const value = process.env[name]
      return value === undefined ? undefined : { value }
    },
  }
}
