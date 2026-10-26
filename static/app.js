"use strict";

// Dates are handled as "YYYY-MM-DD" strings and stepped in UTC, so daylight
// saving changes can never shift a day.
const DAY_MS = 86400000;
const REFRESH_MS = 10 * 60 * 1000;
// The calendar opens on the afternoon: 14:45-19:15 fill the screen.
const FOCUS_START = 14.75;
const FOCUS_HOURS = 4.5;
const MIN_EVENT_MINUTES = 25; // shorter events still get a tappable box

const state = {
  timezone: "Asia/Jerusalem",
  people: [],
  hidden: new Set(readStored("hidden", [])),
  view: readStored("view", "week") === "day" ? "day" : "week",
  anchor: null, // the day shown in day view; its week in week view
  weeks: new Map(), // week start -> /api/events response
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
    /* private mode: settings just won't be remembered */
  }
}

// ---- Dates ----------------------------------------------------------------

function addDays(day, n) {
  const [y, m, d] = day.split("-").map(Number);
  return new Date(Date.UTC(y, m - 1, d) + n * DAY_MS).toISOString().slice(0, 10);
}

function utcDate(day) {
  const [y, m, d] = day.split("-").map(Number);
  return new Date(Date.UTC(y, m - 1, d, 12));
}

function sundayOf(day) {
  return addDays(day, -utcDate(day).getUTCDay()); // the Israeli week starts on Sunday
}

let partsFormat;
/** The day ("YYYY-MM-DD") and minute of the day of an instant, in the family's time zone. */
function zoned(instant) {
  partsFormat ||= new Intl.DateTimeFormat("en-CA", {
    timeZone: state.timezone,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hourCycle: "h23",
  });
  const p = Object.fromEntries(partsFormat.formatToParts(new Date(instant)).map((x) => [x.type, x.value]));
  return { day: `${p.year}-${p.month}-${p.day}`, minutes: Number(p.hour) * 60 + Number(p.minute) };
}

function todayInZone() {
  return zoned(Date.now()).day;
}

function timeOf(instant) {
  return new Intl.DateTimeFormat("he-IL", {
    timeZone: state.timezone,
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(new Date(instant));
}

function visibleDays() {
  if (state.view === "day") return [state.anchor];
  const first = sundayOf(state.anchor);
  return Array.from({ length: 7 }, (_, i) => addDays(first, i));
}

// ---- Server ---------------------------------------------------------------

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
  state.weeks.clear();
  showLogin();
});

// ---- Loading --------------------------------------------------------------

async function start() {
  const info = await api("/api/people");
  state.timezone = info.timezone;
  partsFormat = null;
  state.people = info.people;
  state.anchor = todayInZone();
  $("login").hidden = true;
  $("app").hidden = false;
  renderFilters();
  render();
  focusAfternoon();
  await load();
}

let loading = 0;

/** Fetches the week being shown; the other view of the same week needs no new request. */
async function load() {
  const week = sundayOf(state.anchor);
  loading++;
  $("refresh").classList.add("spinning");
  try {
    const data = await api(`/api/events?start=${week}&end=${addDays(week, 7)}`);
    state.weeks.set(week, data);
    if (sundayOf(state.anchor) === week) render();
  } catch (error) {
    if (error.message === "unauthenticated") return;
    renderNotices([navigator.onLine ? error.message : "אין חיבור לאינטרנט"]);
  } finally {
    if (--loading === 0) $("refresh").classList.remove("spinning");
  }
}

function moveTo(day) {
  const sameWeek = sundayOf(day) === sundayOf(state.anchor);
  state.anchor = day;
  render();
  if (!sameWeek || !state.weeks.has(sundayOf(day))) load();
}

function setView(view) {
  state.view = view;
  store("view", view);
  render();
}

$("prev").addEventListener("click", () => moveTo(addDays(state.anchor, state.view === "day" ? -1 : -7)));
$("next").addEventListener("click", () => moveTo(addDays(state.anchor, state.view === "day" ? 1 : 7)));
$("today").addEventListener("click", () => {
  moveTo(todayInZone());
  focusAfternoon();
});
$("refresh").addEventListener("click", () => load());
for (const button of document.querySelectorAll(".view-toggle button")) {
  button.addEventListener("click", () => setView(button.dataset.view));
}

let lastLoad = Date.now();
function refreshIfVisible(minAge) {
  if (document.visibilityState === "visible" && !$("app").hidden && Date.now() - lastLoad > minAge) {
    lastLoad = Date.now();
    load();
  }
}
document.addEventListener("visibilitychange", () => refreshIfVisible(60000));
setInterval(() => refreshIfVisible(0), REFRESH_MS);
setInterval(() => !$("app").hidden && placeNowLine(), 60000);

// ---- Hour height and scrolling ---------------------------------------------

let hourPx = 80;

/** Sizes an hour so that the afternoon focus fills the visible part of the calendar. */
function fitHours() {
  const scroller = $("scroller");
  const visible = scroller.clientHeight - $("cal-head").offsetHeight;
  if (visible <= 0) return;
  const atTop = scroller.scrollTop / hourPx;
  hourPx = Math.round(Math.min(120, Math.max(40, visible / FOCUS_HOURS)));
  scroller.style.setProperty("--hour", hourPx + "px");
  scroller.scrollTop = atTop * hourPx;
}

function focusAfternoon() {
  $("scroller").scrollTop = FOCUS_START * hourPx;
}

window.addEventListener("resize", () => !$("app").hidden && fitHours());

// ---- Rendering ------------------------------------------------------------

function el(tag, props = {}, children = []) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (key.startsWith("--")) node.style.setProperty(key, value);
    else if (key === "style") Object.assign(node.style, value);
    else if (key in node) node[key] = value;
    else node.setAttribute(key, value);
  }
  for (const child of [].concat(children)) {
    if (child != null) node.append(child);
  }
  return node;
}

function personById(id) {
  return state.people.find((p) => p.id === id) || { name: "", color: "#9ca3af" };
}

function renderFilters() {
  const nav = $("filters");
  nav.replaceChildren();
  for (const person of state.people) {
    const chip = el("button", {
      className: "chip",
      type: "button",
      "--c": person.color,
      "aria-pressed": String(!state.hidden.has(person.id)),
    }, person.name);
    chip.addEventListener("click", () => {
      if (state.hidden.has(person.id)) state.hidden.delete(person.id);
      else state.hidden.add(person.id);
      store("hidden", [...state.hidden]);
      chip.setAttribute("aria-pressed", String(!state.hidden.has(person.id)));
      render();
    });
    nav.append(chip);
  }
}

function renderNotices(messages) {
  $("notices").replaceChildren(...messages.map((m) => el("div", { className: "notice" }, m)));
}

function renderRange() {
  let text;
  if (state.view === "day") {
    text = new Intl.DateTimeFormat("he-IL", { weekday: "long", day: "numeric", month: "numeric", timeZone: "UTC" })
      .format(utcDate(state.anchor));
  } else {
    const first = sundayOf(state.anchor);
    const last = addDays(first, 6);
    // The year only when it isn't this year, so the title fits on a phone.
    const thisYear = todayInZone().slice(0, 4);
    const showYear = first.slice(0, 4) !== thisYear || last.slice(0, 4) !== thisYear;
    text = new Intl.DateTimeFormat("he-IL", {
      day: "numeric",
      month: "long",
      ...(showYear ? { year: "numeric" } : {}),
      timeZone: "UTC",
    }).formatRange(utcDate(first), utcDate(last));
  }
  $("range").textContent = text;
  $("prev").setAttribute("aria-label", state.view === "day" ? "היום הקודם" : "השבוע הקודם");
  $("next").setAttribute("aria-label", state.view === "day" ? "היום הבא" : "השבוע הבא");
  for (const button of document.querySelectorAll(".view-toggle button")) {
    button.setAttribute("aria-pressed", String(button.dataset.view === state.view));
  }
}

/** Days an all-day event covers (its end date is exclusive). */
function allDayDays(event) {
  const days = [];
  for (let d = event.start; d < event.end || d === event.start; d = addDays(d, 1)) {
    days.push(d);
    if (days.length > 62) break;
  }
  return days;
}

/** A timed event cut into one piece per day it touches, in minutes from midnight. */
function pieces(event) {
  const from = zoned(event.start);
  const to = zoned(event.end);
  if (to.day === from.day && to.minutes <= from.minutes) to.minutes = from.minutes;
  const out = [];
  for (let day = from.day; day <= to.day && out.length < 62; day = addDays(day, 1)) {
    const start = day === from.day ? from.minutes : 0;
    const end = day === to.day ? to.minutes : 1440;
    if (end > start || day === from.day) out.push({ event, day, start, end: Math.max(end, start) });
  }
  return out;
}

/** Side-by-side columns for overlapping events, like a paper diary. */
function arrange(items) {
  items.sort((a, b) => a.start - b.start || b.end - a.end);
  let group = [];
  let groupEnd = -1;
  const close = () => {
    const columns = []; // end minute of the last event in each column
    for (const item of group) {
      let col = columns.findIndex((end) => end <= item.start);
      if (col < 0) col = columns.push(0) - 1;
      columns[col] = item.shownEnd;
      item.col = col;
    }
    for (const item of group) item.cols = columns.length;
    group = [];
    groupEnd = -1;
  };
  for (const item of items) {
    item.shownEnd = Math.min(1440, Math.max(item.end, item.start + MIN_EVENT_MINUTES));
    if (group.length && item.start >= groupEnd) close();
    group.push(item);
    groupEnd = Math.max(groupEnd, item.shownEnd);
  }
  if (group.length) close();
  return items;
}

function render() {
  renderRange();
  const days = visibleDays();
  const today = todayInZone();
  const data = state.weeks.get(sundayOf(state.anchor));
  const isDay = state.view === "day";

  const allDay = new Map(days.map((d) => [d, []]));
  const timed = new Map(days.map((d) => [d, []]));
  if (data) {
    for (const event of data.events) {
      if (state.hidden.has(event.person)) continue;
      if (event.allDay) {
        for (const day of allDayDays(event)) allDay.get(day)?.push(event);
      } else {
        for (const piece of pieces(event)) timed.get(piece.day)?.push(piece);
      }
    }
    renderNotices(data.errors.map((e) =>
      `${personById(e.person).name}: ${e.message}` + (e.stale ? " (מוצגת הגרסה האחרונה שנטענה)" : "")));
    $("updated").textContent = "עודכן " + timeOf(data.generatedAt);
  }

  const scroller = $("scroller");
  scroller.style.setProperty("--days", days.length);
  scroller.classList.toggle("day-view", isDay);

  // Head: day names (tap one in week view to open that day), then all-day events.
  const dayName = new Intl.DateTimeFormat("he-IL", { weekday: isDay ? "long" : "narrow", timeZone: "UTC" });
  const labels = days.map((day) => {
    const label = el("button", {
      type: "button",
      className: "day-label" + (day === today ? " is-today" : ""),
      "aria-label": new Intl.DateTimeFormat("he-IL", { weekday: "long", day: "numeric", month: "long", timeZone: "UTC" }).format(utcDate(day)),
    }, [el("span", {}, dayName.format(utcDate(day))), el("span", { className: "num" }, String(Number(day.slice(8))))]);
    label.addEventListener("click", () => {
      if (!isDay) {
        state.anchor = day;
        setView("day");
      }
    });
    return label;
  });
  const head = [el("div", { className: "cal-row" }, [el("div"), ...labels])];
  if (days.some((d) => allDay.get(d).length)) {
    head.push(el("div", { className: "cal-row" }, [
      el("div", { className: "all-day-label" }, "כל היום"),
      ...days.map((d) => el("div", { className: "all-day-cell" }, allDay.get(d).map((e) => eventBox(e, isDay)))),
    ]));
  }
  $("cal-head").replaceChildren(...head);

  // Body: the hours column and one column per day, events placed by their time.
  const axis = el("div", { className: "axis" });
  for (let h = 1; h < 24; h++) {
    axis.append(el("span", { style: { top: `calc(var(--hour) * ${h})` } }, `${String(h).padStart(2, "0")}:00`));
  }
  const columns = days.map((day) => {
    const col = el("div", { className: "day-col" + (day === today ? " is-today" : ""), "data-day": day });
    for (const item of arrange(timed.get(day))) {
      const box = eventBox(item.event, isDay);
      Object.assign(box.style, {
        top: `calc(var(--hour) * ${item.start / 60})`,
        height: `calc(var(--hour) * ${(item.shownEnd - item.start) / 60} - 2px)`,
        insetInlineStart: `calc(${(item.col * 100) / item.cols}% + 2px)`,
        width: `calc(${100 / item.cols}% - 4px)`,
      });
      col.append(box);
    }
    return col;
  });
  $("cal-body").replaceChildren(axis, ...columns);
  placeNowLine();
  fitHours(); // the all-day row may have changed how much room is left
}

function eventBox(event, detailed) {
  const person = personById(event.person);
  const who = detailed && event.location ? `${person.name} · ${event.location}` : person.name;
  const box = el("button", { type: "button", className: "event", "--c": person.color }, [
    el("span", { className: "title" }, event.title),
    el("span", { className: "who" }, who),
  ]);
  box.addEventListener("click", () => showDetails(event));
  return box;
}

function placeNowLine() {
  document.querySelector(".now-line")?.remove();
  const now = zoned(Date.now());
  const col = document.querySelector(`.day-col[data-day="${now.day}"]`);
  if (!col) return;
  col.append(el("div", { className: "now-line", style: { top: `calc(var(--hour) * ${now.minutes / 60})` } }));
}

function showDetails(event) {
  const person = personById(event.person);
  const dialog = $("details");
  dialog.style.setProperty("--c", person.color);
  $("details-person").textContent = person.name;
  $("details-title").textContent = event.title;
  const dateFmt = new Intl.DateTimeFormat("he-IL", { weekday: "long", day: "numeric", month: "numeric", timeZone: "UTC" });
  if (event.allDay) {
    const last = addDays(event.end, -1);
    $("details-when").textContent = last > event.start
      ? `${dateFmt.format(utcDate(event.start))} – ${dateFmt.format(utcDate(last))} · כל היום`
      : `${dateFmt.format(utcDate(event.start))} · כל היום`;
  } else {
    $("details-when").textContent =
      // Isolated left-to-right, or Hebrew text around it shows 10:00–09:00.
      `${dateFmt.format(utcDate(zoned(event.start).day))} · \u2066${timeOf(event.start)}–${timeOf(event.end)}\u2069`;
  }
  $("details-where").textContent = event.location ? "📍 " + event.location : "";
  dialog.showModal();
}

// Tapping outside the details card closes it.
$("details").addEventListener("click", (event) => {
  if (event.target === $("details")) $("details").close();
});

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
