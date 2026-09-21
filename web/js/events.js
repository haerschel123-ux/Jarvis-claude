/* Live connection to the backend event bus (Spec §78).

   One WebSocket for the whole app. It reconnects with backoff and replays the recent history
   on connect, so a reload or a short network blip never leaves the UI showing stale state. */

const RECONNECT_BASE = 800;
const RECONNECT_MAX = 15000;

export class EventStream {
  constructor() {
    this.socket = null;
    this.listeners = new Map();      // type -> Set<handler>
    this.anyListeners = new Set();
    this.attempts = 0;
    this.connected = false;
    this.closedByUs = false;
    this.history = [];
  }

  connect() {
    this.closedByUs = false;
    const protocol = location.protocol === "https:" ? "wss:" : "ws:";
    const socket = new WebSocket(`${protocol}//${location.host}/api/events?history=60`);
    this.socket = socket;

    socket.addEventListener("open", () => {
      this.attempts = 0;
      this.connected = true;
      this.emitLocal("connection", { connected: true });
    });

    socket.addEventListener("message", (event) => {
      let payload;
      try {
        payload = JSON.parse(event.data);
      } catch {
        return;
      }
      if (payload.type === "heartbeat" || payload.type === "pong") return;
      if (payload.type === "connection.ready") {
        this.history = payload.data?.history ?? [];
        this.emitLocal("history", { events: this.history });
        return;
      }
      this.dispatch(payload);
    });

    socket.addEventListener("close", () => {
      this.connected = false;
      this.emitLocal("connection", { connected: false });
      if (!this.closedByUs) this.scheduleReconnect();
    });

    socket.addEventListener("error", () => socket.close());
  }

  scheduleReconnect() {
    this.attempts += 1;
    const delay = Math.min(RECONNECT_BASE * 2 ** (this.attempts - 1), RECONNECT_MAX);
    setTimeout(() => this.connect(), delay);
  }

  close() {
    this.closedByUs = true;
    this.socket?.close();
  }

  dispatch(event) {
    for (const handler of this.listeners.get(event.type) ?? []) {
      try {
        handler(event.data ?? {}, event);
      } catch (error) {
        console.error(`Event-Handler für ${event.type} ist fehlgeschlagen`, error);
      }
    }
    for (const handler of this.anyListeners) {
      try {
        handler(event);
      } catch (error) {
        console.error("Event-Handler ist fehlgeschlagen", error);
      }
    }
  }

  emitLocal(type, data) {
    this.dispatch({ type: `local.${type}`, data, timestamp: Date.now() / 1000 });
  }

  /** Subscribe to one event type. Returns an unsubscribe function. */
  on(type, handler) {
    if (!this.listeners.has(type)) this.listeners.set(type, new Set());
    this.listeners.get(type).add(handler);
    return () => this.listeners.get(type)?.delete(handler);
  }

  /** Subscribe to every event — used by the activity feed. */
  onAny(handler) {
    this.anyListeners.add(handler);
    return () => this.anyListeners.delete(handler);
  }
}

export const events = new EventStream();
