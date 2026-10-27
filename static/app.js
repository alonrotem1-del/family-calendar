"use strict";

// Dates are handled as "YYYY-MM-DD" strings and stepped in UTC, so daylight
// saving changes can never shift a day.
const DAY_MS = 86400000;
const REFRESH_MS = 10 * 60 * 1000;
// The calendar opens on the afternoon: 14:45-19:15 fill the screen.
const FOCUS_START = 14.75;
const FOCUS_HOURS = 4.5;
const MIN_EVENT_MINUTES = 25; // shorter events still get a tappable box
// "workweek" is Sunday to Thursday, the default; "week" adds Friday and Saturday.
const VIEWS = ["day", "workweek", "week"];
const VIEW_DAYS = { day: 1, workweek: 5, week: 7 };

const state = {
  timezone: "Asia/Jerusalem",
  people: [],
  hidden: new Set(readStored("hidden", [])),
  view: VIEWS.includes(readStored("view")) ? readStored("view") : "workweek",
  anchor: null, // the day shown in day view; its week in the week views
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
  return Array.from({ length: VIEW_DAYS[state.view] }, (_, i) => addDays(first, i));
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
  state.storage = info.storage;
  state.storageError = info.storageError;
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
    const last = addDays(first, VIEW_DAYS[state.view] - 1);
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
      if (event.people.every((id) => state.hidden.has(id))) continue;
      if (event.allDay) {
        for (const day of allDayDays(event)) allDay.get(day)?.push(event);
      } else {
        for (const piece of pieces(event)) timed.get(piece.day)?.push(piece);
      }
    }
    renderNotices(data.errors.map((e) =>
      (e.person ? `${personById(e.person).name}: ` : "") + e.message + (e.stale ? " (מוצגת הגרסה האחרונה שנטענה)" : "")));
    $("updated").textContent = "עודכן " + timeOf(data.generatedAt);
  }

  const scroller = $("scroller");
  scroller.style.setProperty("--days", days.length);
  scroller.classList.toggle("day-view", isDay);
  for (const view of VIEWS) scroller.classList.toggle("view-" + view, view === state.view);

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
    col.addEventListener("click", (e) => {
      if (e.target !== col) return; // a tap on an event opens the event instead
      const halfHours = Math.floor((e.offsetY / hourPx) * 2);
      openEditor(null, { day, start: clock(Math.min(halfHours * 30, 23 * 60)) });
    });
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

/** The edge strip of an event: one color band per person in it. */
function stripe(people) {
  if (people.length === 1) return people[0].color;
  const band = 100 / people.length;
  return `linear-gradient(to bottom, ${people.map((p, i) => `${p.color} ${i * band}% ${(i + 1) * band}%`).join(", ")})`;
}

function eventBox(event, detailed) {
  const people = event.people.map(personById);
  const names = people.map((p) => p.name).join(", ");
  const who = detailed && event.location ? `${names} · ${event.location}` : names;
  const box = el("button", {
    type: "button",
    className: "event",
    "--c": people[0].color,
    "--stripe": stripe(people),
  }, [
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
  const dialog = $("details");
  $("details-person").replaceChildren(...event.people.map(personById).flatMap((p) => [
    el("span", { className: "dot", "--c": p.color }),
    el("span", { className: "name" }, p.name),
  ]));
  $("details-title").textContent = event.title;
  // Only events added in the app can change; the rest belong to Outlook, Google or family.yaml.
  $("details-actions").hidden = !event.editable;
  $("details-edit").onclick = () => {
    dialog.close();
    openEditor(event);
  };
  $("details-delete").onclick = () => deleteEvent(event);
  $("details-when").textContent = whenText(event);
  $("details-where").textContent = event.location ? "📍 " + event.location : "";
  dialog.showModal();
}

/** "יום רביעי, 28.10 · 18:30–19:30", or "· כל היום" for an all-day event. */
function whenText(event) {
  const dateFmt = new Intl.DateTimeFormat("he-IL", { weekday: "long", day: "numeric", month: "numeric", timeZone: "UTC" });
  if (event.allDay) {
    const last = addDays(event.end, -1);
    return last > event.start
      ? `${dateFmt.format(utcDate(event.start))} – ${dateFmt.format(utcDate(last))} · כל היום`
      : `${dateFmt.format(utcDate(event.start))} · כל היום`;
  }
  // Isolated left-to-right, or Hebrew text around it shows 10:00–09:00.
  return `${dateFmt.format(utcDate(zoned(event.start).day))} · \u2066${timeOf(event.start)}–${timeOf(event.end)}\u2069`;
}

let toastTimer;
function toast(message, ms = 4000) {
  $("toast").textContent = message;
  $("toast").hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => ($("toast").hidden = true), ms);
}

/** A tap outside the card closes it; a tap on the card's own padding does not. */
function closeOnOutsideTap(dialog) {
  dialog.addEventListener("click", (event) => {
    if (event.target !== dialog) return;
    const r = dialog.getBoundingClientRect();
    const inside = event.clientX >= r.left && event.clientX <= r.right && event.clientY >= r.top && event.clientY <= r.bottom;
    if (!inside) dialog.close();
  });
}
closeOnOutsideTap($("details"));

// ---- Adding and editing events --------------------------------------------

let editing = null; // the event being edited; null when adding a new one
let chosen = new Set(); // ids of the people in it
let endTouched = false; // once the end time is set by hand, it no longer follows the start

function clock(minutes) {
  return `${String(Math.floor(minutes / 60)).padStart(2, "0")}:${String(minutes % 60).padStart(2, "0")}`;
}

function minutesOf(value) {
  const [h, m] = value.split(":").map(Number);
  return h * 60 + m;
}

function hourLater(start) {
  return clock(Math.min(minutesOf(start) + 60, 23 * 60 + 59));
}

/** The day a new event starts on: the day shown, or today if it's on screen. */
function defaultDay() {
  const days = visibleDays();
  const today = todayInZone();
  return days.includes(today) ? today : days[0];
}

function openEditor(event, preset = {}) {
  editing = event || null;
  endTouched = !!event;
  $("editor-heading").textContent = event ? "עריכת אירוע" : "אירוע חדש";
  $("ed-title").value = event ? event.title : "";
  $("ed-date").value = event ? (event.allDay ? event.start : zoned(event.start).day) : preset.day || defaultDay();
  $("ed-allday").checked = !!event?.allDay;
  const start = event && !event.allDay ? timeOf(event.start) : preset.start || "17:00";
  $("ed-start").value = start;
  $("ed-end").value = event && !event.allDay ? timeOf(event.end) : hourLater(start);
  $("ed-location").value = event?.location || "";
  chosen = new Set(event ? event.people : []);
  renderChosen();
  $("ed-times").hidden = $("ed-allday").checked;
  $("ed-warning").hidden = state.storage === "durable";
  $("ed-warning").textContent = state.storage === "unavailable"
    ? `אין חיבור למסד הנתונים, אז ייתכן שהשמירה לא תצליח (${state.storageError}). בדקו את DATABASE_URL ב-Render.`
    : "האפליקציה עוד לא מחוברת למסד נתונים, ולכן אירועים שמוסיפים יימחקו כשהשרת יופעל מחדש. ההוראות לחיבור נמצאות ב-README.";
  $("ed-error").textContent = "";
  $("editor").showModal();
  if (!event) $("ed-title").focus();
}

function renderChosen() {
  const everyone = state.people.every((p) => chosen.has(p.id));
  const all = el("button", { type: "button", className: "chip everyone", "aria-pressed": String(everyone) }, "כולם");
  all.addEventListener("click", () => {
    chosen = everyone ? new Set() : new Set(state.people.map((p) => p.id));
    renderChosen();
  });
  const chips = state.people.map((person) => {
    const chip = el("button", {
      type: "button",
      className: "chip",
      "--c": person.color,
      "aria-pressed": String(chosen.has(person.id)),
    }, person.name);
    chip.addEventListener("click", () => {
      if (chosen.has(person.id)) chosen.delete(person.id);
      else chosen.add(person.id);
      renderChosen();
    });
    return chip;
  });
  $("ed-people").replaceChildren(...chips, all);
}

$("add").addEventListener("click", () => openEditor(null));
$("editor-form").addEventListener("input", () => ($("ed-error").textContent = ""));
$("ed-people").addEventListener("click", () => ($("ed-error").textContent = ""));
$("ed-cancel").addEventListener("click", () => $("editor").close());
$("ed-allday").addEventListener("change", () => ($("ed-times").hidden = $("ed-allday").checked));
$("ed-end").addEventListener("input", () => (endTouched = true));
$("ed-start").addEventListener("input", () => {
  const start = $("ed-start").value;
  if (start && (!endTouched || minutesOf($("ed-end").value || "00:00") <= minutesOf(start))) {
    $("ed-end").value = hourLater(start);
  }
});

$("editor-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const date = $("ed-date").value;
  const body = {
    title: $("ed-title").value,
    people: state.people.map((p) => p.id).filter((id) => chosen.has(id)),
    date,
    allDay: $("ed-allday").checked,
    start: $("ed-start").value || null,
    end: $("ed-end").value || null,
    location: $("ed-location").value,
  };
  // Say what's missing here rather than after a round trip.
  const missing = !body.title.trim() ? "צריך לכתוב מה האירוע"
    : !body.people.length ? "צריך לבחור מי מהמשפחה"
    : !date ? "צריך לבחור תאריך" : "";
  if (missing) {
    $("ed-error").textContent = missing;
    return;
  }
  const save = $("editor-form").querySelector("[type=submit]");
  save.disabled = true;
  try {
    const saved = await api(editing ? `/api/events/${editing.id}` : "/api/events", {
      method: editing ? "PUT" : "POST",
      body: JSON.stringify(body),
    });
    $("editor").close();
    // Show the day the event is on. Friday and Saturday aren't in the work week,
    // so an event on them switches to the full week rather than staying hidden.
    if (state.view === "workweek" && utcDate(date).getUTCDay() >= 5) {
      state.view = "week";
      store("view", "week");
    }
    if (state.view === "day" || !visibleDays().includes(date)) state.anchor = date;
    render();
    await load();
    const shown = state.weeks.get(sundayOf(date))?.events.some((e) => e.id === saved.id);
    toast(shown
      ? `נשמר: ${saved.title} · ${whenText(saved)}`
      : "האירוע נשמר, אבל השרת לא החזיר אותו ביומן. שלחו צילום מסך של המסך הזה.", shown ? 4000 : 10000);
  } catch (error) {
    if (error.message !== "unauthenticated") $("ed-error").textContent = error.message;
  } finally {
    save.disabled = false;
  }
});

async function deleteEvent(event) {
  if (!confirm(`למחוק את "${event.title}"?`)) return;
  try {
    await api(`/api/events/${event.id}`, { method: "DELETE" });
    $("details").close();
    await load();
  } catch (error) {
    if (error.message !== "unauthenticated") alert(error.message);
  }
}

closeOnOutsideTap($("editor"));

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
