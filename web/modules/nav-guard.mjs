/**
 * Navigation lifecycle guard.
 *
 * The shell can have several navigations in flight at once: a click on run A
 * whose `getRun` is slow, then a click on run B that answers first. Because the
 * responses arrive out of order, "am I still the navigation the reader asked
 * for?" cannot be answered by comparing run ids alone — B may not be current
 * yet when A lands, and a disposed workspace has no current run at all.
 *
 * The guard is a monotonic token: `begin()` starts a navigation and returns its
 * token, `invalidate()` retires whatever is in flight (called when leaving a
 * view or disposing), and `isCurrent(token)` tells a late response whether it
 * still owns the screen. It is deliberately DOM-free so Node tests can exercise
 * the race directly.
 */

export function createNavGuard() {
  let current = 0;
  return {
    begin() {
      current += 1;
      return current;
    },
    invalidate() {
      current += 1;
    },
    isCurrent(token) {
      return token === current;
    },
    get token() {
      return current;
    },
  };
}
