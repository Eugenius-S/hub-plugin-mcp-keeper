#!/usr/bin/env python3
"""mcp-keeper — the process of the plugin: calls round every MCP server of Claude Code and catches a lost login
before work stops on it.

Every `every_minutes` (15) one pass:
  1. `claude mcp list` — Claude Code checks every server (user, project, plugin:<plugin>:<server>) and refreshes an
     expired OAuth access under its own lock. Run detached and never killed: a one-time refresh token cut off in the
     middle of an exchange loses the login. A previous run still alive - this pass goes without it.
  2. "needs authentication" - checked again in RECHECK seconds (a single answer like that happens).
  3. A server that answers "connected" may let a client in without any login (some do): its own OAuth access from the
     Claude Code credentials (read only, never written) goes in an `initialize` request; 401/403 - the login is
     rejected. Servers that log in by headers (headersHelper / headers) are judged by the list only.
Lost is only an explicit refusal (needs authentication twice, 401/403 on initialize). A timeout, no network, 5xx -
"later", nothing to the owner. A pass runs only after COOLDOWN seconds of a live network without sleep (the wall clock
jumping away from the monotonic one); a new loss is told only if a second pass CONFIRM_AFTER seconds later sees it too.
A lost login is told again hourly while it stays lost; everything found in one pass goes in one message, the most
important first (one-time logins, servers a working session uses). It is lifted only by a live check. A stored login
of a live server that disappeared is told as erased; for a one-time login (its refresh token works once) the processes
of Claude Code and of the server at that moment are kept in the plugin state, to find who spent it. A user server with
the address of a plugin server hides the plugin's login - told in the same message.

Out: the fact ext.<name>.checked, owner.notify (urgent - a quiet Telegram summary would swallow it), a short message
to the sessions that are working with this server right now, and the status in the plugin's channel (for its view).
Secrets are never printed or written: only server names and states.

Settings - hub.json extensions[<plugin>]: every_minutes (15), warn_days (3), ignore ([names]), quiet_until (epoch or
"YYYY-MM-DDTHH:MM" in the hub timezone); hub.json mcp_auth_ignore is honoured too.
"""
import datetime, hashlib, json, os, re, shutil, socket, subprocess, sys, time, urllib.error, urllib.request

KEY = os.environ.get("HUB_EXTENSION") or "mcp-keeper"   # hub.json extensions key: <account>:mcp-keeper from the market
NAME = KEY.split(":")[-1]                                # channel and fact name
STATE = os.environ.get("HUB_STATE_DIR") or os.path.expanduser("~/.hub-core")
HOME = os.path.join(STATE, "mcp-keeper")
NOTE_ID = "mcp"
DAY = 24 * 3600
REPEAT = 3600             # a lost login is told again hourly while it stays lost
ONE_TIME = ("canva",)     # servers whose refresh token works once: a lost exchange loses the login
EVERY = 15                # minutes between passes
START_DELAY = 60          # first pass after a start: the hub restarts in batches
AGAIN_AFTER = 600         # the process was restarted - a pass not more often than this
CONFIRM_AFTER = 300       # a new loss - checked again after this many seconds
RECHECK = 30              # "needs authentication" - asked again after this many seconds
COOLDOWN = 300            # a pass only after this many seconds of live network without sleep
WAKE_JUMP = 60            # wall clock ran away from the monotonic one by more - the machine slept
NET_STEP = 30
MAX_WAIT = 1800
TRIES = 3
LIST_TIMEOUT = 300
NET_HOSTS = (("api.anthropic.com", 443), ("github.com", 443))
KEYCHAIN = "Claude Code-credentials"
UA = "hub-mcp-keeper (mcp-remote compatible)"   # Cloudflare answers 403 "1010" to Python-urllib

STR = {
    "en": {
        "title": "MCP logins",
        "lost_topic": "MCP login lost",
        "lost": "MCP login lost: {names} - log in:",
        "lost_line": "• {name} - {why}: {fix}",
        "note_topic": "MCP logins",
        "why_wiped": "the stored login was erased (the server was live with it)",
        "first": "MCP was never logged in: {names} - log in once, after that Claude Code refreshes it itself:",
        "first_line": "• {name}: {fix}",
        "dup": "MCP duplicate: {names} - the same server as a plugin's; Claude Code loads the user one and the plugin's "
               "login is never refreshed:",
        "dup_line": "• {name} hides {plugin}: claude mcp remove {name} -s user",
        "why_auth": "needs authentication",
        "why_rejected": "the server rejects the access (HTTP {code})",
        "why_expires": "the token without refresh expires {day}",
        "why_expired": "the token without refresh has expired",
        "to_session": "MCP {server}: the login is lost, the owner is told. Its tools fail until then - do not repeat "
                      "the calls in a loop; work without it or wait, the hub reconnects it after the login.",
        "status": "MCP: {ok} live, {bad} lost, {later} later",
        "view_title": "MCP logins",
        "view_sub": "every MCP server of Claude Code, called round every {n} min",
        "col_server": "Server", "col_state": "State", "col_ok": "Last live",
        "st_ok": "live", "st_lost": "login lost", "st_first": "never logged in", "st_later": "check later",
        "st_failed": "does not connect", "st_idle": "plugin, not logged in",
        "none": "No pass yet: the first one goes a minute after the start, after a calm network.",
        "checked": "Checked {when}",
    },
    "ru": {
        "title": "Входы MCP",
        "lost_topic": "Слетел вход MCP",
        "lost": "Слетел вход MCP: {names} — войди:",
        "lost_line": "• {name} — {why}: {fix}",
        "note_topic": "Входы MCP",
        "why_wiped": "запись входа стёрта (до этого сервер с ней работал)",
        "first": "В MCP ни разу не входили: {names} — войди один раз, дальше Claude Code освежает вход сам:",
        "first_line": "• {name}: {fix}",
        "dup": "Дубль MCP: {names} — тот же сервер, что у плагина; Claude Code берёт пользовательский, вход плагина "
               "не освежается:",
        "dup_line": "• {name} скрывает {plugin}: claude mcp remove {name} -s user",
        "why_auth": "ждёт входа",
        "why_rejected": "сервер отвергает доступ (HTTP {code})",
        "why_expires": "токен без refresh истекает {day}",
        "why_expired": "токен без refresh истёк",
        "to_session": "MCP {server}: слетел вход, владелец знает. До входа его инструменты не работают — не повторяй "
                      "вызовы по кругу; работай без него или подожди, после входа хаб переподключит сервер.",
        "status": "MCP: живы {ok}, слетели {bad}, позже {later}",
        "view_title": "Входы MCP",
        "view_sub": "все MCP Claude Code, прозвон раз в {n} мин",
        "col_server": "Сервер", "col_state": "Состояние", "col_ok": "Последний раз жив",
        "st_ok": "жив", "st_lost": "слетел вход", "st_first": "ни разу не входили", "st_later": "проверить позже",
        "st_failed": "не подключается", "st_idle": "плагин, вход не нужен был",
        "none": "Прохода ещё не было: первый — через минуту после старта, при спокойной сети.",
        "checked": "Проверено {when}",
    },
}


# ───── language, settings, state ─────

def lang():
    """Hub language at the moment of use (it may change while the process lives)."""
    try:
        from core import i18n
        return i18n.current()
    except Exception:   # noqa: BLE001 - no core on the path (tests) or a broken settings file
        return (os.environ.get("HUB_LANG") or "en").split("-")[0].lower()


def tr(key, **params):
    base = (lang() or "en").split("-")[0].lower()
    text = (STR.get(base) or STR["en"]).get(key) or STR["en"][key]
    return text.format(**params) if params else text


def config_path():
    if os.environ.get("HUB_CONFIG"):
        return os.environ["HUB_CONFIG"]
    try:
        from core import catalog
        return catalog.config_path(STATE)
    except Exception:   # noqa: BLE001
        return os.path.join(STATE, "hub.json")


def config():
    try:
        with open(config_path(), "rb") as f:
            raw = f.read()
        return json.loads(raw.decode("utf-8-sig")) or {}
    except (OSError, ValueError):
        return {}


def parse_quiet(value, tz):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        when = datetime.datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    if when.tzinfo is None and tz:
        try:
            import zoneinfo
            when = when.replace(tzinfo=zoneinfo.ZoneInfo(str(tz)))
        except Exception:   # noqa: BLE001 - no tz database (Windows without tzdata): local time
            pass
    return when.timestamp()


def settings():
    cfg = config()
    own = (cfg.get("extensions") or {}).get(KEY) or {}
    skip = cfg.get("mcp_auth_ignore")
    return {"every": max(1.0, float(own.get("every_minutes") or EVERY)) * 60,
            "warn_days": float(own.get("warn_days", 3)),
            "one_time": {short(x) for x in own.get("one_time", ONE_TIME)},
            "quiet_until": parse_quiet(own.get("quiet_until"), cfg.get("timezone")),
            "ignore": {str(x).lower() for x in list(own.get("ignore") or []) + list(skip if isinstance(skip, list) else [])}}


def read_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    for _ in range(5):   # Windows: os.replace over a file another process reads - PermissionError for a moment
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            time.sleep(0.2)
    os.replace(tmp, path)


def load_state():
    return read_json(os.path.join(HOME, "state.json")) or {}


def save_state(st):
    write_json(os.path.join(HOME, "state.json"), st)


def put(type, data, entity=None):
    from core import spool
    spool.put(os.path.join(STATE, "spool"), type, entity, data, src="ext:" + KEY)


def short(name):
    """plugin:canva:canva and canva are one service: ignore lists and 'one alarm a day' go by it."""
    return str(name).split(":")[-1].lower()


def ignored(name, ignore):
    return str(name).lower() in ignore or short(name) in ignore


# ───── commands ─────

def text_of(raw):
    try:
        from platforms import current
        return current.output_text(raw)
    except Exception:   # noqa: BLE001 - no core on the path
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            return raw.decode("cp866" if os.name == "nt" else "latin-1", "replace")


def claude_cmd():
    """As the core: HUB_CLAUDE or claude from PATH (shutil.which finds claude.cmd on Windows). The folder of Claude is
    the hub's one: hub-up pins CLAUDE_CONFIG_DIR for its processes."""
    line = os.environ.get("HUB_CLAUDE")
    if line:
        return [line] if os.path.isfile(line) else line.split()
    found = shutil.which("claude")
    return [found] if found else None


def alive(pid):
    try:
        from platforms import current
        return current.alive(pid)
    except Exception:   # noqa: BLE001
        return False


def detached(cmd, out):
    try:
        from platforms import current
        return current.popen_detached(cmd, stdout=out, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                      cwd=os.path.expanduser("~"))
    except ImportError:
        return subprocess.Popen(cmd, stdout=out, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                cwd=os.path.expanduser("~"), start_new_session=True)


def mcp_list():
    """(code, text) of `claude mcp list`; "busy" - the previous run is still alive; None - could not start.

    Detached, under a lock {pid, at}, never killed: Claude Code may be in the middle of a one-time refresh exchange,
    and a process killed after the server issued a new pair but before it was written loses the login."""
    cmd = claude_cmd()
    if not cmd:
        return None
    lock = os.path.join(HOME, "list.lock.json")
    held = read_json(lock) or {}
    if held.get("pid") and alive(held["pid"]) and time.time() - float(held.get("at") or 0) < 2 * LIST_TIMEOUT:
        return "busy"
    os.makedirs(HOME, exist_ok=True)
    path = os.path.join(HOME, "list.out")
    try:
        with open(path, "wb") as f:
            proc = detached(cmd + ["mcp", "list"], f)
    except OSError:
        return None
    write_json(lock, {"pid": proc.pid, "at": time.time()})
    try:
        code = proc.wait(LIST_TIMEOUT)
    except subprocess.TimeoutExpired:
        return 124, ""   # not killed: the lock holds the next pass off
    if (read_json(lock) or {}).get("pid") == proc.pid:
        try:
            os.unlink(lock)
        except OSError:
            pass
    with open(path, "rb") as f:
        return code, text_of(f.read())


LINE = re.compile(r"^(\S+?): .* - (.+)$")   # names have no spaces but may have colons: plugin:slack:slack


def parse_list(text):
    """`claude mcp list` -> {server: connected | auth | failed}; disabled ones are skipped."""
    out = {}
    for line in text.splitlines():
        m = LINE.match(line.strip())
        if not m:
            continue
        name, status = m.group(1), m.group(2).lower()
        if "disabled" in status:
            continue
        # "! Connected · tools fetch failed - Request timed out": the login was taken, only the tool list was slow
        # (load right after a hub restart) - live; "✘ Failed to connect" - does not connect at all
        out[name] = ("failed" if "failed to connect" in status else "connected" if "connected" in status
                     else "auth" if "auth" in status else "failed")
    return out


# ───── Claude Code files: read only ─────

def claude_dir():
    return os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".claude")


def claude_json():
    d = os.environ.get("CLAUDE_CONFIG_DIR")
    return os.path.join(d, ".claude.json") if d else os.path.join(os.path.expanduser("~"), ".claude.json")


def credentials():
    """The Claude Code credentials blob as a dict, read only: the macOS keychain item of this user (with a custom
    CLAUDE_CONFIG_DIR - the item named with the folder hash, as Claude Code does), otherwise .credentials.json of the
    Claude folder. Not readable - {}."""
    path = os.environ.get("MCP_KEEPER_CREDS")   # tests and unusual setups
    if not path and sys.platform == "darwin" and shutil.which("security"):
        home = os.environ.get("CLAUDE_CONFIG_DIR")
        service = KEYCHAIN + ("-" + hashlib.sha256(home.encode()).hexdigest()[:8] if home else "")
        user = os.environ.get("USER") or os.environ.get("LOGNAME") or ""
        try:
            r = subprocess.run(["security", "find-generic-password", "-s", service, "-a", user, "-w"],
                               capture_output=True, timeout=30, stdin=subprocess.DEVNULL)
        except (OSError, subprocess.TimeoutExpired):
            return {}
        if r.returncode == 0:
            try:
                return json.loads(r.stdout.decode("utf-8").strip()) or {}
            except ValueError:
                return {}
        if r.returncode != 44:   # 44 - no such item: then the file
            return {}
    return read_json(path or os.path.join(claude_dir(), ".credentials.json")) or {}


def oauth_entries(blob):
    """{server name: entry} from mcpOAuth ("<name>|<hash>" keys)."""
    out = {}
    for k, v in ((blob or {}).get("mcpOAuth") or {}).items():
        if isinstance(v, dict):
            out.setdefault(str(v.get("serverName") or str(k).split("|")[0]), v)
    return out


def user_servers():
    """{name: config} of ~/.claude.json: user scope and every project."""
    cj = read_json(claude_json()) or {}
    out = dict(cj.get("mcpServers") or {}) if isinstance(cj.get("mcpServers"), dict) else {}
    for proj in (cj.get("projects") or {}).values():
        for n, c in (((proj or {}).get("mcpServers") or {}) if isinstance(proj, dict) else {}).items():
            out.setdefault(n, c)
    return out


def header_servers():
    """Servers that log in by headers, not OAuth: a credentials entry of theirs is a leftover; the list judges them."""
    return {str(n).lower() for n, c in user_servers().items()
            if isinstance(c, dict) and (c.get("headersHelper") or c.get("headers"))}


def plugin_servers():
    """{plugin:<plugin>:<server>: config} - MCP servers of enabled Claude Code plugins."""
    d = claude_dir()
    on = (read_json(os.path.join(d, "settings.json")) or {}).get("enabledPlugins") or {}
    inst = (read_json(os.path.join(d, "plugins", "installed_plugins.json")) or {}).get("plugins") or {}
    out = {}
    for key, recs in inst.items() if isinstance(inst, dict) else []:
        if not on.get(key) or not isinstance(recs, list):
            continue
        for rec in recs:
            path = isinstance(rec, dict) and rec.get("installPath")
            data = read_json(os.path.join(path, ".mcp.json")) if path else None
            servers = data.get("mcpServers", data) if isinstance(data, dict) else {}
            for n, v in (servers if isinstance(servers, dict) else {}).items():
                if isinstance(v, dict):
                    out.setdefault(f"plugin:{key.split('@', 1)[0]}:{n}", v)
    return out


def fingerprint(server):
    """The same server under different names: its address (or the address in its args), else the package or the program."""
    if not isinstance(server, dict):
        return None
    clean = lambda u: re.sub(r"[?#].*$", "", re.sub(r"//[^@/]*@", "//", u.strip())).rstrip("/").lower()
    if isinstance(server.get("url"), str) and server["url"].strip():
        return clean(server["url"])
    args = [a for a in server.get("args") or [] if isinstance(a, str)]
    url = next((a for a in args if re.match(r"https?://", a)), None)
    if url:
        return clean(url)
    prog = re.sub(r"\.(exe|cmd|bat|ps1)$", "", os.path.basename(str(server.get("command") or "").replace("\\", "/")), flags=re.I).lower()
    if prog in ("npx", "uvx", "bunx", "pnpx"):
        pkg = next((a for a in args if not a.startswith("-") and a not in ("dlx", "exec")), "")
        return "pkg:" + (pkg[:pkg.rindex("@")] if pkg.rfind("@") > 0 else pkg).lower()
    return None


def duplicates():
    """[{name, plugin}] - a user server of ~/.claude.json with the address of an enabled plugin's server: Claude Code
    loads the user one, the plugin's login under plugin:... is never refreshed and sessions see "needs login".
    Not a duplicate: a name the hub catalog serves (hub.json mcp.<name>.server) - hub sessions run with
    --strict-mcp-config, never read ~/.claude.json, and core/catalog.launch_flags drops the plugin's twin in favour of
    the catalog server, so the user-scope entry hides nothing there."""
    cj = read_json(claude_json()) or {}
    catalog = (config() or {}).get("mcp")
    served = {n for n, v in (catalog.items() if isinstance(catalog, dict) else [])
              if isinstance(v, dict) and isinstance(v.get("server"), dict)}
    user = cj.get("mcpServers") if isinstance(cj.get("mcpServers"), dict) else {}
    plugins = {}
    for n, v in sorted(plugin_servers().items()):
        fp = fingerprint(v)
        if fp:
            plugins.setdefault(fp, n)
    return [{"name": n, "plugin": plugins[fingerprint(v)]} for n, v in sorted(user.items())
            if n not in served and fingerprint(v) and fingerprint(v) in plugins]


# ───── network ─────

def probe(ent):
    """Does the server take the access of the entry: True / False (401, 403) / None (cannot tell: no network, 5xx, no
    address or token). The token and the body go nowhere."""
    url, token = ent.get("serverUrl"), ent.get("accessToken")
    if not url or not token:
        return None, None
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                       "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                                  "clientInfo": {"name": "hub-mcp-keeper", "version": "1"}}}).encode()
    head = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream", "User-Agent": UA,
            "Authorization": "Bearer " + token}
    # a proxy with an https:// scheme breaks urllib - the second try goes direct
    for opener in (urllib.request.build_opener(), urllib.request.build_opener(urllib.request.ProxyHandler({}))):
        try:
            with opener.open(urllib.request.Request(url, data=body, headers=head), timeout=30) as r:
                return (True if r.status < 500 else None), r.status
        except urllib.error.HTTPError as e:
            return (False if e.code in (401, 403) else True if e.code < 500 else None), e.code
        except Exception:   # noqa: BLE001 - network: once more without the proxy, then "cannot tell"
            continue
    return None, None


def net_up():
    for host, port in NET_HOSTS:
        try:
            socket.create_connection((host, port), timeout=5).close()
            return True
        except OSError:
            continue
    return False


def mono():
    return time.monotonic()   # stands still while macOS sleeps


def drift():
    return time.time() - time.monotonic()   # jumps by the time of a sleep


class Calm:
    """When it last "shook": a sleep (drift jump), no network, network just back. A start counts as a shake."""

    def __init__(self):
        self.mark, self.shaken, self.down = drift(), mono(), False

    def look(self):
        d = drift()
        if d - self.mark > WAKE_JUMP:
            self.shaken = mono()
        self.mark = d
        if not net_up():
            self.down, self.shaken = True, mono()
        elif self.down:
            self.down, self.shaken = False, mono()

    def settled(self):
        """COOLDOWN seconds of live network without sleep up to now."""
        self.look()
        return not self.down and mono() - self.shaken >= COOLDOWN

    def wait(self, at_least=0):
        """Until it is settled (True) or MAX_WAIT ran out (False: the pass does not go - a one-time login refreshed
        in the first minutes after a wake or on a shaky network may be lost)."""
        start = mono()
        while True:
            self.look()
            if mono() - start >= at_least and not self.down and mono() - self.shaken >= COOLDOWN:
                return True
            if mono() - start >= MAX_WAIT:
                return False
            time.sleep(NET_STEP)

    def steady(self, since):
        self.look()
        return not self.down and self.shaken == since


CALM = Calm()


# ───── one pass ─────

def login_fix(server, hub):
    return f"hub mcp login {hub} {server}"


def hub_state():
    try:
        from core import journal
        return journal.load_state(STATE)
    except Exception:   # noqa: BLE001 - no core on the path or no journal yet
        return {"sessions": {}}


def hub_session(st):
    """Name of the hub session (`hub mcp login` needs an existing session)."""
    return next((n for n, s in (st.get("sessions") or {}).items()
                 if s.get("kind") == "hub" and s.get("state") != "removed"), "hub")


def in_use(st):
    """{server: [sessions]} - sessions working right now (busy or asking) that have the server."""
    out = {}
    for sname, s in sorted((st.get("sessions") or {}).items()):
        if s.get("state") in ("busy", "dialog"):
            for server in (s.get("info") or {}).get("mcp") or []:
                out.setdefault(server, []).append(sname)
    return out


def listed():
    """`claude mcp list` parsed, or a reason why not: a run only on a settled machine - the list refreshes expired
    logins, and a one-time refresh right after a wake or on a shaky network may be lost."""
    if not CALM.settled():
        return "unsettled"
    r = mcp_list()
    if not isinstance(r, tuple):
        return r or "no claude"
    if r[0] == 124:
        return "timeout"
    return parse_list(r[1]) or f"no list (code {r[0]})"


def checkup(conf, now, seen, had, hub="hub"):
    """{servers: {name: {state, why, fix}}, later_all}; states: ok | lost | first | idle | failed | later.
    seen - names once live, had - names that once had a stored login (both from the plugin state)."""
    ignore = conf["ignore"]
    got = listed()
    if not isinstance(got, dict):
        return {"servers": {}, "later_all": True, "why": got}
    if "auth" in got.values():
        time.sleep(RECHECK)
        again = listed()
        got = {n: ((again.get(n, s) if isinstance(again, dict) else "later") if s == "auth" else s)
               for n, s in got.items()}
    entries = oauth_entries(credentials())
    headers = header_servers()
    out = {}
    for name, state in sorted(got.items()):
        if ignored(name, ignore):
            continue
        ent = entries.get(name) or {}
        logged = bool(ent.get("accessToken") or ent.get("refreshToken"))
        oauth = name.lower() not in headers
        if state == "later":
            out[name] = {"state": "later"}
            continue
        if oauth and name in had and not logged and state != "failed":
            # it was live with a stored login and the record is gone: something erased it
            out[name] = {"state": "lost", "why": tr("why_wiped"), "fix": login_fix(name, hub), "wiped": True}
            continue
        if state == "failed":
            out[name] = {"state": "failed"}   # the core reconnects failed servers in sessions itself
            continue
        if state == "auth":
            if name in seen or (logged and oauth):
                out[name] = {"state": "lost", "why": tr("why_auth"), "fix": login_fix(name, hub)}
            elif name.startswith("plugin:"):
                out[name] = {"state": "idle"}   # a plugin's server nobody ever needed: not an alarm
            else:
                out[name] = {"state": "first", "fix": login_fix(name, hub)}
            continue
        row = {"state": "ok"}
        if oauth and ent:
            expires = (ent.get("expiresAt") or 0) / 1000
            if ent.get("accessToken") and (not expires or expires > now + 60):
                ok, code = probe(ent)
                if ok is False:
                    row = {"state": "lost", "why": tr("why_rejected", code=code), "fix": login_fix(name, hub)}
            if row["state"] == "ok" and expires and not ent.get("refreshToken") and expires - now < conf["warn_days"] * DAY:
                why = (tr("why_expired") if expires <= now
                       else tr("why_expires", day=time.strftime("%d.%m", time.localtime(expires))))
                row = {"state": "lost", "why": why, "fix": login_fix(name, hub)}
        out[name] = row
    return {"servers": out, "later_all": False,
            "logged": sorted(n for n, e in entries.items() if e.get("accessToken") or e.get("refreshToken"))}


def lost(res):
    return {n: s for n, s in res["servers"].items() if s["state"] == "lost"}


def confirmed(first, st, conf, seen, had, hub):
    """A new loss - a second pass CONFIRM_AFTER later, on a calm network: only what is lost both times. None - it shook
    in between: the pass does not count."""
    fresh = set(lost(first)) - set(st.get("told") or [])
    if not fresh:
        return first
    if not CALM.wait(CONFIRM_AFTER):
        return None
    since = CALM.shaken
    again = checkup(conf, time.time(), seen, had, hub)
    if not CALM.steady(since) or again["later_all"]:
        return None
    for n, s in again["servers"].items():
        if s["state"] == "lost" and n not in lost(first):
            again["servers"][n] = {"state": "later"}   # new in the second pass - the next pass confirms it
    return again


def calm_pass(conf, st, seen, had, hub):
    res = None
    for _ in range(TRIES):
        if not CALM.wait():
            return {"servers": {}, "later_all": True, "why": "unsettled"}
        since = CALM.shaken
        res = checkup(conf, time.time(), seen, had, hub)
        if res["later_all"]:
            return res
        if CALM.steady(since):
            done = confirmed(res, st, conf, seen, had, hub)
            if done is not None:
                return done
    for n, s in res["servers"].items():   # it kept shaking: no verdict this time
        if s["state"] == "lost":
            res["servers"][n] = {"state": "later"}
    return res


def remember(st, key, names, now, doubt, every=DAY):
    """told - what hangs at the owner now, told_at - when it was said. Say - if a name was not said for `every`; lift -
    when every said name is out of doubt (proven live or gone). Returns "tell" | "resolve" | None."""
    told, at = list(st.get(key) or []), st.setdefault(key + "_at", {})
    for n in told:
        at.setdefault(n, now)
    for n in [n for n in at if n not in names and (n not in doubt or now - at[n] >= DAY)]:
        del at[n]
    st[key] = sorted(set(names) | {n for n in told if n in at})
    if names and [n for n in names if now - at.get(n, -every) >= every]:
        for n in names:
            at[n] = now
        return "tell"
    return "resolve" if told and not st[key] else None


MASK = re.compile(r"[A-Za-z0-9_\-.~+/=]{24,}")   # tokens and keys in command lines


def processes(server):
    """[{pid, started, cmd}] - processes of Claude Code and of this MCP server now: kept in the plugin state when a
    one-time login is lost, to find later who refreshed it. Long tokens in command lines are masked."""
    word = short(server)
    rows = []
    try:
        if os.name == "nt":
            r = subprocess.run(["powershell", "-NoProfile", "-Command",
                                "Get-CimInstance Win32_Process | Select-Object ProcessId,CreationDate,CommandLine "
                                "| ConvertTo-Json -Compress"], capture_output=True, timeout=60, stdin=subprocess.DEVNULL)
            data = json.loads(text_of(r.stdout) or "[]")
            for p in data if isinstance(data, list) else [data]:
                rows.append((p.get("ProcessId"), str(p.get("CreationDate") or ""), p.get("CommandLine") or ""))
        else:
            r = subprocess.run(["ps", "-axww", "-o", "pid=,lstart=,command="], capture_output=True, timeout=60,
                               stdin=subprocess.DEVNULL)
            for line in text_of(r.stdout).splitlines():
                m = re.match(r"\s*(\d+)\s+(\S+\s+\S+\s+\d+\s+[\d:]+\s+\d+)\s+(.*)$", line)
                if m:
                    rows.append((int(m.group(1)), m.group(2), m.group(3)))
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return []
    out = []
    for pid, started, cmd in rows:
        low = cmd.lower()
        if word in low or re.search(r"(^|[\\/ ])claude(\.exe|\.cmd)?( |$)", low) or "mcp-remote" in low:
            if pid != os.getpid():
                out.append({"pid": pid, "started": started, "cmd": MASK.sub("…", cmd)[:300]})
    return out[:40]


def order(bad, conf, use, ok_at):
    """Most important first: one-time logins (lost for good), servers sessions use right now, the ones live most
    recently."""
    return sorted(bad, key=lambda n: (short(n) not in conf["one_time"], n not in use, -float(ok_at.get(n) or 0), n))


def message(bad, first, dups, conf, use, ok_at):
    lines = []
    if bad:
        names = order(bad, conf, use, ok_at)
        lines += [tr("lost", names=", ".join(names))]
        lines += [tr("lost_line", name=n, why=bad[n]["why"], fix=bad[n]["fix"]) for n in names]
    if dups:
        lines += [tr("dup", names=", ".join(d["name"] for d in dups))] + [tr("dup_line", **d) for d in dups]
    if first:
        lines += [tr("first", names=", ".join(sorted(first)))]
        lines += [tr("first_line", name=n, fix=first[n]["fix"]) for n in sorted(first)]
    return "\n".join(lines)


def status_path():
    """status.json next to the plugin: the page reads it from /ext/<plugin>/status.json (no-cache)."""
    return os.environ.get("MCP_KEEPER_STATUS") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "status.json")


def write_status(st):
    """The view's data - from the plugin state (a market update replaces the folder: rewritten at start too)."""
    if st.get("status"):
        write_json(status_path(), st["status"])


def post_status(st, res, conf, now):
    """Rows for the view (status.json) every pass; one human line to the plugin channel when the picture changed."""
    ok_at = st.setdefault("ok_at", {})
    rows = {n: {"state": s["state"], "ok_at": ok_at.get(n), **({"why": s["why"]} if s.get("why") else {})}
            for n, s in sorted(res["servers"].items())}
    st["status"] = {"v": 1, "at": now, "every": conf["every"] / 60, "servers": rows}
    write_status(st)
    count = lambda *states: sum(r["state"] in states for r in rows.values())
    head = tr("status", ok=count("ok"), bad=count("lost"), later=count("later", "failed"))
    if head == st.get("posted"):
        return False
    put("ext.post", {"plugin": NAME, "text": head})
    st["posted"], st["posted_at"] = head, now
    return True


def once(now=None):
    conf = settings()
    st = load_state()
    hub_st = hub_state()
    hub = hub_session(hub_st)
    seen, had = set(st.get("seen_ok") or []), set(st.get("had") or [])
    res = calm_pass(conf, st, seen, had, hub)
    now = now or time.time()
    st["at"] = now
    if res["later_all"]:
        save_state(st)
        put(f"ext.{NAME}.checked", {"later": True, "why": res.get("why")})
        return res
    servers = res["servers"]
    ok_at = st.setdefault("ok_at", {})
    for n, s in servers.items():
        if s["state"] == "ok":
            ok_at[n] = now
    seen |= {n for n, s in servers.items() if s["state"] == "ok"}
    st["seen_ok"] = sorted(seen)
    # a login counts as "had" only on a live server: a record erased later is then told as erased
    st["had"] = sorted(had | {n for n in res.get("logged") or [] if servers.get(n, {}).get("state") == "ok"})
    res["dups"] = duplicates()
    bad = lost(res)
    put(f"ext.{NAME}.checked", {"ok": sorted(n for n, s in servers.items() if s["state"] == "ok"),
                                "lost": [{"name": n, "why": s.get("why")} for n, s in sorted(bad.items())],
                                "first": sorted(n for n, s in servers.items() if s["state"] == "first"),
                                "failed": sorted(n for n, s in servers.items() if s["state"] == "failed"),
                                "later": sorted(n for n, s in servers.items() if s["state"] == "later"),
                                "dups": res["dups"]})
    post_status(st, res, conf, now)
    for n in bad:   # a one-time login lost: who was running then - for finding the cause later
        if short(n) in conf["one_time"] and n not in (st.get("told") or []):
            st.setdefault("traces", {})[n] = {"at": now, "why": bad[n].get("why"), "processes": processes(n)}
    if conf["quiet_until"] and now < conf["quiet_until"]:
        if st.get("hushed") != conf["quiet_until"]:
            put("owner.notify_resolved", {"id": NOTE_ID})
            for k in ("told", "told_at", "told_first", "told_first_at", "told_dup", "told_dup_at"):
                st.pop(k, None)
            st["hushed"] = conf["quiet_until"]
        save_state(st)
        return res
    doubt = {n for n, s in servers.items() if s["state"] in ("later", "failed")}
    first = {n: s for n, s in servers.items() if s["state"] == "first"}
    said = [remember(st, "told", sorted(bad), now, doubt, REPEAT),
            remember(st, "told_dup", sorted(d["name"] for d in res["dups"]), now, set()),
            remember(st, "told_first", sorted(first), now, doubt)]
    use = in_use(hub_st)
    if not st.get("split"):   # 0.1.0 told the owner of duplicates and first logins too: lift that once
        if not st.get("told"):
            put("owner.notify_resolved", {"id": NOTE_ID})
        st["split"] = True
    # the owner - only a login that worked and stopped: one urgent message, the most important first
    if said[0] == "tell":
        put("owner.notify", {"id": NOTE_ID, "title": tr("title"), "topic": tr("lost_topic"),
                             "text": message(bad, {}, [], conf, use, ok_at), "urgent": True, "urgency": "now"})
        res["sessions"] = tell_sessions(order(bad, conf, use, ok_at), use, st, now)
    elif said[0] == "resolve":
        put("owner.notify_resolved", {"id": NOTE_ID})
    # a duplicate and a never-made login block no work: one line a day to the Hub Developer, not to the owner
    if "tell" in said[1:]:
        dups = res["dups"] if said[1] == "tell" else []
        firsts = first if said[2] == "tell" else {}
        res["developer"] = developer(hub_st)
        put("message.received", {"to": res["developer"], "text": message({}, firsts, dups, conf, use, ok_at)})
    save_state(st)
    return res


def developer(st):
    """The Hub Developer (the core names system agents, the first one) - or the hub session."""
    sessions = st.get("sessions") or {}
    for name in st.get("system") or []:
        if (sessions.get(name) or {}).get("state") not in (None, "removed"):
            return name
    return hub_session(st)


def tell_sessions(names, use, st, now):
    """Sessions that work with a lost server right now get one line (once per loss): do not loop on its tools."""
    said = st.setdefault("sessions_told", {})
    for n in [n for n in said if n not in names]:
        del said[n]
    sent = []
    for server in names:
        for sname in use.get(server, []):
            if sname in said.get(server, []):
                continue
            put("message.received", {"to": sname, "text": tr("to_session", server=server)})
            said.setdefault(server, []).append(sname)
            sent.append(sname)
    return sent


def report(res):
    s = res.get("servers") or {}
    pick = lambda state: ", ".join(n for n, x in sorted(s.items()) if x["state"] == state) or "-"
    if res.get("later_all"):
        print(time.strftime("%H:%M:%S"), "mcp-keeper: later -", res.get("why"), flush=True)
        return
    print(time.strftime("%H:%M:%S"), "mcp-keeper: live:", pick("ok"), "| lost:", pick("lost"), "| first login:",
          pick("first"), "| not connecting:", pick("failed"), "| later:", pick("later"), "| duplicates:",
          ", ".join(d["name"] for d in res.get("dups") or []) or "-", flush=True)


def loop():
    write_status(load_state())   # the view has its data right after a market update replaced the folder
    if time.time() - float(load_state().get("at") or 0) >= AGAIN_AFTER:
        time.sleep(START_DELAY)
        report(once())
    while True:
        wake = time.time() + settings()["every"]
        while time.time() < wake:   # sleep of the machine: go by the clock, not by a counter
            time.sleep(min(NET_STEP, max(1, wake - time.time())))
            CALM.look()
        report(once())


def main():
    try:
        from core import catalog
        catalog.pin_claude_dir(STATE)   # run by hand: the hub's Claude folder, not a personal CLAUDE_CONFIG_DIR
    except Exception:   # noqa: BLE001
        pass
    if "--once" in sys.argv[1:]:
        report(once())
        return 0
    loop()


if __name__ == "__main__":
    sys.exit(main())
