/**
 * Session lifecycle at the workbench boundary:
 *   · nothing is revealed until `GET /api/me` answers 200;
 *   · each identity mode gets its own unauthenticated face;
 *   · a 401 mid-session closes the stream, clears the screen and returns to login;
 *   · logout revokes the server session before the screen changes.
 *
 * The DOM is the stub (no layout), the API is controllable, and the auth view is
 * a recording double — `auth-view.test.mjs` covers the view itself.
 */

import { test } from 'node:test';
import assert from 'node:assert/strict';

import { createWorkspace } from '../../web/modules/workspace.mjs';
import { ApiError, getCsrfToken, setCsrfToken } from '../../web/modules/api.mjs';
import { EVENT_VERSIONS } from '../../web/modules/event-schema.gen.mjs';
import { createDomStub } from './dom-stub.mjs';

const tick = () => new Promise((resolve) => setTimeout(resolve, 0));

function runView(runId, prompt = runId) {
  return {
    runId,
    status: 'finished',
    prompt,
    createdAt: 1,
    finishedAt: 2,
    durationSeconds: 1,
    sessionId: null,
    hasReport: false,
    stopReason: null,
    error: null,
    usage: null,
    eventCount: 0,
  };
}

const ACCOUNTS_ME = {
  mode: 'accounts',
  authenticated: true,
  user: { id: 'u1', name: '张明', email: 'admin@example.test', role: 'admin' },
  tenant: { id: 'tenant-alpha', name: '组织甲' },
  csrfToken: 'csrf-token-value',
};

function authStub() {
  const shown = [];
  let handler = null;
  return {
    shown,
    show(options) {
      shown.push(options);
    },
    hide() {
      shown.push({ mode: 'hide' });
    },
    onSession(fn) {
      handler = fn;
    },
    async deliver(me) {
      if (handler) await handler(me);
    },
  };
}

function harness({
  me = async () => ({ mode: 'local', authenticated: false }),
  api = {},
  location,
  eventSourceFactory,
} = {}) {
  const dom = createDomStub(location ? { location } : {});
  dom.install();
  let meCalls = 0;
  const calls = { logout: 0 };
  const base = {
    me: async () => {
      meCalls += 1;
      return me();
    },
    health: async () => ({ pi: { available: true }, configured: { model: true } }),
    config: async () => ({ model: 'test-model' }),
    listRuns: async () => ({ runs: [] }),
    getRun: async (runId) => ({ run: runView(runId) }),
    startRun: async (prompt) => ({ run: runView('new-run', prompt) }),
    stopRun: async () => ({ requested: true }),
    logout: async () => {
      calls.logout += 1;
      return { ok: true };
    },
    createInvitation: async (email, role) => ({ invitation: { code: 'c', email, role } }),
    eventsUrl: (runId) => `/api/runs/${runId}/events`,
    reportUrl: (runId) => `/api/runs/${runId}/report`,
    ...api,
  };
  const auth = authStub();
  setCsrfToken(null);
  const workspace = createWorkspace({ api: base, auth, eventSourceFactory });
  auth.onSession((session) => workspace.enterSession(session));
  return {
    dom,
    api: base,
    auth,
    workspace,
    calls,
    get meCalls() {
      return meCalls;
    },
  };
}

function unauthorized(mode) {
  return new ApiError('unauthenticated', { status: 401, code: 'unauthenticated', payload: { error: 'unauthenticated', mode } });
}

test('local mode enters the workbench without any login face', async (t) => {
  const h = harness();
  t.after(() => h.dom.restore());
  await h.workspace.init();
  assert.equal(h.dom.getElement('app-shell').hidden, false);
  assert.deepEqual(h.auth.shown.filter((entry) => entry.mode !== 'hide'), []);
  assert.equal(h.workspace.identity.mode, 'local');
  assert.equal(h.dom.getElement('account-area').hidden, true);
});

test('every /api/me failure other than 401 fails closed without probing /api/config', async (t) => {
  // A 404 from /api/me (e.g. a host that predates accounts) is not a local
  // workbench signal: there is no fallback, so no config shape can reveal the
  // shell without an identity.
  const notFound = () =>
    new ApiError('unknown_endpoint', {
      status: 404,
      code: 'unknown_endpoint',
      payload: { error: 'unknown_endpoint', path: '/api/me' },
    });
  const cases = [
    { name: '401 accounts', me: () => { throw unauthorized('accounts'); }, config: async () => ({ multiTenant: false }), expect: 'login', probes: 0 },
    { name: '401 gateway', me: () => { throw unauthorized('gateway'); }, config: async () => ({ multiTenant: false }), expect: 'gateway', probes: 0 },
    { name: '404 with multiTenant:false', me: () => { throw notFound(); }, config: async () => ({ multiTenant: false }), expect: 'unavailable', probes: 0 },
    { name: 'network error', me: () => { throw Object.assign(new Error('down'), { status: 0 }); }, config: async () => ({ multiTenant: false }), expect: 'unavailable', probes: 0 },
    { name: 'multitenant config', me: () => { throw notFound(); }, config: async () => ({ multiTenant: true }), expect: 'unavailable', probes: 0 },
    { name: 'missing multiTenant', me: () => { throw notFound(); }, config: async () => ({ model: 'x' }), expect: 'unavailable', probes: 0 },
    { name: 'string false', me: () => { throw notFound(); }, config: async () => ({ multiTenant: 'false' }), expect: 'unavailable', probes: 0 },
    { name: 'config 401', me: () => { throw notFound(); }, config: () => { throw unauthorized('accounts'); }, expect: 'unavailable', probes: 0 },
    { name: 'config network error', me: () => { throw notFound(); }, config: () => { throw Object.assign(new Error('down'), { status: 0 }); }, expect: 'unavailable', probes: 0 },
  ];
  for (const item of cases) {
    let probes = 0;
    const h = harness({
      me: item.me,
      api: {
        config: async () => {
          probes += 1;
          return item.config();
        },
      },
    });
    try {
      await h.workspace.init();
      assert.equal(h.dom.getElement('app-shell').hidden, true, item.name);
      assert.equal(h.workspace.identity, null, item.name);
      assert.equal(probes, item.probes, item.name);
      const face = h.auth.shown.filter((entry) => entry.mode !== 'hide').at(-1);
      assert.equal(face?.mode, item.expect, item.name);
    } finally {
      h.dom.restore();
    }
  }
});

test('a 401 in accounts mode shows the login face and keeps the shell hidden', async (t) => {
  const h = harness({
    me: async () => {
      throw unauthorized('accounts');
    },
  });
  t.after(() => h.dom.restore());
  await h.workspace.init();
  assert.equal(h.dom.getElement('app-shell').hidden, true);
  assert.equal(h.auth.shown.length, 1);
  assert.equal(h.auth.shown[0].mode, 'login');
});

test('a /register#invite fragment goes straight to the invitation registration face', async (t) => {
  const h = harness({
    me: async () => {
      throw unauthorized('accounts');
    },
    location: { pathname: '/register', search: '', hash: '#invite=invite-token', origin: 'http://127.0.0.1:43210' },
  });
  t.after(() => h.dom.restore());
  await h.workspace.init();
  assert.equal(h.auth.shown.length, 1);
  assert.equal(h.auth.shown[0].mode, 'register');
  assert.equal(h.auth.shown[0].invite, 'invite-token');
});

test('an invite fragment arriving in the same document still opens registration', async (t) => {
  // Pasting /register#invite=… while the register face is already open is a
  // same-document navigation: no reload, only a hashchange. The workbench must
  // pick the fragment up there too (and auth.js clears it before the request).
  const h = harness({
    me: async () => {
      throw unauthorized('accounts');
    },
    location: { pathname: '/register', search: '', hash: '', origin: 'http://127.0.0.1:43210' },
  });
  t.after(() => h.dom.restore());
  await h.workspace.init();
  assert.equal(h.auth.shown.length, 1);
  assert.equal(h.auth.shown[0].mode, 'register');
  assert.equal(h.auth.shown[0].invite, '');

  h.dom.location.hash = '#invite=late-arriving-token';
  h.dom.window.dispatch('hashchange', {});
  await tick();
  assert.equal(h.auth.shown.length, 2);
  assert.equal(h.auth.shown[1].mode, 'register');
  assert.equal(h.auth.shown[1].invite, 'late-arriving-token');
});

test('gateway mode gets a gateway explanation, never a login form', async (t) => {
  const h = harness({
    me: async () => {
      throw unauthorized('gateway');
    },
  });
  t.after(() => h.dom.restore());
  await h.workspace.init();
  assert.equal(h.auth.shown.length, 1);
  assert.equal(h.auth.shown[0].mode, 'gateway');
});

test('an unreachable host shows the retry face instead of guessing a session', async (t) => {
  const h = harness({
    me: async () => {
      throw Object.assign(new Error('network down'), { status: 0 });
    },
  });
  t.after(() => h.dom.restore());
  await h.workspace.init();
  assert.equal(h.auth.shown.length, 1);
  assert.equal(h.auth.shown[0].mode, 'unavailable');
});

test('entering with a session records the CSRF token and the real identity', async (t) => {
  const h = harness({ me: async () => ({ ...ACCOUNTS_ME }) });
  t.after(() => h.dom.restore());
  await h.workspace.init();
  assert.equal(h.dom.getElement('app-shell').hidden, false);
  assert.equal(getCsrfToken(), 'csrf-token-value');
  assert.equal(h.dom.getElement('account-area').hidden, false);
  assert.equal(h.dom.getElement('account-org').textContent, '组织甲');
  assert.equal(h.dom.getElement('account-name').textContent, '张明');
  assert.equal(h.dom.getElement('account-logout').hidden, false);
  assert.equal(h.dom.getElement('account-invite').hidden, false);
  assert.equal(h.auth.shown.at(-1).mode, 'hide');
});

test('a 401 while reading history closes the stream, clears the screen and returns to login', async (t) => {
  let fail = false;
  const h = harness({
    me: async () => ({ ...ACCOUNTS_ME }),
    api: {
      listRuns: async () => {
        if (fail) throw unauthorized('accounts');
        return { runs: [runView('run-1', '已有研究')] };
      },
    },
  });
  t.after(() => h.dom.restore());
  await h.workspace.init();
  assert.equal(h.workspace.runs.length, 1);
  assert.equal(h.dom.getElement('run-list').children.length > 0, true);

  fail = true;
  await h.workspace.refreshRuns();
  assert.equal(h.dom.getElement('app-shell').hidden, true);
  assert.equal(getCsrfToken(), null);
  assert.equal(h.workspace.identity, null);
  assert.deepEqual(h.workspace.runs, []);
  assert.equal(h.dom.getElement('account-area').hidden, true);
  assert.equal(h.auth.shown.at(-1).mode, 'login');
  assert.match(h.auth.shown.at(-1).notice, /过期/);
  assert.equal(h.dom.location.pathname, '/login');
});

test('a session that ends during a run closes the stream and clears the run view', async (t) => {
  let expired = false;
  const sources = [];
  const h = harness({
    me: async () => ({ ...ACCOUNTS_ME }),
    api: {
      listRuns: async () => {
        if (expired) throw unauthorized('accounts');
        return { runs: [runView('run-1', '进行中的研究')] };
      },
      getRun: async (runId) => ({ run: { ...runView(runId, '进行中的研究'), status: 'running' } }),
    },
    eventSourceFactory: (url) => {
      const source = {
        url,
        readyState: 0,
        closed: false,
        addEventListener() {},
        close() {
          this.closed = true;
          this.readyState = 2;
        },
      };
      sources.push(source);
      return source;
    },
  });
  t.after(() => h.dom.restore());
  await h.workspace.init();
  await h.workspace.openRun('run-1');
  assert.equal(sources.length, 1, 'the run view must attach one stream');
  assert.equal(h.dom.getElement('chat-view').hidden, false);

  expired = true;
  await h.workspace.refreshRuns();
  assert.equal(sources[0].closed, true, 'the live stream must be closed with the session');
  assert.equal(h.dom.getElement('app-shell').hidden, true);
  assert.equal(h.dom.getElement('run-view').children.length, 0, 'the in-screen run must be cleared');
  assert.equal(h.workspace.runId, null);
  assert.equal(h.auth.shown.at(-1).mode, 'login');
});

test('logout revokes the server session, then returns to login', async (t) => {
  const h = harness({ me: async () => ({ ...ACCOUNTS_ME }) });
  t.after(() => h.dom.restore());
  await h.workspace.init();
  await h.workspace.logout();
  assert.equal(h.calls.logout, 1);
  assert.equal(h.dom.getElement('app-shell').hidden, true);
  assert.equal(h.auth.shown.at(-1).mode, 'login');
  assert.match(h.auth.shown.at(-1).notice, /已退出登录/);
});

test('a failed logout keeps the session and the screen (no pretending)', async (t) => {
  const h = harness({
    me: async () => ({ ...ACCOUNTS_ME }),
    api: {
      logout: async () => {
        throw Object.assign(new Error('nope'), { status: 503 });
      },
    },
  });
  t.after(() => h.dom.restore());
  await h.workspace.init();
  await h.workspace.logout();
  assert.equal(h.dom.getElement('app-shell').hidden, false);
  assert.equal(h.workspace.identity.mode, 'accounts');
});

test('returning to the tab re-checks an accounts session', async (t) => {
  const h = harness({ me: async () => ({ ...ACCOUNTS_ME }) });
  t.after(() => h.dom.restore());
  await h.workspace.init();
  const before = h.meCalls;
  h.dom.document.dispatch('visibilitychange', {});
  await tick();
  assert.equal(h.meCalls, before + 1);
});

test('the service alert follows the tenant-aware /api/config, not the host-level health probe', async (t) => {
  // A tenant whose own credential is configured, on a host with no global key:
  // health reports no host model, config reports this caller's model. The
  // sidebar must stay silent — otherwise a working tenant is told it is not
  // configured.
  const configuredTenant = harness({
    me: async () => ({ ...ACCOUNTS_ME }),
    api: {
      health: async () => ({ pi: { available: true }, configured: { model: false } }),
      config: async () => ({ model: 'tenant-model', configured: { model: true } }),
    },
  });
  t.after(() => configuredTenant.dom.restore());
  await configuredTenant.workspace.init();
  assert.equal(configuredTenant.dom.getElement('sidebar-foot').hidden, true);
  assert.equal(configuredTenant.dom.getElement('service-alert-text').textContent, '');

  const unconfiguredTenant = harness({
    me: async () => ({ ...ACCOUNTS_ME }),
    api: {
      health: async () => ({ pi: { available: true }, configured: { model: true } }),
      config: async () => ({ model: 'tenant-model', configured: { model: false } }),
    },
  });
  await unconfiguredTenant.workspace.init();
  assert.match(
    unconfiguredTenant.dom.getElement('service-alert-text').textContent,
    /模型服务还没有配置/
  );
  assert.equal(unconfiguredTenant.dom.getElement('sidebar-foot').hidden, false);
});


test('an in-flight history response from an ended session never renders', async (t) => {
  // The bug this pins: refreshRuns() was not tied to the identity that started
  // it, so a listRuns answer that landed after sessionExpired() painted the
  // previous account's runs onto the login screen.
  let release;
  const gate = new Promise((resolve) => {
    release = resolve;
  });
  let calls = 0;
  const h = harness({
    me: async () => ({ ...ACCOUNTS_ME }),
    api: {
      listRuns: async () => {
        calls += 1;
        if (calls === 1) return { runs: [runView('run-1', '旧租户研究')] };
        await gate;
        return { runs: [runView('run-2', '过期请求的响应')] };
      },
    },
  });
  t.after(() => h.dom.restore());
  await h.workspace.init();
  assert.equal(h.workspace.runs.length, 1);
  const listedRuns = (h) =>
    Array.from(h.dom.getElement('run-list').children).filter((child) => child.dataset?.runId);
  assert.equal(listedRuns(h).length, 1, '先渲染旧租户的一条研究');

  const pending = h.workspace.refreshRuns();
  await tick();
  h.workspace.sessionExpired('登录状态已过期，请重新登录。');
  release();
  await pending;

  assert.deepEqual(h.workspace.runs, [], '过期身份的响应必须被丢弃');
  assert.equal(listedRuns(h).length, 0, '登录面上不得出现旧租户的研究条目');
  assert.equal(h.dom.getElement('app-shell').hidden, true);
  assert.equal(h.workspace.identity, null);

  // A new login is a new generation, and its own history renders again.
  await h.workspace.enterSession({ ...ACCOUNTS_ME });
  assert.deepEqual(h.workspace.runs.map((run) => run.runId), ['run-2']);
  assert.equal(h.dom.getElement('app-shell').hidden, false);
});

test('a run accepted for an ended session never opens on the login screen', async (t) => {
  let release;
  const gate = new Promise((resolve) => {
    release = resolve;
  });
  const h = harness({
    me: async () => ({ ...ACCOUNTS_ME }),
    api: {
      startRun: async (prompt) => {
        await gate;
        return { run: runView('late-run', prompt) };
      },
    },
  });
  t.after(() => h.dom.restore());
  await h.workspace.init();

  const pending = h.workspace.startRun('登录期间提交');
  await tick();
  h.workspace.sessionExpired('已退出登录。');
  release();
  await pending;

  assert.equal(h.workspace.runId, null, '过期身份的 run 不得打开');
  assert.equal(h.workspace.identity, null);
  assert.equal(h.dom.getElement('app-shell').hidden, true);
  assert.equal(h.dom.location.pathname, '/login');
});

test('a long follow-up chain never opens one stream per ancestor', async (t) => {
  // The regression: `openRun` built a feed per ancestor and `createRunFeed`
  // connected in its constructor, so a 20-turn chain asked the host for 21
  // concurrent streams at once. Past the global `StreamLimits` ceiling every
  // history replay came back `429 stream_limit` and those turns never appeared.
  // History now replays one ancestor at a time, so the live run keeps its slot.
  const depth = 8;
  const chain = new Map();
  for (let index = 0; index < depth; index += 1) {
    chain.set(`past-${index}`, {
      ...runView(`past-${index}`, `第 ${index} 轮`),
      status: 'finished',
      parentRunId: index > 0 ? `past-${index - 1}` : null,
    });
  }
  chain.set('current', {
    ...runView('current', '最新一轮'),
    status: 'running',
    parentRunId: `past-${depth - 1}`,
  });

  const sources = [];
  const h = harness({
    api: {
      getRun: async (runId) => ({ run: chain.get(runId) }),
    },
    eventSourceFactory: (url) => {
      const source = {
        url,
        readyState: 0,
        closed: false,
        listeners: new Map(),
        addEventListener(type, handler) {
          this.listeners.set(type, handler);
        },
        close() {
          this.closed = true;
          this.readyState = 2;
        },
      };
      sources.push(source);
      return source;
    },
  });
  t.after(() => h.dom.restore());
  await h.workspace.init();
  await h.workspace.openRun('current');

  const openSources = () => sources.filter((source) => !source.closed);
  assert.equal(h.dom.getElement('earlier-turns').children.length, depth, '整条父链都渲染');
  assert.ok(openSources().length <= 2, `同时打开的流不得超过 2 条，实际 ${openSources().length}`);

  // 逐条驱动终帧：每收口一条才允许打开下一条，过程中并发数始终 <= 2。
  let peak = openSources().length;
  for (let index = 0; index < depth; index += 1) {
    const ancestor = `past-${index}`;
    const source = openSources().find((candidate) => candidate.url.endsWith(`/${ancestor}/events`));
    assert.ok(source, `第 ${index} 条祖先流应已按序打开`);
    source.listeners.get('run_finished')({
      data: JSON.stringify({
        v: EVENT_VERSIONS.run_finished,
        seq: 1,
        type: 'run_finished',
        runId: ancestor,
        payload: { status: 'finished' },
      }),
    });
    peak = Math.max(peak, openSources().length);
    assert.ok(peak <= 2, `回放过程中同时打开的流不得超过 2 条，实际 ${peak}`);
  }
  assert.equal(openSources().length, 1, '祖先回放结束后只剩当前 run 的流');
  assert.equal(sources.length, depth + 1, '每条祖先最多建立一条连接');
});
