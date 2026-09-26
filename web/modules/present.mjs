/**
 * Product vocabulary: how runtime facts are *said* to the reader.
 *
 * The event model (`run-stream.mjs`) keeps the engine's own words — tool names,
 * stage codes, raw paths, token counts. None of those are product language: a
 * professional reading the workbench wants "检索 · 跨境资金监管", not
 * `web_search  query: · queries: …`, and never the host's filesystem layout.
 * Every string the workbench shows about a run goes through this module, so the
 * wording lives in one place and is testable without a browser
 * (`tests/web/present.test.mjs`).
 *
 * Nothing here names the engine or the execution surface: which process, which
 * container, which binary ran the research is an operator concern, not a reader
 * concern.
 */

/** Run status → the word the reader sees. "Cancelled" is the reader stopping it, not a fault. */
export const STATUS_LABELS = Object.freeze({
  running: '进行中',
  finished: '已完成',
  cancelled: '已停止',
  failed: '未完成',
});

export function statusLabel(status) {
  return STATUS_LABELS[status] ?? '未知状态';
}

/** 42 秒 · 9 分 13 秒 · 1 小时 5 分. Raw floats ("537.838s") are not a reading format. */
export function formatDuration(seconds) {
  const total = Math.round(Number(seconds));
  if (!Number.isFinite(total) || total <= 0) return '';
  if (total < 60) return `${total} 秒`;
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const rest = total % 60;
  if (hours > 0) return minutes ? `${hours} 小时 ${minutes} 分` : `${hours} 小时`;
  return rest ? `${minutes} 分 ${rest} 秒` : `${minutes} 分`;
}

/** Token usage in the register of a Chinese reader: 8,420 · 约 34.9 万. */
export function formatTokens(count) {
  const value = Number(count);
  if (!Number.isFinite(value) || value <= 0) return '';
  if (value < 10000) return value.toLocaleString('zh-CN');
  return `约 ${(value / 10000).toFixed(1).replace(/\.0$/, '')} 万`;
}

/** Model identifiers carry a routing prefix ("openai/…"); the reader only needs the model. */
export function modelLabel(model) {
  const text = String(model ?? '').trim();
  if (!text) return '';
  return text.includes('/') ? text.slice(text.lastIndexOf('/') + 1) : text;
}

// A full stop ends a clause only when it is not inside a word or file name (notes.md, v1.2).
const CLAUSE_END = /[，。；！？,;!?\n]|\.(?=\s|$)/;

/**
 * A research title from the first instruction: its first clause, capped at 20
 * characters (规范 PH-04：≤ 20 字，不抄整句). The full prompt stays in the bubble.
 */
export function researchTitle(prompt, max = 20) {
  const text = String(prompt ?? '').replace(/\s+/g, ' ').trim();
  if (!text) return '新研究';
  const cut = text.search(CLAUSE_END);
  const clause = (cut > 3 ? text.slice(0, cut) : text).trim();
  const chars = Array.from(clause);
  return chars.length > max ? `${chars.slice(0, max).join('')}…` : clause;
}

/** Relative time for list rows, same register as the standard library's 历史行. */
export function relativeTime(seconds, now = Date.now() / 1000) {
  const stamp = Number(seconds);
  if (!Number.isFinite(stamp) || stamp <= 0) return '';
  const delta = now - stamp;
  if (delta < 60) return '刚刚';
  if (delta < 3600) return `${Math.floor(delta / 60)} 分钟前`;
  if (delta < 86400) return `${Math.floor(delta / 3600)} 小时前`;
  if (delta < 172800) return '昨天';
  if (delta < 2592000) return `${Math.floor(delta / 86400)} 天前`;
  const date = new Date(stamp * 1000);
  return `${date.getMonth() + 1} 月 ${date.getDate()} 日`;
}

/* ── paths ─────────────────────────────────────────────────────────────────
 * Every execution surface puts the research workspace somewhere different
 * (a per-run directory under the host's data root, `/workspace` in a container,
 * `/home/user/workspace` in a sandbox). The reader only ever needs "the
 * workspace", so all three spellings collapse to 工作区 and the host's directory
 * layout (user name included) never reaches the screen. */
const HOST_WORKSPACE_RE = /(?:\/[^\s'"`()<>]+)?\/workspaces\/[0-9a-f]{6,}(?![0-9a-z])/gi;
const SANDBOX_WORKSPACE_RE = /(^|[\s'"`(=:])(?:\/home\/user)?\/workspace(?=\/|[\s'"`),:]|$)/gm;

export function displayPath(text) {
  return String(text ?? '')
    .replace(HOST_WORKSPACE_RE, '工作区')
    .replace(SANDBOX_WORKSPACE_RE, '$1工作区');
}

/** A path argument as a chip: relative to the workspace when it lives there. */
function pathObject(value) {
  const shown = displayPath(value);
  if (shown === '工作区') return '工作区';
  return shown.startsWith('工作区/') ? shown.slice('工作区/'.length) : shown;
}

function firstText(values) {
  for (const value of values) {
    if (typeof value === 'string' && value.trim()) return value.trim();
    if (Array.isArray(value)) {
      const strings = value.filter((item) => typeof item === 'string' && item.trim());
      if (strings.length) return strings.length > 1 ? `${strings[0].trim()} 等 ${strings.length} 条` : strings[0].trim();
    }
  }
  return '';
}

function oneLine(text, limit = 96) {
  const line = String(text ?? '').split('\n').find((part) => part.trim()) ?? '';
  const trimmed = line.trim();
  return trimmed.length > limit ? `${trimmed.slice(0, limit)}…` : trimmed;
}

function urlObject(value) {
  if (Array.isArray(value)) {
    const urls = value.filter((item) => typeof item === 'string');
    if (!urls.length) return '';
    return urls.length > 1 ? `${urlObject(urls[0])} 等 ${urls.length} 个网页` : urlObject(urls[0]);
  }
  try {
    const url = new URL(String(value));
    const path = url.pathname === '/' ? '' : url.pathname;
    return `${url.hostname}${path}`;
  } catch {
    return oneLine(value);
  }
}

/** Parse one delegated task: the projection hands array items over as text. */
function parseTask(item) {
  if (item && typeof item === 'object') {
    return { agent: String(item.agent ?? item.name ?? ''), task: String(item.task ?? item.prompt ?? '') };
  }
  const text = typeof item === 'string' ? item.trim() : '';
  if (text.startsWith('{')) {
    try {
      const parsed = JSON.parse(text);
      if (parsed && typeof parsed === 'object') {
        return { agent: String(parsed.agent ?? parsed.name ?? ''), task: String(parsed.task ?? parsed.prompt ?? '') };
      }
    } catch {
      /* A task long enough to be clipped is no longer valid JSON (the host bounds
       * every array item): read the fields straight out of the JSON prefix. */
      const agent = /"agent"\s*:\s*"([^"]*)"/.exec(text);
      const task = /"(?:task|prompt)"\s*:\s*"([^"]*)"/.exec(text);
      if (agent || task) return { agent: agent?.[1] ?? '', task: task?.[1] ?? '' };
    }
  }
  return { agent: '', task: text };
}

/** The read-only subagent actions the guard lets through, in reader language. */
const SUBAGENT_ACTIONS = {
  list: '列出可用子代理',
  get: '查看子代理定义',
  status: '查看子代理状态',
  doctor: '检查子代理配置',
  models: '查看可用模型',
};

/** What a delegation acted on: the agents and tasks of one batch, or a control action. */
function subagentObject(args) {
  const raw = args.tasks ?? args.task ?? args.prompt;
  const items = Array.isArray(raw) ? raw : raw === undefined ? [] : [raw];
  const parsed = items.map(parseTask).filter((item) => item.agent || item.task);
  if (!parsed.length) {
    // A management action (list/get/status) has no tasks: say what it did, in
    // the reader's language — engine action names never appear on screen.
    const action = String(args.action ?? '').trim().toLowerCase();
    if (action) return SUBAGENT_ACTIONS[action] ?? '';
    return oneLine(args.agent ?? '');
  }
  const [first] = parsed;
  const label = [first.agent, oneLine(first.task, 56)].filter(Boolean).join('：');
  return parsed.length > 1 ? `${label} 等 ${parsed.length} 个子代理` : label;
}

/**
 * Tool call → (icon, action, running label, object). The action is a verb the
 * reader recognises; the object is what it acted on, in human form. Engine tool
 * names never appear on screen.
 */
const TOOL_RULES = [
  { test: /^(web_?search|search_web|websearch)$/i, icon: 'search', action: '检索', active: '正在检索',
    object: (args) => firstText([args.query, args.queries, args.q]) },
  { test: /^get_search_content$/i, icon: 'file-text', action: '读取检索结果', active: '正在读取检索结果',
    object: (args) => firstText([args.query, args.url]) },
  { test: /fetch|browse|webfetch|http|scrape/i, icon: 'globe', action: '读取网页', active: '正在读取网页',
    object: (args) => urlObject(args.url ?? args.urls ?? args.href ?? '') },
  { test: /^(bash|shell|terminal|exec|run_command)$/i, icon: 'terminal', action: '执行', active: '正在执行命令',
    object: (args) => oneLine(args.command ?? args.cmd), mono: true },
  { test: /^(read|view|cat|open_file|read_file)$/i, icon: 'file-text', action: '读取', active: '正在读取文件',
    object: (args) => pathObject(args.path ?? args.file_path ?? args.filePath ?? ''), mono: true },
  { test: /^(write|create|create_file|write_file)$/i, icon: 'new-chat', action: '写入', active: '正在写入文件',
    object: (args) => pathObject(args.path ?? args.file_path ?? args.filePath ?? ''), mono: true },
  { test: /edit|apply|patch/i, icon: 'new-chat', action: '修改', active: '正在修改文件',
    object: (args) => pathObject(args.path ?? args.file_path ?? args.filePath ?? ''), mono: true },
  { test: /^(subagent|task|spawn_agent|delegate)$/i, icon: 'agent', action: '派发子代理', active: '正在派发子代理',
    object: (args) => subagentObject(args) },
  { test: /^(grep|find|glob|ls|list|search)$/i, icon: 'search', action: '查找', active: '正在查找',
    object: (args) => oneLine(args.pattern ?? args.query ?? pathObject(args.path ?? '')), mono: true },
  { test: /todo|plan/i, icon: 'checklist', action: '更新计划', active: '正在更新计划',
    object: () => '' },
];

export function describeTool(name, args = {}) {
  const safeArgs = args && typeof args === 'object' ? args : {};
  const text = String(name ?? '');
  const rule = TOOL_RULES.find((candidate) => candidate.test.test(text));
  if (rule) {
    return {
      icon: rule.icon,
      action: rule.action,
      active: rule.active,
      object: displayPath(rule.object(safeArgs) || ''),
      mono: Boolean(rule.mono),
    };
  }
  const fallback = Object.values(safeArgs).find((value) => typeof value === 'string' && value.trim());
  return { icon: 'wrench', action: '调用工具', active: '正在调用工具', object: displayPath(oneLine(fallback ?? '')), mono: false };
}

/** Tool row states that deserve words; a finished call says nothing (the row itself is the record). */
export function toolStateLabel(item) {
  if (item.status === 'error' || item.isError) return '失败';
  if (item.status === 'interrupted') return '已中断';
  if (item.status === 'cancelled') return '已停止';
  return '';
}

/* ── ChildRun（子代理，§3.5） ────────────────────────────────────────────────
 * A dispatched subagent is a first-class object of the run: the timeline shows
 * a dispatch card where it happened, and the detail area lists every child
 * with its status. The words follow the tool cards' register — a verb the
 * reader recognises plus what it acted on, never the engine's call ids. */

/** The dispatch card, in the same shape describeTool returns. */
export function describeChild(item) {
  const agent = String(item?.agent ?? '').trim();
  return {
    icon: 'agent',
    action: agent ? `派发子代理 · ${agent}` : '派发子代理',
    active: agent ? `子代理 ${agent} 运行中` : '子代理运行中',
    object: oneLine(item?.task ?? '', 56),
  };
}

/** Card states that deserve words; a finished child says nothing extra. */
export function childStateLabel(item) {
  if (item?.status === 'failed') return '失败';
  if (item?.status === 'interrupted') return '已中断';
  if (item?.status === 'cancelled') return '已停止';
  return '';
}

/** One child as a row of words: agent：task, degrading to what is known. */
export function childLabel(child) {
  const agent = String(child?.agent ?? '').trim();
  const task = oneLine(child?.task ?? '', 56);
  return [agent, task].filter(Boolean).join('：') || '子代理';
}

/** The panel row's status word — unlike the card, a running child is named here. */
export function childStatusText(status) {
  return {
    running: '进行中',
    finished: '已完成',
    failed: '失败',
    interrupted: '已中断',
    cancelled: '已停止',
  }[status] ?? '未知状态';
}

/**
 * R3a 运行中进度作为一短行事实：3 步 · 最近读取 report.md（没有就空串）。
 * 子会话的工具名同样是引擎词汇，过与主时间轴同一张读者语言表；字节数是
 * 模型事实，不上屏。
 */
export function childProgressText(child) {
  const progress = child?.progress;
  if (!progress || typeof progress !== 'object') return '';
  const parts = [];
  const steps = Number(progress.steps);
  if (Number.isFinite(steps) && steps > 0) parts.push(`${steps} 步`);
  const tool = String(progress.lastToolName ?? '').trim();
  if (tool) {
    const described = describeTool(tool, {});
    const target = oneLine(displayPath(progress.lastToolTarget ?? ''), 40);
    parts.push(`最近${described.action}${target ? ` ${target}` : ''}`);
  }
  return parts.join(' · ');
}

/**
 * The dispatch card's state slot: running progress while the child works,
 * the status word once it settles — a settled card never keeps showing
 * progress, and a child without a progress source shows nothing extra.
 */
export function childCardState(item) {
  if (item?.status === 'running') return childProgressText(item);
  return childStateLabel(item);
}

/** The children panel's title; the panel itself only exists with children. */
export function childrenPanelTitle(count) {
  return count > 1 ? `派发了 ${count} 个子代理` : '派发了 1 个子代理';
}

/** R2 enrichment as one short row of facts: 43 秒 · 6 次工具调用（没有就空串）。 */
export function childFacts(child) {
  const facts = [];
  if (typeof child?.durationMs === 'number' && child.durationMs > 0) {
    facts.push(formatDuration(child.durationMs / 1000));
  }
  if (typeof child?.toolCount === 'number' && child.toolCount > 0) {
    facts.push(`${child.toolCount} 次工具调用`);
  }
  return facts.filter(Boolean).join(' · ');
}

/* ── stages & notices ─────────────────────────────────────────────────────
 * Lifecycle stages (a turn starting, the agent loop ending, the run finishing)
 * are bookkeeping; the process header already says where the run is. Only the
 * stages that change what the reader should expect are shown. */
const VISIBLE_STAGES = {
  compaction_start: '正在整理上下文',
  compaction_end: '上下文整理完成',
  auto_retry_start: '模型服务暂时不可用，正在自动重试',
  auto_retry_end: '重试完成',
};

export function stageText(item) {
  const base = VISIBLE_STAGES[item.stage];
  if (!base) return null;
  if (item.stage === 'auto_retry_start' && item.attempt) return `${base}（第 ${item.attempt} 次）`;
  return base;
}

const NOTICE_TEXT = {
  extension_error: '有一项扩展能力未能正常加载，研究会继续进行，但部分能力可能不可用',
};

export function noticeText(item) {
  return NOTICE_TEXT[item.message] ?? '研究过程中出现了一条提示';
}

/** A download name the reader can file away: 研究报告-<title>.md. */
export function reportFileName(prompt) {
  const title = researchTitle(prompt, 24).replace(/…$/, '').replace(/[\\/:*?"<>|\s]+/g, ' ').trim();
  return `研究报告${title ? `-${title}` : ''}.md`;
}
