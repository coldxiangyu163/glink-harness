import { planEntries, planStatusLabel } from './present.mjs';

/** Only ACP's projected todo_write is a whole-list replacement. Other plan-like
 * tools (including pi-family calls) are not a source for this panel. */
export function isTodoWrite(item) {
  return item?.kind === 'tool' && item.presentation === 'plan' && item.name === 'todo_write';
}

export function latestTodoEntries(items) {
  for (let index = items.length - 1; index >= 0; index -= 1) {
    if (isTodoWrite(items[index])) return planEntries(items[index].args);
  }
  return [];
}

/** A single run-owned view of the latest list, independent of the process fold. */
export function createTodoPanel() {
  const panel = document.createElement('section');
  panel.className = 'pi-todo-panel';
  panel.setAttribute('aria-label', '当前任务');
  panel.hidden = true;
  const head = document.createElement('div');
  head.className = 'pi-todo-head';
  const title = document.createElement('h3');
  title.textContent = '当前任务';
  const count = document.createElement('span');
  count.className = 'pi-todo-count';
  count.setAttribute('role', 'status');
  head.append(title, count);
  const list = document.createElement('ol');
  list.className = 'pi-todo-list';
  panel.append(head, list);
  let previous = null;

  return {
    node: panel,
    render(items) {
      const entries = latestTodoEntries(items);
      const key = JSON.stringify(entries);
      panel.hidden = entries.length === 0;
      if (previous === key) return;
      previous = key;
      const active = entries.filter((entry) => entry.status === 'in_progress').length;
      const done = entries.filter((entry) => entry.status === 'done' || entry.status === 'completed').length;
      count.textContent = `${done}/${entries.length} 项已完成${active > 1 ? ` · ${active} 项进行中` : ''}`;
      list.replaceChildren(...entries.map((entry) => {
        const row = document.createElement('li');
        row.className = 'pi-todo-item';
        row.dataset.status = entry.status;
        const mark = document.createElement('span');
        mark.className = 'pi-todo-mark';
        mark.setAttribute('aria-hidden', 'true');
        const state = document.createElement('span');
        state.className = 'pi-todo-state';
        state.textContent = planStatusLabel(entry.status);
        const content = document.createElement('span');
        content.className = 'pi-todo-content';
        content.textContent = entry.content;
        row.append(mark, state, content);
        return row;
      }));
    },
  };
}
