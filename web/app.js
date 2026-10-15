const I18N = {
  ru: {
    title: "Чат",
    destination: "Куда",
    destinationHere: "этот сайт",
    message: "Сообщение",
    send: "Отправить",
    attach: "Файл",
    download: "Скачать",
    peerOnline: "Канал с другим офисом поднят",
    peerOffline: "Канал с другим офисом недоступен",
    empty: "Сообщений пока нет",
    connecting: "Подключение…",
    fileTooLarge: "Файл больше допустимого размера",
    progress: "передаётся {got} из {total}",
    waitingLink: "ожидание канала",
    sending: "отправляется",
    sendFailed: "Не удалось отправить",
    placeholderMessage: "Текст сообщения",
    localOnline: "На этом сайте: {n}",
    peerUsers: "На другом сайте: {n}",
    siteLine: "{name} · {site}",
  },
  en: {
    title: "Chat",
    destination: "To",
    destinationHere: "this site",
    message: "Message",
    send: "Send",
    attach: "File",
    download: "Download",
    peerOnline: "Link to the other office is up",
    peerOffline: "Link to the other office is down",
    empty: "No messages yet",
    connecting: "Connecting…",
    fileTooLarge: "File exceeds the size limit",
    progress: "transferring {got} of {total}",
    waitingLink: "waiting for the link",
    sending: "sending",
    sendFailed: "Could not send",
    placeholderMessage: "Message text",
    localOnline: "On this site: {n}",
    peerUsers: "On the other site: {n}",
    siteLine: "{name} · {site}",
  },
};

const state = {
  lang: initialLang(),
  name: "",
  localAddress: "",
  destinations: [],
  messages: new Map(),
  peerOnline: false,
  peerUsers: 0,
  localOnline: 0,
  siteName: "",
  maxFileBytes: 100 * 1024 * 1024,
  wsOpen: false,
  socket: null,
  pendingRef: "",
  pendingText: "",
};

const transcript = document.getElementById("transcript");
const destSelect = document.getElementById("destination");
const textInput = document.getElementById("text");
const fileInput = document.getElementById("file");
const fileName = document.getElementById("file-name");
const formError = document.getElementById("form-error");
const sendButton = document.getElementById("send");
const siteLine = document.getElementById("site-line");
const peerEl = document.getElementById("peer");

function initialLang() {
  const saved = localStorage.getItem("ui_lang");
  if (saved === "ru" || saved === "en") return saved;
  const nav = (navigator.language || "ru").toLowerCase();
  if (nav.startsWith("en")) return "en";
  return "ru";
}

function t(key) {
  return (I18N[state.lang] && I18N[state.lang][key]) || key;
}

function fill(template, values) {
  return template.replace(/\{(\w+)\}/g, (_, key) => String(values[key] ?? ""));
}

function applyLang() {
  document.documentElement.lang = state.lang;
  document.title = t("title");
  document.querySelectorAll("[data-i18n]").forEach((el) => {
    el.textContent = t(el.dataset.i18n);
  });
  document.querySelectorAll("[data-i18n-placeholder]").forEach((el) => {
    el.placeholder = t(el.dataset.i18nPlaceholder);
  });
  document.getElementById("lang-ru").classList.toggle("active", state.lang === "ru");
  document.getElementById("lang-en").classList.toggle("active", state.lang === "en");
  document.getElementById("lang-ru").setAttribute("aria-pressed", state.lang === "ru" ? "true" : "false");
  document.getElementById("lang-en").setAttribute("aria-pressed", state.lang === "en" ? "true" : "false");
  sendButton.textContent = state.pendingRef ? t("sending") : t("send");
  renderChrome();
  renderDestinations();
  renderMessages();
}

function setLang(lang) {
  state.lang = lang;
  localStorage.setItem("ui_lang", lang);
  applyLang();
}

function renderDestinations() {
  const previous = destSelect.value || localStorage.getItem("dest") || "";
  destSelect.replaceChildren();
  for (const addr of state.destinations) {
    const option = document.createElement("option");
    option.value = addr;
    const here = addr === state.localAddress ? ` (${t("destinationHere")})` : "";
    option.textContent = addr + here;
    destSelect.append(option);
  }
  const remote = state.destinations.find((addr) => addr !== state.localAddress);
  if (state.destinations.includes(previous)) destSelect.value = previous;
  else if (remote) destSelect.value = remote;
  else if (state.destinations.length) destSelect.value = state.destinations[0];
}

function renderChrome() {
  siteLine.textContent = state.siteName
    ? fill(t("siteLine"), { name: state.name || state.siteName, site: state.siteName })
    : "";
  peerEl.classList.toggle("online", state.wsOpen && state.peerOnline);
  peerEl.classList.toggle("offline", state.wsOpen && !state.peerOnline);
  const bits = [];
  if (!state.wsOpen) bits.push(t("connecting"));
  else bits.push(state.peerOnline ? t("peerOnline") : t("peerOffline"));
  if (state.wsOpen) {
    bits.push(fill(t("localOnline"), { n: state.localOnline }));
    if (state.peerOnline) bits.push(fill(t("peerUsers"), { n: state.peerUsers }));
  }
  peerEl.replaceChildren();
  const dot = document.createElement("span");
  dot.className = "dot";
  peerEl.append(dot, document.createTextNode(bits.join(" · ")));
}

function formatTime(ts) {
  const locale = state.lang === "en" ? "en-GB" : "ru-RU";
  return new Date(ts * 1000).toLocaleString(locale, {
    day: "2-digit",
    month: "short",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function formatSize(size) {
  const units = state.lang === "ru" ? ["Б", "КБ", "МБ", "ГБ"] : ["B", "KB", "MB", "GB"];
  let value = Number(size) || 0;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  const digits = unit === 0 || value >= 10 ? 0 : 1;
  let text = value.toFixed(digits);
  if (state.lang === "ru") text = text.replace(".", ",");
  return `${text} ${units[unit]}`;
}

function transferLabel(message) {
  const size = Number(message.size || 0);
  const received = Number(message.received || 0);
  if (!(size > 0 && received < size)) return "";
  const remote = Boolean(message.dest && state.localAddress && message.dest !== state.localAddress);
  if (message.file_complete && !remote) return "";
  if (!state.peerOnline) return t("waitingLink");
  return fill(t("progress"), { got: formatSize(received), total: formatSize(size) });
}

function releaseComposer(clearText) {
  const unchanged = textInput.value === state.pendingText;
  state.pendingRef = "";
  state.pendingText = "";
  if (clearText && unchanged) textInput.value = "";
  const open = Boolean(state.socket && state.socket.readyState === WebSocket.OPEN);
  state.wsOpen = open;
  sendButton.disabled = !open;
  sendButton.textContent = t("send");
}

function acceptOutgoing(payload) {
  if (!state.pendingRef || !payload || !payload.message) return false;
  if (payload.ref && payload.ref === state.pendingRef) return true;
  const message = payload.message;
  return message.kind !== "file"
    && message.site === state.siteName
    && message.text === state.pendingText
    && Math.abs(Number(message.ts) * 1000 - Date.now()) < 15000;
}

function renderMessages() {
  const nearBottom = transcript.scrollHeight - transcript.scrollTop - transcript.clientHeight < 80;
  transcript.replaceChildren();
  const items = [...state.messages.values()].sort((a, b) => a.ts - b.ts || String(a.id).localeCompare(String(b.id)));
  if (!items.length) {
    const empty = document.createElement("p");
    empty.className = "empty";
    empty.textContent = t("empty");
    transcript.append(empty);
    return;
  }
  for (const message of items) {
    const card = document.createElement("article");
    card.className = "bubble" + (message.site === state.siteName ? " local" : "");
    const meta = document.createElement("div");
    meta.className = "meta";
    const nick = document.createElement("span");
    nick.className = "nick";
    nick.textContent = message.nick;
    const site = document.createElement("span");
    site.className = "site";
    site.textContent = message.site;
    const time = document.createElement("time");
    time.dateTime = new Date(message.ts * 1000).toISOString();
    time.textContent = formatTime(message.ts);
    meta.append(nick, site, time);
    if (message.dest) {
      const dest = document.createElement("span");
      dest.textContent = "→ " + message.dest;
      meta.append(dest);
    }
    card.append(meta);
    if (message.kind === "file") {
      const row = document.createElement("div");
      row.className = "file-row";
      const name = document.createElement("span");
      name.textContent = `${message.filename} (${formatSize(message.size || 0)})`;
      row.append(name);
      const label = transferLabel(message);
      if (label) {
        const pending = document.createElement("span");
        pending.className = "transfer";
        pending.textContent = label;
        row.append(pending);
      }
      const size = Number(message.size || 0);
      const received = Number(message.received || 0);
      if (message.file_complete || (size > 0 && received >= size)) {
        const link = document.createElement("a");
        link.href = `/api/files/${encodeURIComponent(message.id)}`;
        link.textContent = t("download");
        row.append(link);
      }
      card.append(row);
    } else {
      const body = document.createElement("div");
      body.className = "body";
      body.textContent = message.text || "";
      card.append(body);
    }
    transcript.append(card);
  }
  if (nearBottom) transcript.scrollTop = transcript.scrollHeight;
}

function upsert(message) {
  state.messages.set(message.id, message);
  renderMessages();
}

function showError(text) {
  formError.hidden = !text;
  formError.textContent = text || "";
}

function newRef() {
  // Workstations open this page over plain HTTP on the LAN. The UUID helper
  // on crypto is limited to HTTPS, so the id is built from getRandomValues.
  const bytes = new Uint8Array(16);
  crypto.getRandomValues(bytes);
  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  let hex = "";
  for (const byte of bytes) hex += byte.toString(16).padStart(2, "0");
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}

function connect() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const socket = new WebSocket(`${proto}://${location.host}/ws`);
  state.socket = socket;
  socket.addEventListener("open", () => {
    state.wsOpen = true;
    sendButton.disabled = Boolean(state.pendingRef);
    renderChrome();
  });
  socket.addEventListener("message", (event) => {
    let payload;
    try {
      payload = JSON.parse(event.data);
    } catch (_err) {
      return;
    }
    if (payload.type === "hello") {
      state.siteName = payload.site_name || "";
      state.name = payload.name || "";
      applyDestinationList(payload.address, payload.destinations);
      state.peerOnline = Boolean(payload.peer_online);
      state.peerUsers = payload.peer_users || 0;
      state.localOnline = payload.local_online || 0;
      state.maxFileBytes = payload.max_file_bytes || state.maxFileBytes;
      state.messages = new Map((payload.history || []).map((message) => [message.id, message]));
      renderChrome();
      renderMessages();
    } else if (payload.type === "message" && payload.message) {
      if (acceptOutgoing(payload)) releaseComposer(true);
      upsert(payload.message);
    } else if (payload.type === "file_progress") {
      const message = state.messages.get(String(payload.id || "").toLowerCase()) || state.messages.get(payload.id);
      if (message) {
        const next = Number(payload.received) || 0;
        const previous = Number(message.received || 0);
        if (next === 0 && Number(message.size) > 0) {
          message.received = 0;
          message.rejected = true;
        } else if (!message.rejected && next >= previous) {
          message.received = next;
        }
        renderMessages();
      }
    } else if (payload.type === "destinations") {
      applyDestinationList(payload.address, payload.addresses);
      renderChrome();
    } else if (payload.type === "peer") {
      state.peerOnline = Boolean(payload.online);
      state.peerUsers = payload.users || 0;
      renderChrome();
      renderMessages();
    } else if (payload.type === "presence") {
      state.localOnline = payload.local_online || 0;
      renderChrome();
    } else if (payload.type === "error") {
      if (state.pendingRef) releaseComposer(false);
      showError(payload.error || t("sendFailed"));
    }
  });
  socket.addEventListener("close", () => {
    if (state.socket !== socket) return;
    state.wsOpen = false;
    state.peerOnline = false;
    if (state.pendingRef) {
      releaseComposer(false);
      showError(t("sendFailed"));
    } else {
      sendButton.disabled = true;
    }
    renderChrome();
    renderMessages();
    setTimeout(connect, 1000);
  });
}

document.getElementById("lang-ru").addEventListener("click", () => setLang("ru"));
document.getElementById("lang-en").addEventListener("click", () => setLang("en"));

function applyDestinationList(address, addresses) {
  state.localAddress = address || "";
  state.destinations = Array.isArray(addresses) ? addresses : [];
  renderDestinations();
}

destSelect.addEventListener("change", () => {
  localStorage.setItem("dest", destSelect.value);
});

fileInput.addEventListener("change", () => {
  const file = fileInput.files && fileInput.files[0];
  fileName.textContent = file ? file.name : "";
  showError("");
});

document.getElementById("composer").addEventListener("submit", async (event) => {
  event.preventDefault();
  showError("");
  const dest = destSelect.value;
  if (!dest) {
    showError(t("sendFailed"));
    return;
  }
  localStorage.setItem("dest", dest);
  const text = textInput.value;
  const file = fileInput.files && fileInput.files[0];
  if (!text.trim() && !file) return;
  if (file && file.size > state.maxFileBytes) {
    showError(t("fileTooLarge"));
    return;
  }
  try {
    if (text.trim()) {
      if (state.pendingRef) return;
      if (!state.socket || state.socket.readyState !== WebSocket.OPEN) {
        showError(t("sendFailed"));
        return;
      }
      const ref = newRef();
      state.pendingRef = ref;
      state.pendingText = text;
      sendButton.disabled = true;
      sendButton.textContent = t("sending");
      try {
        state.socket.send(JSON.stringify({ type: "chat", text, to: dest, ref }));
      } catch (_sendErr) {
        releaseComposer(false);
        showError(t("sendFailed"));
        return;
      }
    }
    if (file) {
      const form = new FormData();
      form.append("to", dest);
      form.append("file", file);
      const response = await fetch("/api/upload", { method: "POST", body: form });
      if (!response.ok) {
        showError(t("sendFailed"));
        return;
      }
      fileInput.value = "";
      fileName.textContent = "";
    }
  } catch (_err) {
    showError(t("sendFailed"));
  }
});

sendButton.disabled = true;
applyLang();
connect();
