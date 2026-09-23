/**
 * One floating tooltip for every `[data-tip]` control (标准库 Tooltip 的口径：
 * 圆角 8、12/20、近黑底反白字、300ms 延迟).
 *
 * A CSS `::after` bubble is clipped by any scrolling ancestor — the collapsed
 * sidebar is one — so the bubble lives on `document.body` and is positioned
 * from the trigger's box. Controls opt in with:
 *
 *   data-tip="名字"             the text (usually the control's accessible name)
 *   data-tip-side="right"       bottom (default) · top · right · left
 *   data-tip-when="collapsed"   only while the sidebar is an icon rail
 *
 * The tooltip is decoration for sighted pointer users: the accessible name stays
 * on the control itself, so the bubble is `aria-hidden`.
 */

const SHOW_DELAY = 300;
const GAP = 6;
const MARGIN = 8;

export function installTooltips({ isRail = () => false } = {}) {
  const bubble = document.createElement('div');
  bubble.className = 'tooltip';
  bubble.setAttribute('aria-hidden', 'true');
  bubble.hidden = true;
  document.body.append(bubble);

  let target = null;
  let timer = null;

  const eligible = (node) => {
    if (!node || !node.dataset.tip) return false;
    if (node.dataset.tipWhen === 'collapsed' && !isRail()) return false;
    return true;
  };

  function place(node) {
    const rect = node.getBoundingClientRect();
    const side = node.dataset.tipSide || 'bottom';
    bubble.dataset.side = side;
    const box = bubble.getBoundingClientRect();
    let left;
    let top;
    if (side === 'right') {
      left = rect.right + GAP;
      top = rect.top + rect.height / 2 - box.height / 2;
    } else if (side === 'left') {
      left = rect.left - GAP - box.width;
      top = rect.top + rect.height / 2 - box.height / 2;
    } else if (side === 'top') {
      left = rect.left + rect.width / 2 - box.width / 2;
      top = rect.top - GAP - box.height;
    } else {
      left = rect.left + rect.width / 2 - box.width / 2;
      top = rect.bottom + GAP;
    }
    const maxLeft = window.innerWidth - box.width - MARGIN;
    const maxTop = window.innerHeight - box.height - MARGIN;
    bubble.style.left = `${Math.round(Math.max(MARGIN, Math.min(left, maxLeft)))}px`;
    bubble.style.top = `${Math.round(Math.max(MARGIN, Math.min(top, maxTop)))}px`;
  }

  function show(node) {
    target = node;
    bubble.textContent = node.dataset.tip;
    bubble.hidden = false;
    place(node);
  }

  function hide() {
    clearTimeout(timer);
    timer = null;
    target = null;
    bubble.hidden = true;
  }

  function schedule(node, delay) {
    clearTimeout(timer);
    if (!eligible(node)) {
      hide();
      return;
    }
    timer = setTimeout(() => show(node), delay);
  }

  document.addEventListener('pointerover', (event) => {
    const node = event.target.closest?.('[data-tip]');
    if (node === target) return;
    if (!node) {
      hide();
      return;
    }
    schedule(node, SHOW_DELAY);
  });
  document.addEventListener('pointerout', (event) => {
    const node = event.target.closest?.('[data-tip]');
    if (node && !node.contains(event.relatedTarget)) hide();
  });
  document.addEventListener('focusin', (event) => {
    const node = event.target.closest?.('[data-tip]');
    // Keyboard focus shows the name at once; a mouse click must not pin a bubble.
    if (node && node.matches(':focus-visible')) schedule(node, 0);
    else hide();
  });
  document.addEventListener('focusout', hide);
  document.addEventListener('pointerdown', hide, true);
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') hide();
  });
  window.addEventListener('scroll', hide, true);
  window.addEventListener('resize', hide);

  return { hide };
}
