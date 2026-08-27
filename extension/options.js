/* Persist the panel port. The content script watches storage and reconnects. */
const DEFAULT_PORT = 8770;
const portEl = document.getElementById("port");
const okEl = document.getElementById("ok");

const urlEl = document.getElementById("serverUrl");

chrome.storage.local.get({ port: DEFAULT_PORT, serverUrl: "" }, (v) => {
  portEl.value = v.port || DEFAULT_PORT;
  urlEl.value = v.serverUrl || "";
});

document.getElementById("save").addEventListener("click", () => {
  const n = parseInt(portEl.value, 10);
  if (!Number.isInteger(n) || n < 1 || n > 65535) {
    okEl.textContent = "not a valid port";
    okEl.style.color = "#b3261e";
    return;
  }
  const url = urlEl.value.trim().replace(/\/+$/, "");
  if (url && !/^https?:\/\//.test(url)) {
    okEl.textContent = "server URL must start with http(s)://";
    okEl.style.color = "#b3261e";
    return;
  }
  chrome.storage.local.set({ port: n, serverUrl: url }, () => {
    okEl.style.color = "#147a4a";
    okEl.textContent = "saved";
    setTimeout(() => { okEl.textContent = ""; }, 1800);
  });
});
