/* Service worker: the one place allowed to talk to the local panel.
 *
 * A content script's fetch runs with the Sleeper page's origin, so Chrome
 * applies CORS and Private Network Access to it and a probe from there fails
 * even when the panel is running perfectly. The service worker runs with the
 * extension's own origin and its host_permissions, which is exempt - so all
 * liveness checks go through here.
 *
 * It only ever performs a GET against /api/ping. It never reads the Sleeper
 * page and never calls anything that could change a draft.
 */

const PING_TIMEOUT_MS = 2500;

async function ping(port) {
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), PING_TIMEOUT_MS);
  try {
    const res = await fetch(`http://127.0.0.1:${port}/api/ping`, {
      signal: ctl.signal, cache: "no-store",
    });
    if (!res.ok) return { up: false, reason: `HTTP ${res.status}` };
    const body = await res.json();
    return body && body.name === "ff-draft-bot"
      ? { up: true }
      : { up: false, reason: "something else is on that port" };
  } catch (err) {
    return { up: false, reason: err && err.name === "AbortError"
      ? "timed out" : "no answer" };
  } finally {
    clearTimeout(timer);
  }
}

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (!msg || msg.type !== "ffbot-ping") return false;
  const port = Number(msg.port) || 8770;
  ping(port).then(sendResponse);
  return true;   // keep the channel open for the async reply
});
