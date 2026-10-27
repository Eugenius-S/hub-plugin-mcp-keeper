#!/usr/bin/env python3
"""mcp-keeper — the process of the plugin: watches the OAuth logins of the MCP servers the hub gives to its sessions
and says so before work stops on a lost one.

What it watches: the servers of hub.json `mcp.<name>.server` plus the MCP servers (also plugins') that live hub sessions
carry now (session info) - not ~/.claude.json of a plain Claude Code. Of them the remote ones: an OAuth login, or
`headers`/`headersHelper` (the helper runs with a limit, the server gets an initialize with those headers; values stay in
memory). A stdio server is never started, and `claude mcp list` is never run.

Every `every_minutes` (15) one pass, per server:
  1. the stored access (Claude Code credentials, read only, never written) goes in an `initialize` request to the
     server's own address: 401/403 - the login is rejected.
  2. the access has expired and there is a refresh token: Claude Code refreshes it itself, under its own lock - the
     plugin asks `claude mcp get <server>` (this server only, at most once an hour, never for a one-time login) with a
     time limit, the whole process tree is killed when it runs over, and the output is parsed in memory, never stored.
The token goes only to https (http - only to localhost) and never through a redirect. The plugin never exchanges a
refresh token itself, and keeps away from a refresh Claude Code is doing right now (its mcp-refresh-*.lock is fresh).
Claude Code knows ~/.claude.json and plugins, not the hub catalog: "no such server" is not a verdict - then the word of
the hub's live sessions counts (needs-auth - lost, after the second pass; connected - live).
Lost is only an explicit refusal (401/403, "needs authentication", an erased record, a token that cannot be refreshed).
A timeout, no network, 5xx - "later", nothing is said. A pass runs only after COOLDOWN seconds of a live network
without sleep (the wall clock jumping away from the monotonic one); a new loss is told only if a second pass
CONFIRM_AFTER seconds later sees it too.

To the owner: one urgent message per pass with the servers that are newly lost. One alarm per server per day, kept on
disk: it is repeated only after the server was seen live and was lost again, and never within a day of the last one; the
whole plugin says at most `max_alarms` a day. Sessions that work with a lost server get one line.
Never a server never logged in (state "first" when sessions wait for it: one line a day to the Hub Developer) and
never a duplicate unless `check_duplicates` is on.

Out: the fact ext.<name>.checked, owner.notify (urgent - a quiet Telegram summary would swallow it), a short message
to the sessions that are working with this server right now, and the status in the plugin's channel (for its view).
The state files hold server names, states and times only - no command lines, arguments, headers or addresses.

cover (off by default; true or a list of machine names): every MCP server of this machine - the hub catalog,
~/.claude.json, enabled plugins - each with its holder (by): claude, headers, conduit (read only: the hub's conduit
module refreshes it, a parallel refresh kills the pair), mcp-remote / auth-keeper (stdio with mcp-remote: its stored
login in ~/.mcp-auth is read, never exchanged), program (any other stdio: skipped, never started), local, none. A loss
then goes at once to the Hub Developer (hub send), to the owner only after owner_after_passes (2) more passes, never one
auth-keeper holds (it tells the owner itself). Refreshing ahead is not done: `claude mcp get` does not refresh a live
access, Claude Code keeps the login itself under mcp-refresh-*.lock.

Settings - hub.json extensions[<plugin>]: every_minutes (15), warn_days (3), ignore ([names]), one_time ([names]),
max_alarms (6 a day), ask_claude (true), net_check_minutes (5), stale_days (2), check_duplicates (false), cover (false), owner_after_passes (2), read_only ([names]), quiet_until (epoch or "YYYY-MM-DDTHH:MM" in the hub
timezone); hub.json mcp_auth_ignore is honoured too.
"""
import datetime, glob, hashlib, json, os, re, shutil, signal, socket, subprocess, sys, time, urllib.error, urllib.parse, urllib.request

KEY = os.environ.get("HUB_EXTENSION") or "mcp-keeper"   # hub.json extensions key: <account>:mcp-keeper from the market
NAME = KEY.split(":")[-1]                                # channel and fact name
STATE = os.environ.get("HUB_STATE_DIR") or os.path.expanduser("~/.hub-core")
HOME = os.path.join(STATE, "mcp-keeper")
NOTE_ID = "mcp"
DAY = 24 * 3600
ONE_TIME = ("canva",)     # servers whose refresh token works once: a lost exchange loses the login
EVERY = 15                # minutes between passes
MAX_ALARMS = 6            # messages to the owner a day, the whole plugin
START_DELAY = 60          # first pass after a start: the hub restarts in batches
AGAIN_AFTER = 600         # the process was restarted - a pass not more often than this
CONFIRM_AFTER = 300       # a new loss - checked again after this many seconds
RECHECK = 30              # "needs authentication" - asked again after this many seconds
ASK_EVERY = 3600          # `claude mcp get` about one server - not more often than this
GET_TIMEOUT = 90          # a `claude mcp get` run over this is killed, with everything it started
KILL_GRACE = 3            # seconds between SIGTERM and SIGKILL of that tree
COOLDOWN = 300            # a pass only after this many seconds of live network without sleep
WAKE_JUMP = 60            # wall clock ran away from the monotonic one by more - the machine slept
NET_STEP = 30             # the loop looks at the clock this often (cheap: no network)
NET_EVERY = 5             # minutes between TCP checks of the network in the idle loop (net_check_minutes)
STALE_DAYS = 2            # an access expired this long that Claude Code cannot be asked about - shown in the view
LOCK_FRESH = 300          # a refresh lock of Claude Code younger than this: someone is refreshing right now
MAX_WAIT = 1800
TRIES = 3
NET_HOSTS = (("api.anthropic.com", 443), ("github.com", 443))
KEYCHAIN = "Claude Code-credentials"
UA = "hub-mcp-keeper (mcp-remote compatible)"   # Cloudflare answers 403 "1010" to Python-urllib
HELPER_TIMEOUT = 30       # a headersHelper run over this is killed: "later"
STATES = ("ok", "lost", "later", "first", "skip")
HOLDERS = ("claude", "headers", "conduit", "auth-keeper", "mcp-remote", "program", "none", "local")
OWNER_AFTER = 2           # cover: the owner hears of a loss only when this many more passes still see it
READ_ONLY = ("conduit",)  # cover: logins another part of the hub refreshes - only looked at, never through claude
LIVE = ("idle", "busy", "dialog")   # sessions with a process: their MCP status is Claude Code's own fresh word

STR = {
    "en": {
        "why_stale": "the access expired {days} days ago and Claude Code does not know this server - the hub's sessions "
                     "refresh it when they use it",
        "title": "MCP logins",
        "lost_topic": "MCP login lost",
        "lost": "MCP login lost: {names} - log in:",
        "lost_line": "\u2022 {name} - {why}: {fix}",
        "why_wiped": "the stored login was erased (the server was live with it)",
        "dup": "MCP duplicate: {names} - the same server as a plugin's; Claude Code loads the user one and the plugin's "
               "login is never refreshed:",
        "dup_line": "\u2022 {name} hides {plugin}: claude mcp remove {name} -s user",
        "why_auth": "needs authentication",
        "why_rejected": "the server rejects the access (HTTP {code})",
        "why_expires": "the token without refresh expires {day}",
        "why_expired": "the token without refresh has expired",
        "to_session": "MCP {server}: the login is lost, the owner is told. Its tools fail until then - do not repeat "
                      "the calls in a loop; work without it or wait, the hub reconnects it after the login.",
        "status": "MCP: {ok} live, {bad} lost, {later} later",
        "status_first": "MCP: {ok} live, {bad} lost, {later} later, {first} never logged in",
        "why_first": "never logged in; sessions wait for it",
        "first": "MCP never logged in: {names} - sessions get these servers but nobody has logged in yet (not a lost "
                 "login). One login per server is enough, every session picks it up:",
        "first_line": "\u2022 {name}: hub mcp login {hub} {name}",
        "why_helper": "the headersHelper failed (exit {code})",
        "why_helper_out": "the headersHelper printed no headers",
        "why_headers": "the server rejects the headers (HTTP {code})",
        "fix_headers": "renew the key of hub.json mcp.{name}.server",
        "fix_mcp": "/mcp in Claude Code -> {name}",
        "first_line_fix": "\u2022 {name}: {fix}",
        "why_local": "a server on this machine: no login",
        "why_open": "the server answers without a login",
        "why_program": "a program with its own login: not started, not checked",
        "why_ms365": "a program with its own login: auth-keeper checks it (--verify-login)",
        "why_read_only": "the access lives 15 minutes; the hub's conduit module refreshes it when it is used - only looked at",
        "why_remote_expired": "the mcp-remote access has expired and auth-keeper did not refresh it ahead",
        "why_remote_later": "the mcp-remote access has expired: mcp-remote refreshes it at its start",
        "dev": "MCP: first sign of a lost login (the owner hears only if it stays lost for {passes} more passes):",
        "dev_line": "\u2022 {name} ({by}) - {why}: {fix}",
    },
    "ru": {
        "why_stale": "доступ истёк {days} дн. назад, а Claude Code этот сервер не знает — хаб освежит его, когда сессия "
                     "им воспользуется",
        "title": "Входы MCP",
        "lost_topic": "Слетел вход MCP",
        "lost": "Слетел вход MCP: {names} — войди:",
        "lost_line": "\u2022 {name} — {why}: {fix}",
        "why_wiped": "запись входа стёрта (до этого сервер с ней работал)",
        "dup": "Дубль MCP: {names} — тот же сервер, что у плагина; Claude Code берёт пользовательский, вход плагина "
               "не освежается:",
        "dup_line": "\u2022 {name} скрывает {plugin}: claude mcp remove {name} -s user",
        "why_auth": "ждёт входа",
        "why_rejected": "сервер отвергает доступ (HTTP {code})",
        "why_expires": "токен без refresh истекает {day}",
        "why_expired": "токен без refresh истёк",
        "to_session": "MCP {server}: слетел вход, владелец знает. До входа его инструменты не работают — не повторяй "
                      "вызовы по кругу; работай без него или подожди, после входа хаб переподключит сервер.",
        "status": "MCP: живы {ok}, слетели {bad}, позже {later}",
        "status_first": "MCP: живы {ok}, слетели {bad}, позже {later}, не входили {first}",
        "why_first": "ни разу не входили, сессии ждут входа",
        "first": "MCP без входа: {names} — сессии получают эти серверы, но в них ещё ни разу не входили (это не слёт). "
                 "Хватит одного входа на сервер, все сессии подхватят:",
        "first_line": "\u2022 {name}: hub mcp login {hub} {name}",
        "why_helper": "headersHelper не сработал (код {code})",
        "why_helper_out": "headersHelper не выдал заголовков",
        "why_headers": "сервер отвергает заголовки (HTTP {code})",
        "fix_headers": "обнови ключ в hub.json mcp.{name}.server",
        "fix_mcp": "/mcp в Claude Code -> {name}",
        "first_line_fix": "\u2022 {name}: {fix}",
        "why_local": "сервер на этой машине: входа нет",
        "why_open": "сервер отвечает без входа",
        "why_program": "программа со своим входом: не запускается, не проверяется",
        "why_ms365": "программа со своим входом: её проверяет auth-keeper (--verify-login)",
        "why_read_only": "доступ живёт 15 минут, его освежает модуль conduit хаба при использовании — только смотрим",
        "why_remote_expired": "доступ mcp-remote истёк, auth-keeper не освежил его заранее",
        "why_remote_later": "доступ mcp-remote истёк: mcp-remote освежит его при запуске",
        "dev": "MCP: первый признак слёта входа (владелец узнает, только если не оживёт ещё {passes} прохода):",
        "dev_line": "\u2022 {name} ({by}) — {why}: {fix}",
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
            # the built-in one-time servers cannot be switched off: a setting only adds to them
            "one_time": {short(x) for x in list(ONE_TIME) + list(own.get("one_time") or [])},
            "net_every": max(1.0, float(own.get("net_check_minutes") or NET_EVERY)) * 60,
            "stale_days": max(0.0, float(own.get("stale_days", STALE_DAYS))),
            "quiet_until": parse_quiet(own.get("quiet_until"), cfg.get("timezone")),
            "max_alarms": max(1, int(own.get("max_alarms") or MAX_ALARMS)),
            "ask_claude": own.get("ask_claude", True) is not False,
            "check_duplicates": own.get("check_duplicates", False) is True,
            "cover": cover_on(own.get("cover")),
            "owner_after": max(0, int(own.get("owner_after_passes", OWNER_AFTER))),
            "read_only": {short(x) for x in list(READ_ONLY) + list(own.get("read_only") or [])},
            "auth_keeper": any(short(k) == "auth-keeper" and isinstance(v, dict) and not v.get("disabled")
                               for k, v in (cfg.get("extensions") or {}).items()),
            "ignore": {str(x).lower() for x in list(own.get("ignore") or []) + list(skip if isinstance(skip, list) else [])}}


def cover_on(value):
    """cover: true, or a list of machine names (hub.json settings travel between machines: a list keeps it on one)."""
    if value is True:
        return True
    if not isinstance(value, list):
        return False
    host = socket.gethostname().lower()
    return any(str(x).lower() in (host, host.split(".")[0]) for x in value)


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


def kill_tree(proc):
    """The process and everything it started: its process group on POSIX (it was started as a leader), taskkill /T on
    Windows. SIGTERM, a short grace, then SIGKILL for the stragglers."""
    if os.name == "nt":
        try:
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True, timeout=30,
                           stdin=subprocess.DEVNULL)
        except (OSError, subprocess.SubprocessError):
            pass
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return
    try:
        proc.wait(KILL_GRACE)
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def run_limited(cmd, timeout, merge=True):
    """(code, text) of a command run with a time limit; the output stays in memory. Over the limit - (124, "") and the
    whole process tree killed, no orphans. None - could not start. merge=False - stderr is dropped, not read."""
    kw = {"stdout": subprocess.PIPE, "stderr": subprocess.STDOUT if merge else subprocess.DEVNULL, "stdin": subprocess.DEVNULL,
          "cwd": os.path.expanduser("~")}
    if os.name != "nt":
        kw["start_new_session"] = True
    try:
        proc = subprocess.Popen(cmd, **kw)
    except OSError:
        return None
    try:
        out, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        kill_tree(proc)
        try:
            proc.communicate(timeout=5)
        except Exception:   # noqa: BLE001 - the tree is dead; whatever is left in the pipe is not needed
            pass
        return 124, ""
    return proc.returncode, text_of(out)


def get_url(text):
    m = re.search(r"^\s*url:\s*(\S+)", text or "", re.I | re.M)
    return m.group(1) if m else None


def same_server(a, b):
    """The address Claude Code shows and the catalog address are one server (query and trailing slash aside)."""
    return fingerprint({"url": a}) == fingerprint({"url": b}) if a and b else True


def mcp_get(name):
    """(code, text) of `claude mcp get <name>` - one server, never the whole list (that starts every stdio server);
    None - no claude. Claude Code refreshes an expired OAuth access of this server itself, under its own lock."""
    cmd = claude_cmd()
    if not cmd or str(name).startswith("-"):
        return None
    return run_limited(cmd + ["mcp", "get", str(name)], GET_TIMEOUT)


def parse_get(text):
    """`claude mcp get` -> connected | auth | failed | None (no such server, no answer)."""
    low = (text or "").lower()
    if not low.strip():
        return None
    if "no mcp server" in low:
        return "unknown"   # the CLI knows ~/.claude.json and plugins, not the hub catalog (sessions run --strict-mcp-config)
    m = re.search(r"status:\s*(.+)", low)
    status = m.group(1) if m else low
    # "! Connected - tools fetch failed - Request timed out": the login was taken, only the tool list was slow (load
    # right after a hub restart) - live; "failed to connect" - does not connect at all
    if "failed to connect" in status:
        return "failed"
    if "connected" in status:
        return "connected"
    if "auth" in status:
        return "auth"
    return None


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

LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1", "[::1]")


def safe_url(url):
    """The token goes only to https (http - only to the machine itself), never to an address with a login in it."""
    try:
        p = urllib.parse.urlsplit(str(url).strip())
        host = p.hostname or ""
    except ValueError:
        return False
    if p.username or p.password or not host:
        return False
    return p.scheme == "https" or (p.scheme == "http" and host in LOCAL_HOSTS)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect would carry the Authorization header to another place: not followed, the 3xx answer is the result."""

    def redirect_request(self, *args, **kwargs):
        return None


def probe(ent):
    """Does the server take the access of the entry: True / False (401, 403) / None (cannot tell: no network, 5xx, a
    redirect, an unsafe address, no address or token). The token and the body go nowhere."""
    url, token = ent.get("serverUrl"), ent.get("accessToken")
    if not url or not token:
        return None, None
    return probe_with(url, {"Authorization": "Bearer " + token})


def probe_keyed(url, head):
    """A headers server asked with its own headers (they stay in memory)."""
    return probe_with(url, head)


def probe_with(url, auth):
    """probe() with the given login headers (a bearer token or what a headers server sends)."""
    if not url or not auth or not safe_url(url):
        return None, None
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                       "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                                  "clientInfo": {"name": "hub-mcp-keeper", "version": "1"}}}).encode()
    head = {**auth, "Content-Type": "application/json", "Accept": "application/json, text/event-stream",
            "User-Agent": UA}
    # a proxy with an https:// scheme breaks urllib - the second try goes direct
    for opener in (urllib.request.build_opener(NoRedirect), urllib.request.build_opener(NoRedirect, urllib.request.ProxyHandler({}))):
        try:
            with opener.open(urllib.request.Request(url, data=body, headers=head), timeout=30) as r:
                return (True if r.status < 300 else None), r.status
        except urllib.error.HTTPError as e:
            e.close()
            if 300 <= e.code < 400:   # moved: nobody judged the login
                return None, e.code
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

    def look(self, net=True):
        """net=False - only the clock (a sleep), no TCP: the idle loop does that every step, the network - rarely."""
        d = drift()
        if d - self.mark > WAKE_JUMP:
            self.shaken = mono()
        self.mark = d
        if not net:
            return
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


# ───── what the hub gives to sessions ─────

def remote(server):
    """A server that logs in by OAuth over the network: an address, no program, no login headers (those log in by
    themselves; a plain header such as a client tag does not make it one)."""
    return (isinstance(server, dict) and isinstance(server.get("url"), str) and bool(server["url"].strip())
            and not server.get("command") and not keyed(server))


AUTH_HEADER = re.compile(r"auth|key|token|secret|bearer|cookie|session", re.I)


def keyed(server):
    """A server that logs in by headers: a `headersHelper` command or a login header (Authorization, an API key, a
    token) among its static `headers`; an address, no program."""
    head = server.get("headers") if isinstance(server, dict) else None
    return (isinstance(server, dict) and isinstance(server.get("url"), str) and bool(server["url"].strip())
            and not server.get("command")
            and (bool(server.get("headersHelper")) or any(AUTH_HEADER.search(str(k)) for k in (head or {})
                                                            if isinstance(head, dict))))


def header_servers(cover=False):
    """{name: server} - headers servers the hub gives to its sessions (the hub catalog and plugins); with cover - every
    server of this machine (~/.claude.json too)."""
    return {n: v for n, v in (cover_servers() if cover else hub_servers()).items() if keyed(v)}


def catalog_servers():
    catalog = (config() or {}).get("mcp")
    return {str(n): v["server"] for n, v in sorted((catalog if isinstance(catalog, dict) else {}).items())
            if isinstance(v, dict) and isinstance(v.get("server"), dict)}


def hub_servers():
    """{name: server} - the hub catalog, then servers of enabled plugins."""
    out = dict(catalog_servers())
    for n, v in sorted(plugin_servers().items()):
        out.setdefault(n, v)
    return out


def user_servers():
    """{name: server} of ~/.claude.json (user scope and every project), read only."""
    cj = read_json(claude_json()) or {}
    out = {}
    blocks = [cj.get("mcpServers")] + [p.get("mcpServers") for p in (cj.get("projects") or {}).values() if isinstance(p, dict)]
    for block in blocks:
        for n, v in sorted((block if isinstance(block, dict) else {}).items()):
            if isinstance(v, dict):
                out.setdefault(str(n), v)
    return out


def cover_servers():
    """{name: server} - every MCP server of this machine: the hub catalog, ~/.claude.json, enabled plugins; a server
    under a second name (the same address or package) once, by its first name."""
    out, prints = {}, set()
    for block in (catalog_servers(), user_servers(), plugin_servers()):
        for n, v in sorted(block.items()):
            fp = fingerprint(v)
            if n in out or (fp and fp in prints):
                continue
            out[n] = v
            if fp:
                prints.add(fp)
    return out


def local_url(url):
    try:
        return (urllib.parse.urlsplit(str(url).strip()).hostname or "") in LOCAL_HOSTS
    except ValueError:
        return False


def remote_url(server):
    """mcp-remote <https address> in a stdio server's args - its address, else None."""
    args = [a for a in (server.get("args") or []) if isinstance(a, str)] if isinstance(server, dict) else []
    if not any("mcp-remote" in a for a in args + [str(server.get("command") or "")]):
        return None
    return next((a for a in args if re.match(r"https?://", a)), None)


def remote_tokens(url):
    """The stored login of mcp-remote for this address: the newest <md5(url)>_tokens.json of
    <MCP_REMOTE_CONFIG_DIR or ~/.mcp-auth>/mcp-remote-*/, read only (mcp-remote and auth-keeper refresh it)."""
    base = os.environ.get("MCP_REMOTE_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".mcp-auth")
    name = hashlib.md5(url.encode()).hexdigest() + "_tokens.json"
    found = glob.glob(os.path.join(glob.escape(base), "mcp-remote-*", name))
    for path in sorted(found, key=lambda p: os.path.getmtime(p), reverse=True):
        data = read_json(path)
        if isinstance(data, dict):
            exp = data.get("expires_at")
            if isinstance(exp, (int, float)) and exp:
                exp = exp / 1000 if exp > 1e11 else exp
            elif isinstance(data.get("expires_in"), (int, float)):
                exp = os.path.getmtime(path) + data["expires_in"]
            else:
                exp = 0
            return {"access": data.get("access_token"), "refresh": bool(data.get("refresh_token")), "expires": exp}
    return None


def probe_open(url):
    """Does the server answer an initialize without any login: True (2xx) / False (401, 403) / None."""
    ok, code = probe_with(url, {"User-Agent": UA})
    return True if ok and code and code < 300 else False if ok is False else None


def expand(value):
    """${VAR} and ${VAR:-default} of the environment, as Claude Code expands them in an MCP config."""
    return re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}",
                  lambda m: os.environ.get(m.group(1)) or (m.group(2) or ""), str(value))


def helper_headers(srv):
    """(headers, why) of a headers server: its static headers plus what its headersHelper prints (a JSON object of
    strings). (None, "later") - the helper ran over its time; (None, why) - it failed. Values stay in memory."""
    head = {str(k): expand(v) for k, v in (srv.get("headers") or {}).items()} if isinstance(srv.get("headers"), dict) else {}
    cmd = srv.get("headersHelper")
    if isinstance(cmd, str) and cmd.strip():
        shell = ["cmd", "/c", cmd] if os.name == "nt" else ["/bin/sh", "-c", cmd]
        r = run_limited(shell, HELPER_TIMEOUT, merge=False)
        if not isinstance(r, tuple) or r[0] == 124:
            return None, "later"
        if r[0] != 0:
            return None, tr("why_helper", code=r[0])
        try:
            got = json.loads(r[1].strip() or "null")
        except ValueError:
            got = None
        if not isinstance(got, dict) or not got or not all(isinstance(v, str) and v for v in got.values()):
            return None, tr("why_helper_out")
        head.update({str(k): v for k, v in got.items()})
    return (head or None), (None if head else tr("why_helper_out"))


def session_verdict(name, hub_st, live=True):
    """What Claude Code in the hub's own sessions says about this server (their MCP status at start and at a turn):
    auth | connected | None. live - only sessions with a process (a sleeping one keeps the word of its last start)."""
    said = None
    for s in (hub_st.get("sessions") or {}).values():
        if s.get("state") == "removed" or (live and s.get("state") not in LIVE):
            continue
        info = s.get("info") or {}
        if name in (info.get("mcp_auth") or []):
            return "auth"
        if name in (info.get("mcp") or []) and name not in (info.get("mcp_failed") or []):
            said = "connected"
    return said


def served(hub_st, cover=False):
    """{name: url or None} - remote servers the hub gives to its sessions: the hub catalog (hub.json mcp.<name>.server) and
    MCP servers of plugins (plugin:<plugin>:<server>). The MCP of Claude Code outside the hub (~/.claude.json) is not
    here, and a server that runs as a program (stdio) is never here: nothing is started to look at it."""
    catalog = (config() or {}).get("mcp")
    out, prints = {}, set()
    for n, v in sorted((catalog if isinstance(catalog, dict) else {}).items()):
        srv = v.get("server") if isinstance(v, dict) else None
        if remote(srv):
            out[str(n)] = srv["url"]
        if isinstance(srv, dict) and fingerprint(srv):
            prints.add(fingerprint(srv))
    for n, v in sorted(plugin_servers().items()):
        if remote(v) and fingerprint(v) not in prints:   # the core drops the plugin's twin of a catalog server
            out.setdefault(n, v["url"])
    if cover:   # every remote server of this machine, not only what the hub gives
        have = {fingerprint({"url": u}) for u in out.values() if u}
        for n, v in sorted(cover_servers().items()):
            if remote(v) and n not in out and fingerprint(v) not in have:
                out[n] = v["url"]
    for s in (hub_st.get("sessions") or {}).values():
        for n in (s.get("info") or {}).get("mcp") or []:
            if isinstance(n, str) and n.startswith("plugin:"):
                out.setdefault(n, None)
    return out


# ───── one pass ─────

def login_fix(server, hub, catalog=None):
    """The login command: the hub's for its catalog and plugins, /mcp of Claude Code for a server only it has."""
    if catalog is not None and server not in catalog and not str(server).startswith("plugin:"):
        return tr("fix_mcp", name=server)
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


def verdict_of(name, url=None):
    """connected | auth | unknown | later from one `claude mcp get <name>`. unknown - Claude Code has no such server (the
    hub catalog is not ~/.claude.json) or the name there is another server (another address): its answer says nothing."""
    r = mcp_get(name)
    if not isinstance(r, tuple) or r[0] == 124:
        return "later"
    said = parse_get(r[1])
    if said in ("connected", "auth", "failed") and not same_server(get_url(r[1]), url):
        return "unknown"
    return {"connected": "connected", "auth": "auth", "unknown": "unknown"}.get(said, "later")


def refresh_busy(now):
    """Claude Code refreshes an MCP login under ~/.claude/mcp-refresh-<key>.lock: while one is fresh somebody is in the
    middle of an exchange - the plugin does not start a second Claude Code next to it (a kill or a repeat of a one-time
    refresh loses the login)."""
    for p in glob.glob(os.path.join(glob.escape(claude_dir()), "mcp-refresh-*.lock")):
        try:
            if now - os.path.getmtime(p) < LOCK_FRESH:
                return True
        except OSError:
            continue
    return False


def ask(name, ent, now, st, conf, url=None):
    """What Claude Code says about this one server (it refreshes an expired OAuth access itself, under its own lock).
    Not for a one-time login (a run killed in the middle of the exchange loses it), not when switched off. One run
    asks again a minute later if it says "needs authentication"; the answer is kept for an hour while the stored login
    does not change, so a confirming pass runs nothing. Nothing of the output is kept but the verdict."""
    if not conf["ask_claude"] or short(name) in conf["one_time"]:
        return "later"
    rec = (st.setdefault("asked", {})).get(name)
    exp = ent.get("expiresAt") or 0
    if isinstance(rec, dict) and now - rec.get("at", 0) < ASK_EVERY and rec.get("exp") == exp:
        return rec.get("verdict") or "later"
    if refresh_busy(now):
        return "later"
    verdict = verdict_of(name, url)
    if verdict == "auth":
        time.sleep(RECHECK)
        verdict = verdict_of(name, url)
    st["asked"][name] = {"at": now, "exp": exp, "verdict": verdict}
    return verdict


def checkup(conf, now, st, hub, hub_st):
    """{servers: {name: {state, why, fix}}, logged} for what the hub gives to sessions; states: ok | lost | later | first.
    A live access is asked of the server itself (initialize); an expired one - of Claude Code, about this server only,
    and where it cannot tell (a hub catalog server) - of the hub's live sessions. A headers server - its helper runs
    and the server is asked with those headers. first - never logged in while sessions wait for it (not a loss)."""
    ignore, had, cover = conf["ignore"], set(st.get("had") or []), conf.get("cover")
    entries = oauth_entries(credentials())
    out, logged = {}, []
    catalog = set(catalog_servers()) if cover else None
    for name, url in sorted(served(hub_st, cover).items()):
        if ignored(name, ignore):
            continue
        fix = login_fix(name, hub, catalog)
        if cover and url and local_url(url):   # a server on this machine (a desktop app): nothing to log in to
            out[name] = {"state": "skip", "why": tr("why_local"), "by": "local"}
            continue
        ent = entries.get(name) or {}
        has = bool(ent.get("accessToken") or ent.get("refreshToken"))
        by = "conduit" if cover and short(name) in conf["read_only"] else "claude"
        if not has:
            if name in had:   # it was live with a stored login and the record is gone: something erased it
                out[name] = {"state": "lost", "why": tr("why_wiped"), "fix": fix, "by": by}
            elif session_verdict(name, hub_st, live=False) == "auth":   # never logged in, and sessions wait for it
                out[name] = {"state": "first", "why": tr("why_first"), "fix": fix, "by": by}
            elif cover and url:   # every server of the machine: one that needs no login, or one never logged in
                ok = probe_open(url)
                out[name] = ({"state": "skip", "why": tr("why_open"), "by": "none"} if ok
                             else {"state": "first", "why": tr("why_first"), "fix": fix, "by": by} if ok is False
                             else {"state": "later", "by": by})
            continue   # never logged in and nobody waits: a login nobody made is no loss
        logged.append(name)
        lose = lambda why: {"state": "lost", "why": why, "fix": fix, "by": by}
        if by == "conduit":   # refreshed by the hub's conduit module when used: a parallel refresh kills the pair
            out[name] = read_only_row(ent, now, lose)
            continue
        expires = (ent.get("expiresAt") or 0) / 1000
        row = {"state": "later"}
        if ent.get("accessToken") and (not expires or expires > now + 60):
            # the stored login of another address (a name that two servers share) never gets sent to this one
            ok, code = probe({**ent, "serverUrl": ent.get("serverUrl") or url}) if same_server(ent.get("serverUrl"), url) \
                else (None, None)
            if ok is False:
                row = lose(tr("why_rejected", code=code))
            elif ok:
                row = {"state": "ok"}
        elif ent.get("refreshToken"):
            said = ask(name, ent, now, st, conf, url)
            if said in ("unknown", "later") and not (short(name) in conf["one_time"] and said == "later" and
                                                     refresh_busy(now)):
                # Claude Code outside the hub does not know a catalog server: the word of the hub's live sessions -
                # their Claude Code refreshed this login itself, under its lock, when it connected
                # ("connected" only where Claude Code cannot tell at all: a session's word of its start may be old)
                word = session_verdict(name, hub_st)
                said = word if word == "auth" or (word and said == "unknown") else said
            row = lose(tr("why_auth")) if said == "auth" else {"state": "ok"} if said == "connected" else row
            days = (now - expires) / DAY if expires else 0
            if said == "unknown" and conf["stale_days"] and days >= conf["stale_days"]:
                row = {"state": "later", "why": tr("why_stale", days=int(days))}
        else:
            row = lose(tr("why_expired"))
        if row["state"] == "ok" and expires and not ent.get("refreshToken") and expires - now < conf["warn_days"] * DAY:
            row = lose(tr("why_expires", day=time.strftime("%d.%m", time.localtime(expires))))
        out[name] = {**row, "by": by} if cover else row
    for name, srv in sorted(header_servers(cover).items()):
        if ignored(name, ignore) or name in out:
            continue
        head, why = helper_headers(srv)
        fix = tr("fix_headers", name=name) if name in catalog_servers() else tr("fix_mcp", name=name)
        if head is None:
            row = {"state": "later"} if why == "later" else {"state": "lost", "why": why, "fix": fix}
        else:
            ok, code = probe_keyed(srv["url"], head)
            row = ({"state": "lost", "why": tr("why_headers", code=code), "fix": fix} if ok is False
                   else {"state": "ok"} if ok else {"state": "later"})
        out[name] = {**row, "by": "headers"} if cover else row
    if cover:
        programs(conf, now, out, logged, had, hub, catalog)
    else:   # 0.3.0 rows: no holder
        for r in out.values():
            r.pop("by", None)
    return {"servers": out, "logged": logged, "later_all": False}


def read_only_row(ent, now, lose):
    """A login another part of the hub refreshes (conduit): a live access is asked of the server, an expired one with a
    refresh token is the normal state between uses - never asked of claude, never refreshed here."""
    expires = (ent.get("expiresAt") or 0) / 1000
    if ent.get("accessToken") and (not expires or expires > now + 60):
        ok, code = probe(ent)
        return (lose(tr("why_rejected", code=code)) if ok is False else
                {"state": "ok", "by": "conduit"} if ok else {"state": "later", "by": "conduit"})
    if ent.get("refreshToken"):
        return {"state": "skip", "why": tr("why_read_only"), "by": "conduit"}
    return lose(tr("why_expired"))


def programs(conf, now, out, logged, had, hub, catalog):
    """cover: stdio servers - never started. mcp-remote: its stored login is read (never refreshed: mcp-remote does it
    at its start, auth-keeper ahead); the Microsoft 365 program is auth-keeper's; any other program is skipped."""
    for name, srv in sorted(cover_servers().items()):
        if name in out or ignored(name, conf["ignore"]) or not isinstance(srv, dict) or not srv.get("command"):
            continue
        url = remote_url(srv)
        holder = "auth-keeper" if conf["auth_keeper"] else "mcp-remote"
        fix = login_fix(name, hub, catalog)
        if url:
            tok = remote_tokens(url)
            if not tok or not (tok["access"] or tok["refresh"]):
                out[name] = ({"state": "lost", "why": tr("why_wiped"), "fix": fix, "by": holder} if name in had
                             else {"state": "first", "why": tr("why_first"), "fix": fix, "by": holder})
                continue
            logged.append(name)
            if tok["access"] and (not tok["expires"] or tok["expires"] > now + 60):
                ok, code = probe_with(url, {"Authorization": "Bearer " + tok["access"]})
                out[name] = ({"state": "lost", "why": tr("why_rejected", code=code), "fix": fix, "by": holder}
                             if ok is False else {"state": "ok" if ok else "later", "by": holder})
            elif tok["refresh"] and conf["auth_keeper"]:   # it refreshes 2 h ahead: expired means it did not
                out[name] = {"state": "lost", "why": tr("why_remote_expired"), "fix": fix, "by": holder}
            elif tok["refresh"]:
                out[name] = {"state": "later", "why": tr("why_remote_later"), "by": holder}
            else:
                out[name] = {"state": "lost", "why": tr("why_expired"), "fix": fix, "by": holder}
        elif "ms-365-mcp-server" in json.dumps(srv.get("args") or []) + str(srv.get("command")):
            out[name] = {"state": "skip", "by": "auth-keeper" if conf["auth_keeper"] else "program",
                         "why": tr("why_ms365") if conf["auth_keeper"] else tr("why_program")}
        else:
            out[name] = {"state": "skip", "why": tr("why_program"), "by": "program"}


def lost(res):
    return {n: s for n, s in res["servers"].items() if s["state"] == "lost"}


def confirmed(first, now, conf, st, hub, hub_st):
    """A new loss - a second pass CONFIRM_AFTER later, on a calm network: only what is lost both times. None - it shook
    in between: the pass does not count."""
    fresh = {n for n in lost(first) if not (st.get("alarms") or {}).get(n, {}).get("open")}
    if not fresh or conf.get("cover"):   # cover: the passes of the ladder confirm it, the first sign goes out now
        return first
    if not CALM.wait(CONFIRM_AFTER):
        return None
    since = CALM.shaken
    again = checkup(conf, now + CONFIRM_AFTER, st, hub, hub_st)
    if not CALM.steady(since):
        return None
    for n, s in again["servers"].items():
        if s["state"] == "lost" and n not in lost(first):
            again["servers"][n] = {"state": "later"}   # new in the second pass - the next pass confirms it
    return again


def calm_pass(conf, now, st, hub, hub_st):
    res = None
    for _ in range(TRIES):
        if not CALM.wait():
            return {"servers": {}, "later_all": True, "why": "unsettled"}
        since = CALM.shaken
        res = checkup(conf, now, st, hub, hub_st)
        if CALM.steady(since):
            done = confirmed(res, now, conf, st, hub, hub_st)
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


def alarms_step(st, servers, bad, conf, now, hold):
    """One alarm per server a day, kept in the state file: a server lost stays "open" until a pass sees it live; a loss
    again within a day of its last alarm is not told; the plugin as a whole says at most `max_alarms` messages a day,
    the rest waits for room. Returns (servers to tell in this message, the card is to be lifted)."""
    al = st.setdefault("alarms", {})
    st["sent"] = sent = [t for t in st.get("sent") or [] if now - t < DAY]
    hanging = lambda: any(a.get("open") and a.get("told") for a in al.values())
    before = hanging()
    for n, a in al.items():
        if a.get("open") and n not in bad and (n not in servers or servers[n]["state"] == "ok"):
            a["open"] = False   # proven live (a server in doubt keeps its alarm)
    for n in [n for n, a in al.items() if not a.get("open") and now - a.get("at", 0) >= DAY]:
        del al[n]
    fresh = []
    for n in sorted(bad):
        a = al.get(n) or {}
        if a.get("open"):
            continue
        if "at" in a and now - a["at"] < DAY:
            al[n] = {"open": True, "told": False, "at": a["at"]}   # lost again within a day: quiet
        else:
            fresh.append(n)
    told = []
    if fresh and not hold and len(sent) < conf["max_alarms"]:
        sent.append(now)
        told = fresh
        for n in told:
            al[n] = {"open": True, "told": True, "at": now}
    return told, before and not hanging()


MAX_TRACES = 20
KINDS = ("claude", "server")


def processes(server):
    """[{pid, started, kind}] - processes of Claude Code and of this MCP server now: kept in the plugin state when a
    one-time login is lost, to find later who refreshed it. Never the command line: only who, since when, what kind."""
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
        kind = ("claude" if re.search(r"(^|[\\/ ])claude(\.exe|\.cmd)?( |$)", low)
                else "server" if word in low or "mcp-remote" in low else None)
        if kind and pid != os.getpid():
            out.append({"pid": pid, "started": started, "kind": kind})
    return out[:40]


def order(bad, conf, use, ok_at):
    """Most important first: one-time logins (lost for good), servers sessions use right now, the ones live most
    recently."""
    return sorted(bad, key=lambda n: (short(n) not in conf["one_time"], n not in use, -float(ok_at.get(n) or 0), n))


def message(bad, dups, conf, use, ok_at):
    lines = []
    if bad:
        names = order(bad, conf, use, ok_at)
        lines += [tr("lost", names=", ".join(names))]
        lines += [tr("lost_line", name=n, why=bad[n]["why"], fix=bad[n]["fix"]) for n in names]
    if dups:
        lines += [tr("dup", names=", ".join(d["name"] for d in dups))] + [tr("dup_line", **d) for d in dups]
    return "\n".join(lines)


# ───── status and cleaning ─────

def status_path():
    """status.json next to the plugin: the page reads it from /ext/<plugin>/status.json (no-cache)."""
    return os.environ.get("MCP_KEEPER_STATUS") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "status.json")


def clean_status(data):
    """The view's data as a whitelist: per server a state and the time it was last live - nothing else, whatever an
    older version wrote there."""
    if not isinstance(data, dict):
        return None
    num = lambda x: x if isinstance(x, (int, float)) and not isinstance(x, bool) else None
    rows = {}
    for n, r in (data.get("servers") if isinstance(data.get("servers"), dict) else {}).items():
        if isinstance(r, dict) and r.get("state") in STATES:
            why = r.get("why") if isinstance(r.get("why"), str) and len(r["why"]) <= 160 and "://" not in r["why"] else None
            rows[str(n)] = {"state": r["state"], **({"ok_at": num(r.get("ok_at"))} if num(r.get("ok_at")) else {}),
                            **({"why": why} if why else {}), **({"by": r["by"]} if r.get("by") in HOLDERS else {})}
    return {"v": 1, "at": num(data.get("at")) or 0, "every": num(data.get("every")) or EVERY, "servers": rows}


def write_status(st):
    """The view's data - from the plugin state (a market update replaces the folder: rewritten at start too)."""
    if st.get("status"):
        write_json(status_path(), st["status"])


def start_clean():
    """At start: what 0.1.x kept goes away - the raw `claude mcp list` output and its lock, command lines of processes,
    free-form rows of the status; the old "told" lists become alarms (one hanging at the owner stays hanging)."""
    for name in ("list.out", "list.lock.json"):
        try:
            os.unlink(os.path.join(HOME, name))
        except OSError:
            pass
    st = load_state()
    for t in (st.get("traces") or {}).values():
        if isinstance(t, dict):
            t["processes"] = [{k: p[k] for k in ("pid", "started", "kind") if k in p}
                              for p in t.get("processes") or [] if isinstance(p, dict)]
    al = st.setdefault("alarms", {})
    old_at = st.get("told_at") if isinstance(st.get("told_at"), dict) else {}
    for n in st.pop("told", None) or []:
        al.setdefault(n, {"open": True, "told": True, "at": old_at.get(n) or 0})
    for k in ("told_at", "told_first", "told_first_at", "split"):
        st.pop(k, None)
    clean = clean_status(st.get("status"))
    st.pop("status", None)
    if clean:
        st["status"] = clean
    save_state(st)
    old = clean_status(read_json(status_path()))
    if old:
        write_json(status_path(), old)


def post_status(st, res, conf, now):
    """Rows for the view (status.json) every pass; one human line to the plugin channel when the picture changed."""
    ok_at = st.setdefault("ok_at", {})
    rows = {n: {"state": s["state"], "ok_at": ok_at.get(n), **({"why": s["why"]} if s.get("why") else {}),
                **({"by": s["by"]} if s.get("by") else {})}
            for n, s in sorted(res["servers"].items())}
    st["status"] = {"v": 1, "at": now, "every": conf["every"] / 60, "servers": rows}
    write_status(st)
    count = lambda *states: sum(r["state"] in states for r in rows.values())
    first = count("first")
    head = tr("status_first" if first else "status", ok=count("ok"), bad=count("lost"), later=count("later", "failed"),
              first=first)
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
    now = now or time.time()
    st["at"] = now
    res = calm_pass(conf, now, st, hub, hub_st)
    if res["later_all"]:
        save_state(st)
        put(f"ext.{NAME}.checked", {"later": True, "why": res.get("why")})
        return res
    servers = res["servers"]
    ok_at = st.setdefault("ok_at", {})
    for n, s in servers.items():
        if s["state"] == "ok":
            ok_at[n] = now
    # a login counts as "had" only on a live server: a record erased later is then told as erased
    st["had"] = sorted(set(st.get("had") or []) | {n for n in res["logged"] if servers.get(n, {}).get("state") == "ok"})
    res["dups"] = duplicates() if conf["check_duplicates"] else []
    bad = lost(res)
    put(f"ext.{NAME}.checked", {"ok": sorted(n for n, s in servers.items() if s["state"] == "ok"),
                                "lost": [{"name": n, "why": s.get("why")} for n, s in sorted(bad.items())],
                                "later": sorted(n for n, s in servers.items() if s["state"] in ("later", "failed")),
                                "skip": sorted(n for n, s in servers.items() if s["state"] == "skip"),
                                "dups": res["dups"]})
    post_status(st, res, conf, now)
    al = st.setdefault("alarms", {})
    for n in bad:   # a one-time login lost: who was running then - for finding the cause later
        if short(n) in conf["one_time"] and not al.get(n, {}).get("open"):
            traces = st.setdefault("traces", {})
            traces[n] = {"at": now, "why": bad[n].get("why"), "processes": processes(n)}
            for old in sorted(traces, key=lambda k: traces[k].get("at") or 0)[:-MAX_TRACES]:
                del traces[old]
    hold = bool(conf["quiet_until"] and now < conf["quiet_until"])
    if hold and st.get("hushed") != conf["quiet_until"]:   # quiet: the card goes, what is still lost is told after it
        if any(a.get("open") and a.get("told") for a in al.values()):
            put("owner.notify_resolved", {"id": NOTE_ID})
        for a in al.values():
            if a.get("told"):
                a["open"], a["at"] = False, 0
        st["hushed"] = conf["quiet_until"]
    use = in_use(hub_st)
    owner_bad = bad
    if conf["cover"]:   # the Hub Developer at the first sign, the owner only when it stays lost
        owner_bad = ladder(st, servers, bad, conf, now, hub_st, res)
    names, resolve = alarms_step(st, servers, owner_bad, conf, now, hold)
    if names:   # the owner - only a login that worked and stopped: one urgent message, the most important first
        put("owner.notify", {"id": NOTE_ID, "title": tr("title"), "topic": tr("lost_topic"),
                             "text": message({n: bad[n] for n in names}, [], conf, use, ok_at), "urgent": True,
                             "urgency": "now"})
        res["sessions"] = tell_sessions(order(names, conf, use, ok_at), use, st, now)
    elif resolve:
        put("owner.notify_resolved", {"id": NOTE_ID})
    # a duplicate blocks no work: one line a day to the Hub Developer, not to the owner
    if remember(st, "told_dup", sorted(d["name"] for d in res["dups"]), now, set()) == "tell":
        res["developer"] = developer(hub_st)
        put("message.received", {"to": res["developer"], "text": message({}, res["dups"], conf, use, ok_at)})
    # never logged in is no loss (owner 22.10): one line a day to the Hub Developer, who asks the owner for one login
    first = sorted(n for n, s in servers.items() if s["state"] == "first")
    if remember(st, "told_never", first, now, set()) == "tell":
        res["developer"] = developer(hub_st)
        put("message.received", {"to": res["developer"], "text": "\n".join(
            [tr("first", names=", ".join(first))] + [tr("first_line_fix", name=n, fix=servers[n].get("fix") or
                                                        login_fix(n, hub)) for n in first])})
    save_state(st)
    return res


def ladder(st, servers, bad, conf, now, hub_st, res):
    """cover: a lost login goes to the Hub Developer at once (what is seen, the holder, the fix; again not within a day),
    to the owner only after `owner_after` more passes still see it lost, never one auth-keeper holds (it tells the owner
    itself). A pass that cannot tell (later) neither counts nor resets. Returns the servers for the owner's alarm."""
    streak, warned = st.setdefault("streak", {}), st.setdefault("dev_warned", {})
    for n in list(streak):
        if n not in bad and (servers.get(n) or {}).get("state") != "later":
            del streak[n]
    for n in list(warned):
        if (servers.get(n) or {}).get("state") in (None, "ok", "skip", "first"):
            del warned[n]
    for n in bad:
        streak[n] = streak.get(n, 0) + 1
    new = sorted(n for n in bad if now - warned.get(n, -DAY) >= DAY)
    if new:
        res["developer"] = developer(hub_st)
        put("message.received", {"to": res["developer"], "text": "\n".join(
            [tr("dev", passes=conf["owner_after"])] +
            [tr("dev_line", name=n, by=bad[n].get("by") or "-", why=bad[n].get("why") or "-", fix=bad[n].get("fix") or "-")
             for n in new])})
        for n in new:
            warned[n] = now
    return {n: s for n, s in bad.items() if streak[n] > conf["owner_after"] and s.get("by") != "auth-keeper"}


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
    print(time.strftime("%H:%M:%S"), "mcp-keeper: live:", pick("ok"), "| lost:", pick("lost"), "| later:", pick("later"),
          "| first:", pick("first"), "| skip:", pick("skip"),
          "| duplicates:", ", ".join(d["name"] for d in res.get("dups") or []) or "-", flush=True)


def loop():
    start_clean()
    write_status(load_state())   # the view has its data right after a market update replaced the folder
    if time.time() - float(load_state().get("at") or 0) >= AGAIN_AFTER:
        time.sleep(START_DELAY)
        report(once())
    while True:
        conf = settings()
        wake, net_at = time.time() + conf["every"], mono()
        while time.time() < wake:   # sleep of the machine: go by the clock, not by a counter
            time.sleep(min(NET_STEP, max(1, wake - time.time())))
            due = mono() - net_at >= conf["net_every"]   # TCP to the outside - not every step
            CALM.look(net=due)
            if due:
                net_at = mono()
        report(once())


def main():
    try:
        from core import catalog
        catalog.pin_claude_dir(STATE)   # run by hand: the hub's Claude folder, not a personal CLAUDE_CONFIG_DIR
    except Exception:   # noqa: BLE001
        pass
    if "--once" in sys.argv[1:]:
        start_clean()
        report(once())
        return 0
    loop()


if __name__ == "__main__":
    sys.exit(main())
