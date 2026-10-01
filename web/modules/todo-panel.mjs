import { planEntries, planStatusLabel } from './present.mjs';

/** Only the whole-list todo_write projection supplies a task plan. Other
 * plan-like tools remain execution rows, not a made-up replacement list. */
export function isTodoWrite(item) {
  return item?.kind === 'tool' && item.presentation === 'plan' && item.name === 'todo_write';
}

export function latestTodoEntries(items) {
  for (let index = items.length - 1; index >= 0; index -= 1) {
    if (isTodoWrite(items[index])) return planEntries(items[index].args);
  }
  return [];
}

function icon(name) {
  const node = document.createElement('span');
  node.className = 'icon';
  node.dataset.icon = name;
  node.setAttribute('aria-hidden', 'true');
  node.style.setProperty('--icon-size', '12px');
  return node;
}

/** TaskRows StatusRing: the same 22px indeterminate arc for plans and fallback
 * execution rows. A run's task count is not a per-task percentage. */
export function createProgressMarker(status, label) {
  const mark = document.createElement('span');
  mark.className = 'progress-marker';
  if (status === 'running') {
    mark.setAttribute('role', 'progressbar');
    mark.setAttribute('aria-label', `${label}进度`);
    mark.setAttribute('aria-valuemin', '0');
    mark.setAttribute('aria-valuemax', '100');
    const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('width', '22');
    svg.setAttribute('height', '22');
    svg.setAttribute('aria-hidden', 'true');
    for (const active of [false, true]) {
      const circle = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
      for (const [name, value] of Object.entries({ cx: 11, cy: 11, r: 10, fill: 'none',
        stroke: active ? 'var(--foreground)' : 'var(--border)', 'stroke-width': 2 })) {
        circle.setAttribute(name, value);
      }
      if (active) {
        circle.setAttribute('stroke-linecap', 'round');
        circle.setAttribute('stroke-dasharray', `${2 * Math.PI * 10 * 0.75} ${2 * Math.PI * 10 * 0.25}`);
        circle.classList.add('progress-arc');
      }
      svg.append(circle);
    }
    mark.append(svg);
  } else {
    mark.setAttribute('aria-hidden', 'true');
    if (status === 'done' || status === 'failed') mark.append(icon(status === 'done' ? 'check' : 'close'));
  }
  return mark;
}

/** Unboxed TaskRows timeline, owned solely by the progress surface. */
export function createTodoTimeline() {
  const list = document.createElement('ol');
  list.className = 'progress-steps glink-todo-list';
  list.hidden = true;
  let previous = null;
  return {
    node: list,
    render(entries, running) {
      const key = JSON.stringify([entries, running]);
      list.hidden = entries.length === 0;
      if (previous === key) return;
      previous = key;
      list.replaceChildren(...entries.map((entry) => {
        const status = entry.status === 'done' || entry.status === 'completed' ? 'done'
          : entry.status === 'in_progress' ? running ? 'running' : 'stopped' : 'pending';
        const row = document.createElement('li');
        row.className = 'progress-step glink-todo-item';
        row.dataset.status = status;
        row.dataset.todoStatus = entry.status;
        const state = document.createElement('span');
        state.className = 'glink-todo-state sr-only';
        state.textContent = status === 'stopped' ? '未完成' : planStatusLabel(entry.status);
        const content = document.createElement('span');
        content.className = 'progress-step-text';
        content.textContent = entry.content;
        const body = document.createElement('div');
        body.className = 'progress-step-row';
        body.append(createProgressMarker(status, entry.content), state, content);
        row.append(body);
        return row;
      }));
    },
  };
}
