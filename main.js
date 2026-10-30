// MCP logins: the plugin process writes status.json next to this file every pass; the channel gets a human line.
const NAME = "mcp-keeper";
const STR = {
  en: {
    title: "MCP logins", subtitle: "MCP servers of the hub, checked every {n} min",
    server: "Server", state: "State", ok_at: "Last connected", checked: "Checked {when}",
    none: "No pass yet: the first one goes a minute after the start, after a calm network.",
    ok: "connected", lost: "lost", first: "never logged in", never: "never",
    skip: "not checked", by: "Held by",
    login: "Log in", relogin: "Log in again", login_wait: "login… opened in the browser",
    login_done: "logged in, checking", login_fail: "login did not go through", login_sent: "asking the hub…",
    reconnect: "Reconnect", reconnect_sent: "reconnecting…", reconnect_done: "reconnected",
    reconnect_fail: "did not come back", auto: "Reconnect automatically",
    auto_hint: "a lost server reconnects in the running sessions by itself, at most once in 10 min",
    switch: "Another login", no_account: "no account", switch_sent: "opening a terminal…",
    switch_done: "terminal opened: follow the steps in it", switch_fail: "the terminal did not open",
    ignored: "Not followed", core_login: "waits for a login: the button is in the hub's strip",
    add: "Add MCP", add_name: "Name", add_url: "Address, https://…", add_go: "Add", add_cancel: "Cancel",
    add_hint: "The service gives the name and the address. A key is not typed here: you log in in the browser.",
    add_bad: "Check the name (letters, digits, - and _) and the address (https, no key or parameters in it)",
    add_sent: "adding…", add_ok: "Added: {name}", add_fail: "Not added", add_restart: "Sessions get it after they restart.",
  },
  ru: {
    title: "Входы MCP", subtitle: "MCP-серверы хаба, проверка раз в {n} мин",
    server: "Сервер", state: "Состояние", ok_at: "Последний раз подключён", checked: "Проверено {when}",
    none: "Проверки ещё не было: первая пройдёт через минуту после старта, при спокойной сети.",
    ok: "подключён", lost: "отвалился", first: "ещё не входили", never: "никогда",
    skip: "не проверяется", by: "Кто держит",
    login: "Войти", relogin: "Войти заново", login_wait: "вход… открыт в браузере",
    login_done: "вошли, проверяю", login_fail: "вход не прошёл", login_sent: "прошу хаб…",
    reconnect: "Переподключить", reconnect_sent: "переподключаю…", reconnect_done: "переподключено",
    reconnect_fail: "не ожил", auto: "Автопереподключение",
    auto_hint: "отвалившийся сервер сам переподключается в работающих сессиях, не чаще раза в 10 мин",
    switch: "Другой логин", no_account: "без аккаунта", switch_sent: "открываю терминал…",
    switch_done: "терминал открыт: дальше по шагам в нём", switch_fail: "терминал не открылся",
    ignored: "Не следим", core_login: "ждёт входа — кнопка в полосе хаба",
    add: "Добавить MCP", add_name: "Название", add_url: "Адрес, https://…", add_go: "Добавить", add_cancel: "Отмена",
    add_hint: "Название и адрес даёт сам сервис. Ключ тут не вводится: войдёшь в браузере.",
    add_bad: "Проверь название (буквы, цифры, - и _) и адрес (https, без ключа и параметров)",
    add_sent: "добавляю…", add_ok: "Добавлено: {name}", add_fail: "Не добавлено", add_restart: "Сессии получат его после перезапуска.",
  },
};
const ORDER = { lost: 0, first: 1, ok: 3, skip: 4 };
const COLOR = { lost: "#e5484d", first: "#f5a524", ok: "#30a46c", skip: "#8b8d98" };

let hub;
let last = { id: 0, at: 0, data: null, snap: null };
const sent = {};   // server -> {at, act}: the row says what was asked until the process answers
const SENT_FOR = 20000;
const form = { open: false, name: "", url: "", bad: false, at: 0, focus: "" };   // "Add MCP": what is typed survives a redraw
const NAME_OK = /^[A-Za-z0-9][A-Za-z0-9_-]{0,39}$/;
const URL_OK = /^(https:\/\/[A-Za-z0-9.-]{1,200}(:\d{1,5})?|http:\/\/(localhost|127\.0\.0\.1|\[::1\])(:\d{1,5})?)(\/[A-Za-z0-9._~%+/-]{0,200})?$/;   // like the process: no login, query or key

function tr(key, params = {}) {
  const table = Object.hasOwn(STR, hub.lang) ? STR[hub.lang] : STR.en;
  const text = Object.hasOwn(table, key) ? table[key] : STR.en[key];
  return text.replace(/\{(\w+)\}/g, (_, k) => String(params[k] ?? ""));
}

function when(sec) {
  if (!sec) return tr("never");
  return new Date(sec * 1000).toLocaleString(hub.lang, { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" });
}

// a login is open: the process writes its end to status.json, the view looks more often until then
function waiting() {
  const rows = Object.entries((last.data && last.data.servers) || {});
  const add = (last.data && last.data.add) || {};
  return rows.some(([n, r]) => (r.login && ["wait", "sent"].includes(r.login.state)) || mine(n)) || adding() ||
    (add.login && add.login.state === "wait");
}

// the click on "Add" is waiting for the process: its answer (data.add) is newer than the click
function adding() {
  const add = (last.data && last.data.add) || {};
  return !!form.at && Date.now() - form.at < 180000 && !(add.at && add.at * 1000 >= form.at - 1000);
}

async function load(snap, force = false) {
  const before = [...(coreWaits(last.snap) || [])].sort().join("\n");
  if (snap) last.snap = snap;
  const moved = before !== [...(coreWaits(last.snap) || [])].sort().join("\n");
  const top = Number((snap && snap.feeds && snap.feeds[NAME]) || last.id);
  if (!force && last.data && top === last.id && Date.now() - last.at < (waiting() ? 4000 : 60000)) return moved;
  const r = await fetch(new URL("status.json", import.meta.url), { cache: "no-store" });
  const data = r.ok ? await r.json() : null;
  const changed = moved || JSON.stringify(data) !== JSON.stringify(last.data);
  last = { id: top, at: Date.now(), data: data && typeof data.servers === "object" ? data : null, snap: last.snap };
  return changed;
}

// A core that has its own "MCP waits for a login" strip (Plugin API 1.31) puts mcp_waits in the snapshot; an older one
// has no such key, and the view works as before. A server in that list is signed in through the strip: no "Log in" here.
function coreWaits(snap) {
  return snap && Array.isArray(snap.mcp_waits) ? new Set(snap.mcp_waits.map((w) => w && w.server)) : null;
}

function coreWaiting(name, s) {
  const w = coreWaits(last.snap);
  return w ? w.has(name) : !!s.core_wait;
}

function mine(name, act) {
  const c = sent[name];
  return !!c && Date.now() - c.at < SENT_FOR && (!act || c.act === act);
}

function button(cls, label, text, name, act, td) {
  const btn = hub.el("button", "btn " + cls, tr(label));
  btn.type = "button";
  btn.onclick = () => {
    sent[name] = { at: Date.now(), act };
    btn.disabled = true;
    hub.postFeed(act + " " + name);   // the process reads the channel and does what the hub command would
    td.replaceChildren(hub.el("span", "", tr(text)));
    setTimeout(() => load(null, true).then(() => draw(root_)).catch(() => {}), 6000);
  };
  return btn;
}

// The actions of a row: "Another login" for a connected server with a login of its own (the process opens a
// terminal where the login is changed), "Reconnect" (the server reconnects in every running session, no login) for a lost
// one, "Log in" / "Log in again" (the process runs `hub mcp login`) where a login can help; or what is going on now.
function actionCell(row, name, s) {
  const td = hub.el("td");
  td.style.cssText = "padding:6px 8px;border-top:1px solid rgba(127,127,127,.2);white-space:nowrap";
  row.append(td);
  const l = s.login || {};
  const re = l.act === "reconnect";
  const sw = l.act === "switch";
  const busy = (re && l.state === "sent") || (!re && !sw && l.state === "wait");
  const asked = sent[name] && mine(name) && !(l.at && l.at * 1000 >= sent[name].at && l.state !== "sent" && l.state !== "wait");
  if (busy || asked) {
    const act = asked ? sent[name].act : re ? "reconnect" : "login";
    td.textContent = tr(act === "reconnect" ? "reconnect_sent" : act === "switch" ? "switch_sent" : l.state === "wait" ? "login_wait" : "login_sent");
    td.style.opacity = ".8";
    return;
  }
  if (l.state === "done" && !re && !sw && !s.can_login) {
    td.textContent = tr("login_done");
    return;
  }
  if (l.state === "fail" || (l.state === "done" && (re || sw))) {
    const ok = l.state === "done";
    const key = re ? (ok ? "reconnect_done" : "reconnect_fail") : sw ? (ok ? "switch_done" : "switch_fail") : "login_fail";
    const msg = hub.el("div", ok ? "mk-done" : "mk-fail", tr(key) + (l.why && !ok ? ": " + l.why : ""));
    msg.style.cssText = "color:" + (ok ? COLOR.ok : COLOR.lost) + ";font-size:.9em;margin-bottom:4px;white-space:normal";
    td.append(msg);
  }
  const bar = hub.el("div", "mk-acts");
  bar.style.cssText = "display:flex;gap:6px;flex-wrap:wrap";
  if (s.can_switch) bar.append(button("mk-switch", "switch", "switch_sent", name, "switch", td));
  else if (s.state !== "skip" && s.state !== "ok") bar.append(button("mk-reconnect", "reconnect", "reconnect_sent", name, "reconnect", td));
  const core = s.state !== "ok" && s.state !== "skip" && coreWaiting(name, s);   // the hub's strip has this login
  if (s.can_login && s.state !== "ok" && !core) bar.append(button("mk-login", s.state === "lost" ? "relogin" : "login", "login_sent", name, "login", td));
  if (core) {
    const hint = hub.el("span", "mk-core", tr("core_login"));
    hint.style.cssText = "opacity:.7;font-size:.9em;align-self:center";
    bar.append(hint);
  }
  if (bar.firstChild) td.append(bar);
  else if (s.no_account) {
    const grey = hub.el("span", "mk-noacc", tr("no_account"));
    grey.style.cssText = "opacity:.55";
    td.append(grey);
  }
}

// "Add MCP" under the table: a name and an address (https). The click posts "add <name> <address>"; the process adds the
// server the hub's own way, opens the login, and says how it went in data.add.
function addBox(d) {
  const wrap = hub.el("div", "mk-add");
  wrap.style.cssText = "margin-top:14px";
  const add = d.add || null;
  if (adding()) {
    wrap.append(hub.el("p", "mk-add-sent", tr("add_sent")));
    return wrap;
  }
  if (add) {
    const ok = add.state === "done";
    const line = hub.el("div", ok ? "mk-done" : "mk-fail", ok ? tr("add_ok", { name: add.name }) : tr("add_fail") + (add.why ? ": " + add.why : ""));
    line.style.cssText = "color:" + (ok ? COLOR.ok : COLOR.lost) + ";font-size:.9em;margin-bottom:6px;white-space:normal";
    wrap.append(line);
    if (ok) {
      const l = add.login || {};
      const text = l.state === "wait" ? tr("login_wait") : l.state === "fail" ? tr("login_fail") + (l.why ? ": " + l.why : "") : tr("add_restart");
      const note = hub.el("div", "mk-add-login", text);
      note.style.cssText = "font-size:.9em;opacity:.8;margin-bottom:6px;white-space:normal";
      wrap.append(note);
    }
  }
  if (!form.open) {
    const open = hub.el("button", "btn mk-add-open", tr("add"));
    open.type = "button";
    open.onclick = () => { form.open = true; form.bad = false; draw(root_); };
    wrap.append(open);
    return wrap;
  }
  const row = hub.el("div", "mk-add-form");
  row.style.cssText = "display:flex;gap:6px;flex-wrap:wrap;align-items:center";
  const field = (key, ph, width) => {
    const input = hub.el("input", "mk-add-" + key);
    input.type = "text";
    input.placeholder = tr(ph);
    input.value = form[key];
    input.style.cssText = "padding:6px 8px;width:" + width;
    input.oninput = () => { form[key] = input.value; form.bad = false; };
    input.onfocus = () => { form.focus = key; };
    return input;
  };
  const go = hub.el("button", "btn mk-add-go", tr("add_go"));
  go.type = "button";
  go.onclick = () => {
    const name = form.name.trim();
    const url = form.url.trim();
    if (!NAME_OK.test(name) || !URL_OK.test(url)) { form.bad = true; draw(root_); return; }
    form.at = Date.now();
    form.open = false;
    form.name = form.url = "";
    hub.postFeed("add " + name + " " + url);   // the process reads the channel and does what the hub commands would
    setTimeout(() => load(null, true).then(() => draw(root_)).catch(() => {}), 6000);
    draw(root_);
  };
  const cancel = hub.el("button", "btn mk-add-cancel", tr("add_cancel"));
  cancel.type = "button";
  cancel.onclick = () => { form.open = false; form.bad = false; draw(root_); };
  row.append(field("name", "add_name", "10em"), field("url", "add_url", "20em"), go, cancel);
  wrap.append(row);
  const hint = hub.el("p", form.bad ? "mk-add-bad" : "mk-hint", tr(form.bad ? "add_bad" : "add_hint"));
  hint.style.cssText = "margin:4px 0 0;font-size:.9em;opacity:" + (form.bad ? "1" : ".7") + (form.bad ? ";color:" + COLOR.lost : "");
  wrap.append(hint);
  return wrap;
}

function autoBox(d) {
  const label = hub.el("label", "mk-auto");
  label.style.cssText = "display:flex;gap:8px;align-items:center;margin-top:14px;cursor:pointer";
  const box = hub.el("input");
  box.type = "checkbox";
  box.checked = d.auto !== false;
  box.onchange = () => {
    d.auto = box.checked;
    hub.postFeed(box.checked ? "auto on" : "auto off");   // the process keeps the choice in its state
  };
  label.append(box, hub.el("span", "", tr("auto")));
  const hint = hub.el("p", "mk-hint", tr("auto_hint"));
  hint.style.cssText = "margin:4px 0 0 26px;opacity:.7;font-size:.9em";
  const wrap = hub.el("div");
  wrap.append(label, hint);
  return wrap;
}

let root_ = null;

function draw(root) {
  root_ = root;
  const d = last.data;
  root.replaceChildren();
  const wrap = hub.el("div", "mk");
  wrap.style.cssText = "padding:16px;max-width:760px";
  if (!d) {
    wrap.append(hub.el("p", "mk-none", tr("none")));
    root.append(wrap);
    return;
  }
  wrap.append(hub.el("p", "mk-sub", tr("subtitle", { n: d.every || 15 }) + " · " + tr("checked", { when: when(d.at) })));
  const table = hub.el("table", "mk-table");
  table.style.cssText = "border-collapse:collapse;width:100%";
  const head = hub.el("tr");
  const shown = Object.entries(d.servers).filter(([, s]) => !s.ignored);
  const quiet = Object.entries(d.servers).filter(([, s]) => s.ignored).map(([n, s]) => s.title || n);
  const by = shown.some(([, s]) => s.by);
  for (const k of by ? ["server", "state", "by", "ok_at"] : ["server", "state", "ok_at"]) {
    const th = hub.el("th", "", tr(k));
    th.style.cssText = "text-align:left;padding:6px 8px;opacity:.7;font-weight:500";
    head.append(th);
  }
  const acts = (s) => s.can_switch || s.no_account || (s.can_login && s.state !== "ok") || s.login || (s.state !== "skip" && s.state !== "ok");
  const withActs = shown.some(([, s]) => acts(s));
  if (withActs) head.append(hub.el("th"));
  table.append(head);
  const rows = shown.sort((a, b) => (ORDER[a[1].state] ?? 9) - (ORDER[b[1].state] ?? 9) || a[0].localeCompare(b[0]));
  for (const [name, s] of rows) {
    const tr_ = hub.el("tr");
    const cell = (text, css = "") => {
      const td = hub.el("td", "", text);
      td.style.cssText = "padding:6px 8px;border-top:1px solid rgba(127,127,127,.2);" + css;
      tr_.append(td);
      return td;
    };
    cell(s.title || name).title = name;
    const st = cell((Object.hasOwn(STR.en, s.state) ? tr(s.state) : s.state) + (s.why ? ": " + s.why : ""));
    st.style.color = COLOR[s.state] || "";
    if (by) cell(s.by || "", "font-family:ui-monospace,monospace;opacity:.8");
    cell(s.ok_at || s.state !== "skip" ? when(s.ok_at) : "");
    if (acts(s)) actionCell(tr_, name, s);
    else if (withActs) cell("");
    table.append(tr_);
  }
  wrap.append(table);
  if (quiet.length) {
    const note = hub.el("p", "mk-ignored", tr("ignored") + ": " + quiet.join(", "));
    note.style.cssText = "margin:10px 0 0;opacity:.6;font-size:.9em";
    wrap.append(note);
  }
  wrap.append(addBox(d));
  wrap.append(autoBox(d));
  root.append(wrap);
  const typed = form.open && form.focus ? wrap.querySelector(".mk-add-" + form.focus) : null;
  if (typed) typed.focus();   // a redraw (the status changed) does not take the cursor away
}

export default function register(h) {
  hub = h;
  hub.addView({
    id: NAME,
    title: tr("title"),
    icon: "icon.svg",
    render(root, snap) {
      if (!root.firstChild) draw(root);
      load(snap).then((changed) => changed && draw(root)).catch(() => {});
    },
  });
}
