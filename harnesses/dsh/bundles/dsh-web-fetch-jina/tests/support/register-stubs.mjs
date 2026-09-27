/**
 * Register the dsh seam stubs for this bundle's unit tests.
 * Usage: node --import ./harnesses/dsh/bundles/dsh-web-fetch-jina/tests/support/register-stubs.mjs --test …
 */
import { register } from 'node:module'

register('./stub-hooks.mjs', import.meta.url)
