import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createRunState, reduceAll } from '../../web/modules/run-reducer.mjs';
import { createRunView } from '../../web/modules/run-view.mjs';

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
      this.style = { setProperty() {} }; this._classes = '';
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
    addEventListener() {}
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
  globalThis.document = { createElement: (tag) => new Node(tag), createTextNode: (text) => new Node('#text') };
  globalThis.requestAnimationFrame = (callback) => { raf.push(callback); return raf.length; };
  globalThis.cancelAnimationFrame = () => {};
  return {
    root: new Node('div'),
    flush() { const jobs = raf; raf = []; jobs.forEach((callback) => callback()); },
    restore() { Object.assign(globalThis, previous); },
  };
}

test('ACP todo events render latest list, refresh in place and keep earlier calls as plan-update rows', () => {
  const env = dom();
  try {
    const view = createRunView({ root: env.root });
    const first = reduceAll(createRunState({ runId: 'todo-fixture' }), frames.slice(0, 2));
    view.render(first); env.flush();
    const panel = env.root.querySelector('.pi-todo-panel');
    assert.ok(panel, 'latest todo list is visible outside the process fold');
    assert.equal(panel.parentElement, env.root);
    assert.deepEqual(panel.querySelectorAll('.pi-todo-item').map((row) => row.textContent),
      ['进行中核验资料', '待开始整理报告']);
    view.render(reduceAll(first, frames.slice(2))); env.flush();
    assert.equal(env.root.querySelector('.pi-todo-panel'), panel, 'one persistent panel, not one per call');
    assert.deepEqual(panel.querySelectorAll('.pi-todo-item').map((row) => [row.dataset.status, row.textContent]), [
      ['completed', '已完成核验资料'], ['in_progress', '进行中查证来源'], ['in_progress', '进行中整理报告'],
    ]);
    assert.match(panel.textContent, /2 项进行中/);
    const rows = env.root.querySelectorAll('.pi-plan-update');
    assert.equal(rows.length, 2);
    assert.deepEqual(rows.map((row) => row.textContent), ['计划更新', '计划更新']);
    assert.equal(env.root.querySelectorAll('.pi-tool').length, 0, 'no old todo tool cards or repeated list snapshots');
    view.dispose();
  } finally { env.restore(); }
});

test('active panel docks outside the scroll instead of covering process controls; dispose removes it', () => {
  const env = dom();
  try {
    const chat = document.createElement('div'); chat.id = 'chat-view';
    const scroll = document.createElement('div'); scroll.id = 'conversation-scroll';
    const conversation = document.createElement('div');
    env.root.id = 'run-view';
    chat.append(scroll); scroll.append(conversation); conversation.append(env.root);
    const view = createRunView({ root: env.root });
    view.render(reduceAll(createRunState({ runId: 'todo-fixture' }), frames)); env.flush();
    assert.equal(chat.children[0].className, 'pi-todo-panel');
    assert.equal(chat.children[1], scroll);
    assert.equal(env.root.querySelector('.pi-todo-panel'), null);
    assert.equal(chat.children[0].hidden, false);
    view.dispose();
    assert.equal(chat.children.length, 1);
    assert.equal(chat.children[0], scroll);
  } finally { env.restore(); }
});

test('no todo source, legacy run and empty replacement never invent a panel', () => {
  const env = dom();
  try {
    const view = createRunView({ root: env.root });
    const plain = reduceAll(createRunState({ runId: 'pi-fixture' }), [
      { ...frames[0], seq: 1, payload: { ...frames[0].payload, toolName: 'bash', presentation: 'execute' } },
    ]);
    view.render(plain); env.flush();
    assert.equal(env.root.querySelector('.pi-todo-panel').hidden, true);
    view.render(createRunState({ runId: 'legacy', status: 'finished' })); env.flush();
    assert.equal(env.root.querySelector('.pi-todo-panel').hidden, true);
    view.dispose();
    const todoView = createRunView({ root: env.root });
    const populated = reduceAll(createRunState({ runId: 'todo-fixture' }), frames);
    todoView.render(populated); env.flush();
    assert.equal(env.root.querySelector('.pi-todo-panel').hidden, false);
    const emptied = reduceAll(populated, [
      { ...frames[0], seq: 5, payload: { ...frames[0].payload, toolCallId: 'p3', args: { todos: [] } } },
    ]);
    todoView.render(emptied); env.flush();
    assert.equal(env.root.querySelector('.pi-todo-panel').hidden, true, 'latest empty replacement clears the list');
    assert.equal(env.root.querySelectorAll('.pi-todo-item').length, 0);
    todoView.dispose();
  } finally { env.restore(); }
});
