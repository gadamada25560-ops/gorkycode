const chatEl = document.getElementById("chat");
const inputEl = document.getElementById("input");
const sendEl = document.getElementById("send");
const micEl = document.getElementById("mic");
const hintEl = document.getElementById("hint");
const eventsEl = document.getElementById("events");
const overlayEl = document.getElementById("overlay");
const closeCardEl = document.getElementById("closeCard");

const history = [];
let scope = "upcoming";
let mediaRecorder = null;
let chunks = [];
let recording = false;
let currentAudio = null;
let vadTimer = null;
let audioContext = null;

const kinds = { task: "задача", meeting: "встреча", reminder: "напоминание" };
const statuses = { open: "запланировано", done: "сделано" };

function apiError(data, fallback) {
  const detail = data && data.detail;
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail) && detail[0]) {
    if (typeof detail[0] === "string") return detail[0];
    if (detail[0].msg) return detail[0].msg;
  }
  return fallback;
}

function addMessage(role, text) {
  const node = document.createElement("div");
  node.className = `msg ${role}`;
  node.textContent = text;
  chatEl.appendChild(node);
  chatEl.scrollTop = chatEl.scrollHeight;
}

async function playSpeech(text) {
  if (!text) return;
  try {
    const res = await fetch("/api/speak", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text, history: [], speak: false }),
    });
    if (!res.ok) return;
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    if (currentAudio) currentAudio.pause();
    currentAudio = new Audio(url);
    currentAudio.play().catch(() => {});
  } catch (_) {}
}

function openCard(item) {
  document.getElementById("cardKind").textContent = kinds[item.event_type] || item.event_type;
  document.getElementById("cardTitle").textContent = item.title || "Без названия";
  document.getElementById("cardWhen").textContent = [item.date, item.time].filter(Boolean).join(" ") || "дата не указана";
  document.getElementById("cardDescription").textContent = item.description || "Без описания";
  document.getElementById("cardStatus").textContent = statuses[item.status] || item.status || "—";
  document.getElementById("cardCreated").textContent = item.created_at || "—";
  overlayEl.classList.remove("hidden");
}

function closeCard() {
  overlayEl.classList.add("hidden");
}

function renderEvents(items) {
  eventsEl.innerHTML = "";
  if (!items.length) {
    eventsEl.innerHTML = '<li class="empty">На эти дни записей нет</li>';
    return;
  }
  for (const item of items) {
    const li = document.createElement("li");
    const when = [item.date, item.time].filter(Boolean).join(" ") || "без даты";
    li.innerHTML = `<div class="kind">${kinds[item.event_type] || item.event_type}</div>
      <div class="title">${item.title}</div>
      <div class="when">${when}</div>`;
    li.addEventListener("click", () => openCard(item));
    eventsEl.appendChild(li);
  }
}

async function loadEvents() {
  try {
    const res = await fetch(`/api/events?scope=${scope}`);
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(apiError(data, "Не удалось загрузить список"));
    renderEvents(data.events || []);
  } catch (err) {
    eventsEl.innerHTML = `<li class="empty">${err.message || "Список недоступен"}</li>`;
  }
}

async function sendText(text) {
  const value = text.trim();
  if (!value) return;
  addMessage("user", value);
  history.push({ role: "user", content: value });
  inputEl.value = "";
  hintEl.textContent = "Секунду…";
  try {
    const res = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text: value, history, speak: false }),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(apiError(data, "Запрос не прошёл"));
    addMessage("assistant", data.reply);
    history.push({ role: "assistant", content: data.reply });
    playSpeech(data.reply);
    if (data.upcoming) renderEvents(data.upcoming);
    hintEl.textContent = "Можно говорить или писать";
  } catch (err) {
    addMessage("system", String(err.message || err));
    hintEl.textContent = "Не вышло. Попробуйте ещё раз.";
  }
}

function stopVad() {
  if (vadTimer) {
    clearInterval(vadTimer);
    vadTimer = null;
  }
  if (audioContext) {
    audioContext.close().catch(() => {});
    audioContext = null;
  }
}

function watchSilence(stream) {
  audioContext = new AudioContext();
  const source = audioContext.createMediaStreamSource(stream);
  const analyser = audioContext.createAnalyser();
  analyser.fftSize = 2048;
  source.connect(analyser);
  const data = new Uint8Array(analyser.fftSize);
  let heard = false;
  let silenceFrom = 0;
  const startedAt = Date.now();

  vadTimer = setInterval(() => {
    analyser.getByteTimeDomainData(data);
    let sum = 0;
    for (const value of data) {
      const norm = (value - 128) / 128;
      sum += norm * norm;
    }
    const rms = Math.sqrt(sum / data.length);
    const now = Date.now();
    if (rms > 0.018) {
      heard = true;
      silenceFrom = now;
    } else if (heard && now - silenceFrom > 1200) {
      stopRecording();
    } else if (!heard && now - startedAt > 8000) {
      stopRecording();
    }
  }, 80);
}

function pickMime() {
  const options = ["audio/webm;codecs=opus", "audio/webm", "audio/mp4"];
  return options.find((type) => window.MediaRecorder && MediaRecorder.isTypeSupported(type)) || "";
}

async function startRecording() {
  const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
  chunks = [];
  const mime = pickMime();
  mediaRecorder = new MediaRecorder(stream, mime ? { mimeType: mime } : undefined);
  mediaRecorder.ondataavailable = (event) => {
    if (event.data.size) chunks.push(event.data);
  };
  mediaRecorder.onstop = async () => {
    stopVad();
    stream.getTracks().forEach((track) => track.stop());
    const type = mediaRecorder.mimeType || mime || "audio/webm";
    const blob = new Blob(chunks, { type });
    if (blob.size < 500) {
      hintEl.textContent = "Похоже, ничего не записалось. Нажмите на кружок и скажите фразу.";
      return;
    }
    const ext = type.includes("mp4") ? "mp4" : "webm";
    const form = new FormData();
    form.append("audio", blob, `speech.${ext}`);
    hintEl.textContent = "Разбираю запись…";
    try {
      const res = await fetch("/api/transcribe", { method: "POST", body: form });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(apiError(data, "Не удалось разобрать голос"));
      if (data.text) await sendText(data.text);
      else hintEl.textContent = "Не разобрала, что сказано. Повторите или напишите текстом.";
    } catch (err) {
      addMessage("system", String(err.message || err));
      hintEl.textContent = "Голос не распознался. Можно написать запрос в поле справа.";
    }
  };
  mediaRecorder.start();
  recording = true;
  micEl.classList.add("rec");
  hintEl.textContent = "Слушаю. Замолчите — остановлюсь.";
  watchSilence(stream);
}

function stopRecording() {
  if (!recording) return;
  recording = false;
  micEl.classList.remove("rec");
  stopVad();
  if (mediaRecorder && mediaRecorder.state !== "inactive") mediaRecorder.stop();
}

micEl.addEventListener("click", async () => {
  try {
    if (recording) stopRecording();
    else await startRecording();
  } catch (_) {
    hintEl.textContent = "Нет доступа к микрофону. Разрешите его в браузере или пишите текстом.";
  }
});

sendEl.addEventListener("click", () => sendText(inputEl.value));
inputEl.addEventListener("keydown", (event) => {
  if (event.key === "Enter") sendText(inputEl.value);
});
closeCardEl.addEventListener("click", closeCard);
overlayEl.addEventListener("click", (event) => {
  if (event.target === overlayEl) closeCard();
});

document.querySelectorAll(".tabs button").forEach((button) => {
  button.addEventListener("click", () => {
    document.querySelectorAll(".tabs button").forEach((item) => item.classList.remove("active"));
    button.classList.add("active");
    scope = button.dataset.scope;
    loadEvents();
  });
});

addMessage("assistant", "Напишите или скажите, что нужно сделать: задачу, встречу, план на день. Справа — список дел, нажмите на строку, чтобы открыть карточку.");
loadEvents();
