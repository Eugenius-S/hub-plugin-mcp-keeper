// MCP logins: the last status the plugin process posted to its channel (a summary line + JSON).
const NAME = "mcp-keeper";
const STR = {
  en: {
    title: "MCP logins", subtitle: "every MCP server, called round every {n} min",
    server: "Server", state: "State", ok_at: "Last live", checked: "Checked {when}",
    none: "No pass yet: the first one goes a minute after the start, after a calm network.",
    ok: "live", lost: "login lost", first: "never logged in", later: "check later", failed: "does not connect",
    idle: "plugin, not logged in", never: "never",
  },
  ru: {
    title: "Входы MCP", subtitle: "все MCP, прозвон раз в {n} мин",
    server: "Сервер", state: "Состояние", ok_at: "Последний раз жив", checked: "Проверено {when}",
    none: "Прохода ещё не было: первый — через минуту после старта, при спокойной сети.",
    ok: "жив", lost: "слетел вход", first: "ни разу не входили", later: "проверить позже", failed: "не подключается",
    idle: "плагин, вход не нужен был", never: "никогда",
  },
};
const ORDER = { lost: 0, first: 1, failed: 2, later: 3, ok: 4, idle: 5 };
const COLOR = { lost: "#e5484d", first: "#f5a524", failed: "#f5a524", later: "#8b8d98", ok: "#30a46c", idle: "#8b8d98" };

let hub;
let last = { id: 0, data: null };

function tr(key, params = {}) {
  const table = Object.hasOwn(STR, hub.lang) ? STR[hub.lang] : STR.en;
  const text = Object.hasOwn(table, key) ? table[key] : STR.en[key];
  return text.replace(/\{(\w+)\}/g, (_, k) => String(params[k] ?? ""));
}

export function parse(text) {
  const nl = String(text || "").indexOf("\n");
  if (nl < 0) return null;
  try {
    const data = JSON.parse(text.slice(nl + 1));
    return data && typeof data.servers === "object" ? data : null;
  } catch {
    return null;
  }
}

function when(sec) {
  if (!sec) return tr("never");
  return new Date(sec * 1000).toLocaleString(hub.lang, { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" });
}

async function load(snap) {
  const top = Number((snap && snap.feeds && snap.feeds[NAME]) || 0);
  if (top && top === last.id) return false;
  const posts = await hub.feed(0);
  for (let i = posts.length - 1; i >= 0; i--) {
    const data = parse(posts[i].text);
    if (data) {
      last = { id: top || Number(String(posts[i].id).slice(1)), data };
      return true;
    }
  }
  return false;
}

function draw(root) {
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
  for (const k of ["server", "state", "ok_at"]) {
    const th = hub.el("th", "", tr(k));
    th.style.cssText = "text-align:left;padding:6px 8px;opacity:.7;font-weight:500";
    head.append(th);
  }
  table.append(head);
  const rows = Object.entries(d.servers).sort((a, b) => (ORDER[a[1].state] ?? 9) - (ORDER[b[1].state] ?? 9) || a[0].localeCompare(b[0]));
  for (const [name, s] of rows) {
    const tr_ = hub.el("tr");
    const cell = (text, css = "") => {
      const td = hub.el("td", "", text);
      td.style.cssText = "padding:6px 8px;border-top:1px solid rgba(127,127,127,.2);" + css;
      tr_.append(td);
      return td;
    };
    cell(name, "font-family:ui-monospace,monospace");
    const st = cell((Object.hasOwn(STR.en, s.state) ? tr(s.state) : s.state) + (s.why ? ": " + s.why : ""));
    st.style.color = COLOR[s.state] || "";
    cell(when(s.ok_at));
    table.append(tr_);
  }
  wrap.append(table);
  root.append(wrap);
}

export default function register(h) {
  hub = h;
  hub.addView({
    id: NAME,
    title: tr("title"),
    icon: "icon.png",
    render(root, snap) {
      if (!root.firstChild) draw(root);
      load(snap).then((changed) => changed && draw(root)).catch(() => {});
    },
  });
}
