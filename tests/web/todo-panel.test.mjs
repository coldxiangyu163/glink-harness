import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createRunState, reduceAll } from '../../web/modules/run-reducer.mjs';
import { createRunView } from '../../web/modules/run-view.mjs';
import { createProgressPanel } from '../../web/modules/progress-panel.mjs';

const frames = readFileSync(new URL('./fixtures/todo-updates.jsonl', import.meta.url), 'utf8')
  .trim().split('\n').map(JSON.parse);

// A small DOM with real descendant selection and text aggregation: the assertions
// exercise the production run view, not a hand-built final-state substitute.
function dom() {
  let raf = [];
  class Node {
    constructor(tag) {
      this.tagName = tag.toUpperCase(); this.children = []; this.dataset = {}; this.attributes = {};
      this._text = ''; this.hidden = false; this.open = false; this.parentElement = null;
      this.style = { setProperty() {} }; this._classes = ''; this.listeners = {};
      this.classList = {
        add: (...names) => { this.className = [...new Set([...this.className.split(' '), ...names])].join(' ').trim(); },
        contains: (name) => this.className.split(' ').includes(name),
        toggle: (name, force) => {
          const on = force ?? !this.classList.contains(name);
          this.className = [...new Set([...this.className.split(' ').filter((part) => part !== name), ...(on ? [name] : [])])].join(' ').trim();
          return on;
        },
      };
    }
    set className(value) { this._classes = value; }
    get className() { return this._classes; }
    set textContent(value) { this._text = String(value); this.replaceChildren(); }
    get textContent() { return this._text + this.children.map((child) => child.textContent).join(''); }
    set innerHTML(value) { this.textContent = value; }
    append(...children) { for (const child of children) {
      child.remove?.(); child.parentElement = this; this.children.push(child);
    } }
    replaceChildren(...children) {
      for (const child of this.children) child.parentElement = null;
      this.children = []; this.append(...children);
    }
    insertBefore(child, sibling) {
      child.remove?.(); child.parentElement = this;
      this.children.splice(this.children.indexOf(sibling), 0, child);
    }
    remove() {
      if (!this.parentElement) return;
      this.parentElement.children = this.parentElement.children.filter((child) => child !== this);
      this.parentElement = null;
    }
    setAttribute(name, value) { this.attributes[name] = String(value); }
    getAttribute(name) { return this.attributes[name] ?? null; }
    addEventListener(name, listener) { this.listeners[name] = listener; }
    click() { this.listeners.click?.(); }
    contains(node) { return this === node || this.children.some((child) => child.contains?.(node)); }
    querySelector(selector) { return this.querySelectorAll(selector)[0] ?? null; }
    querySelectorAll(selector) {
      const parts = selector.replaceAll(' > ', ' ').split(/\s+/);
      const matches = (node, part) => {
        const not = part.match(/:not\(\.([\w-]+)\)/)?.[1];
        const tag = part.match(/^[a-z]+/i)?.[0];
        const classes = [...part.replace(/:not\([^)]*\)/g, '').matchAll(/\.([\w-]+)/g)].map((m) => m[1]);
        return (!not || !node.classList.contains(not)) && (!tag || node.tagName === tag.toUpperCase())
          && classes.every((name) => node.classList.contains(name));
      };
      const result = [];
      const walk = (node) => {
        for (const child of node.children) {
          if (matches(child, parts.at(-1))) {
            let ancestor = child.parentElement;
            if (parts.slice(0, -1).reverse().every((part) => {
              while (ancestor && !matches(ancestor, part)) ancestor = ancestor.parentElement;
              if (!ancestor) return false;
              ancestor = ancestor.parentElement;
              return true;
            })) result.push(child);
          }
          walk(child);
        }
      };
      walk(this);
      return result;
    }
  }
  const previous = { document: globalThis.document, requestAnimationFrame: globalThis.requestAnimationFrame,
    cancelAnimationFrame: globalThis.cancelAnimationFrame };
  globalThis.document = { createElement: (tag) => new Node(tag), createElementNS: (_, tag) => new Node(tag),
    createTextNode: (text) => new Node('#text') };
  globalThis.requestAnimationFrame = (callback) => { raf.push(callback); return raf.length; };
  globalThis.cancelAnimationFrame = () => {};
  return {
    root: new Node('div'),
    flush() { const jobs = raf; raf = []; jobs.forEach((callback) => callback()); },
    restore() { Object.assign(globalThis, previous); },
  };
}

test('current and historical run views keep only plan-update rows, never a docked or inline task card', () => {
  const env = dom();
  try {
    const chat = document.createElement('div'); chat.id = 'chat-view';
    const scroll = document.createElement('div'); scroll.id = 'conversation-scroll';
    const conversation = document.createElement('div');
    env.root.id = 'run-view'; chat.append(scroll); scroll.append(conversation); conversation.append(env.root);
    const model = reduceAll(createRunState({ runId: 'todo-fixture' }), frames);
    const view = createRunView({ root: env.root });
    view.render(model); env.flush();
    assert.equal(chat.children.length, 1, 'a plan must not consume space above the conversation');
    assert.equal(chat.children[0], scroll);
    assert.equal(env.root.querySelector('.pi-todo-panel'), null);
    assert.equal(env.root.querySelector('.pi-todo-list'), null);
    assert.deepEqual(env.root.querySelectorAll('.pi-plan-update').map(row => row.textContent), ['计划更新', '计划更新']);
    assert.equal(env.root.querySelectorAll('.pi-tool').length, 0, 'no repeated todo snapshots in tool cards');
    const pastRoot = document.createElement('div');
    const past = createRunView({ root: pastRoot }); past.render(model); env.flush();
    assert.equal(pastRoot.querySelector('.pi-todo-list'), null, 'past turns also remain a lightweight conversation');
    past.dispose(); view.dispose();
    assert.equal(chat.children.length, 1, 'dispose must not touch the conversation scroller');
  } finally { env.restore(); }
});

test('progress panel owns one latest task timeline, not one list per plan update', () => {
  const env = dom();
  try {
    const panel = createProgressPanel({ root: env.root });
    const first = reduceAll(createRunState({ runId: 'todo-fixture' }), frames.slice(0, 2));
    panel.render(first);
    const list = env.root.querySelector('.pi-todo-list');
    assert.ok(list, 'the task list belongs to the progress surface');
    assert.deepEqual(list.querySelectorAll('.pi-todo-item').map(row => row.textContent),
      ['进行中核验资料', '待开始整理报告']);
    assert.equal(env.root.querySelector('.pi-todo-panel'), null, 'no card inside a card');
    panel.render(reduceAll(first, frames.slice(2)));
    assert.equal(env.root.querySelector('.pi-todo-list'), list);
    assert.deepEqual(list.querySelectorAll('.pi-todo-item').map(row => [row.dataset.todoStatus, row.textContent]), [
      ['completed', '已完成核验资料'], ['in_progress', '进行中查证来源'], ['in_progress', '进行中整理报告'],
    ]);
    assert.match(env.root.querySelector('.progress-section-meta').textContent, /1\/3 项已完成.*2 项进行中/);
    assert.equal(env.root.querySelectorAll('.progress-step').filter(row => !row.hidden).length, 3);
  } finally { env.restore(); }
});

test('timeline keeps TaskRows rings, truthful accessibility and the existing progress fold', () => {
  const env = dom();
  try {
    let closed = 0;
    const panel = createProgressPanel({ root: env.root, onClose: () => closed++ });
    const first = reduceAll(createRunState({ runId: 'todo-fixture' }), frames.slice(0, 2));
    panel.render(first);
    const head = env.root.querySelector('.progress-section-head');
    const body = env.root.querySelector('.progress-steps-body');
    assert.equal(head.getAttribute('aria-controls'), body.id);
    assert.equal(head.getAttribute('aria-label'), '收起进度');
    const running = env.root.querySelector('.pi-todo-item .progress-marker');
    assert.equal(running.getAttribute('role'), 'progressbar');
    assert.equal(running.getAttribute('aria-label'), '核验资料进度');
    assert.equal(running.getAttribute('aria-valuenow'), null, 'no fabricated percentage');
    assert.equal(running.querySelectorAll('circle').length, 2);
    assert.equal(env.root.querySelector('.pi-todo-state').classList.contains('sr-only'), true);
    head.click();
    assert.equal(head.getAttribute('aria-expanded'), 'false');
    assert.equal(head.getAttribute('aria-label'), '展开进度');
    assert.equal(body.hidden, true);
    panel.render(reduceAll(first, frames.slice(2)));
    assert.equal(body.hidden, true, 'incoming replacement preserves the reader fold');
    assert.equal(env.root.querySelector('.pi-todo-item .icon').dataset.icon, 'check');
    head.click(); assert.equal(body.hidden, false);
    env.root.querySelector('.progress-close').click(); assert.equal(closed, 1);
  } finally { env.restore(); }
});

test('empty replacement and a different run clear tasks; no-source runs retain actual execution steps', () => {
  const env = dom();
  try {
    const panel = createProgressPanel({ root: env.root });
    const plain = reduceAll(createRunState({ runId: 'pi-fixture' }), [
      { ...frames[0], seq: 1, payload: { ...frames[0].payload, toolName: 'bash', presentation: 'execute', args: { command: 'ls' } } },
    ]);
    panel.render(plain);
    const list = env.root.querySelector('.pi-todo-list');
    assert.equal(list.hidden, true);
    assert.match(env.root.querySelector('.progress-steps:not(.pi-todo-list)').textContent, /执行.*ls/);
    const populated = reduceAll(createRunState({ runId: 'todo-fixture' }), frames);
    panel.render(populated); assert.equal(list.hidden, false);
    const emptied = reduceAll(populated, [
      { ...frames[0], seq: 5, payload: { ...frames[0].payload, toolCallId: 'p3', args: { todos: [] } } },
    ]);
    panel.render(emptied);
    assert.equal(list.hidden, true, 'latest empty replacement clears the task source');
    assert.equal(list.querySelectorAll('.pi-todo-item').length, 0);
    assert.equal(env.root.querySelector('.progress-steps:not(.pi-todo-list)').hidden, true, 'plan calls are not duplicated as execution tasks');
    panel.render(populated);
    panel.render(createRunState({ runId: 'legacy', status: 'finished' }));
    assert.equal(list.hidden, true, 'opening another run cannot inherit the previous plan');
    assert.equal(list.children.length, 0);
  } finally { env.restore(); }
});

test('terminal run does not pretend unfinished plan items are done or still running', () => {
  const env = dom();
  try {
    const panel = createProgressPanel({ root: env.root });
    const stopped = reduceAll(createRunState({ runId: 'todo-fixture' }), [...frames,
      { type: 'run_cancelled', v: 1, runId: 'todo-fixture', seq: 5, ts: 5, payload: { status: 'cancelled' } },
    ]);
    panel.render(stopped);
    const rows = env.root.querySelectorAll('.pi-todo-item');
    assert.deepEqual(rows.map(row => row.dataset.status), ['done', 'stopped', 'stopped']);
    assert.match(rows[1].textContent, /未完成查证来源/);
    assert.doesNotMatch(env.root.querySelector('.progress-section-meta').textContent, /进行中/);
    assert.equal(rows[1].querySelector('.progress-marker').getAttribute('role'), null);
  } finally { env.restore(); }
});
