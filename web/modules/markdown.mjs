/** markdown: product-owned browser module; no startup side effects. */
import { escapeHtml, safeUrl } from './ui.mjs';
import { t } from './i18n.mjs';

const LATEX_COMMANDS = Object.freeze({
  times: "×", cdot: "·", div: "÷", pm: "±", mp: "∓",
  le: "≤", leq: "≤", ge: "≥", geq: "≥", ne: "≠",
  neq: "≠", approx: "≈", sim: "∼", infty: "∞",
  sum: "∑", prod: "∏", Delta: "Δ", delta: "δ",
  alpha: "α", beta: "β", gamma: "γ", theta: "θ",
  lambda: "λ", mu: "μ", sigma: "σ", pi: "π", rho: "ρ",
  "%": "%", "&": "&", _: "_", "#": "#", "$": "$",
});

const LATEX_SUPERSCRIPTS = Object.freeze({
  "0": "⁰", "1": "¹", "2": "²", "3": "³", "4": "⁴", "5": "⁵",
  "6": "⁶", "7": "⁷", "8": "⁸", "9": "⁹", "+": "⁺", "-": "⁻",
  "=": "⁼", "(": "⁽", ")": "⁾",
});

const LATEX_SUBSCRIPTS = Object.freeze({
  "0": "₀", "1": "₁", "2": "₂", "3": "₃", "4": "₄", "5": "₅",
  "6": "₆", "7": "₇", "8": "₈", "9": "₉", "+": "₊", "-": "₋",
  "=": "₌", "(": "₍", ")": "₎",
});

function latexScript(value, superscript) {
  const cleaned = String(value || "").replace(/\s+/g, " ").trim();
  const table = superscript ? LATEX_SUPERSCRIPTS : LATEX_SUBSCRIPTS;
  const translated = [...cleaned].map((character) => table[character] || character).join("");
  if (cleaned && /^[0-9+\-=()]+$/.test(cleaned)) return translated;
  return `${superscript ? "^" : "_"}(${cleaned})`;
}

function latexToText(value) {
  let text = String(value || "").trim();
  text = text.replace(/\\begin\{(?:aligned|align\*?|equation\*?|cases)\}/g, "");
  text = text.replace(/\\end\{(?:aligned|align\*?|equation\*?|cases)\}/g, "");
  text = text.replace(/\\\\/g, "; ").replace(/&/g, "");
  text = text.replace(/\\(?:left|right)\b/g, "");

  while (/\\(?:text|mathrm|mathbf|operatorname)\s*\{([^{}]*)\}/.test(text)) {
    text = text.replace(/\\(?:text|mathrm|mathbf|operatorname)\s*\{([^{}]*)\}/g, "$1");
  }
  while (/\\sqrt\s*\{([^{}]*)\}/.test(text)) {
    text = text.replace(/\\sqrt\s*\{([^{}]*)\}/g, "√($1)");
  }

  text = text.replace(/\^([A-Za-z0-9()+\-=])/g, (_all, script) => latexScript(script, true));
  text = text.replace(/_([A-Za-z0-9()+\-=])/g, (_all, script) => latexScript(script, false));
  while (/([_^])\s*\{([^{}]*)\}/.test(text)) {
    text = text.replace(/([_^])\s*\{([^{}]*)\}/g, (_all, marker, script) => (
      latexScript(script, marker === "^")
    ));
  }

  while (/\\frac\s*\{([^{}]*)\}\s*\{([^{}]*)\}/.test(text)) {
    text = text.replace(/\\frac\s*\{([^{}]*)\}\s*\{([^{}]*)\}/g, "($1)/($2)");
  }
  text = text.replace(/\\([A-Za-z]+|[%&_$#])/g, (_all, command) => (
    LATEX_COMMANDS[command] ?? command
  ));
  text = text.replace(/\\(?=\s)/g, "");
  text = text.replace(/\{([,.;:])\}/g, "$1");
  text = text.replace(/\{/g, "(").replace(/\}/g, ")");
  return text.replace(/\s+/g, " ").replace(/^[ ;]+|[ ;]+$/g, "");
}

function bareUrlParts(candidate) {
  let url = candidate;
  let suffix = "";
  while (url) {
    const escapedQuote = ["&quot;", "&#039;"].find((entity) => url.endsWith(entity));
    if (escapedQuote) {
      url = url.slice(0, -escapedQuote.length);
      suffix = escapedQuote + suffix;
      continue;
    }
    const tail = url.at(-1);
    if ('。，、；：！？）】"\'.,;:!?'.includes(tail)) {
      url = url.slice(0, -1);
      suffix = tail + suffix;
      continue;
    }
    if (tail === ")") {
      const opens = (url.match(/\(/g) || []).length;
      const closes = (url.match(/\)/g) || []).length;
      if (closes > opens) {
        url = url.slice(0, -1);
        suffix = `)${suffix}`;
        continue;
      }
    }
    break;
  }
  return { url, suffix };
}

export function citationSourceUrl(value) {
  const text = String(value || "");
  if (/[\s<>"'`\\\u0000-\u001f]/.test(text) || text.includes("…") || text.includes("...")) return null;
  try {
    const url = new URL(text);
    if (!["http:", "https:"].includes(url.protocol) || url.username || url.password
      || !/^[a-z0-9.-]+$/i.test(url.hostname)) return null;
    return url;
  } catch { return null; }
}

function referenceHeadingLabel(value) {
  return String(value || "")
    .replace(/^(?:\d+(?:[.．]\d+)*[.、)]?\s*|[（(]?[一二三四五六七八九十]+[）)、.]\s*)/, "")  // i18n-syntax:report-token
    .trim();
}

function isReferenceHeadingLabel(value) {
  return /^(?:完整\s*)?(?:参考文献|参考资料|引用来源|来源列表|证据与来源|来源|references|bibliography|sources|(?:complete\s+)?source\s+list)(?:\s*[（(]\s*(?:references|bibliography|sources)\s*[）)])?\s*[:：]?$/i.test(value);  // i18n-syntax:report-token
}

function isCompoundReferenceHeadingLabel(value) {
  return /^(?:来源[与和及、/／—\-:：].+|(?:references|bibliography|sources)\s+(?:and|&)\s+.+)$/i.test(value);  // i18n-syntax:report-token
}

export function reportCitationSources(markdown, sources = [], entryRanges = null) {
  const references = new Map();
  // ``compound`` is the active compound source chapter; ``region`` is the
  // heading span currently collecting entries and decides their semantics.
  let compound = null;
  let region = null;
  let fence = null;
  let pending = null;
  const commitEntry = ({ number, source, start, end }) => {
    // Render-only positions share the exact parser boundaries. Consumers still
    // check the final mapping, so a later conflicting entry invalidates both.
    if (source) entryRanges?.set(start, { number, url: source.url, end });
    if (references.has(number) && references.get(number)?.url !== source?.url) references.set(number, null);
    else if (!references.has(number)) references.set(number, source);
  };
  const closeCompound = () => {
    if (!compound) return;
    const entries = compound.entries;
    // Fail closed on sparse, duplicate or URL-less numbering: keep the whole
    // chapter as plain text instead of inventing a citation mapping.
    const complete = !compound.invalid && entries.length > 0
      && entries.every((item, index) => item.number === index + 1 && item.source);
    if (complete) entries.forEach(commitEntry);
    compound = null;
  };
  const flushEntry = () => {
    if (!pending) return;
    const { number, lines, invalid, start, end } = pending;
    pending = null;
    const body = lines.join("\n");
    // Accept one explicit URL, never infer a citation from discovery order.
    const links = [...body.matchAll(/\[([^\]\n]+)\]\(/g)];
    const link = links[0];
    const candidates = body.match(/https?:\/\/[^\s<。，、；：！？）】]+/gi) || [];
    let destination = "";
    if (link && links.length === 1) {
      // Authors may precede the link. Match its closing delimiter, not a
      // URL suffix: balanced parentheses and punctuation inside are data.
      const start = link.index + link[0].length;
      let depth = 1;
      for (let end = start; end < body.length; end += 1) {
        if (body[end] === "(") depth += 1;
        if (body[end] === ")" && --depth === 0) {
          destination = body.slice(start, end);
          break;
        }
      }
    } else if (!links.length && candidates.length === 1) {
      destination = bareUrlParts(candidates[0]).url;
    }
    const url = !invalid && !body.includes("`") && candidates.length === 1
      ? citationSourceUrl(destination) : null;
    let source = null;
    if (url) {
      const key = (value) => {
        const parsed = citationSourceUrl(value);
        if (!parsed) return "";
        parsed.hash = "";
        return parsed.href.replace(/\/$/, "");
      };
      const captured = sources.find((item) => key(item.url) === key(url.href));
      source = {
        ...captured, number, url: url.href, domain: url.hostname,
        title: (link?.[1] || (lines.length > 1 ? lines[0].trim() : "") || captured?.title || url.hostname).replace(/[*_`~]+/g, ""),
      };
    }
    // Compound chapters defer the commit until the whole direct body proves
    // to be a contiguous source list.
    if (region?.strict && compound) compound.entries.push({ number, source, start, end });
    else commitEntry({ number, source, start, end });
  };
  for (const [index, line] of String(markdown || "").replace(/\r/g, "").split("\n").entries()) {
    if (fence) {
      const closing = line.trim().match(/^(`{3,}|~{3,})\s*$/);
      if (closing && closing[1][0] === fence[0] && closing[1].length >= fence.length) fence = null;
      continue;
    }
    const opening = line.trim().match(/^(`{3,}|~{3,})([^\s`~]*)\s*$/);
    if (opening) { flushEntry(); fence = opening[1]; continue; }
    const heading = line.match(/^ {0,3}(#{1,6})\s+(.+?)\s*#*$/);
    if (heading) {
      flushEntry();
      const label = referenceHeadingLabel(heading[2]);
      const level = heading[1].length;
      if (isReferenceHeadingLabel(label)) {
        if (compound && level <= compound.level) closeCompound();
        // A named subgroup does not narrow the enclosing references chapter.
        if (region && level > region.level) region.strict = false;
        else region = { level, strict: false };
      } else if (isCompoundReferenceHeadingLabel(label)
        && (!region || region.strict || level <= region.level)) {
        // A compound heading nested in a lenient whitelist chapter never
        // reaches this branch: the whitelist's semantics stay unchanged.
        closeCompound();
        compound = { level, entries: [], invalid: false };
        region = { level, strict: true };
      } else if (compound && level <= compound.level) {
        closeCompound();
        region = null;
      } else if (region && level <= region.level) {
        region = null;
      } else if (region?.strict) {
        // A deeper subsection ends the compound chapter's direct entry body;
        // its content is never parsed as sources.
        region = null;
      }
      continue;
    }
    if (!region) continue;
    const entry = line.match(/^ {0,3}(?:\[(\d+)\][.:：]?|(\d+)[.)])(?:\s+(.*)|\s*)$/);
    if (entry) {
      flushEntry();
      const number = Number(entry[1] || entry[2]);
      if (Number.isSafeInteger(number) && number > 0) pending = {number, lines:[entry[3] || ""], invalid:false, start:index, end:index};
    } else if (!line.trim()) {
      flushEntry();
    } else if (pending) {
      // Only adjacent standalone URLs clearly continue a numbered title.
      // Keep all candidates until the next boundary, so two URLs fail closed.
      pending.lines.push(line.trim());
      pending.end = index;
      if (!/^https?:\/\/\S+$/i.test(line.trim())) pending.invalid = true;
    } else if (region.strict && compound) {
      // Non-entry prose directly under a compound source heading is not a
      // source list; keep the whole chapter as plain text.
      compound.invalid = true;
    }
  }
  flushEntry();
  closeCompound();
  return [...references.values()].filter(Boolean).sort((a, b) => a.number - b.number);
}

function citationMetadata(source, number, translator = t) {
  if (!source || typeof source !== "object") return null;
  const title = String(source.title || "").trim();
  let domain = String(source.domain || "").trim();
  if (!domain && source.url) {
    try { domain = new URL(String(source.url)).hostname; } catch { domain = ""; }
  }
  const sourceTitle = title || domain || translator("report:citation.sourceFallback", { number });
  const tip = domain && domain !== sourceTitle ? `${sourceTitle} · ${domain}` : sourceTitle;
  return { tip, ariaLabel: translator("report:citation.viewSourceAria", { number, title: tip }) };
}

// 报告语法 token 是双语的：模型按报告语言写 `[实]/[推]/[疑]` 或
// `[Verified]/[Inferred]/[Unverified]`。识别同时接受两种写法，角标文案跟随
// **标记自身的语言**而不是界面语言，否则英文报告会在中文界面下长出中文角标。
const QUALIFIER_BADGES = Object.freeze({
  zh: Object.freeze({ verified: "实", inferred: "推", hypothesis: "疑" }),  // i18n-syntax:report-token
  en: Object.freeze({ verified: "V", inferred: "I", hypothesis: "U" }),
});
const QUALIFIER_LABELS = Object.freeze({
  zh: Object.freeze({ verified: "已核验事实", inferred: "分析推断", hypothesis: "待验证假设" }),  // i18n-syntax:report-token
  en: Object.freeze({ verified: "Verified", inferred: "Inferred", hypothesis: "Unverified" }),
});
const QUALIFIER_TOKENS = Object.freeze([
  ["已核验事实", "verified", "zh"], ["分析推断", "inferred", "zh"], ["待验证假设", "hypothesis", "zh"],  // i18n-syntax:report-token
  ["实", "verified", "zh"], ["推", "inferred", "zh"], ["疑", "hypothesis", "zh"],  // i18n-syntax:report-token
  ["verified", "verified", "en"], ["inferred", "inferred", "en"], ["unverified", "hypothesis", "en"],
]);
const QUALIFIER_LOOKUP = new Map(
  QUALIFIER_TOKENS.map(([label, kind, language]) => [label, Object.freeze({ kind, language })]),
);

function qualifierKindOf(label) {
  return QUALIFIER_LOOKUP.get(String(label ?? "").toLowerCase()) ?? null;
}

function qualifierBadge(kind, language = "zh") {
  const badges = QUALIFIER_BADGES[language] ?? QUALIFIER_BADGES.zh;
  const labels = QUALIFIER_LABELS[language] ?? QUALIFIER_LABELS.zh;
  const text = badges[kind];
  const label = labels[kind];
  return `<span class="qualifier-badge qualifier-${kind}" role="note" aria-label="${label}" data-tip="${label}">${text}</span>`;
}

function qualifierBadgeFor(label) {
  const match = qualifierKindOf(label);
  return match ? qualifierBadge(match.kind, match.language) : null;
}

function inlineMarkdown(value, { citationPrefix = "", citationMap = new Map(), artifactResolver = null, translator = t } = {}) {
  let text = String(value ?? "");
  let marker = "\u0000";
  while (text.includes(marker)) marker += "\u0000";
  const tokens = [];
  const protect = (html) => {
    const token = `${marker}${tokens.length}${marker}`;
    tokens.push(html);
    return token;
  };

  text = text.replace(/`([^`\n]+)`/g, (_all, code) => protect(`<code>${escapeHtml(code)}</code>`));
  text = text.replace(/\\\((.*?)\\\)/g, (_all, formula) => protect(escapeHtml(latexToText(formula))));
  text = text.replace(/(?<!\\)\$(?!\$)([^$\n]+?)(?<!\\)\$/g, (_all, formula) => (
    protect(escapeHtml(latexToText(formula)))
  ));
  // Protect Markdown escapes before recognizing qualifiers or emphasis.
  text = text.replace(/\\([\\`*{}\[\]()#+\-.!_>])/g, (_all, literal) => protect(escapeHtml(literal)));
  text = escapeHtml(text);

  const linkPattern = /\[((?:[^\[\]\n]|\[[^\[\]\n]*\])+)\]\(/g;
  let linked = "";
  let cursor = 0;
  let searchFrom = 0;
  while (searchFrom < text.length) {
    linkPattern.lastIndex = searchFrom;
    const match = linkPattern.exec(text);
    if (!match) break;
    const destinationStart = linkPattern.lastIndex;
    let depth = 1;
    let destinationEnd = destinationStart;
    for (; destinationEnd < text.length; destinationEnd += 1) {
      if (text[destinationEnd] === "(") depth += 1;
      if (text[destinationEnd] === ")") {
        depth -= 1;
        if (depth === 0) break;
      }
    }
    if (depth !== 0) {
      searchFrom = destinationStart;
      continue;
    }
    const url = text.slice(destinationStart, destinationEnd);
    let anchor;
    if (!/^(?:https?:\/\/|#|sandbox:\/)[^\s<]+$/.test(url)) {
      // Even rejected links are literal, not a new qualifier parsing surface.
      anchor = text.slice(match.index, destinationEnd + 1);
    } else if (url.startsWith("sandbox:/")) {
      let resolved = "";
      try {
        resolved = typeof artifactResolver === "function"
          ? String(artifactResolver(url.slice("sandbox:/".length)) || "")
          : "";
      } catch {
        resolved = "";
      }
      const safe = /^\/api\/[^\s"'<>]+$/.test(resolved) ? escapeHtml(resolved) : "";
      anchor = safe
        ? `<a href="${safe}" target="_blank" rel="noopener noreferrer" download>${match[1]}</a>`
        : `📎 ${match[1]}`;
    } else {
      const internal = url.startsWith("#");
      anchor = `<a href="${safeUrl(url)}"${internal ? "" : ' target="_blank" rel="noopener noreferrer"'}>${match[1]}</a>`;
    }
    linked += text.slice(cursor, match.index) + protect(anchor);
    cursor = destinationEnd + 1;
    searchFrom = cursor;
  }
  text = linked + text.slice(cursor);
  const insideBareUrl = (source, offset) => /https?:\/\/[^\s<。，、；：！？）】]*$/.test(source.slice(0, offset));

  // 边界含英文句点：英文报告的性质标记常出现在句末（"…beneficiaries. [疑] (Section 8.3)"），
  // 漏掉 "." 会让标记裸露成普通文本。
  const qualifierBoundary = "(^|[。！？!?；;.]\\s*|\\n\\s*)";
  // 标记同时接受中英文写法；英文大小写不敏感（模型会写 Verified / VERIFIED）。
  const longQualifierWord = "(已核验事实|分析推断|待验证假设|Verified|Inferred|Unverified)";  // i18n-syntax:report-token
  const longQualifier = new RegExp(
    `${qualifierBoundary}(?:\\*\\*\\s*${longQualifierWord}\\s*(?:[：:]\\s*\\*\\*|\\*\\*\\s*[：:])|${longQualifierWord}\\s*[：:])\\s*`,
    "gi",
  );
  text = text.replace(longQualifier, (all, boundary, boldLabel, plainLabel, offset, source) => {
    if (insideBareUrl(source, offset + boundary.length)) return all;
    const badge = qualifierBadgeFor(boldLabel || plainLabel);
    return badge ? `${boundary}${protect(badge)}` : all;
  });
  const shortQualifierWord = "(实|推|疑|Verified|Inferred|Unverified)";  // i18n-syntax:report-token
  // 变体容错：角标词后必须以分隔符开头才能带后缀（[实—用户陈述]），否则
  // [实时]/[推送通知] 这类正常词汇会被误判；后缀只用于识别，不进入 DOM。
  const shortQualifierSuffix = "(?:[—–\\-:：|｜、\\s][^\\]\\n]{0,40})?";
  const shortQualifier = new RegExp(
    `${qualifierBoundary}(?:\\*\\*\\[${shortQualifierWord}${shortQualifierSuffix}\\]\\*\\*`
    + `|\\[${shortQualifierWord}${shortQualifierSuffix}\\])`,
    "gi",
  );
  text = text.replace(shortQualifier, (all, boundary, boldLabel, plainLabel, offset, source) => {
    if (insideBareUrl(source, offset + boundary.length)) return all;
    const badge = qualifierBadgeFor(boldLabel || plainLabel);
    return badge ? `${boundary}${protect(badge)}` : all;
  });

  const isWordCharacter = (character) => Boolean(character && /[\p{L}\p{N}]/u.test(character));
  const emphasize = (pattern, render, boundary = "none") => {
    text = text.replace(pattern, (all, content, offset, source) => {
      if (insideBareUrl(source, offset)) return all;
      const before = source[offset - 1];
      const after = source[offset + all.length];
      const touchesWord = isWordCharacter(before) || isWordCharacter(after);
      const insideWord = isWordCharacter(before) && isWordCharacter(after);
      if ((boundary === "strict" && touchesWord) || (boundary === "inside-word" && insideWord)) return all;
      return render(content);
    });
  };
  const strongContent = (content) => content
    .replace(/^(\s*)\[(实|推|疑|Verified|Inferred|Unverified)(?:[—–\-:：|｜、\s][^\]\n]{0,40})?\]/i, (all, space, label) => {  // i18n-syntax:report-token
      const badge = qualifierBadgeFor(label);
      return badge ? space + protect(badge) : all;
    })
    .replace(/^(\s*[^\[\]\n]+?[：:｜|]\s*)\[(实|推|疑|Verified|Inferred|Unverified)(?:[—–\-:：|｜、\s][^\]\n]{0,40})?\](\s*)$/i, (all, label, mark, space) => {  // i18n-syntax:report-token
      if (insideBareUrl(content, label.trimEnd().length)) return all;
      const badge = qualifierBadgeFor(mark);
      return badge ? label + protect(badge) + space : all;
    });
  emphasize(/\*\*\*([^*\n]+?)\*\*\*/g, (content) => `<strong><em>${strongContent(content)}</em></strong>`);
  emphasize(/___([^_\n]+?)___/g, (content) => `<strong><em>${strongContent(content)}</em></strong>`, "strict");
  emphasize(/\*\*([^*\n]+?)\*\*/g, (content) => `<strong>${strongContent(content)}</strong>`);
  emphasize(/__([^_\n]+?)__/g, (content) => `<strong>${strongContent(content)}</strong>`, "strict");
  emphasize(/\*([^*\n]+?)\*/g, (content) => `<em>${content}</em>`, "inside-word");
  emphasize(/_([^_\n]+?)_/g, (content) => `<em>${content}</em>`, "strict");
  // A short qualifier right after a closed emphasis block labels that block;
  // only real generated tags qualify, so unclosed markers and URL paths stay literal.
  text = text.replace(/(<\/(?:strong|em)>)([ \t]*)\[(实|推|疑|Verified|Inferred|Unverified)(?:[—–\-:：|｜、\s][^\]\n]{0,40})?\]/gi, (all, close, space, mark) => {  // i18n-syntax:report-token
    const badge = qualifierBadgeFor(mark);
    return badge ? `${close}${space}${protect(badge)}` : all;
  });
  // After emphasis delimiters, before citations: URL paths are literal.
  text = text.replace(/https?:\/\/[^\s<。，、；：！？）】]+/g, (candidate) => {
    const { url, suffix } = bareUrlParts(candidate);
    return protect(`<a href="${safeUrl(url)}" target="_blank" rel="noopener noreferrer">${url}</a>`) + suffix;
  });
  if (citationPrefix && citationMap.size) {
    const prefix = String(citationPrefix).replace(/[^\w-]/g, "-");
    text = text.replace(/\[(\d+)\]/g, (all, rawNumber) => {
      const number = Number(rawNumber);
      if (!Number.isSafeInteger(number) || number < 1) return all;
      const metadata = citationMetadata(citationMap.get(number), number, translator);
      if (!metadata) return all;
      return protect(`<sup class="citation"><a class="citation-link" href="#source-${prefix}-${number}" aria-label="${escapeHtml(metadata.ariaLabel)}" data-tip="${escapeHtml(metadata.tip)}">${number}</a></sup>`);
    });
  }
  for (let index = tokens.length - 1; index >= 0; index -= 1) {
    text = text.replaceAll(`${marker}${index}${marker}`, tokens[index]);
  }
  return text;
}

function markdownHeadingId(value, seen) {
  const base = String(value)
    .replace(/\*\*|__|`/g, "")
    .trim().toLowerCase()
    .replace(/[^\w\u3400-\u9fff]+/g, "-")
    .replace(/^-+|-+$/g, "") || "section";
  const count = seen.get(base) || 0;
  seen.set(base, count + 1);
  return count ? `${base}-${count + 1}` : base;
}

function renderChartFallback(raw, inline, translator = t) {
  try {
    const spec = JSON.parse(raw);
    const rawSeries = spec?.series === undefined ? [] : spec.series;
    if (!Array.isArray(spec?.labels) || !spec.labels.length || !Array.isArray(rawSeries)) throw new Error("invalid chart");
    const series = rawSeries.filter((item) => item && Array.isArray(item.data));
    const ranges = spec.type === "range_bar"
      ? [["low", "report:chart.lowHeader"], ["high", "report:chart.highHeader"]].filter(([key]) => Array.isArray(spec[key]))
      : [];
    if (!series.length && !ranges.length) throw new Error("invalid chart");
    const headers = [translator("report:chart.labelHeader"), ...ranges.map(([, key]) => translator(key)), ...series.map((item, index) => String(item.name || translator("report:chart.series", { index: index + 1 })))];
    const rows = spec.labels.map((label, index) => [
      label,
      ...ranges.map(([key]) => index < spec[key].length ? spec[key][index] : "—"),
      ...series.map((item) => index < item.data.length ? item.data[index] : "—"),
    ]);
    return `<div class="report-chart-fallback"><p>${inline(spec.title || translator("report:chart.dataTitle"))}</p><div class="report-table-wrap" tabindex="0" role="region" aria-label="${escapeHtml(translator("report:chart.dataTableAria"))}"><table><thead><tr>${headers.map((cell) => `<th>${inline(String(cell))}</th>`).join("")}</tr></thead><tbody>${rows.map((row) => `<tr>${row.map((cell) => `<td>${inline(typeof cell === "object" ? JSON.stringify(cell) : String(cell ?? ""))}</td>`).join("")}</tr>`).join("")}</tbody></table></div></div>`;
  } catch {
    return `<pre><code>${escapeHtml(raw)}</code></pre>`;
  }
}

// 后端在图表/流程图渲染失败时保留原始 JSON：无语言围栏 + 紧随其后的
// `<!-- chart degraded: … -->`。这里识别这个配对，围栏内容不再直显，
// 改为一条带失败原因的提示；原始数据仍留在 Markdown 源码里。
const CHART_DEGRADED_COMMENT = /^\s*<!--\s*chart degraded\s*:\s*(.*?)\s*-->\s*$/i;

function chartDegradedReasonAfter(lines, fenceEndIndex) {
  let cursor = fenceEndIndex + 1;
  while (cursor < lines.length && !lines[cursor].trim()) cursor += 1;
  const match = (lines[cursor] || "").match(CHART_DEGRADED_COMMENT);
  return match ? match[1].trim() : null;
}

function renderDegradedChart(reason, translator = t) {
  const detail = reason || translator("report:chart.degradedReasonUnknown");
  const text = translator("report:chart.degraded", { reason: detail });
  return `<div class="report-chart-fallback report-chart-degraded" role="note"><p>${escapeHtml(text)}</p></div>`;
}

function renderFlowFallback(raw, inline, translator = t) {
  try {
    const spec = JSON.parse(raw);
    if (!Array.isArray(spec?.nodes) || !Array.isArray(spec?.edges) || !spec.edges.length) throw new Error("invalid flow");
    const labels = new Map(spec.nodes
      .filter((node) => node && typeof node.id === "string")
      .map((node) => [node.id, String(node.label || node.id)]));
    const rows = spec.edges
      .filter((edge) => edge && typeof edge.from === "string" && typeof edge.to === "string")
      .map((edge) => [labels.get(edge.from) || edge.from, edge.label ? String(edge.label) : "—", labels.get(edge.to) || edge.to]);
    if (!rows.length) throw new Error("invalid flow");
    const headers = [translator("report:chart.flowStartHeader"), translator("report:chart.flowConditionHeader"), translator("report:chart.flowTargetHeader")];
    return `<div class="report-chart-fallback"><p>${inline(spec.title || translator("report:chart.flowTitle"))}</p><div class="report-table-wrap" tabindex="0" role="region" aria-label="${escapeHtml(translator("report:chart.flowStepAria"))}"><table><thead><tr>${headers.map((cell) => `<th>${inline(cell)}</th>`).join("")}</tr></thead><tbody>${rows.map((row) => `<tr>${row.map((cell) => `<td>${inline(String(cell ?? ""))}</td>`).join("")}</tr>`).join("")}</tbody></table></div></div>`;
  } catch {
    return `<pre><code>${escapeHtml(raw)}</code></pre>`;
  }
}

export function parseMarkdownImage(value) {
  const match = String(value || "").trim().match(/^!\[([^\]]*)\]\(([^)]+)\)$/);
  return match ? { alt: match[1], reference: match[2] } : null;
}

function figureParagraphKind(text) {
  const value = String(text ?? "").trim();
  if (!/[\r\n]/.test(value) && /^(?:图[ \t]*[0-9]+[：:]|Figure[ \t]*[0-9]+[:：.])/i.test(value)) return "caption";  // i18n-syntax:report-token
  if (/^(?:资料来源：|Sources?:)/i.test(value)) return "source";  // i18n-syntax:report-token
  if (/^(?:注：|Notes?:)\s*\S/i.test(value)) return "note";  // i18n-syntax:report-token
  return "";
}

function hasFigureCaptionAbove(lines, imageIndex) {
  let cursor = imageIndex - 1;
  while (cursor >= 0 && !lines[cursor].trim()) cursor -= 1;
  return cursor >= 0 && (cursor === 0 || !lines[cursor - 1].trim())
    && figureParagraphKind(lines[cursor].trim()) === "caption";
}

// 报告表格的短标签单元格（「等级」「重大」「中高」这类）在
// `overflow-wrap: anywhere` 下只有单字的最小内容宽度，自动表格布局会把整
// 列压成逐字竖排。这里只按可见文本给有限的 nowrap 保护：长正文、公司名、
// 长 URL 与含空白的单元格继续可换行，必要时整表横向滚动。
const COMPACT_CELL_MAX_CHARS = 6;

function plainTableCellText(value) {
  return String(value ?? "")
    .replace(/!\[([^\]]*)\]\([^)]*\)/g, "$1")
    .replace(/\[([^\]]*)\]\([^)]*\)/g, "$1")
    .replace(/[*_`~]/g, "")
    .trim();
}

function isCompactTableCell(value) {
  const text = plainTableCellText(value);
  if (!text || text.length > COMPACT_CELL_MAX_CHARS) return false;
  if (/[\s\u00a0]/.test(text)) return false;
  return !/^https?:\/\//i.test(text);
}

function renderMarkdownImage(alt, reference, options, captionAbove = false) {
  const translator = typeof options?.translator === "function" ? options.translator : t;
  const resolver = typeof options.imageResolver === "function" ? options.imageResolver : null;
  const resolved = resolver ? resolver(reference) : "";
  const safe = String(resolved || "").startsWith("/api/")
    ? String(resolved)
    : /^https?:\/\//.test(String(resolved || "")) ? safeUrl(resolved) : "";
  if (safe) {
    const caption = captionAbove ? "" : `<figcaption>${escapeHtml(alt)}</figcaption>`;
    const href = escapeHtml(safe);
    // 默认视图把大图缩放完整显示；同一安全 URL 同时作为图片链接与明确的
    // 「查看原图」入口，保留同 run / 分享 resolver 与权限，避免误触。
    const zoom = `<a class="report-chart-zoom" href="${href}" target="_blank" rel="noopener noreferrer"><img src="${href}" alt="${escapeHtml(alt)}" loading="lazy"></a>`;
    // 可见文案只走 CSS 伪元素与可访问属性：Range/root.textContent（审阅选区与
    // 服务端原文投影都依赖它）必须只看到报告原文，不能把 UI chrome 当成正文。
    const zoomLabel = escapeHtml(translator("report:chart.viewOriginal"));
    const zoomLink = `<a class="report-chart-zoom-link" href="${href}" target="_blank" rel="noopener noreferrer" aria-label="${zoomLabel}" data-label="${zoomLabel}"></a>`;
    return `<figure class="report-chart">${zoom}${zoomLink}${caption}</figure>`;
  }
  return `<div class="report-chart-placeholder" role="img" aria-label="${escapeHtml(alt)}">${escapeHtml(translator("report:chart.placeholder", { alt: alt || translator("report:chart.unnamed") }))}</div>`;
}

function displayFormulaAt(lines, start) {
  const trimmed = lines[start].trim();
  const delimiter = trimmed.startsWith("\\[")
    ? { opening: "\\[", closing: "\\]" }
    : trimmed.startsWith("$$") ? { opening: "$$", closing: "$$" } : null;
  if (!delimiter) return null;
  let value = trimmed.slice(delimiter.opening.length);
  for (let cursor = start; cursor < lines.length; cursor += 1) {
    if (cursor > start) value += `\n${lines[cursor]}`;
    const closingAt = value.indexOf(delimiter.closing);
    if (closingAt < 0) continue;
    if (value.slice(closingAt + delimiter.closing.length).trim()) return null;
    return { value: value.slice(0, closingAt), end: cursor };
  }
  return null;
}

export function renderMarkdown(markdown, options = {}) {
  const citationEntries = new Map();
  const citationMap = new Map(reportCitationSources(markdown, options.citationSources || [], citationEntries).map((source) => [source.number, source]));
  options = { ...options, citationMap, citationEntries };
  return renderMarkdownBlocks(markdown, options, new Map());
}

function renderMarkdownBlocks(markdown, options, headingIds, quoteDepth = 0) {
  // translator 可注入：分享页用内容语言实例渲染其 UI 兜底 copy（默认仍是全局实例）。
  const translator = typeof options?.translator === "function" ? options.translator : t;
  // Bound both call-stack depth and nested block padding. Beyond ordinary
  // nesting, preserve the entire remaining quote as escaped, scrollable text.
  if (quoteDepth >= 8) return `<pre class="report-quote-fallback"><code>${escapeHtml(markdown)}</code></pre>`;
  const lines = String(markdown || "").replace(/\r/g, "").split("\n");
  const out = [];
  const inline = (value) => inlineMarkdown(value, options);
  const sourceAnchors = new Set();
  let paragraph = [];
  let listRoots = [];
  let listStack = [];
  let code = null;

  const flushParagraph = () => {
    if (paragraph.length) {
      const text = paragraph.join("\n");
      const role = figureParagraphKind(text);
      out.push(`<p${role ? ` class="report-figure-${role}"` : ""}>${inline(text)}</p>`);
    }
    paragraph = [];
  };
  const renderList = (node) => {
    const start = node.type === "ol" && node.start !== 1 ? ` start="${node.start}"` : "";
    const items = node.items.map((item) => (
      `<li>${inline(item.lines.join("\n"))}${item.children.map(renderList).join("")}</li>`
    )).join("");
    return `<${node.type}${start}>${items}</${node.type}>`;
  };
  const flushList = () => {
    if (listRoots.length) out.push(listRoots.map(renderList).join(""));
    listRoots = [];
    listStack = [];
  };
  const flushCode = (closedFenceIndex = -1) => {
    if (code) {
      const raw = code.lines.join("\n");
      // 只有闭合的无语言围栏才可能与降级注释配对；未闭合围栏保持原样。
      const degradedReason = !code.language && closedFenceIndex >= 0
        ? chartDegradedReasonAfter(lines, closedFenceIndex)
        : null;
      out.push(degradedReason !== null
        ? renderDegradedChart(degradedReason, translator)
        : code.language === "chart"
          ? renderChartFallback(raw, inline, translator)
          : code.language === "flow"
            ? renderFlowFallback(raw, inline, translator)
            : `<pre><code>${escapeHtml(raw)}</code></pre>`);
    }
    code = null;
  };
  const nextContentLine = (from) => {
    let cursor = from;
    while (cursor < lines.length && !lines[cursor].trim()) cursor += 1;
    return lines[cursor] || "";
  };
  const indentation = (value) => String(value).replace(/\t/g, "    ").length;
  const parseListMarker = (value) => {
    const bullet = String(value).match(/^([ \t]*)[-*+]\s+(.+)/);
    if (bullet) return { indent: indentation(bullet[1]), type: "ul", start: 1, text: bullet[2] };
    const ordered = String(value).match(/^([ \t]*)(\d+)[.)]\s+(.+)/);
    return ordered
      ? { indent: indentation(ordered[1]), type: "ol", start: Number(ordered[2]), text: ordered[3] }
      : null;
  };
  const addListItem = (marker) => {
    while (listStack.length && marker.indent < listStack.at(-1).indent) listStack.pop();
    let entry = listStack.at(-1);
    if (!entry || marker.indent > entry.indent) {
      const node = { type: marker.type, start: marker.start, items: [] };
      if (entry?.node.items.length) entry.node.items.at(-1).children.push(node);
      else listRoots.push(node);
      entry = { indent: marker.indent, node };
      listStack.push(entry);
    } else if (entry.node.type !== marker.type) {
      listStack.pop();
      const parent = listStack.at(-1);
      const node = { type: marker.type, start: marker.start, items: [] };
      if (parent?.node.items.length && marker.indent > parent.indent) parent.node.items.at(-1).children.push(node);
      else listRoots.push(node);
      entry = { indent: marker.indent, node };
      listStack.push(entry);
    }
    entry.node.items.push({ lines: [marker.text], children: [] });
  };
  const appendListContinuation = (indent, text) => {
    let target = listStack.length - 1;
    while (target >= 0 && indent <= listStack[target].indent) target -= 1;
    if (target < 0) return false;
    listStack.length = target + 1;
    const item = listStack[target].node.items.at(-1);
    if (!item) return false;
    item.lines.push(text);
    return true;
  };
  const splitTableRow = (value) => {
    const source = String(value).trim().replace(/^\|/, "").replace(/\|\s*$/, "");
    const cells = [];
    let cell = "";
    for (let index = 0; index < source.length; index += 1) {
      if (source[index] !== "|") {
        cell += source[index];
        continue;
      }
      let slashCount = 0;
      for (let cursor = index - 1; cursor >= 0 && source[cursor] === "\\"; cursor -= 1) slashCount += 1;
      if (slashCount % 2) {
        cell = `${cell.slice(0, -1)}|`;
      } else {
        cells.push(cell.trim());
        cell = "";
      }
    }
    cells.push(cell.trim());
    return cells;
  };
  const normalizeTableRow = (row, columns) => {
    const cells = row.slice(0, columns);
    while (cells.length < columns) cells.push("");
    return cells;
  };

  for (let index = 0; index < lines.length; index += 1) {
    const line = lines[index];
    const trimmed = line.trim();
    if (code) {
      const closingFence = trimmed.match(/^(`{3,}|~{3,})\s*$/);
      if (closingFence && closingFence[1][0] === code.fenceCharacter && closingFence[1].length >= code.fenceLength) flushCode(index);
      else code.lines.push(line);
      continue;
    }
    const entry = !quoteDepth && options.citationPrefix && options.citationEntries.get(index);
    const source = entry && options.citationMap.get(entry.number);
    if (source && source.url === entry.url) {
      flushParagraph(); flushList();
      const prefix = String(options.citationPrefix).replace(/[^\w-]/g, "-");
      // Repeated identical references retain their text, but only the first
      // occurrence owns the anchor. Keep the entry number literal.
      const id = sourceAnchors.has(source.number) ? "" : ` id="source-${prefix}-${source.number}"`;
      sourceAnchors.add(source.number);
      const text = lines.slice(index, entry.end + 1).map((line) => line.trim()).join("\n");
      out.push(`<p class="report-source-entry"${id}>${inlineMarkdown(text, { ...options, citationMap: new Map() })}</p>`);
      index = entry.end;
      continue;
    }
    const quoteLine = line.match(/^ {0,3}>[ \t]?(.*)$/);
    if (quoteLine) {
      flushParagraph(); flushList();
      const quoted = [quoteLine[1]];
      while (index + 1 < lines.length) {
        const nextQuoteLine = lines[index + 1].match(/^ {0,3}>[ \t]?(.*)$/);
        if (!nextQuoteLine) break;
        quoted.push(nextQuoteLine[1]);
        index += 1;
      }
      out.push(`<blockquote>${renderMarkdownBlocks(quoted.join("\n"), options, headingIds, quoteDepth + 1)}</blockquote>`);
      continue;
    }
    if ((trimmed.match(/`{3,}/g) || []).length >= 2) {
      flushParagraph(); flushList();
      out.push(`<p>${escapeHtml(trimmed)}</p>`);
      continue;
    }
    const openingFence = trimmed.match(/^(`{3,}|~{3,})([^\s`~]*)\s*$/);
    if (openingFence) {
      flushParagraph(); flushList();
      code = { language: (openingFence[2] || "").toLowerCase(), fenceCharacter: openingFence[1][0], fenceLength: openingFence[1].length, lines: [] };
      continue;
    }
    const displayFormula = displayFormulaAt(lines, index);
    if (displayFormula) {
      flushParagraph(); flushList();
      const formula = latexToText(displayFormula.value);
      if (formula) out.push(`<p>${escapeHtml(translator("report:formula.prefix", { formula }))}</p>`);
      index = displayFormula.end;
      continue;
    }
    if (/^\s*<!--.*?-->\s*$/.test(line)) continue;
    if (/^\s*(?:-{3,}|_{3,}|\*{3,})\s*$/.test(line)) {
      flushParagraph(); flushList(); out.push("<hr>"); continue;
    }
    const headerCells = splitTableRow(line);
    const delimiterCells = splitTableRow(lines[index + 1] || "");
    if (headerCells.length > 1
      && delimiterCells.length === headerCells.length
      && delimiterCells.every((cell) => /^:?-{3,}:?$/.test(cell))) {
      flushParagraph(); flushList();
      const rows = [];
      let cursor = index + 2;
      while (cursor < lines.length && lines[cursor].trim() && lines[cursor].includes("|")) {
        rows.push(normalizeTableRow(splitTableRow(lines[cursor]), headerCells.length));
        cursor += 1;
      }
      index = cursor - 1;
      const cell = (tag, value) => `<${tag}${isCompactTableCell(value) ? ' class="report-cell-compact"' : ""}>${inline(value)}</${tag}>`;
      out.push(`<div class="report-table-wrap" tabindex="0" role="region" aria-label="${escapeHtml(translator("report:table.aria"))}"><table><thead><tr>${headerCells.map((value) => cell("th", value)).join("")}</tr></thead><tbody>${rows.map((row) => `<tr>${row.map((value) => cell("td", value)).join("")}</tr>`).join("")}</tbody></table></div>`);
      continue;
    }
    const heading = line.match(/^(#{1,4})\s+(.+)/);
    if (heading) {
      flushParagraph(); flushList();
      const id = markdownHeadingId(heading[2], headingIds);
      out.push(`<h${heading[1].length} id="${id}">${inline(heading[2])}</h${heading[1].length}>`);
      continue;
    }
    const listMarker = parseListMarker(line);
    if (listMarker) {
      flushParagraph();
      addListItem(listMarker);
      continue;
    }
    const continuation = line.match(/^([ \t]+)(\S.*)$/);
    if (continuation && appendListContinuation(indentation(continuation[1]), continuation[2].trim())) {
      continue;
    }
    const image = parseMarkdownImage(line);
    if (image) {
      flushParagraph(); flushList();
      out.push(renderMarkdownImage(image.alt, image.reference, options, hasFigureCaptionAbove(lines, index)));
      continue;
    }
    if (!line.trim()) {
      flushParagraph();
      if (listRoots.length) {
        const next = nextContentLine(index + 1);
        if (!parseListMarker(next) && !/^[ \t]+\S/.test(next)) flushList();
      }
      continue;
    }
    flushList();
    paragraph.push(line.trim());
  }
  flushParagraph(); flushList(); flushCode();
  return out.join("");
}
