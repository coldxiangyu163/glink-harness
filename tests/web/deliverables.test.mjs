import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createDomStub } from './dom-stub.mjs';
import { createRunState } from '../../web/modules/run-reducer.mjs';
import { createRunView } from '../../web/modules/run-view.mjs';

function collect(node, predicate) {
  return [node, ...(node.children ?? []).flatMap((child) => collect(child, predicate))].filter(predicate);
}

test('the run view renders declared files with description and a real per-index download', () => {
  const dom = createDomStub();
  const root = dom.document.createElement('div');
  const view = createRunView({ root, deliverableHref: (id, n) => `/api/runs/${id}/deliverables/${n}` });
  try {
    view.render(createRunState({ runId: 'sample', status: 'finished', hasReport: true,
      deliverables: [
        { path: 'region/brief.md', description: '辖区备忘录', status: 'ok', available: true },
        { path: 'unsafe.md', description: '不可用文件', status: 'rejected', reason: '链接', available: false },
      ],
    }));
    dom.raf.shift()();
    const section = collect(root, (node) => node.className?.includes('glink-deliverables'))[0];
    assert.ok(section && !section.hidden);
    const anchors = collect(section, (node) => node.tagName === 'A');
    assert.equal(anchors.length, 3, 'open and download for valid file; no link for rejected file');
    assert.equal(anchors[0].textContent, 'brief.md');
    assert.equal(anchors[0].href, '/api/runs/sample/deliverables/0');
    assert.equal(anchors[1].getAttribute('download'), 'brief.md');
    assert.equal(anchors[2].getAttribute('aria-disabled'), 'true');
    assert.ok(collect(section, (node) => node.textContent?.includes('辖区备忘录')).length);
    assert.ok(collect(section, (node) => node.textContent?.includes('未能保存：链接')).length);
  } finally {
    view.dispose();
    dom.restore();
  }
});

test('a historical run without declarations has no card section', () => {
  const dom = createDomStub();
  const root = dom.document.createElement('div');
  const view = createRunView({ root });
  try {
    view.render(createRunState({ runId: 'old', status: 'finished', hasReport: true }));
    dom.raf.shift()();
    assert.ok(collect(root, (node) => node.className?.includes('glink-deliverables'))[0].hidden);
  } finally {
    view.dispose();
    dom.restore();
  }
});
