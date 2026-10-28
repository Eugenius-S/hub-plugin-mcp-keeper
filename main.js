// MCP logins: the plugin process writes status.json next to this file every pass; the channel gets a human line.
const NAME = "mcp-keeper";
const STR = {
  en: {
    title: "MCP logins", subtitle: "MCP servers of the hub, checked every {n} min",
    server: "Server", state: "State", ok_at: "Last active", checked: "Checked {when}",
    none: "No pass yet: the first one goes a minute after the start, after a calm network.",
    ok: "active", lost: "lost", first: "never logged in", never: "never",
    skip: "not checked", by: "Held by",
    login: "Log in", relogin: "Log in again", login_wait: "login… opened in the browser",
    login_done: "logged in, checking", login_fail: "login did not go through", login_sent: "asking the hub…",
    reconnect: "Reconnect", reconnect_sent: "reconnecting…", reconnect_done: "reconnected",
    reconnect_fail: "did not come back", auto: "Reconnect automatically",
    auto_hint: "a lost server reconnects in the running sessions by itself, at most once in 10 min",
    switch: "Connect another account", switch_sent: "opening a terminal…",
    switch_done: "terminal opened: sign in with the other account there", switch_fail: "the terminal did not open",
    ignored: "Not followed",
  },
  ru: {
    title: "Входы MCP", subtitle: "MCP-серверы хаба, проверка раз в {n} мин",
    server: "Сервер", state: "Состояние", ok_at: "Последний раз активен", checked: "Проверено {when}",
    none: "Прохода ещё не было: первый — через минуту после старта, при спокойной сети.",
    ok: "активен", lost: "отвалился", first: "ещё не входили", never: "никогда",
    skip: "не проверяется", by: "Кто держит",
    login: "Войти", relogin: "Войти заново", login_wait: "вход… открыт в браузере",
    login_done: "вошли, проверяю", login_fail: "вход не прошёл", login_sent: "прошу хаб…",
    reconnect: "Переподключить", reconnect_sent: "переподключаю…", reconnect_done: "переподключено",
    reconnect_fail: "не ожил", auto: "Автопереподключение",
    auto_hint: "отвалившийся сервер сам переподключается в работающих сессиях, не чаще раза в 10 мин",
    switch: "Подключить другой", switch_sent: "открываю терминал…",
    switch_done: "терминал открыт: войди там под другим аккаунтом", switch_fail: "терминал не открылся",
    ignored: "Не следим",
  },
};
const ORDER = { lost: 0, first: 1, ok: 3, skip: 4 };
const COLOR = { lost: "#e5484d", first: "#f5a524", ok: "#30a46c", skip: "#8b8d98" };

let hub;
let last = { id: 0, at: 0, data: null };
const sent = {};   // server -> {at, act}: the row says what was asked until the process answers
const SENT_FOR = 20000;

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
  return rows.some(([n, r]) => (r.login && ["wait", "sent"].includes(r.login.state)) || mine(n));
}

async function load(snap, force = false) {
  const top = Number((snap && snap.feeds && snap.feeds[NAME]) || last.id);
  if (!force && last.data && top === last.id && Date.now() - last.at < (waiting() ? 4000 : 60000)) return false;
  const r = await fetch(new URL("status.json", import.meta.url), { cache: "no-store" });
  const data = r.ok ? await r.json() : null;
  const changed = JSON.stringify(data) !== JSON.stringify(last.data);
  last = { id: top, at: Date.now(), data: data && typeof data.servers === "object" ? data : null };
  return changed;
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

// The actions of a row: "Connect another account" for an active server with a login of its own (the process opens a
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
  if (s.can_login && s.state !== "ok") bar.append(button("mk-login", s.state === "lost" ? "relogin" : "login", "login_sent", name, "login", td));
  if (bar.firstChild) td.append(bar);
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
  const acts = (s) => s.can_switch || (s.can_login && s.state !== "ok") || s.login || (s.state !== "skip" && s.state !== "ok");
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
  wrap.append(autoBox(d));
  root.append(wrap);
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
