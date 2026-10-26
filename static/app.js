"use strict";

// Dates are handled as "YYYY-MM-DD" strings and stepped in UTC, so daylight
// saving changes can never shift a day.
const DAY_MS = 86400000;
const REFRESH_MS = 10 * 60 * 1000;

const state = {
  timezone: "Asia/Jerusalem",
  calendars: [],
  hidden: new Set(readStored("hidden", [])),
  weekStart: null,
  data: null,
};

const $ = (id) => document.getElementById(id);

function readStored(key, fallback) {
  try {
    const value = localStorage.getItem("fc:" + key);
    return value ? JSON.parse(value) : fallback;
  } catch {
    return fallback;
  }
}

function store(key, value) {
  try {
    localStorage.setItem("fc:" + key, JSON.stringify(value));
  } catch {
    /* private mode: filters just won't be remembered */
  }
}

function addDays(day, n) {
  const [y, m, d] = day.split("-").map(Number);
  return new Date(Date.UTC(y, m - 1, d) + n * DAY_MS).toISOString().slice(0, 10);
}

function weekday(day) {
  const [y, m, d] = day.split("-").map(Number);
  return new Date(Date.UTC(y, m - 1, d)).getUTCDay(); // 0 = Sunday
}

function utcDate(day) {
  const [y, m, d] = day.split("-").map(Number);
  return new Date(Date.UTC(y, m - 1, d, 12));
}

function todayInZone() {
  // en-CA formats as YYYY-MM-DD.
  return new Intl.DateTimeFormat("en-CA", { timeZone: state.timezone }).format(new Date());
}

function dayOf(isoDateTime) {
  return new Intl.DateTimeFormat("en-CA", { timeZone: state.timezone }).format(new Date(isoDateTime));
}

function timeOf(isoDateTime) {
  return new Intl.DateTimeFormat("he-IL", {
    timeZone: state.timezone,
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(new Date(isoDateTime));
}

function sundayOf(day) {
  return addDays(day, -weekday(day)); // the Israeli week starts on Sunday
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    credentials: "same-origin",
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  if (response.status === 401 && path !== "/api/login") {
    showLogin();
    throw new Error("unauthenticated");
  }
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(body.detail || "שגיאה " + response.status);
  return body;
}

// ---- Login ----------------------------------------------------------------

function showLogin(message = "") {
  $("app").hidden = true;
  $("login").hidden = false;
  $("login-error").textContent = message;
  $("password").focus();
}

$("login-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  $("login-error").textContent = "";
  try {
    await api("/api/login", {
      method: "POST",
      body: JSON.stringify({ password: $("password").value }),
    });
    $("password").value = "";
    await start();
  } catch (error) {
    $("login-error").textContent = error.message;
  }
});

$("logout").addEventListener("click", async () => {
  await api("/api/logout", { method: "POST" }).catch(() => {});
  showLogin();
});

// ---- Loading --------------------------------------------------------------

async function start() {
  const info = await api("/api/calendars");
  state.timezone = info.timezone;
  state.calendars = info.calendars;
  state.weekStart = sundayOf(todayInZone());
  $("login").hidden = true;
  $("app").hidden = false;
  renderFilters();
  await load();
}

let loadSeq = 0;

async function load() {
  const seq = ++loadSeq;
  const start = state.weekStart;
  renderRange();
  $("refresh").classList.add("spinning");
  try {
    const data = await api(`/api/events?start=${start}&end=${addDays(start, 7)}`);
    if (seq !== loadSeq) return; // the user already moved to another week
    state.data = data;
    renderWeek();
  } catch (error) {
    if (seq !== loadSeq || error.message === "unauthenticated") return;
    renderNotices([navigator.onLine ? error.message : "אין חיבור לאינטרנט"]);
  } finally {
    if (seq === loadSeq) $("refresh").classList.remove("spinning");
  }
}

function showWeek(weekStart) {
  if (weekStart !== state.weekStart) {
    state.weekStart = weekStart;
    state.data = null;
    renderWeek();
  }
  load();
}

$("prev").addEventListener("click", () => showWeek(addDays(state.weekStart, -7)));
$("next").addEventListener("click", () => showWeek(addDays(state.weekStart, 7)));
$("today").addEventListener("click", () => showWeek(sundayOf(todayInZone())));
$("refresh").addEventListener("click", () => load());

let lastLoad = Date.now();
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible" && !$("app").hidden && Date.now() - lastLoad > 60000) {
    lastLoad = Date.now();
    load();
  }
});
setInterval(() => {
  if (document.visibilityState === "visible" && !$("app").hidden) {
    lastLoad = Date.now();
    load();
  }
}, REFRESH_MS);

// ---- Rendering ------------------------------------------------------------

function el(tag, props = {}, children = []) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (key === "style") Object.assign(node.style, value);
    else if (key.startsWith("--")) node.style.setProperty(key, value);
    else if (key in node) node[key] = value;
    else node.setAttribute(key, value);
  }
  for (const child of [].concat(children)) {
    if (child != null) node.append(child);
  }
  return node;
}

function renderRange() {
  const first = utcDate(state.weekStart);
  const last = utcDate(addDays(state.weekStart, 6));
  // The year only when it isn't this year, so the title fits on a phone.
  const thisYear = todayInZone().slice(0, 4);
  const showYear = state.weekStart.slice(0, 4) !== thisYear || addDays(state.weekStart, 6).slice(0, 4) !== thisYear;
  const fmt = new Intl.DateTimeFormat("he-IL", {
    day: "numeric",
    month: "long",
    ...(showYear ? { year: "numeric" } : {}),
    timeZone: "UTC",
  });
  $("range").textContent = fmt.formatRange(first, last);
}

function renderFilters() {
  const nav = $("filters");
  nav.replaceChildren();
  for (const cal of state.calendars) {
    const chip = el("button", {
      className: "chip",
      type: "button",
      "--c": cal.color,
      "aria-pressed": String(!state.hidden.has(cal.id)),
    }, [el("span", { className: "dot" }), cal.name]);
    chip.addEventListener("click", () => {
      if (state.hidden.has(cal.id)) state.hidden.delete(cal.id);
      else state.hidden.add(cal.id);
      store("hidden", [...state.hidden]);
      chip.setAttribute("aria-pressed", String(!state.hidden.has(cal.id)));
      renderWeek();
    });
    nav.append(chip);
  }
  if (!state.calendars.length) {
    renderNotices(["עדיין לא הוגדרו יומנים. ראו את קובץ ההוראות (README)."]);
  }
}

function renderNotices(messages) {
  $("notices").replaceChildren(...messages.map((m) => el("div", { className: "notice" }, m)));
}

function calendarById(id) {
  return state.calendars.find((c) => c.id === id) || { name: "", color: "#9ca3af" };
}

/** Days of the week an event appears on (all-day events span each of their days). */
function daysOf(event) {
  if (event.allDay) {
    const days = [];
    for (let d = event.start; d < event.end || d === event.start; d = addDays(d, 1)) days.push(d);
    return days;
  }
  return [dayOf(event.start)];
}

function renderWeek() {
  const today = todayInZone();
  const byDay = new Map();
  for (let i = 0; i < 7; i++) byDay.set(addDays(state.weekStart, i), []);

  const data = state.data;
  if (data) {
    for (const event of data.events) {
      if (state.hidden.has(event.calendar)) continue;
      for (const day of daysOf(event)) byDay.get(day)?.push(event);
    }
    const notices = data.errors.map((e) => {
      const name = calendarById(e.calendar).name;
      return `${name}: ${e.message}` + (e.stale ? " (מוצגת הגרסה האחרונה שנטענה)" : "");
    });
    renderNotices(notices);
    $("updated").textContent = "עודכן " + timeOf(data.generatedAt);
  }

  const weekdayFmt = new Intl.DateTimeFormat("he-IL", { weekday: "long", timeZone: "UTC" });
  const dateFmt = new Intl.DateTimeFormat("he-IL", { day: "numeric", month: "numeric", timeZone: "UTC" });

  const days = [...byDay.entries()].map(([day, events]) => {
    const isToday = day === today;
    const head = el("div", { className: "day-head" }, [
      weekdayFmt.format(utcDate(day)),
      el("span", { className: "date" }, dateFmt.format(utcDate(day))),
      isToday ? el("span", { className: "badge" }, "היום") : null,
    ]);
    let body;
    if (!data) body = el("div", { className: "day-empty" }, "טוען…");
    else if (!events.length) body = el("div", { className: "day-empty" }, "אין אירועים");
    else body = el("ul", { className: "events" }, events.map(renderEvent));
    return el("section", {
      className: "day" + (isToday ? " is-today" : "") + (day < today ? " is-past" : ""),
    }, [head, body]);
  });
  $("week").replaceChildren(...days);
}

function renderEvent(event) {
  const cal = calendarById(event.calendar);
  let time;
  if (event.allDay) time = "כל היום";
  else time = `${timeOf(event.start)}–${timeOf(event.end)}`;
  const meta = [cal.name, event.location].filter(Boolean).join(" · ");
  return el("li", { className: "event" + (event.allDay ? " all-day" : ""), "--c": cal.color }, [
    el("span", { className: "time" }, time),
    el("div", { className: "body" }, [
      el("div", { className: "title" }, event.title),
      el("div", { className: "meta" }, meta),
    ]),
  ]);
}

// ---- Boot -----------------------------------------------------------------

(async function boot() {
  if ("serviceWorker" in navigator) {
    navigator.serviceWorker.register("/sw.js").catch(() => {});
  }
  try {
    const session = await api("/api/session");
    if (!session.configured) showLogin("לא הוגדרה סיסמה לאפליקציה (FAMILY_PASSWORD)");
    else if (!session.authenticated) showLogin();
    else await start();
  } catch (error) {
    if (error.message !== "unauthenticated") showLogin(navigator.onLine ? error.message : "אין חיבור לאינטרנט");
  }
})();
