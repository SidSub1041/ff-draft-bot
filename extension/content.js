/* ff-draft-bot sidebar - docks the local panel beside a Sleeper draft.
 *
 * This script is deliberately a VIEWER and nothing else. It does not read the
 * draft page, does not scrape picks, does not click anything and cannot make a
 * pick. All it does is mount an iframe pointing at the panel server running on
 * your own machine; the bot learns about the draft from Sleeper's public API,
 * exactly as it does when you run the panel in its own window.
 *
 * Everything lives in a shadow root so Sleeper's stylesheet and ours cannot
 * reach each other, and the shell is a pure overlay - Sleeper's layout is never
 * reflowed.
 */

(() => {
  "use strict";

  const HOST_ID = "ffbot-sidebar-host";

  const DEFAULTS = { port: 8770, serverUrl: "", width: 430, collapsed: false };
  const MIN_W = 320;
  const MAX_W = 900;
  const PROBE_MS = 5000;

  const store = {
    get(cb) {
      try {
        chrome.storage.local.get(DEFAULTS, (v) => cb({ ...DEFAULTS, ...(v || {}) }));
      } catch (e) {
        cb({ ...DEFAULTS });
      }
    },
    set(patch) {
      try { chrome.storage.local.set(patch); } catch (e) { /* non-fatal */ }
    },
  };

  /* Sleeper is a single-page app: entering a draft room from the lobby is a
     client-side route change, not a page load, so Chrome never re-injects
     this script at the /draft/ URL. Instead the script runs on all of
     sleeper.com and watches the path itself, mounting only inside a draft
     room and cleaning up on the way out. */
  const onDraftPage = () => {
    if (location.hostname.endsWith("sleeper.com")
        || location.hostname.endsWith("sleeper.app")) {
      return location.pathname.startsWith("/draft/");
    }
    // Yahoo: fantasy draft clients live under /f1/<id>/draftclient or a
    // path containing "draft"; mount there and nowhere else on Yahoo.
    return /draft/i.test(location.pathname);
  };

  function sync() {
    const host = document.getElementById(HOST_ID);
    if (onDraftPage() && !host) {
      store.get((cfg) => {
        if (onDraftPage() && !document.getElementById(HOST_ID)) mount(cfg);
      });
    } else if (!onDraftPage() && host) {
      host.remove();
    }
  }

  sync();
  window.addEventListener("popstate", sync);
  setInterval(sync, 1000);   // pushState in the page world is invisible here

  function mount(cfg) {
    const host = document.createElement("div");
    host.id = HOST_ID;
    const shadow = host.attachShadow({ mode: "open" });

    const link = document.createElement("link");
    link.rel = "stylesheet";
    link.href = chrome.runtime.getURL("sidebar.css");
    shadow.appendChild(link);

    const root = document.createElement("div");
    root.className = "root";
    shadow.appendChild(root);

    // ---------------------------------------------------------- structure
    const panel = document.createElement("div");
    panel.className = "panel";
    panel.style.setProperty("--ffbot-w", clamp(cfg.width) + "px");

    const grip = document.createElement("div");
    grip.className = "grip";
    grip.title = "Drag to resize";

    const bar = document.createElement("div");
    bar.className = "bar";
    const dot = document.createElement("span");
    dot.className = "dot";
    const title = document.createElement("span");
    title.className = "title";
    title.textContent = "ff-draft-bot";
    const reload = mkBtn("reload", "Reload the panel");
    const hide = mkBtn("hide", "Collapse the sidebar");
    bar.append(dot, title, reload, hide);

    const frame = document.createElement("iframe");
    frame.className = "frame";
    frame.title = "ff-draft-bot panel";
    frame.hidden = true;

    const down = document.createElement("div");
    down.className = "down-msg";
    down.hidden = true;

    panel.append(grip, bar, frame, down);

    const pill = document.createElement("button");
    pill.className = "pill";
    pill.type = "button";
    pill.textContent = "ff-draft-bot";
    pill.hidden = true;

    root.append(panel, pill);
    (document.body || document.documentElement).appendChild(host);

    // ------------------------------------------------------------ helpers
    function mkBtn(label, tip) {
      const b = document.createElement("button");
      b.className = "btn";
      b.type = "button";
      b.textContent = label;
      b.title = tip;
      return b;
    }

    function clamp(w) {
      w = Number(w) || DEFAULTS.width;
      return Math.max(MIN_W, Math.min(MAX_W, Math.round(w)));
    }

    function serverBase() {
      const url = String(cfg.serverUrl || "").trim().replace(/\/+$/, "");
      return url || ("http://127.0.0.1:" + cfg.port);
    }

    /* /app is the panel on both the hosted service and a local server. */
    function panelUrl() { return serverBase() + "/app"; }

    function isHosted() { return serverBase().indexOf("https:") === 0; }

    function setCollapsed(next) {
      cfg.collapsed = !!next;
      panel.hidden = cfg.collapsed;
      pill.hidden = !cfg.collapsed;
      store.set({ collapsed: cfg.collapsed });
    }

    /* The panel is only mounted once we know something is answering, so a
       stopped server shows instructions instead of a broken frame. */
    function showDown() {
      frame.hidden = true;
      frame.removeAttribute("src");
      down.hidden = false;
      dot.className = "dot down";
      title.textContent = "ff-draft-bot - panel not running";

      down.textContent = "";
      const h = document.createElement("h2");
      h.textContent = "The panel server isn't running";
      const p1 = document.createElement("p");
      p1.textContent = "Start it in a terminal, then this sidebar connects "
        + "on its own within a few seconds:";
      const code = document.createElement("code");
      code.textContent = isHosted()
        ? "check " + serverBase() + " in a tab - the service may be down"
        : "cd ~/ff-draft-bot && ./scripts/ffbot-panel --port "
          + cfg.port + " --no-window";
      const p2 = document.createElement("p");
      p2.className = "muted";
      p2.textContent = "Expecting it on port " + cfg.port
        + ". Change that in the extension's options if you use another port.";
      down.append(h, p1, code, p2);
    }

    function showUp() {
      down.hidden = true;
      dot.className = "dot up";
      title.textContent = "ff-draft-bot";
      if (!frame.src) frame.src = panelUrl();
      frame.hidden = false;
    }

    /* Liveness goes through the service worker. A fetch from here would run
       as https://sleeper.com and be refused by CORS and Private Network
       Access, reporting the panel as down while it is happily running. */
    function probe() {
      if (isHosted()) { showUp(); return; }
      try {
        chrome.runtime.sendMessage(
          { type: "ffbot-ping", port: cfg.port },
          (res) => {
            if (chrome.runtime.lastError || !res || !res.up) showDown();
            else showUp();
          });
      } catch (e) {
        showDown();
      }
    }

    // ------------------------------------------------------------- events
    hide.addEventListener("click", () => setCollapsed(true));
    pill.addEventListener("click", () => setCollapsed(false));
    reload.addEventListener("click", () => {
      frame.removeAttribute("src");
      probe();
    });

    let dragging = false;
    grip.addEventListener("mousedown", (e) => {
      dragging = true;
      grip.classList.add("active");
      panel.classList.add("dragging");
      e.preventDefault();
    });
    document.addEventListener("mousemove", (e) => {
      if (!dragging) return;
      const w = clamp(window.innerWidth - e.clientX);
      panel.style.setProperty("--ffbot-w", w + "px");
    });
    document.addEventListener("mouseup", () => {
      if (!dragging) return;
      dragging = false;
      grip.classList.remove("active");
      panel.classList.remove("dragging");
      const w = clamp(parseInt(panel.style.getPropertyValue("--ffbot-w"), 10));
      cfg.width = w;
      store.set({ width: w });
    });

    try {
      chrome.storage.onChanged.addListener((changes, area) => {
        if (area !== "local" || (!changes.port && !changes.serverUrl)) return;
        if (changes.port) cfg.port = changes.port.newValue || DEFAULTS.port;
        if (changes.serverUrl) cfg.serverUrl = changes.serverUrl.newValue || "";
        frame.removeAttribute("src");
        probe();
      });
    } catch (e) { /* options page may be unavailable; not fatal */ }

    setCollapsed(cfg.collapsed);
    probe();
    setInterval(() => { if (frame.hidden && !cfg.collapsed) probe(); }, PROBE_MS);
  }
})();
