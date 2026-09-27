/**
 * Stub of the `@deepseek-ai/dsh-web` seam surface the bundle imports.
 *
 * Unit tests run inside this repository, which has no dsh installation; the stub mirrors the
 * documented contract that matters here: `WebError` is an `Error` carrying a machine-routable
 * `code` and an optional `cause`. The real class is exercised by the Docker/E2B faces, whose
 * evidence is recorded in docs/acp-integration-notes.md §18.
 */
export class WebError extends Error {
  constructor(message, code, options) {
    super(message, options)
    this.name = 'WebError'
    this.code = code
  }
}
