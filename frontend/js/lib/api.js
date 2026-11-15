// API client with offline awareness.
//  - GET requests go to the network; when offline the service worker answers from its cache and tags the
//    response `X-PulseChain-Cache: offline`, so pages can show "cached" state honestly.
//  - A small set of low-risk edits (inventory edits, acknowledging alerts, approving/rejecting transfers,
//    threshold) is queued in localStorage while offline and replayed in order when the connection returns.
//  - Forecast re-computation, scenario changes, SMS sending and demo reset need the server and are NOT queued.
const QUEUE_KEY = "pulsechain.queue.v1";
export const bus = new EventTarget();
const emit = (type, detail) => bus.dispatchEvent(new CustomEvent(type, { detail }));

export const net = { online: navigator.onLine, servedFromCache: false, lastSync: null };
const QUEUEABLE = [
  /^PATCH \/api\/inventory\/batches\/\d+$/, /^POST \/api\/inventory\/batches$/, /^PUT \/api\/inventory\/[^/]+\/[^/]+\/policy$/,
  /^POST \/api\/alerts\/acknowledge$/, /^POST \/api\/redistribution\/decision$/, /^PUT \/api\/settings$/,
];

export class ApiError extends Error {
  constructor(status, code, message) { super(message); this.status = status; this.code = code; }
}

function readQueue() { try { return JSON.parse(localStorage.getItem(QUEUE_KEY) || "[]"); } catch { return []; } }
function writeQueue(q) { try { localStorage.setItem(QUEUE_KEY, JSON.stringify(q)); } catch { /* storage unavailable */ } emit("queue", q.length); }
export const queueLength = () => readQueue().length;
export const queuedActions = () => readQueue();

async function request(method, path, body) {
  let resp;
  try {
    resp = await fetch(path, { method, headers: body !== undefined ? { "Content-Type": "application/json" } : undefined, body: body !== undefined ? JSON.stringify(body) : undefined });
  } catch (e) {
    setOnline(false);
    throw new ApiError(0, "network", "The server cannot be reached.");
  }
  const cached = resp.headers.get("X-PulseChain-Cache") === "offline";
  if (cached) { setOnline(false); net.servedFromCache = true; } else { net.servedFromCache = false; setOnline(true); }
  let data = null;
  try { data = await resp.json(); } catch { /* non-JSON */ }
  if (!resp.ok) {
    const err = (data && data.error) || {};
    throw new ApiError(resp.status, err.code || "error", err.message || `Request failed (${resp.status})`);
  }
  return { data, cached };
}

export function setOnline(v) {
  if (net.online === v) return;
  net.online = v; emit("net", v);
  if (v) flushQueue();
}
window.addEventListener("online", () => setOnline(true));
window.addEventListener("offline", () => setOnline(false));

export async function get(path) { return (await request("GET", path)).data; }
export async function getWithMeta(path) { return request("GET", path); }

/** Send a change. Returns {queued:boolean, data}. Throws for validation errors and for non-queueable offline writes. */
export async function mutate(method, path, body, { label = "Change" } = {}) {
  const sig = `${method} ${path}`;
  const queueable = QUEUEABLE.some((re) => re.test(sig));
  if (!net.online) {
    if (!queueable) throw new ApiError(0, "offline", "This action needs a connection to the server and cannot be saved offline.");
    const q = readQueue(); q.push({ method, path, body, label, at: new Date().toISOString() }); writeQueue(q);
    return { queued: true, data: null };
  }
  try {
    const { data } = await request(method, path, body);
    emit("mutated", { method, path });
    return { queued: false, data };
  } catch (e) {
    if (e.status === 0 && queueable) { const q = readQueue(); q.push({ method, path, body, label, at: new Date().toISOString() }); writeQueue(q); return { queued: true, data: null }; }
    throw e;
  }
}
export const post = (p, b, o) => mutate("POST", p, b, o);
export const patch = (p, b, o) => mutate("PATCH", p, b, o);
export const put = (p, b, o) => mutate("PUT", p, b, o);

let flushing = false;
export async function flushQueue() {
  if (flushing) return;
  const q = readQueue();
  if (!q.length) return;
  flushing = true;
  const result = { sent: 0, rejected: [] };
  try {
    const rest = [];
    for (let i = 0; i < q.length; i++) {
      const a = q[i];
      try { await request(a.method, a.path, a.body); result.sent++; }
      catch (e) {
        if (e.status === 0) { rest.push(...q.slice(i)); break; }          // still offline: keep the rest, in order
        result.rejected.push({ label: a.label, reason: e.message });       // server refused it (e.g. stale transfer): drop and report
      }
    }
    writeQueue(rest);
    if (!rest.length) net.lastSync = new Date();
    emit("synced", result);
  } finally { flushing = false; }
}
export function discardQueue() { writeQueue([]); }
