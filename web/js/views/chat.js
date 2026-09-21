/* The chat view (Spec §59): streaming, Markdown, tool cards, citations, attachments,
   retry/edit/stop and the voice button. This is the central surface of the app. */

import { api, ApiError } from "../api.js";
import { clear, el, formatRelative, formatTime, icon, mount, renderMarkdown } from "../dom.js";
import { setState, state } from "../state.js";
import { createOrb, emptyState, setOrbLevel, setOrbState, toast } from "../ui.js";

const STATUS_LABELS = {
  Analysiere: "Analysiere",
  Gedächtnis: "Durchsuche das Gedächtnis",
  routing: "Wähle ein Modell",
};

let controller = null;       // AbortController of the running stream
let waveTimer = null;

export function renderChat(container) {
  const layout = el("div", { class: "chat" });

  const scroll = el("div", { class: "chat__scroll", id: "chat-scroll" });
  const inner = el("div", { class: "chat__inner", id: "chat-messages" });
  scroll.append(inner);

  layout.append(scroll, buildComposer());
  mount(container, layout);

  renderMessages();
  loadConversation();
  return layout;
}

/* --- conversation loading --------------------------------------------------------------- */

async function loadConversation() {
  if (!state.conversationId) {
    renderMessages();
    return;
  }
  try {
    const data = await api.conversation(state.conversationId);
    setState({ messages: data.messages ?? [] });
    renderMessages();
    scrollToEnd(true);
  } catch (error) {
    if (error instanceof ApiError && error.status === 404) {
      setState({ conversationId: null, messages: [] });
      renderMessages();
    }
  }
}

export async function openConversation(id) {
  setState({ conversationId: id, messages: [] });
  await loadConversation();
}

export function newConversation() {
  setState({ conversationId: null, messages: [] });
  renderMessages();
  document.getElementById("composer-input")?.focus();
}

/* --- rendering --------------------------------------------------------------------------- */

export function renderMessages() {
  const host = document.getElementById("chat-messages");
  if (!host) return;
  clear(host);

  if (!state.messages.length && !state.streaming) {
    host.append(welcomePanel());
    return;
  }
  for (const message of state.messages) {
    const node = messageNode(message);
    if (node) host.append(node);
  }
}

function welcomePanel() {
  const orb = createOrb({ size: "lg", withLabel: true });
  setOrbState(orb, state.assistantState);

  const suggestions = [
    "Was kannst du auf diesem System tun?",
    "Zeig mir den Status meiner Anbieter.",
    "Erkläre mir, wie das Berechtigungssystem funktioniert.",
    "Welche Werkzeuge stehen dir zur Verfügung?",
  ];

  return el("div", { class: "orb-stage anim-rise" },
    orb,
    el("div", { class: "orb-stage__caption" },
      el("div", { class: "orb-stage__state" },
        `${state.settings?.assistant?.name ?? "JARVIS"} ist bereit.`),
      el("div", { class: "orb-stage__detail" },
        "Sprich oder schreib mir. Ich frage nach, bevor ich etwas verändere."),
    ),
    el("div", { class: "row wrap", style: { justifyContent: "center", maxWidth: "620px" } },
      ...suggestions.map((text) => el("button", {
        class: "pill",
        style: { cursor: "pointer" },
        onClick: () => {
          const input = document.getElementById("composer-input");
          if (!input) return;
          input.value = text;
          input.dispatchEvent(new Event("input"));
          input.focus();
        },
      }, text)),
    ),
  );
}

function messageNode(message) {
  if (message.role === "system") return null;

  if (message.role === "tool") {
    return toolCardFromMessage(message);
  }

  const isUser = message.role === "user";
  const isError = Boolean(message.error);
  const wrapper = el("div", {
    class: `msg msg--${isUser ? "user" : "assistant"}${isError ? " msg--error" : ""}`,
    dataset: { messageId: String(message.id ?? "") },
  });

  wrapper.append(el("div", { class: "msg__avatar" }, isUser ? "DU" : "J"));

  const bubble = el("div", { class: "bubble" });
  if (isUser) {
    // The user's own text is inserted verbatim, without Markdown interpretation.
    bubble.append(el("div", { style: { whiteSpace: "pre-wrap" } }, message.content ?? ""));
  } else {
    bubble.append(renderMarkdown(message.content ?? ""));
  }

  const body = el("div", { class: "msg__body" }, bubble);

  if (message.citations?.length) body.append(citationList(message.citations));
  body.append(messageMeta(message, isUser));
  wrapper.append(body);
  return wrapper;
}

function citationList(citations) {
  return el("div", { class: "row wrap small" },
    el("span", { class: "muted" }, "Quellen:"),
    ...citations.slice(0, 6).map((citation, index) => el("a", {
      class: "pill",
      href: citation.url,
      target: "_blank",
      rel: "noopener noreferrer",
      title: citation.url,
    }, `${index + 1}. ${citation.title || new URL(citation.url).hostname}`)),
  );
}

function messageMeta(message, isUser) {
  const meta = el("div", { class: "msg__meta" });
  if (message.created_at) meta.append(el("span", {}, formatRelative(message.created_at)));
  if (message.model_id) meta.append(el("span", { class: "mono" }, message.model_id));
  if (message.completion_tokens) {
    meta.append(el("span", {}, `${message.completion_tokens} Token`));
  }

  const actions = el("div", { class: "msg__actions" });
  actions.append(iconButton("copy", "Kopieren", async () => {
    await navigator.clipboard?.writeText(message.content ?? "");
    toast("In die Zwischenablage kopiert.", { kind: "ok", timeout: 2000 });
  }));
  if (isUser) {
    actions.append(iconButton("edit", "Bearbeiten und erneut senden",
      () => editAndResend(message)));
  } else if (!message.error) {
    actions.append(iconButton("retry", "Antwort neu erzeugen", () => retryLast()));
  }
  meta.append(actions);
  return meta;
}

function iconButton(name, title, onClick) {
  return el("button", { class: "btn btn--icon btn--ghost btn--sm", title, "aria-label": title, onClick },
    icon(name, 14));
}

function toolCardFromMessage(message) {
  return toolCard({
    tool: message.agent || "Werkzeug",
    ok: !message.error,
    display: { summary: (message.content ?? "").slice(0, 160) },
  });
}

export function toolCard({ tool, ok = true, running = false, denied = false, display = {} }) {
  const modifier = running ? "running" : denied ? "denied" : ok ? "ok" : "fail";
  const card = el("div", { class: `toolcard toolcard--${modifier}` });

  const head = el("div", { class: "toolcard__head" },
    running ? el("span", { class: "status-line__spinner" }) : icon(ok ? "check" : "warn", 14),
    el("span", { class: "toolcard__name" }, tool),
    el("span", { class: "grow" }),
    el("span", { class: "tiny muted" }, formatTime(new Date())),
  );
  card.append(head);

  const summary = display.summary || (running ? "läuft …" : ok ? "erledigt" : "fehlgeschlagen");
  const body = el("div", { class: "toolcard__body" }, summary);

  const extra = { ...display };
  delete extra.summary;
  if (Object.keys(extra).length) {
    body.append(el("details", { style: { marginTop: "6px" } },
      el("summary", { class: "tiny muted" }, "Details"),
      el("pre", { class: "tiny" }, JSON.stringify(extra, null, 2)),
    ));
  }
  card.append(body);
  return card;
}

/* --- composer ------------------------------------------------------------------------------ */

let attachments = [];

function buildComposer() {
  const input = el("textarea", {
    id: "composer-input",
    class: "composer__input",
    rows: "1",
    placeholder: "Nachricht an JARVIS …",
    "aria-label": "Nachricht",
    onInput: (event) => autoGrow(event.target),
    onKeydown: (event) => {
      if (event.key === "Enter" && !event.shiftKey) {
        event.preventDefault();
        submit();
      }
    },
  });

  const attachmentBar = el("div", { class: "composer__attachments hidden", id: "composer-attachments" });

  const fileInput = el("input", {
    type: "file",
    accept: "image/*,text/*,.md,.json,.xml,.csv,.log,.py,.js",
    multiple: true,
    class: "hidden",
    id: "composer-file",
    onChange: (event) => addFiles(event.target.files),
  });

  const micButton = el("button", {
    class: "mic",
    id: "composer-mic",
    type: "button",
    title: "Spracheingabe",
    "aria-label": "Spracheingabe",
    onClick: toggleMic,
  }, icon("mic", 17));

  const sendButton = el("button", {
    class: "btn btn--primary btn--icon",
    id: "composer-send",
    title: "Senden",
    "aria-label": "Senden",
    onClick: submit,
  }, icon("send", 17));

  const stopButton = el("button", {
    class: "btn btn--danger btn--icon hidden",
    id: "composer-stop",
    title: "Antwort stoppen",
    "aria-label": "Antwort stoppen",
    onClick: stopStream,
  }, icon("stop", 17));

  return el("div", { class: "composer" },
    el("div", { class: "composer__inner" },
      attachmentBar,
      el("div", { class: "composer__row" },
        micButton,
        el("button", {
          class: "btn btn--icon btn--ghost",
          title: "Datei anhängen",
          "aria-label": "Datei anhängen",
          onClick: () => fileInput.click(),
        }, icon("file", 17)),
        el("button", {
          class: "btn btn--icon btn--ghost",
          title: "Bildschirmfoto anhängen",
          "aria-label": "Bildschirmfoto anhängen",
          onClick: captureScreen,
        }, icon("screen", 17)),
        input,
        stopButton,
        sendButton,
        fileInput,
      ),
      el("div", { class: "composer__hint" },
        el("span", {}, el("kbd", {}, "Enter"), " senden · ",
          el("kbd", {}, "Shift"), "+", el("kbd", {}, "Enter"), " neue Zeile"),
        el("span", { id: "composer-model", class: "muted" }, ""),
      ),
    ),
  );
}

function autoGrow(textarea) {
  textarea.style.height = "auto";
  textarea.style.height = `${Math.min(textarea.scrollHeight, 210)}px`;
}

async function addFiles(fileList) {
  for (const file of fileList) {
    if (file.size > 6 * 1024 * 1024) {
      toast(`"${file.name}" ist größer als 6 MB und wurde nicht angehängt.`, { kind: "warn" });
      continue;
    }
    if (file.type.startsWith("image/")) {
      attachments.push({ kind: "image", name: file.name, data: await readAsDataUrl(file) });
    } else {
      attachments.push({ kind: "text", name: file.name, text: await file.text() });
    }
  }
  renderAttachments();
}

function readAsDataUrl(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result));
    reader.onerror = reject;
    reader.readAsDataURL(file);
  });
}

function renderAttachments() {
  const bar = document.getElementById("composer-attachments");
  if (!bar) return;
  clear(bar);
  bar.classList.toggle("hidden", attachments.length === 0);
  attachments.forEach((attachment, index) => {
    bar.append(el("span", { class: "attachment" },
      attachment.kind === "image"
        ? el("img", { src: attachment.data, alt: "" })
        : icon("file", 13),
      el("span", { class: "truncate", style: { maxWidth: "150px" } }, attachment.name),
      el("button", {
        class: "btn btn--icon btn--ghost btn--sm",
        "aria-label": "Anhang entfernen",
        onClick: () => { attachments.splice(index, 1); renderAttachments(); },
      }, icon("x", 12)),
    ));
  });
}

async function captureScreen() {
  // The browser cannot capture the screen without the user picking a source, and it cannot
  // reach the rest of the PC at all. Say so rather than pretending (Spec §108).
  if (!navigator.mediaDevices?.getDisplayMedia) {
    toast(
      "Bildschirmaufnahme ist in diesem Browser nicht verfügbar. Auf dem Windows-Host kann " +
      "JARVIS den Bildschirm direkt erfassen.",
      { kind: "warn", timeout: 7000 },
    );
    return;
  }
  try {
    const stream = await navigator.mediaDevices.getDisplayMedia({ video: true });
    const track = stream.getVideoTracks()[0];
    const bitmap = await new ImageCapture(track).grabFrame();
    track.stop();
    const canvas = document.createElement("canvas");
    canvas.width = bitmap.width;
    canvas.height = bitmap.height;
    canvas.getContext("2d").drawImage(bitmap, 0, 0);
    attachments.push({ kind: "image", name: "Bildschirmfoto.png", data: canvas.toDataURL("image/png") });
    renderAttachments();
  } catch (error) {
    if (error.name !== "NotAllowedError") {
      toast(`Bildschirmaufnahme fehlgeschlagen: ${error.message}`, { kind: "error" });
    }
  }
}

/* --- microphone ------------------------------------------------------------------------------ */

async function toggleMic() {
  const button = document.getElementById("composer-mic");
  if (state.micActive) {
    setState({ micActive: false });
    button.dataset.active = "false";
    stopWaveAnimation();
    return;
  }
  // Voice capture arrives with the voice stage; until then the UI states that plainly
  // instead of offering a button that quietly does nothing (Spec §108).
  toast(
    "Die Spracheingabe wird mit der Sprachstufe aktiviert. Bis dahin kannst du schreiben — " +
    "der Status wird hier angezeigt, sobald das Mikrofon verfügbar ist.",
    { kind: "info", timeout: 6000 },
  );
}

function stopWaveAnimation() {
  if (waveTimer) {
    cancelAnimationFrame(waveTimer);
    waveTimer = null;
  }
}

export function animateOrbs() {
  const orbs = document.querySelectorAll(".orb");
  const active = ["LISTENING", "SPEAKING"].includes(state.assistantState);
  if (!active) {
    stopWaveAnimation();
    return;
  }
  const tick = () => {
    for (const orb of orbs) setOrbLevel(orb, null);
    waveTimer = requestAnimationFrame(tick);
  };
  stopWaveAnimation();
  waveTimer = requestAnimationFrame(tick);
}

/* --- sending -------------------------------------------------------------------------------- */

async function submit() {
  const input = document.getElementById("composer-input");
  if (!input || state.streaming) return;
  const text = input.value.trim();
  if (!text && attachments.length === 0) return;

  const textAttachments = attachments.filter((a) => a.kind === "text");
  const images = attachments.filter((a) => a.kind === "image").map((a) => a.data);
  const composed = textAttachments.length
    ? `${text}\n\n${textAttachments.map((a) => `--- ${a.name} ---\n${a.text}`).join("\n\n")}`
    : text;

  input.value = "";
  autoGrow(input);
  attachments = [];
  renderAttachments();

  await sendMessage(composed, images);
}

export async function sendMessage(text, images = []) {
  if (state.streaming) return;

  const host = document.getElementById("chat-messages");
  if (host && !state.messages.length) clear(host);

  const userMessage = { role: "user", content: text, created_at: new Date().toISOString() };
  state.messages.push(userMessage);
  host?.append(messageNode(userMessage));

  const assistantWrapper = el("div", { class: "msg msg--assistant" },
    el("div", { class: "msg__avatar" }, "J"),
  );
  const bubble = el("div", { class: "bubble" });
  const streamText = el("span");
  const caret = el("span", { class: "stream-caret" });
  bubble.append(streamText, caret);
  const toolArea = el("div", { class: "col", style: { gap: "6px" } });
  const statusArea = el("div", {});
  const body = el("div", { class: "msg__body" }, statusArea, toolArea, bubble);
  assistantWrapper.append(body);
  host?.append(assistantWrapper);
  scrollToEnd();

  setState({ streaming: true });
  toggleStreamButtons(true);

  controller = new AbortController();
  let answer = "";
  const citations = [];
  const runningTools = new Map();

  const setStatus = (label) => {
    clear(statusArea);
    if (label) {
      statusArea.append(el("div", { class: "status-line" },
        el("span", { class: "status-line__spinner" }), label));
    }
  };
  setStatus("Analysiere");

  try {
    await api.streamChat({
      message: text,
      conversationId: state.conversationId,
      images,
    }, (event) => {
      const data = event.data ?? {};
      switch (event.type) {
        case "turn.started":
          if (!state.conversationId) setState({ conversationId: data.conversation_id });
          break;
        case "conversation.titled":
          window.dispatchEvent(new CustomEvent("jarvis:conversations-changed"));
          break;
        case "status":
          setStatus(STATUS_LABELS[data.stage] ?? data.stage ?? "Arbeite");
          break;
        case "model.selected":
          setState({ activeModel: data.model?.key ?? null });
          document.getElementById("composer-model").textContent = data.model?.name ?? "";
          break;
        case "delta":
          setStatus("");
          answer += data.text ?? "";
          streamText.textContent = answer;
          scrollToEnd();
          break;
        case "reasoning":
          setStatus("Denke nach");
          break;
        case "tool.requested": {
          setStatus("");
          const card = toolCard({ tool: data.tool, running: true, display: { summary: "läuft …" } });
          runningTools.set(data.tool, card);
          toolArea.append(card);
          scrollToEnd();
          break;
        }
        case "tool.result": {
          const card = runningTools.get(data.tool);
          const replacement = toolCard({
            tool: data.tool,
            ok: data.ok,
            denied: data.display?.denied,
            display: data.display ?? {},
          });
          if (card) card.replaceWith(replacement);
          else toolArea.append(replacement);
          runningTools.delete(data.tool);
          break;
        }
        case "turn.failed":
          setStatus("");
          assistantWrapper.classList.add("msg--error");
          answer = data.user_message || "Es ist ein Fehler aufgetreten.";
          streamText.textContent = answer;
          break;
        case "turn.completed":
          setStatus("");
          break;
        default:
          break;
      }
    }, controller.signal);
  } catch (error) {
    if (error.name !== "AbortError") {
      setStatus("");
      assistantWrapper.classList.add("msg--error");
      answer = answer || (error instanceof ApiError ? error.message : "Die Verbindung ist abgebrochen.");
      streamText.textContent = answer;
    }
  } finally {
    caret.remove();
    setStatus("");
    setState({ streaming: false });
    toggleStreamButtons(false);
    controller = null;

    // Replace the live text with rendered Markdown now that the answer is complete.
    clear(bubble);
    bubble.append(renderMarkdown(answer));
    if (citations.length) body.append(citationList(citations));

    state.messages.push({
      role: "assistant",
      content: answer,
      created_at: new Date().toISOString(),
      model_id: state.activeModel,
    });
    body.append(messageMeta(state.messages.at(-1), false));
    scrollToEnd();
    window.dispatchEvent(new CustomEvent("jarvis:conversations-changed"));
  }
}

function toggleStreamButtons(streaming) {
  document.getElementById("composer-send")?.classList.toggle("hidden", streaming);
  document.getElementById("composer-stop")?.classList.toggle("hidden", !streaming);
}

function stopStream() {
  controller?.abort();
  toast("Antwort gestoppt.", { kind: "info", timeout: 2200 });
}

async function retryLast() {
  const lastUser = [...state.messages].reverse().find((m) => m.role === "user");
  if (!lastUser) return;
  await sendMessage(lastUser.content);
}

function editAndResend(message) {
  const input = document.getElementById("composer-input");
  if (!input) return;
  input.value = message.content ?? "";
  autoGrow(input);
  input.focus();
  if (message.id && state.conversationId) {
    api.truncateConversation(state.conversationId, message.id - 1)
      .then(() => loadConversation())
      .catch(() => {});
  }
}

function scrollToEnd(instant = false) {
  const scroll = document.getElementById("chat-scroll");
  if (!scroll) return;
  const distance = scroll.scrollHeight - scroll.scrollTop - scroll.clientHeight;
  // Only auto-scroll when the user is already near the bottom, so reading older messages
  // is not interrupted by new output.
  if (instant || distance < 220) {
    scroll.scrollTo({ top: scroll.scrollHeight, behavior: instant ? "auto" : "smooth" });
  }
}

export { emptyState };
