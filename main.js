// MCP logins: the plugin process writes status.json next to this file every pass; the channel gets a human line.
const NAME = "mcp-keeper";
const STR = {
  en: {
    title: "MCP logins", subtitle: "MCP servers of the hub, checked every {n} min",
    server: "Server", state: "State", ok_at: "Last live", checked: "Checked {when}",
    none: "No pass yet: the first one goes a minute after the start, after a calm network.",
    ok: "live", lost: "login lost", later: "check later", first: "never logged in", never: "never",
    skip: "not checked", by: "Held by",
  },
  ru: {
    title: "Входы MCP", subtitle: "MCP-серверы хаба, проверка раз в {n} мин",
    server: "Сервер", state: "Состояние", ok_at: "Последний раз жив", checked: "Проверено {when}",
    none: "Прохода ещё не было: первый — через минуту после старта, при спокойной сети.",
    ok: "жив", lost: "слетел вход", later: "проверить позже", first: "ещё не входили", never: "никогда",
    skip: "не проверяется", by: "Кто держит",
  },
};
const ORDER = { lost: 0, first: 1, later: 2, ok: 3, skip: 4 };
const COLOR = { lost: "#e5484d", first: "#f5a524", later: "#8b8d98", ok: "#30a46c", skip: "#8b8d98" };

let hub;
let last = { id: 0, at: 0, data: null };

function tr(key, params = {}) {
  const table = Object.hasOwn(STR, hub.lang) ? STR[hub.lang] : STR.en;
  const text = Object.hasOwn(table, key) ? table[key] : STR.en[key];
  return text.replace(/\{(\w+)\}/g, (_, k) => String(params[k] ?? ""));
}

function when(sec) {
  if (!sec) return tr("never");
  return new Date(sec * 1000).toLocaleString(hub.lang, { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" });
}

async function load(snap) {
  const top = Number((snap && snap.feeds && snap.feeds[NAME]) || 0);
  if (last.data && top === last.id && Date.now() - last.at < 60000) return false;
  const r = await fetch(new URL("status.json", import.meta.url), { cache: "no-store" });
  const data = r.ok ? await r.json() : null;
  const changed = JSON.stringify(data) !== JSON.stringify(last.data);
  last = { id: top, at: Date.now(), data: data && typeof data.servers === "object" ? data : null };
  return changed;
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
  const by = Object.values(d.servers).some((s) => s.by);
  for (const k of by ? ["server", "state", "by", "ok_at"] : ["server", "state", "ok_at"]) {
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
    if (by) cell(s.by || "", "font-family:ui-monospace,monospace;opacity:.8");
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
