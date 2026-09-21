/* Shared application state with a tiny subscription mechanism.

   Deliberately not a framework: a plain object plus change notifications is enough for this
   app and keeps the front-end buildless (Spec §74). */

const listeners = new Set();

export const state = {
  status: null,
  health: null,
  settings: null,
  platform: null,
  assistantState: "IDLE",
  assistantDetail: "",
  activeModel: null,
  activeTask: null,
  emergencyEngaged: false,
  connected: false,
  conversationId: null,
  conversations: [],
  messages: [],
  streaming: false,
  pendingPermissions: [],
  events: [],
  toolRuns: [],
  micActive: false,
  micAvailable: false,
};

export function setState(patch) {
  let changed = false;
  for (const [key, value] of Object.entries(patch)) {
    if (state[key] !== value) {
      state[key] = value;
      changed = true;
    }
  }
  if (changed) notify(Object.keys(patch));
}

export function notify(keys = []) {
  for (const listener of listeners) {
    try {
      listener(state, keys);
    } catch (error) {
      console.error("State-Listener ist fehlgeschlagen", error);
    }
  }
}

export function subscribe(listener) {
  listeners.add(listener);
  return () => listeners.delete(listener);
}
