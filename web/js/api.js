/* Thin wrapper around the JARVIS HTTP API.
   Every call goes through request(), so error handling, the device token and JSON parsing
   live in exactly one place. */

const TOKEN_KEY = "jarvis.device_token";

export class ApiError extends Error {
  constructor(message, status, payload) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.payload = payload ?? {};
  }
}

function authHeaders() {
  const token = localStorage.getItem(TOKEN_KEY);
  return token ? { Authorization: `Bearer ${token}` } : {};
}

async function apiRequest(path, { method = "GET", body, signal, headers } = {}) {
  let response;
  try {
    response = await fetch(path, {
      method,
      signal,
      headers: {
        ...(body === undefined ? {} : { "Content-Type": "application/json" }),
        ...authHeaders(),
        ...headers,
      },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch (error) {
    if (error.name === "AbortError") throw error;
    throw new ApiError("JARVIS ist nicht erreichbar.", 0, { cause: String(error) });
  }

  const isJson = (response.headers.get("content-type") || "").includes("application/json");
  const payload = isJson ? await response.json().catch(() => ({})) : await response.text();

  if (!response.ok) {
    // The backend maps its error taxonomy onto HTTP and always carries a German explanation
    // in user_message; prefer that over a generic status text.
    const detail = payload?.detail ?? payload;
    const message =
      detail?.user_message || detail?.detail || detail?.message ||
      (typeof detail === "string" ? detail : `Fehler ${response.status}`);
    throw new ApiError(message, response.status, detail);
  }
  return payload;
}

export const api = {
  setDeviceToken(token) {
    if (token) localStorage.setItem(TOKEN_KEY, token);
    else localStorage.removeItem(TOKEN_KEY);
  },

  /* --- system --- */
  health: (refresh = false) => apiRequest(`/api/health${refresh ? "?refresh=true" : ""}`),
  status: () => apiRequest("/api/status"),
  platform: () => apiRequest("/api/platform"),
  eventHistory: (limit = 100) => apiRequest(`/api/events/history?limit=${limit}`),

  /* --- chat --- */
  chatDefaults: () => apiRequest("/api/chat/defaults"),
  conversations: (limit = 50) => apiRequest(`/api/chat/conversations?limit=${limit}`),
  conversation: (id) => apiRequest(`/api/chat/conversations/${id}`),
  createConversation: (title = "") =>
    apiRequest(`/api/chat/conversations?title=${encodeURIComponent(title)}`, { method: "POST" }),
  renameConversation: (id, title) =>
    apiRequest(`/api/chat/conversations/${id}?title=${encodeURIComponent(title)}`, { method: "PATCH" }),
  deleteConversation: (id) => apiRequest(`/api/chat/conversations/${id}`, { method: "DELETE" }),
  truncateConversation: (id, afterMessageId) =>
    apiRequest(`/api/chat/conversations/${id}/truncate?after_message_id=${afterMessageId}`,
            { method: "POST" }),

  /* --- models --- */
  providers: () => apiRequest("/api/providers"),
  models: (filters = {}) => {
    const query = new URLSearchParams();
    for (const [key, value] of Object.entries(filters)) {
      if (value !== undefined && value !== null && value !== "" && value !== false) {
        query.set(key, String(value));
      }
    }
    const suffix = query.toString();
    return apiRequest(`/api/models${suffix ? `?${suffix}` : ""}`);
  },
  refreshModels: () => apiRequest("/api/models/refresh?force=true", { method: "POST" }),
  previewSelection: (taskKind = "chat") =>
    apiRequest(`/api/models/select?task_kind=${encodeURIComponent(taskKind)}`, { method: "POST" }),

  /* --- settings and secrets --- */
  settings: () => apiRequest("/api/settings"),
  updateSettings: (patch) => apiRequest("/api/settings", { method: "PUT", body: { patch } }),
  resetSettings: () => apiRequest("/api/settings/reset", { method: "POST" }),
  secrets: () => apiRequest("/api/secrets"),
  setSecret: (name, value) => apiRequest("/api/secrets", { method: "PUT", body: { name, value } }),
  deleteSecret: (name) => apiRequest(`/api/secrets/${encodeURIComponent(name)}`, { method: "DELETE" }),
  permissions: () => apiRequest("/api/permissions"),
  setPermissions: (body) => apiRequest("/api/permissions", { method: "PUT", body }),
  setup: () => apiRequest("/api/setup"),
  completeSetup: () => apiRequest("/api/setup/complete", { method: "POST" }),

  /* --- tools --- */
  tools: () => apiRequest("/api/tools"),
  executeTool: (tool, args = {}, conversationId = null) =>
    apiRequest("/api/tools/execute", {
      method: "POST",
      body: { tool, arguments: args, conversation_id: conversationId },
    }),
  toolRuns: (limit = 50) => apiRequest(`/api/tools/runs?limit=${limit}`),
  pendingPermissions: () => apiRequest("/api/permissions/pending"),
  answerPermission: (id, answer) =>
    apiRequest("/api/permissions/answer", { method: "POST", body: { id, answer } }),
  grants: () => apiRequest("/api/permissions/grants"),
  revokeGrant: (capability, scope) => {
    const query = capability && scope
      ? `?capability=${encodeURIComponent(capability)}&scope=${encodeURIComponent(scope)}`
      : "";
    return apiRequest(`/api/permissions/grants${query}`, { method: "DELETE" });
  },

  /* --- emergency stop --- */
  emergencyState: () => apiRequest("/api/emergency"),
  emergencyStop: (reason = "Über die Oberfläche ausgelöst") =>
    apiRequest(`/api/emergency/stop?reason=${encodeURIComponent(reason)}`, { method: "POST" }),
  emergencyRelease: () => apiRequest("/api/emergency/release", { method: "POST" }),

  /* --- chat streaming (SSE over POST, so it cannot use EventSource) --- */
  async streamChat({ message, conversationId, projectId, model, images = [] }, onEvent, signal) {
    const response = await fetch("/api/chat/stream", {
      method: "POST",
      headers: { "Content-Type": "application/json", ...authHeaders() },
      signal,
      body: JSON.stringify({
        message,
        conversation_id: conversationId ?? null,
        project_id: projectId ?? null,
        model: model ?? null,
        images,
      }),
    });

    if (!response.ok || !response.body) {
      throw new ApiError("Der Chat-Stream konnte nicht gestartet werden.", response.status, {});
    }

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";

    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });

      // SSE frames are separated by a blank line.
      let boundary;
      while ((boundary = buffer.indexOf("\n\n")) !== -1) {
        const frame = buffer.slice(0, boundary);
        buffer = buffer.slice(boundary + 2);
        for (const line of frame.split("\n")) {
          if (!line.startsWith("data: ")) continue;
          const data = line.slice(6);
          if (data === "[DONE]") return;
          try {
            onEvent(JSON.parse(data));
          } catch {
            /* a partial frame; the next chunk completes it */
          }
        }
      }
    }
  },
};

/* --- stage 4: memory, tasks, reminders, backups --------------------------------------- */

Object.assign(api, {
  memories: (filters = {}) => {
    const query = new URLSearchParams();
    for (const [key, value] of Object.entries(filters)) {
      if (value) query.set(key, String(value));
    }
    const suffix = query.toString();
    return apiRequest(`/api/memory${suffix ? `?${suffix}` : ""}`);
  },
  createMemory: (body) => apiRequest("/api/memory", { method: "POST", body }),
  updateMemory: (id, body) => apiRequest(`/api/memory/${id}`, { method: "PATCH", body }),
  deleteMemory: (id) => apiRequest(`/api/memory/${id}`, { method: "DELETE" }),
  searchMemories: (q, limit = 12) =>
    apiRequest(`/api/memory/search?q=${encodeURIComponent(q)}&limit=${limit}`),
  exportMemories: () => apiRequest("/api/memory/export/all"),
  importMemories: (memories) =>
    apiRequest("/api/memory/import", { method: "POST", body: { memories } }),

  tasks: (activeOnly = false) => apiRequest(`/api/tasks?active_only=${activeOnly}`),
  task: (id) => apiRequest(`/api/tasks/${id}`),
  createTask: (body) => apiRequest("/api/tasks", { method: "POST", body }),
  cancelTask: (id) => apiRequest(`/api/tasks/${id}/cancel`, { method: "POST" }),

  reminders: (includeDisabled = false) =>
    apiRequest(`/api/reminders?include_disabled=${includeDisabled}`),
  createReminder: (body) => apiRequest("/api/reminders", { method: "POST", body }),
  previewReminder: (text, timezone) =>
    apiRequest(`/api/reminders/parse?text=${encodeURIComponent(text)}` +
               `&timezone=${encodeURIComponent(timezone)}`, { method: "POST" }),
  setReminderEnabled: (id, enabled) =>
    apiRequest(`/api/reminders/${id}?enabled=${enabled}`, { method: "PATCH" }),
  deleteReminder: (id) => apiRequest(`/api/reminders/${id}`, { method: "DELETE" }),

  backups: () => apiRequest("/api/backups"),
  createBackup: (label = "") =>
    apiRequest(`/api/backups?label=${encodeURIComponent(label)}`, { method: "POST" }),
  proactiveState: () => apiRequest("/api/proactive"),
});
