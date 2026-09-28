/**
 * Run event subscription: SSE transport → reducer.
 *
 * Two layers, neither of which renders:
 *
 * * `createRunSubscription` — one run's `EventSource` with bounded retry and
 *   status reporting; it parses frames and hands them on, nothing more. The
 *   named frames it listens for are the schema's vocabulary
 *   (`event-schema.gen.mjs`), not a hand-kept list.
 * * `createRunFeed` — a subscription folded through the pure reducer
 *   (`run-reducer.mjs`): it owns the run's current state, notifies the view
 *   with every new state, and closes the stream on the terminal *frame* (a
 *   finished run starts at a terminal status, so closing on the state's status
 *   would end a history replay after its first frame).
 */

import { EVENT_TYPES, TERMINAL_EVENT_TYPES } from './event-schema.gen.mjs';
import { createRunState, reduce } from './run-reducer.mjs';

/**
 * Bounded reconnect delays for a stream the server refused outright.
 *
 * `EventSource` reconnects network drops by itself, but a non-200 answer — an
 * unknown run, or the host's `429 stream_limit` (whose JSON body the browser
 * cannot read) — closes it for good. The refusal is temporary by nature, so the
 * subscription retries a few times before reporting `failed`; it must never
 * spin forever on a run that really is gone.
 */
export const STREAM_RETRY_DELAYS_MS = [1500, 4000, 10000];

/** Subscribe to a run's SSE stream with reconnect-aware status reporting. */
export function createRunSubscription({
  runId,
  eventsUrl,
  eventSourceFactory = (url) => new EventSource(url),
  onEvent,
  onStatus,
  onError,
  retryDelaysMs = STREAM_RETRY_DELAYS_MS,
  schedule = (callback, delayMs) => setTimeout(callback, delayMs),
  cancelSchedule = (handle) => clearTimeout(handle),
}) {
  let source = null;
  let closed = false;
  let status = 'idle';
  let attempt = 0;
  let retryHandle = null;

  const report = (nextStatus) => {
    if (closed || status === nextStatus) return;
    status = nextStatus;
    onStatus?.(nextStatus);
  };

  const deliver = (message) => {
    try {
      onEvent?.(JSON.parse(message.data));
    } catch (error) {
      onError?.(error);
    }
  };

  const open = () => {
    report('connecting');
    source = eventSourceFactory(eventsUrl(runId));
    source.onopen = () => {
      attempt = 0;
      report('live');
    };
    source.onerror = () => {
      // readyState 2 (CLOSED) means the connection was failed rather than
      // dropped: retry a bounded number of times, then report the failure.
      if (source?.readyState !== 2) {
        report('reconnecting');
        return;
      }
      if (attempt >= retryDelaysMs.length) {
        report('failed');
        return;
      }
      const delay = retryDelaysMs[attempt];
      attempt += 1;
      report('reconnecting');
      retryHandle = schedule(() => {
        retryHandle = null;
        if (!closed) open();
      }, delay);
    };
    source.onmessage = deliver;
    // Named frames (`event: <type>`) do not reach onmessage; listen explicitly.
    for (const type of EVENT_TYPES) source.addEventListener(type, deliver);
  };

  open();

  return {
    get status() {
      return status;
    },
    close() {
      closed = true;
      if (retryHandle !== null) {
        cancelSchedule(retryHandle);
        retryHandle = null;
      }
      source?.close();
      source = null;
      status = 'closed';
    },
  };
}

/**
 * One run's live state: subscribe, reduce, notify, close on the terminal frame.
 *
 * `onState(state, event)` runs after every applied frame; `onTerminal(state)`
 * once, after the terminal frame's state was delivered and the stream closed.
 * `initialState` (default: `createRunState(run)`) is what the owner already
 * rendered before the stream opened.
 * `isActive()` lets the owner drop late frames from a run it no longer shows.
 */
export function createRunFeed({
  run,
  initialState = createRunState(run),
  eventsUrl,
  eventSourceFactory,
  onState,
  onTerminal,
  onStatus,
  onError,
  isActive = () => true,
  retryDelaysMs,
  schedule,
  cancelSchedule,
}) {
  let state = initialState;
  let frames = 0;
  const subscription = createRunSubscription({
    runId: run.runId,
    eventsUrl,
    eventSourceFactory,
    retryDelaysMs,
    schedule,
    cancelSchedule,
    onEvent: (event) => {
      if (!isActive()) return;
      frames += 1;
      state = reduce(state, event);
      onState?.(state, event);
      if (event && TERMINAL_EVENT_TYPES.has(event.type)) {
        subscription.close();
        onTerminal?.(state);
      }
    },
    onStatus: (status) => {
      if (!isActive()) return;
      onStatus?.(status);
    },
    onError,
  });
  return {
    get state() {
      return state;
    },
    get status() {
      return subscription.status;
    },
    /** Frames delivered to the reducer (applied, ignored or rejected). */
    get frames() {
      return frames;
    },
    close() {
      subscription.close();
    },
  };
}
