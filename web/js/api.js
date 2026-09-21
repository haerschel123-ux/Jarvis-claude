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

async function request(path, { method = "GET", body, signal, headers } = {}) {
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
  health: (refresh = false) => request(`/api/health${refresh ? "?refresh=true" : ""}`),
  status: () => request("/api/status"),
  platform: () => request("/api/platform"),
  eventHistory: (limit = 100) => request(`/api/events/history?limit=${limit}`),

  /* --- chat --- */
  chatDefaults: () => request("/api/chat/defaults"),
  conversations: (limit = 50) => request(`/api/chat/conversations?limit=${limit}`),
  conversation: (id) => request(`/api/chat/conversations/${id}`),
  createConversation: (title = "") =>
    request(`/api/chat/conversations?title=${encodeURIComponent(title)}`, { method: "POST" }),
  renameConversation: (id, title) =>
    request(`/api/chat/conversations/${id}?title=${encodeURIComponent(title)}`, { method: "PATCH" }),
  deleteConversation: (id) => request(`/api/chat/conversations/${id}`, { method: "DELETE" }),
  truncateConversation: (id, afterMessageId) =>
    request(`/api/chat/conversations/${id}/truncate?after_message_id=${afterMessageId}`,
            { method: "POST" }),

  /* --- models --- */
  providers: () => request("/api/providers"),
  models: (filters = {}) => {
    const query = new URLSearchParams();
    for (const [key, value] of Object.entries(filters)) {
      if (value !== undefined && value !== null && value !== "" && value !== false) {
        query.set(key, String(value));
      }
    }
    const suffix = query.toString();
    return request(`/api/models${suffix ? `?${suffix}` : ""}`);
  },
  refreshModels: () => request("/api/models/refresh?force=true", { method: "POST" }),
  previewSelection: (taskKind = "chat") =>
    request(`/api/models/select?task_kind=${encodeURIComponent(taskKind)}`, { method: "POST" }),

  /* --- settings and secrets --- */
  settings: () => request("/api/settings"),
  updateSettings: (patch) => request("/api/settings", { method: "PUT", body: { patch } }),
  resetSettings: () => request("/api/settings/reset", { method: "POST" }),
  secrets: () => request("/api/secrets"),
  setSecret: (name, value) => request("/api/secrets", { method: "PUT", body: { name, value } }),
  deleteSecret: (name) => request(`/api/secrets/${encodeURIComponent(name)}`, { method: "DELETE" }),
  permissions: () => request("/api/permissions"),
  setPermissions: (body) => request("/api/permissions", { method: "PUT", body }),
  setup: () => request("/api/setup"),
  completeSetup: () => request("/api/setup/complete", { method: "POST" }),

  /* --- tools --- */
  tools: () => request("/api/tools"),
  executeTool: (tool, args = {}, conversationId = null) =>
    request("/api/tools/execute", {
      method: "POST",
      body: { tool, arguments: args, conversation_id: conversationId },
    }),
  toolRuns: (limit = 50) => request(`/api/tools/runs?limit=${limit}`),
  pendingPermissions: () => request("/api/permissions/pending"),
  answerPermission: (id, answer) =>
    request("/api/permissions/answer", { method: "POST", body: { id, answer } }),
  grants: () => request("/api/permissions/grants"),
  revokeGrant: (capability, scope) => {
    const query = capability && scope
      ? `?capability=${encodeURIComponent(capability)}&scope=${encodeURIComponent(scope)}`
      : "";
    return request(`/api/permissions/grants${query}`, { method: "DELETE" });
  },

  /* --- emergency stop --- */
  emergencyState: () => request("/api/emergency"),
  emergencyStop: (reason = "Über die Oberfläche ausgelöst") =>
    request(`/api/emergency/stop?reason=${encodeURIComponent(reason)}`, { method: "POST" }),
  emergencyRelease: () => request("/api/emergency/release", { method: "POST" }),

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
