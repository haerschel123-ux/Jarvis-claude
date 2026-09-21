/* DOM helpers, a Markdown renderer and a small syntax highlighter.

   Everything the user or a model produces is inserted as *text*, never as HTML, so a model
   response or a file name can never inject markup (Spec §90). */

export function el(tag, attributes = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attributes)) {
    if (value === null || value === undefined || value === false) continue;
    if (key === "class") node.className = value;
    else if (key === "dataset") Object.assign(node.dataset, value);
    else if (key === "style" && typeof value === "object") Object.assign(node.style, value);
    else if (key.startsWith("on") && typeof value === "function") {
      node.addEventListener(key.slice(2).toLowerCase(), value);
    } else if (key === "html") node.innerHTML = value;   // only for trusted, local markup
    else if (value === true) node.setAttribute(key, "");
    else node.setAttribute(key, String(value));
  }
  appendAll(node, children);
  return node;
}

function appendAll(node, children) {
  for (const child of children.flat(Infinity)) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
}

export function clear(node) {
  while (node.firstChild) node.removeChild(node.firstChild);
  return node;
}

export function mount(node, ...children) {
  clear(node);
  appendAll(node, children);
  return node;
}

/** Inline SVG icons, drawn from a small set so the UI needs no icon font or sprite file. */
const ICONS = {
  dashboard: "M4 13h6V4H4v9Zm0 7h6v-5H4v5Zm10 0h6v-9h-6v9Zm0-16v5h6V4h-6Z",
  chat: "M21 12a8 8 0 0 1-11.6 7.1L4 20l1-4.4A8 8 0 1 1 21 12Z",
  projects: "M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V7Z",
  tasks: "M9 5h10M9 12h10M9 19h10M4 5l1.5 1.5L8 4M4 12l1.5 1.5L8 11M4 19l1.5 1.5L8 18",
  memory: "M12 3a4 4 0 0 0-4 4v1a3 3 0 0 0 0 6v2a3 3 0 0 0 6 0V7a4 4 0 0 0-2-4Zm0 0a4 4 0 0 1 4 4v1a3 3 0 0 1 0 6v2a3 3 0 0 1-6 0",
  automations: "M12 2v3m0 14v3M2 12h3m14 0h3M5 5l2 2m10 10 2 2M5 19l2-2M17 7l2-2M12 8a4 4 0 1 0 0 8 4 4 0 0 0 0-8Z",
  integrations: "M10 3H5a2 2 0 0 0-2 2v5m7-7h9a2 2 0 0 1 2 2v5M10 3v18m0 0H5a2 2 0 0 1-2-2v-5m7 7h9a2 2 0 0 0 2-2v-5",
  settings: "M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6Zm7.4-3a7.4 7.4 0 0 0-.1-1.2l2-1.6-2-3.4-2.4 1a7.5 7.5 0 0 0-2-1.2L14.5 2h-5l-.4 2.6a7.5 7.5 0 0 0-2 1.2l-2.4-1-2 3.4 2 1.6a7.4 7.4 0 0 0 0 2.4l-2 1.6 2 3.4 2.4-1a7.5 7.5 0 0 0 2 1.2l.4 2.6h5l.4-2.6a7.5 7.5 0 0 0 2-1.2l2.4 1 2-3.4-2-1.6c.06-.4.1-.8.1-1.2Z",
  mic: "M12 15a3 3 0 0 0 3-3V6a3 3 0 0 0-6 0v6a3 3 0 0 0 3 3Zm7-3a7 7 0 0 1-14 0m7 7v4",
  send: "m4 12 16-8-5 8 5 8-16-8Z",
  stop: "M7 7h10v10H7z",
  plus: "M12 5v14M5 12h14",
  file: "M14 3v5h5M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8l-5-5Z",
  screen: "M3 5h18v11H3zM8 20h8M12 16v4",
  trash: "M4 7h16M10 11v6M14 11v6M6 7l1 13h10l1-13M9 7V4h6v3",
  copy: "M9 9h10v12H9zM5 15V3h10v2",
  retry: "M20 12a8 8 0 1 1-2.3-5.7M20 4v5h-5",
  edit: "M4 20h4L19 9l-4-4L4 16v4ZM14 6l4 4",
  check: "m5 13 4 4L19 7",
  x: "M6 6l12 12M18 6 6 18",
  warn: "M12 9v4m0 4h.01M10.3 3.9 2.4 17.5A2 2 0 0 0 4.1 20.5h15.8a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0Z",
  info: "M12 16v-4m0-4h.01M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0Z",
  refresh: "M4 12a8 8 0 0 1 13.7-5.7L20 8M20 4v4h-4M20 12a8 8 0 0 1-13.7 5.7L4 16m0 4v-4h4",
  search: "M11 19a8 8 0 1 0 0-16 8 8 0 0 0 0 16Zm10 2-4.3-4.3",
  pin: "M12 17v5M9 3h6l-1 6 3 3v2H7v-2l3-3-1-6Z",
  cpu: "M9 3v3m6-3v3M9 18v3m6-3v3M3 9h3m-3 6h3m12-6h3m-3 6h3M6 6h12v12H6z",
  shield: "M12 3 4 6v6c0 5 3.4 8.5 8 9.5 4.6-1 8-4.5 8-9.5V6l-8-3Z",
  bolt: "m13 2-9 12h7l-1 8 9-12h-7l1-8Z",
  clock: "M12 7v5l3 2m6-2a9 9 0 1 1-18 0 9 9 0 0 1 18 0Z",
  folder: "M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V7Z",
  github: "M9 19c-4 1.5-4-2.5-6-3m12 5v-3.5c0-1 .1-1.4-.5-2 2.8-.3 5.5-1.4 5.5-6A4.6 4.6 0 0 0 18.9 6a4.3 4.3 0 0 0-.1-3.2s-1.1-.3-3.5 1.3a12 12 0 0 0-6.6 0C6.3 2.5 5.2 2.8 5.2 2.8A4.3 4.3 0 0 0 5 6a4.6 4.6 0 0 0-1.1 3.5c0 4.6 2.7 5.7 5.5 6-.6.6-.6 1.2-.5 2V21",
  brain: "M12 3a4 4 0 0 0-4 4v1a3 3 0 0 0 0 6v2a3 3 0 0 0 6 0V7a4 4 0 0 0-2-4Z",
};

export function icon(name, size = 18, extraClass = "") {
  const path = ICONS[name] ?? ICONS.info;
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("width", String(size));
  svg.setAttribute("height", String(size));
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("fill", "none");
  svg.setAttribute("stroke", "currentColor");
  svg.setAttribute("stroke-width", "1.7");
  svg.setAttribute("stroke-linecap", "round");
  svg.setAttribute("stroke-linejoin", "round");
  svg.setAttribute("aria-hidden", "true");
  if (extraClass) svg.setAttribute("class", extraClass);
  const node = document.createElementNS("http://www.w3.org/2000/svg", "path");
  node.setAttribute("d", path);
  svg.append(node);
  return svg;
}

/* --- formatting ---------------------------------------------------------------------- */

export function formatTime(value) {
  const date = value instanceof Date ? value : new Date(typeof value === "number" ? value * 1000 : value);
  if (Number.isNaN(date.getTime())) return "";
  return date.toLocaleTimeString("de-DE", { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

export function formatDateTime(value) {
  const date = value instanceof Date ? value : new Date(typeof value === "number" ? value * 1000 : value);
  if (Number.isNaN(date.getTime())) return "";
  return date.toLocaleString("de-DE", {
    day: "2-digit", month: "2-digit", year: "numeric", hour: "2-digit", minute: "2-digit",
  });
}

export function formatRelative(value) {
  const date = value instanceof Date ? value : new Date(typeof value === "number" ? value * 1000 : value);
  if (Number.isNaN(date.getTime())) return "";
  const seconds = Math.round((Date.now() - date.getTime()) / 1000);
  if (seconds < 60) return "gerade eben";
  if (seconds < 3600) return `vor ${Math.floor(seconds / 60)} min`;
  if (seconds < 86400) return `vor ${Math.floor(seconds / 3600)} h`;
  return `vor ${Math.floor(seconds / 86400)} Tagen`;
}

export function formatBytes(bytes) {
  if (bytes === null || bytes === undefined) return "—";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let value = Number(bytes);
  let index = 0;
  while (value >= 1024 && index < units.length - 1) {
    value /= 1024;
    index += 1;
  }
  return `${index === 0 ? value : value.toFixed(1)} ${units[index]}`;
}

export function formatNumber(value) {
  if (value === null || value === undefined) return "—";
  return Number(value).toLocaleString("de-DE");
}

/** Price per million tokens, which is the unit people actually compare. */
export function formatPrice(perToken) {
  if (perToken === null || perToken === undefined) return "Unknown";
  const value = Number(perToken);
  if (!Number.isFinite(value)) return "Unknown";
  if (value === 0) return "kostenlos";
  return `$${(value * 1_000_000).toFixed(2)}/M`;
}

/* --- markdown ---------------------------------------------------------------------------- */

const KEYWORDS = new RegExp(
  "\\b(?:async|await|break|case|catch|class|const|continue|def|elif|else|except|export|" +
  "finally|for|from|function|if|import|in|is|lambda|let|new|not|or|and|pass|raise|return|" +
  "self|switch|this|throw|try|type|var|while|with|yield|True|False|None|null|true|false|" +
  "undefined|public|private|static|void|int|str|bool|float)\\b", "g");

function highlight(code) {
  // Order matters: comments and strings are matched first so keywords inside them are left
  // alone. Everything is escaped before any markup is added.
  const escaped = escapeHtml(code);
  const placeholders = [];
  let working = escaped.replace(
    /(&quot;[^&]*?&quot;|&#39;[^&]*?&#39;|`[^`]*?`)|((?:#|\/\/)[^\n]*|\/\*[\s\S]*?\*\/)/g,
    (match, string, comment) => {
      const cls = string ? "tok-str" : "tok-com";
      placeholders.push(`<span class="${cls}">${string || comment}</span>`);
      return `\u0000${placeholders.length - 1}\u0000`;
    },
  );
  working = working
    .replace(KEYWORDS, (match) => `<span class="tok-kw">${match}</span>`)
    .replace(/\b(\d+(?:\.\d+)?)\b/g, '<span class="tok-num">$1</span>')
    .replace(/\b([A-Za-z_][\w]*)(?=\()/g, '<span class="tok-fn">$1</span>');
  return working.replace(/\u0000(\d+)\u0000/g, (_, index) => placeholders[Number(index)]);
}

export function escapeHtml(text) {
  return String(text)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

function inlineMarkdown(text) {
  return escapeHtml(text)
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/(^|[^*])\*([^*\n]+)\*/g, "$1<em>$2</em>")
    .replace(/~~([^~]+)~~/g, "<del>$1</del>")
    // Only http(s) links become anchors; anything else stays literal text.
    .replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g,
             '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');
}

/**
 * Render a Markdown subset into a DOM fragment.
 * Model output is untrusted, so the source is escaped first and only the markup this
 * function itself produces is ever interpreted.
 */
export function renderMarkdown(source) {
  const fragment = document.createDocumentFragment();
  const lines = String(source ?? "").split("\n");
  let index = 0;

  const flushParagraph = (buffer) => {
    if (!buffer.length) return;
    const paragraph = document.createElement("p");
    paragraph.innerHTML = inlineMarkdown(buffer.join("\n")).replace(/\n/g, "<br>");
    fragment.append(paragraph);
    buffer.length = 0;
  };

  const paragraph = [];
  while (index < lines.length) {
    const line = lines[index];

    const fence = line.match(/^```(\w*)\s*$/);
    if (fence) {
      flushParagraph(paragraph);
      const language = fence[1] || "";
      const code = [];
      index += 1;
      while (index < lines.length && !/^```\s*$/.test(lines[index])) {
        code.push(lines[index]);
        index += 1;
      }
      index += 1;
      fragment.append(codeBlock(code.join("\n"), language));
      continue;
    }

    const heading = line.match(/^(#{1,4})\s+(.*)$/);
    if (heading) {
      flushParagraph(paragraph);
      const node = document.createElement(`h${Math.min(heading[1].length + 1, 4)}`);
      node.innerHTML = inlineMarkdown(heading[2]);
      fragment.append(node);
      index += 1;
      continue;
    }

    if (/^\s*[-*+]\s+/.test(line) || /^\s*\d+\.\s+/.test(line)) {
      flushParagraph(paragraph);
      const ordered = /^\s*\d+\.\s+/.test(line);
      const list = document.createElement(ordered ? "ol" : "ul");
      while (index < lines.length &&
             (/^\s*[-*+]\s+/.test(lines[index]) || /^\s*\d+\.\s+/.test(lines[index]))) {
        const item = document.createElement("li");
        item.innerHTML = inlineMarkdown(lines[index].replace(/^\s*(?:[-*+]|\d+\.)\s+/, ""));
        list.append(item);
        index += 1;
      }
      fragment.append(list);
      continue;
    }

    if (/^\s*>\s?/.test(line)) {
      flushParagraph(paragraph);
      const quote = document.createElement("blockquote");
      const parts = [];
      while (index < lines.length && /^\s*>\s?/.test(lines[index])) {
        parts.push(lines[index].replace(/^\s*>\s?/, ""));
        index += 1;
      }
      quote.innerHTML = inlineMarkdown(parts.join("\n")).replace(/\n/g, "<br>");
      fragment.append(quote);
      continue;
    }

    if (/^\s*(?:---|\*\*\*|___)\s*$/.test(line)) {
      flushParagraph(paragraph);
      fragment.append(document.createElement("hr"));
      index += 1;
      continue;
    }

    if (line.trim() === "") {
      flushParagraph(paragraph);
      index += 1;
      continue;
    }

    paragraph.push(line);
    index += 1;
  }
  flushParagraph(paragraph);
  return fragment;
}

function codeBlock(code, language) {
  const wrapper = el("div", { class: "codeblock" });
  const bar = el("div", { class: "codeblock__bar" },
    el("span", {}, language || "Code"),
    el("button", {
      class: "btn btn--ghost btn--sm",
      style: { marginLeft: "auto" },
      title: "Code kopieren",
      onClick: async (event) => {
        await navigator.clipboard?.writeText(code);
        const button = event.currentTarget;
        const previous = button.textContent;
        button.textContent = "Kopiert";
        setTimeout(() => { button.textContent = previous; }, 1400);
      },
    }, "Kopieren"),
  );
  const pre = el("pre", {});
  const codeNode = el("code", {});
  codeNode.innerHTML = highlight(code);
  pre.append(codeNode);
  wrapper.append(bar, pre);
  return wrapper;
}
