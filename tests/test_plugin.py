"""mcp-keeper: no real claude, network or keychain - a fake `claude` script, a credentials file and patched probes."""
import http.server, json, os, shutil, signal, stat, subprocess, sys, tempfile, threading, time, unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = tempfile.mkdtemp(prefix="mcpk-")
os.environ["HUB_STATE_DIR"] = os.path.join(TMP, "state")   # never the live hub
os.environ["HUB_CONFIG"] = os.path.join(TMP, "hub.json")
os.environ["CLAUDE_CONFIG_DIR"] = os.path.join(TMP, "claude")
os.environ["MCP_KEEPER_CREDS"] = os.path.join(TMP, "creds.json")
os.environ["HUB_CLAUDE"] = os.path.join(TMP, "claude.sh")
os.environ["MCP_KEEPER_STATUS"] = os.path.join(TMP, "status.json")
os.environ.pop("HUB_EXTENSION", None)
sys.path.insert(0, ROOT)
import main  # noqa: E402

REAL_GET, REAL_PROCESSES, REAL_SLEEP = getattr(main, "mcp_get", None), main.processes, time.sleep   # before any patch
REAL_PROBE = main.probe
REAL_CURL = main.curl_status

NOW = 2_000_000_000.0


def write(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(data if isinstance(data, str) else json.dumps(data))


def read(path, raw=False):
    with open(path, encoding="utf-8", errors="replace") as f:
        return f.read() if raw else json.load(f)


class Base(unittest.TestCase):
    def setUp(self):
        for root, dirs, files in os.walk(TMP, topdown=False):
            for f in files:
                os.unlink(os.path.join(root, f))
        self.config({})
        os.makedirs(os.path.join(TMP, "claude"), exist_ok=True)
        self.facts = []
        self.asked = []    # names `claude mcp get` was asked about
        self.gets = {}     # name -> output of `claude mcp get <name>` (a list is consumed, the last repeats)
        self.probes = {}   # server url -> (ok, code)
        self.keyed = []    # (url, header names) a headers server was asked with
        patches = [
            mock.patch.object(main, "put", lambda t, d, entity=None: self.facts.append((t, d))),
            mock.patch.object(main, "mcp_get", self.fake_get),
            mock.patch.object(main, "probe", lambda ent: self.probes.get(ent.get("serverUrl"), (True, 200))),
            mock.patch.object(main, "probe_keyed", self.fake_keyed),
            mock.patch.object(main, "net_up", lambda: True),
            mock.patch.object(main, "processes", lambda name: [{"pid": 1, "started": "x", "kind": "claude"}]),
            mock.patch.object(main, "lang", lambda: "en"),
            mock.patch.object(main.time, "sleep", self.tick),
            mock.patch.object(main, "mono", lambda: self.clock),
            mock.patch.object(main, "drift", lambda: 0.0),
            mock.patch.object(main, "hub_state", lambda: self.hub),
            mock.patch.object(main, "curl_status", lambda *a: None),
            mock.patch.object(main, "RECHECK_ASYNC", False),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.clock = 100_000.0
        self.hub = {"system": ["hub-dev"], "sessions": {"hub": {"kind": "hub", "state": "idle"},
                                 "hub-dev": {"state": "idle"},
                                 "worker": {"state": "busy", "info": {"mcp": ["canva", "figma"]}},
                                 "sleepy": {"state": "parked", "info": {"mcp": ["canva"]}}}}
        main.CALM.shaken = self.clock - 10 * main.COOLDOWN   # a settled machine
        main.CALM.down = False
        main.CALM.mark = 0.0
        self.live()

    def config(self, extra, plugin=None):
        cfg = {"language": "en", "mcp": {n: {"server": {"type": "http", "url": u}} for n, u in URLS.items()}}
        cfg.update(extra)
        if plugin is not None:
            cfg["extensions"] = {"mcp-keeper": plugin}
        write(os.environ["HUB_CONFIG"], cfg)

    def fake_keyed(self, url, head):
        self.keyed.append((url, sorted(head)))
        return self.probes.get(url, (True, 200))

    def tick(self, seconds):
        self.clock += seconds

    def fake_get(self, name):
        self.asked.append(name)
        out = self.gets.get(name, G_OK)
        if isinstance(out, list):
            out = out.pop(0) if len(out) > 1 else out[0]
        return 0, out

    def live(self, expires=None, **extra):
        """Stored logins of the catalog servers: a live access (expires in an hour) unless `extra` says otherwise."""
        recs = {}
        for n, u in URLS.items():
            recs[n] = {"serverUrl": u, "accessToken": "tok-" + n, "refreshToken": "ref-" + n,
                       "expiresAt": ((NOW + 3600) if expires is None else expires) * 1000}
            recs[n].update(extra.get(n, {}))
        self.creds(**recs)

    def creds(self, **entries):
        write(os.environ["MCP_KEEPER_CREDS"], {"mcpOAuth": {f"{n}|abc": {"serverName": n, **e} for n, e in entries.items()}})

    def notes(self):
        return [d for t, d in self.facts if t == "owner.notify"]

    def dev(self):
        return [d for t, d in self.facts if t == "message.received" and d["to"] == "hub-dev"]

    def assertUnclear(self, row, msg=None):
        """No clear answer after the retries: the view says lost with the reason, nobody is alarmed for it."""
        self.assertEqual(row["state"], "lost", msg)
        self.assertTrue(row.get("unclear"), msg)
        self.assertIn("no clear answer after", row["why"], msg)

    def run_once(self, now=NOW):
        self.facts.clear()
        return main.once(now)

    def reject(self, *names):
        for n in names:
            self.probes[URLS[n]] = (False, 401)

    def accept(self, *names):
        for n in names:
            self.probes.pop(URLS[n], None)


URLS = {"canva": "https://mcp.canva.com/mcp", "figma": "https://mcp.figma.com/mcp", "mobbin": "https://mcp.mobbin.com/mcp",
        "notion": "https://mcp.notion.com/mcp"}
G_OK = "figma:\n  Status: ✓ Connected\n  Type: http\n"
G_AUTH = "figma:\n  Status: ! Needs authentication\n  Type: http\n"
G_FAIL = "figma:\n  Status: ✗ Failed to connect\n"


class Parse(Base):
    def test_get_status(self):
        self.assertEqual(main.parse_get(G_OK), "connected")
        self.assertEqual(main.parse_get(G_AUTH), "auth")
        self.assertEqual(main.parse_get(G_FAIL), "failed")
        self.assertEqual(main.parse_get("! Connected · tools fetch failed — Request timed out"), "connected")
        self.assertEqual(main.parse_get("No MCP server named \"figma\". Configured servers: a"), "unknown")
        self.assertIsNone(main.parse_get(""))


class Served(Base):
    def test_only_what_the_hub_gives_sessions(self):
        # a stray login of Claude Code outside the hub is not checked
        d = os.environ["CLAUDE_CONFIG_DIR"]
        write(os.path.join(d, ".claude.json"), {"mcpServers": {"stray": {"type": "http", "url": "https://stray/mcp"}}})
        self.live()
        recs = read(os.environ["MCP_KEEPER_CREDS"])
        recs["mcpOAuth"]["stray|x"] = {"serverName": "stray", "serverUrl": "https://stray/mcp", "accessToken": "t"}
        write(os.environ["MCP_KEEPER_CREDS"], recs)
        self.reject("canva")
        self.probes["https://stray/mcp"] = (False, 401)
        res = self.run_once()
        self.assertEqual(sorted(res["servers"]), ["canva", "figma", "mobbin", "notion"])

    def test_plugin_server_of_a_session_is_checked(self):
        self.hub["sessions"]["worker"]["info"]["mcp"].append("plugin:slack:slack")
        recs = read(os.environ["MCP_KEEPER_CREDS"])
        recs["mcpOAuth"]["plugin:slack:slack|x"] = {"serverName": "plugin:slack:slack", "serverUrl": "https://s/mcp",
                                                    "accessToken": "t", "refreshToken": "r",
                                                    "expiresAt": (NOW + 3600) * 1000}
        write(os.environ["MCP_KEEPER_CREDS"], recs)
        self.assertEqual(self.run_once()["servers"]["plugin:slack:slack"]["state"], "ok")

    def test_stdio_server_is_never_started(self):
        self.config({"mcp": {"files": {"server": {"command": "npx", "args": ["-y", "files-mcp"]}}}})
        self.hub["sessions"]["worker"]["info"]["mcp"] = ["files"]
        with mock.patch.object(subprocess, "Popen", side_effect=AssertionError("started a process")):
            res = self.run_once()
        self.assertEqual(res["servers"]["files"], {"state": "ok"})   # not started: a live session says it is connected
        self.assertEqual(self.asked, [])

    def test_header_server_is_not_judged_by_a_leftover_login(self):
        self.config({"mcp": {"buffer": {"server": {"type": "http", "url": "https://b/mcp", "headers": {"X-Api-Key": "y"}}}}})
        recs = read(os.environ["MCP_KEEPER_CREDS"])
        recs["mcpOAuth"]["buffer|x"] = {"serverName": "buffer", "serverUrl": "https://b/mcp", "accessToken": "old"}
        write(os.environ["MCP_KEEPER_CREDS"], recs)
        self.probes["https://b/mcp"] = (False, 401)    # the old token is dead ...
        self.hub["sessions"]["worker"]["info"]["mcp"].append("buffer")
        with mock.patch.object(main, "probe", side_effect=AssertionError("the leftover token was sent")):
            res = self.run_once()
        # ... and the server is judged by its own headers only
        self.assertEqual(self.keyed, [("https://b/mcp", ["X-Api-Key"])] * len(self.keyed))
        self.assertEqual(res["servers"]["buffer"]["state"], "lost")


class Pass(Base):
    def test_all_live_no_alarm_and_status_post(self):
        res = self.run_once()
        self.assertEqual({s["state"] for s in res["servers"].values()}, {"ok"})
        self.assertEqual(self.notes(), [])
        posts = [d for t, d in self.facts if t == "ext.post"]
        self.assertEqual(posts, [{"plugin": "mcp-keeper", "text": "MCP: 4 connected, 0 lost"}])   # a human line only
        with open(os.environ["MCP_KEEPER_STATUS"], encoding="utf-8") as f:
            self.assertEqual(json.load(f)["servers"]["canva"]["state"], "ok")   # the view's data - the file
        self.run_once(NOW + 900)
        self.assertEqual([t for t, d in self.facts if t == "ext.post"], [])   # same picture - no new post
        self.assertEqual(self.asked, [], "a live access is never a reason to run claude")

    def test_rejected_access_is_urgent_one_message(self):
        self.run_once(NOW)
        self.reject("canva")
        self.run_once(NOW + 900)
        notes = self.notes()
        self.assertEqual(len(notes), 1)
        self.assertTrue(notes[0]["urgent"])
        self.assertIn("HTTP 401", notes[0]["text"])
        self.assertIn("hub mcp login hub canva", notes[0]["text"])
        sent = [d for t, d in self.facts if t == "message.received"]
        self.assertEqual([d["to"] for d in sent], ["worker"])   # busy with canva; the parked one is not woken

    def test_network_doubt_is_not_lost(self):
        self.probes[URLS["figma"]] = (None, None)
        res = self.run_once()
        self.assertUnclear(res["servers"]["figma"])
        self.assertEqual(self.notes(), [])

    def test_expired_access_asks_claude_about_this_server_only(self):
        self.live(expires=NOW - 60, figma={"expiresAt": (NOW - 60) * 1000})
        self.live(expires=NOW + 3600)
        recs = read(os.environ["MCP_KEEPER_CREDS"])
        recs["mcpOAuth"]["figma|abc"]["expiresAt"] = (NOW - 60) * 1000
        write(os.environ["MCP_KEEPER_CREDS"], recs)
        res = self.run_once()
        self.assertEqual(self.asked, ["figma"])
        self.assertEqual(res["servers"]["figma"]["state"], "ok")

    def expire(self, *names):
        recs = read(os.environ["MCP_KEEPER_CREDS"])
        for n in names:
            recs["mcpOAuth"][n + "|abc"]["expiresAt"] = (NOW - 60) * 1000
        write(os.environ["MCP_KEEPER_CREDS"], recs)

    def test_claude_says_needs_authentication_twice_is_lost(self):
        self.expire("figma")
        self.gets["figma"] = G_AUTH
        res = self.run_once()
        self.assertEqual(res["servers"]["figma"]["state"], "lost")
        self.assertEqual(self.asked, ["figma", "figma", "figma"][:len(self.asked)])
        self.assertEqual(len(self.notes()), 1)

    def test_single_auth_answer_is_rechecked(self):
        self.expire("figma")
        self.gets["figma"] = [G_AUTH, G_OK]
        res = self.run_once()
        self.assertEqual(res["servers"]["figma"]["state"], "ok")
        self.assertEqual(self.notes(), [])

    def test_claude_is_asked_at_most_once_an_hour_per_server(self):
        self.expire("figma")
        self.gets["figma"] = G_FAIL
        self.run_once(NOW)
        n = len(self.asked)
        res = self.run_once(NOW + 900)
        self.assertEqual(len(self.asked), n, "no second run within the hour")
        self.assertUnclear(res["servers"]["figma"])
        self.run_once(NOW + 3700)
        self.assertGreater(len(self.asked), n)

    def test_one_time_login_never_goes_through_claude(self):
        self.expire("canva")
        res = self.run_once()
        self.assertNotIn("canva", self.asked)
        self.assertUnclear(res["servers"]["canva"])

    def test_ask_claude_off(self):
        self.config({}, {"ask_claude": False})
        self.expire("figma")
        res = self.run_once()
        self.assertEqual(self.asked, [])
        self.assertUnclear(res["servers"]["figma"])

    def test_expired_without_refresh_is_lost(self):
        self.live(figma={"refreshToken": None, "expiresAt": (NOW - 60) * 1000})
        res = self.run_once()
        self.assertEqual(res["servers"]["figma"]["state"], "lost")

    def test_wiped_record_of_a_live_server(self):
        self.run_once(NOW)
        recs = read(os.environ["MCP_KEEPER_CREDS"])
        recs["mcpOAuth"]["canva|abc"] = {"serverName": "canva", "serverUrl": URLS["canva"]}   # tokens gone
        write(os.environ["MCP_KEEPER_CREDS"], recs)
        res = self.run_once(NOW + 900)
        self.assertEqual(res["servers"]["canva"]["state"], "lost")
        self.assertIn("erased", self.notes()[0]["text"])

    def test_one_time_loss_keeps_a_trace_without_command_lines(self):
        self.run_once(NOW)
        self.reject("canva")
        self.run_once(NOW + 900)
        trace = main.load_state()["traces"]["canva"]
        self.assertEqual(trace["at"], NOW + 900)
        self.assertEqual(trace["processes"], [{"pid": 1, "started": "x", "kind": "claude"}])

    def test_most_important_first_in_one_message(self):
        self.reject("figma", "mobbin", "canva")
        self.run_once(NOW)
        notes = self.notes()
        self.assertEqual(len(notes), 1)
        lines = notes[0]["text"].splitlines()
        self.assertTrue(lines[1].startswith("• canva"), lines)    # one-time login first
        self.assertTrue(lines[2].startswith("• figma"), lines)    # then the one a working session uses

    def test_never_logged_in_server_is_not_an_alarm(self):
        self.creds(canva={"serverUrl": URLS["canva"], "accessToken": "t", "refreshToken": "r",
                          "expiresAt": (NOW + 3600) * 1000})
        res = self.run_once()
        self.assertEqual(res["servers"]["figma"], {"state": "ok"})   # a session holds it connected, no alarm
        self.assertEqual((self.notes(), self.dev()), ([], []))

    def test_ignore_lists(self):
        self.config({"mcp_auth_ignore": ["canva"]}, {"ignore": ["figma"]})
        self.reject("canva", "figma")
        res = self.run_once(NOW)
        self.assertEqual(sorted(res["servers"]), ["canva", "figma", "mobbin", "notion"])
        self.assertEqual({n for n, s in res["servers"].items() if s.get("ignored")}, {"canva", "figma"})

    def test_unsettled_machine_does_not_ask_claude(self):
        self.expire("figma")
        main.CALM.shaken = self.clock   # just woke up
        with mock.patch.object(main, "MAX_WAIT", 0):
            res = self.run_once(NOW)
        self.assertTrue(res["later_all"])
        self.assertEqual(self.asked, [])

    def test_quiet_until_holds_alarms(self):
        self.config({}, {"quiet_until": NOW + 10_000})
        self.reject("canva")
        self.run_once(NOW)
        self.assertEqual(self.notes(), [])

    def test_duplicate_check_is_off_by_default(self):
        self.plugin_dup()
        self.run_once(NOW)
        self.assertEqual(self.dev(), [])
        self.assertEqual(main.duplicates(), [{"name": "canva", "plugin": "plugin:canva:canva"}])   # found, just not told

    def plugin_dup(self):
        d = os.environ["CLAUDE_CONFIG_DIR"]
        inst = os.path.join(TMP, "plug")
        write(os.path.join(inst, ".mcp.json"), {"mcpServers": {"canva": {"type": "http", "url": "https://mcp.canva.com/mcp"}}})
        write(os.path.join(d, "settings.json"), {"enabledPlugins": {"canva@market": True}})
        write(os.path.join(d, "plugins", "installed_plugins.json"), {"plugins": {"canva@market": [{"installPath": inst}]}})
        write(os.path.join(d, ".claude.json"), {"mcpServers": {"canva": {"type": "http", "url": "https://mcp.canva.com/mcp/"}}})
        self.config({"mcp": {}})   # the hub catalog does not serve canva: the user entry hides the plugin's

    def test_duplicate_of_plugin_server_when_asked_for(self):
        self.plugin_dup()
        self.config({"mcp": {}}, {"check_duplicates": True})
        self.assertEqual(main.duplicates(), [{"name": "canva", "plugin": "plugin:canva:canva"}])
        self.run_once(NOW)
        self.assertEqual(self.notes(), [])   # a duplicate is the Hub Developer's, not an urgent alarm to the owner
        self.assertIn("claude mcp remove canva -s user", self.dev()[0]["text"])
        self.run_once(NOW + 900)
        self.assertEqual(self.dev(), [])   # once a day

    def test_catalog_server_is_not_a_duplicate(self):
        self.plugin_dup()
        self.config({"mcp": {"canva": {"server": {"url": "https://mcp.canva.com/mcp"}}}})
        self.assertEqual(main.duplicates(), [])


class Alarms(Base):
    """One alarm per server per day, kept on disk; a cap of alarms a day for the whole plugin."""

    def test_open_alarm_is_not_repeated_hourly(self):
        self.reject("figma")
        self.run_once(NOW)
        self.assertEqual(len(self.notes()), 1)
        for step in range(1, 30):   # a day of passes every 15 minutes
            self.run_once(NOW + 900 * step)
            self.assertEqual(self.notes(), [], step)

    def test_open_alarm_survives_a_restart(self):
        self.reject("figma")
        self.run_once(NOW)
        main.CALM.shaken = self.clock - 10 * main.COOLDOWN
        self.run_once(NOW + 900)   # a new process reads the same state file
        self.assertEqual(self.notes(), [])
        self.assertIn("figma", main.load_state()["alarms"])

    def test_recovery_lifts_it_and_a_new_loss_within_a_day_stays_quiet(self):
        self.reject("figma")
        self.run_once(NOW)
        self.accept("figma")
        self.run_once(NOW + 900)
        self.assertIn(("owner.notify_resolved", {"id": "mcp"}), self.facts)
        self.reject("figma")
        self.run_once(NOW + 1800)
        self.assertEqual(self.notes(), [], "less than a day since the last alarm of this server")

    def test_lost_again_after_recovery_and_a_day_is_told(self):
        self.reject("figma")
        self.run_once(NOW)
        self.accept("figma")
        self.run_once(NOW + 900)
        self.reject("figma")
        self.live(expires=NOW + main.DAY + 9000)
        self.run_once(NOW + main.DAY + 1000)
        self.assertEqual(len(self.notes()), 1)

    def test_still_lost_without_recovery_is_not_repeated_after_a_day_either(self):
        self.reject("figma")
        self.run_once(NOW)
        self.run_once(NOW + 3 * main.DAY)
        self.assertEqual(self.notes(), [])

    def test_alarms_a_day_are_capped(self):
        self.config({}, {"max_alarms": 2})
        told = 0
        for i, n in enumerate(["canva", "figma", "mobbin", "notion"]):
            self.reject(n)
            self.run_once(NOW + 900 * i)
            told += len(self.notes())
        self.assertEqual(told, 2)
        self.live(expires=NOW + main.DAY + 9000)
        self.run_once(NOW + main.DAY + 5000)   # the day passed: the held ones go in one message
        notes = self.notes()
        self.assertEqual(len(notes), 1)
        self.assertIn("mobbin", notes[0]["text"])
        self.assertIn("notion", notes[0]["text"])

    def test_default_cap_is_not_zero_and_not_every_pass(self):
        self.assertEqual(main.settings()["max_alarms"], 6)

    def test_new_servers_of_one_pass_share_a_message(self):
        self.reject("figma", "mobbin")
        self.run_once(NOW)
        self.assertEqual(len(self.notes()), 1)
        self.assertEqual(len(main.load_state()["sent"]), 1)


class Secrets(Base):
    SECRET = "sk-live-0123456789abcdefghijklmnop"

    def test_processes_keep_no_command_lines(self):
        ps = ("  101 Mon Oct 26 10:00:00 2026 /usr/local/bin/claude mcp get canva --token=short --header 'Authorization: "
              "Bearer " + self.SECRET + "'\n"
              "  102 Mon Oct 26 10:00:01 2026 npx -y mcp-remote https://mcp.canva.com/mcp?key=" + self.SECRET + "\n"
              "  103 Mon Oct 26 10:00:02 2026 /bin/zsh -l\n")
        if os.name == "nt":   # Windows: the same processes as PowerShell's JSON
            ps = json.dumps([{"ProcessId": int(l.split()[0]), "CreationDate": "2026-10-26",
                              "CommandLine": l.split(" 2026 ", 1)[1]} for l in ps.splitlines()])
        done = subprocess.CompletedProcess([], 0, stdout=ps.encode(), stderr=b"")
        with mock.patch.object(main.subprocess, "run", lambda *a, **k: done):
            rows = REAL_PROCESSES("canva")
        self.assertTrue(rows)
        blob = json.dumps(rows)
        for bad in ("token", "short", "Bearer", self.SECRET, "https://", "mcp-remote", "--"):
            self.assertNotIn(bad, blob)
        self.assertEqual({r["kind"] for r in rows}, {"claude", "server"})

    def test_state_and_status_never_hold_secrets(self):
        self.live(canva={"accessToken": self.SECRET, "refreshToken": "r-" + self.SECRET})
        self.reject("canva")
        self.run_once(NOW)
        with open(os.environ["MCP_KEEPER_STATUS"], encoding="utf-8") as f:
            status = f.read()
        with open(os.path.join(main.HOME, "state.json"), encoding="utf-8") as f:
            state = f.read()
        files = []
        for root, _, names in os.walk(main.HOME):
            for n in names:
                files.append(read(os.path.join(root, n), raw=True))
        for text in files + [status, state]:
            self.assertNotIn(self.SECRET, text)
            self.assertNotIn("://", text)   # no address, no query
            self.assertNotIn("Bearer", text)

    def test_nothing_of_claude_output_is_stored(self):
        self.live(figma={"expiresAt": (NOW - 60) * 1000})
        self.gets["figma"] = "figma: npx -y x --token " + self.SECRET + "\n  Status: ✓ Connected\n"
        self.run_once(NOW)
        for root, _, names in os.walk(main.HOME):
            for n in names:
                self.assertNotIn(self.SECRET, read(os.path.join(root, n), raw=True))
        self.assertFalse(os.path.exists(os.path.join(main.HOME, "list.out")))

    def test_old_files_are_cleaned_at_start(self):
        write(os.path.join(main.HOME, "list.out"), "figma: npx x --token " + self.SECRET)
        write(os.path.join(main.HOME, "list.lock.json"), {"pid": 1, "at": 1})
        write(os.path.join(main.HOME, "state.json"), {
            "traces": {"canva": {"at": 1, "why": "x", "processes": [{"pid": 7, "started": "t", "cmd": "claude --token " + self.SECRET}]}},
            "told": ["figma"], "told_at": {"figma": 5}, "told_first": ["x"], "told_first_at": {"x": 1}, "split": True,
            "status": {"v": 1, "at": 5, "every": 15, "servers": {"figma": {"state": "lost", "ok_at": 3, "why": "http://x/?k=" + self.SECRET,
                                                                           "cmd": "npx --token " + self.SECRET}}}})
        write(os.environ["MCP_KEEPER_STATUS"], {"v": 1, "servers": {"figma": {"state": "ok", "cmd": "npx --token " + self.SECRET}}})
        main.start_clean()
        self.assertFalse(os.path.exists(os.path.join(main.HOME, "list.out")))
        self.assertFalse(os.path.exists(os.path.join(main.HOME, "list.lock.json")))
        with open(os.path.join(main.HOME, "state.json"), encoding="utf-8") as f:
            state = f.read()
        with open(os.environ["MCP_KEEPER_STATUS"], encoding="utf-8") as f:
            status = f.read()
        self.assertNotIn(self.SECRET, state + status)
        self.assertNotIn("cmd", state + status)
        self.assertEqual(json.loads(state)["traces"]["canva"]["processes"], [{"pid": 7, "started": "t"}])
        self.assertTrue(json.loads(state)["alarms"]["figma"]["open"], "an alarm hanging at the owner stays hanging")

    def test_status_rows_are_a_whitelist(self):
        got = main.clean_status({"v": 1, "at": 1, "every": 15,
                                 "servers": {"a": {"state": "ok", "ok_at": 2, "args": ["--token", "x"], "url": "https://u?k=1"},
                                             "b": {"state": "weird"}, "c": "text"}})
        self.assertEqual(got["servers"], {"a": {"state": "ok", "ok_at": 2, "title": "A"}})
        got = main.clean_status({"servers": {"a": {"state": "lost", "can_login": True}, "b": {"state": "lost", "can_login": "yes"}}})
        self.assertEqual(got["servers"], {"a": {"state": "lost", "title": "A", "can_login": True},
                                          "b": {"state": "lost", "title": "B"}})
        got = main.clean_status({"servers": {"a": {"state": "ok", "can_switch": True, "title": "Mobbin"},
                                             "b": {"state": "skip", "ignored": True, "title": "x" * 99},
                                             "c": {"state": "ok", "can_switch": "yes", "ignored": 1}}})
        self.assertEqual(got["servers"], {"a": {"state": "ok", "title": "Mobbin", "can_switch": True},
                                          "b": {"state": "skip", "title": "B", "ignored": True},
                                          "c": {"state": "ok", "title": "C"}})


class Token(Base):
    """The access token goes only to https (http - only to this machine) and is never carried through a redirect."""

    def serve(self, handler):
        class Read(handler):
            # read the body first: on Windows a socket closed with unread data sends a reset, the client sees no answer
            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                handler.do_POST(self)
        srv = http.server.HTTPServer(("127.0.0.1", 0), Read)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        return srv

    def test_url_rules(self):
        for ok in ("https://mcp.example.com/mcp", "http://127.0.0.1:8000/mcp", "http://localhost/mcp", "http://[::1]:1/x"):
            self.assertTrue(main.safe_url(ok), ok)
        for bad in ("http://mcp.example.com/mcp", "ftp://x/y", "https://user:pw@mcp.example.com/", "https:///x", "",
                    "http://127.0.0.1.evil.com/mcp", "file:///etc/passwd"):
            self.assertFalse(main.safe_url(bad), bad)

    def test_plain_http_to_a_foreign_host_sends_nothing(self):
        with mock.patch.object(main.urllib.request.OpenerDirector, "open", side_effect=AssertionError("sent")):
            self.assertEqual(REAL_PROBE({"serverUrl": "http://mcp.example.com/mcp", "accessToken": "secret"}), (None, None))

    def test_http_to_localhost_is_sent(self):
        seen = []

        class H(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                seen.append(self.headers.get("Authorization"))
                self.send_response(200)
                self.end_headers()

            def log_message(self, *a):
                pass

        srv = self.serve(H)
        got = REAL_PROBE({"serverUrl": f"http://127.0.0.1:{srv.server_port}/mcp", "accessToken": "secret"})
        self.assertEqual(got, (True, 200))
        self.assertEqual(seen, ["Bearer secret"])

    def test_redirect_is_not_followed_and_the_token_does_not_travel(self):
        seen = []

        class Target(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                seen.append(self.headers.get("Authorization"))
                self.send_response(200)
                self.end_headers()

            do_GET = do_POST

            def log_message(self, *a):
                pass

        target = self.serve(Target)

        class Redirect(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                self.send_response(307)
                self.send_header("Location", f"http://127.0.0.1:{target.server_port}/steal")
                self.end_headers()

            def log_message(self, *a):
                pass

        front = self.serve(Redirect)
        got = REAL_PROBE({"serverUrl": f"http://127.0.0.1:{front.server_port}/mcp", "accessToken": "secret"})
        self.assertEqual(got, (None, 307), "a redirect is no verdict about the login")
        self.assertEqual(seen, [], "the second address got nothing")

    def test_login_of_another_address_is_not_sent(self):
        self.creds(figma={"serverUrl": "https://other.example.com/mcp", "accessToken": "t", "refreshToken": "r",
                          "expiresAt": (NOW + 3600) * 1000})
        with mock.patch.object(main, "probe", side_effect=AssertionError("sent")):
            res = main.checkup(main.settings(), NOW, {}, "hub", self.hub)
        self.assertEqual(res["servers"]["figma"]["state"], "later")


class Unknown(Base):
    """Catalog servers are not in ~/.claude.json: `claude mcp get` does not know them - that is not a verdict."""

    def setUp(self):
        super().setUp()
        self.hub["sessions"]["worker"]["info"]["mcp"] = ["canva"]   # no session says a word about figma

    def expire(self, *names):
        recs = read(os.environ["MCP_KEEPER_CREDS"])
        for n in names:
            recs["mcpOAuth"][n + "|abc"]["expiresAt"] = (NOW - 60) * 1000
        write(os.environ["MCP_KEEPER_CREDS"], recs)

    def test_unknown_to_claude_is_later_not_ok_not_lost(self):
        self.expire("figma")
        self.gets["figma"] = 'No MCP server named "figma". Configured servers: github'
        res = self.run_once()
        self.assertUnclear(res["servers"]["figma"])
        self.assertEqual(self.notes(), [])

    def test_a_name_of_another_server_is_no_answer(self):
        self.expire("figma")
        self.gets["figma"] = "figma:\n  Status: \u2713 Connected\n  Type: http\n  URL: https://elsewhere.example.com/mcp\n"
        res = self.run_once()
        self.assertUnclear(res["servers"]["figma"])

    def test_same_address_answer_counts(self):
        self.expire("figma")
        self.gets["figma"] = "figma:\n  Status: \u2713 Connected\n  URL: https://mcp.figma.com/mcp/\n"
        self.assertEqual(self.run_once()["servers"]["figma"]["state"], "ok")

    def test_long_expired_unknown_shows_why_in_the_view(self):
        self.expire("figma")
        self.gets["figma"] = "No MCP server named figma"
        res = self.run_once(NOW + 3 * 86400)
        self.assertUnclear(res["servers"]["figma"])
        self.assertIn("3 days", res["servers"]["figma"]["why"])
        self.assertEqual(self.notes(), [])
        self.assertIn("days ago", read(os.environ["MCP_KEEPER_STATUS"])["servers"]["figma"]["why"])

    def test_stale_days_zero_switches_it_off(self):
        self.config({}, {"stale_days": 0})
        self.expire("figma")
        self.gets["figma"] = "No MCP server named figma"
        self.assertNotIn("days", self.run_once(NOW + 9 * 86400)["servers"]["figma"]["why"])


class Refresh(Base):
    """The plugin never exchanges a refresh token itself and keeps away from a running exchange of Claude Code."""

    def test_no_token_exchange_in_the_code(self):
        with open(os.path.join(ROOT, "main.py"), encoding="utf-8") as f:
            src = f.read()
        for word in ("grant_type", "token_endpoint", "/oauth/token", "refresh_token="):
            self.assertNotIn(word, src)

    def test_refresh_token_value_is_never_sent(self):
        sent = []

        def spy(url, data=None, headers=None, **kw):
            sent.append((data, headers))
            raise OSError("no network")

        Unknown.expire.__get__(self)("figma")
        self.gets["figma"] = G_OK
        with mock.patch.object(main.urllib.request, "Request", spy):
            self.run_once()
        self.assertEqual(sent, [])   # an expired access is asked of Claude Code, the plugin sends nothing

    def test_one_time_defaults_cannot_be_switched_off(self):
        self.config({}, {"one_time": []})
        self.assertIn("canva", main.settings()["one_time"])
        self.config({}, {"one_time": ["extra"]})
        self.assertEqual(main.settings()["one_time"], {"canva", "extra"})

    def test_one_time_stays_out_of_claude_with_empty_setting(self):
        self.config({}, {"one_time": []})
        Unknown.expire.__get__(self)("canva")
        res = self.run_once()
        self.assertNotIn("canva", self.asked)
        self.assertUnclear(res["servers"]["canva"])

    def lock(self, age):
        path = os.path.join(TMP, "claude", "mcp-refresh-abc123.lock")
        shutil.rmtree(path, ignore_errors=True)
        os.makedirs(path)
        os.utime(path, (NOW - age, NOW - age))
        self.addCleanup(shutil.rmtree, path, True)

    def test_running_exchange_of_claude_code_holds_the_question(self):
        Unknown.expire.__get__(self)("figma")
        self.lock(10)
        res = self.run_once()
        self.assertEqual(self.asked, [])
        self.assertUnclear(res["servers"]["figma"])

    def test_old_lock_does_not_hold(self):
        Unknown.expire.__get__(self)("figma")
        self.lock(2 * main.LOCK_FRESH)
        self.run_once()
        self.assertEqual(self.asked, ["figma"])


class Net(Base):
    """The TCP check of the network is rare in the idle loop; the sleep check (a clock) stays frequent."""

    def test_look_without_net_makes_no_tcp(self):
        calls = []
        with mock.patch.object(main, "net_up", lambda: calls.append(1) or True):
            main.CALM.look(net=False)
            self.assertEqual(calls, [])
            main.CALM.look()
            self.assertEqual(calls, [1])

    def test_sleep_is_still_seen_without_net(self):
        main.CALM.shaken = self.clock - 10 * main.COOLDOWN
        main.CALM.mark = 0.0
        with mock.patch.object(main, "drift", lambda: 1000.0):
            main.CALM.look(net=False)
        self.assertEqual(main.CALM.shaken, self.clock)

    def test_idle_loop_asks_the_network_every_net_check_minutes(self):
        self.config({}, {"every_minutes": 15, "net_check_minutes": 5})
        self.assertEqual(main.settings()["net_every"], 300)
        self.assertEqual(main.settings()["every"], 900)
        wall = [NOW]
        net = []

        def sleep(sec):
            self.clock += sec
            wall[0] += sec

        class Stop(Exception):
            pass

        passes = []
        def once():
            passes.append(1)
            if len(passes) > 1:
                raise Stop
            return {"servers": {}, "later_all": True}

        with mock.patch.object(main.time, "sleep", sleep), mock.patch.object(main.time, "time", lambda: wall[0]), \
                mock.patch.object(main, "net_up", lambda: net.append(1) or True), mock.patch.object(main, "once", once), \
                mock.patch.object(main, "start_clean", lambda: None), mock.patch.object(main, "write_status", lambda s: None), \
                mock.patch.object(main, "load_state", lambda: {"at": wall[0]}), mock.patch.object(main, "report", lambda r: None),                 mock.patch.object(main, "login_watch", lambda: None), mock.patch.object(main, "start_auto", lambda: None):
            # a real login_watch thread outlives the test and writes login.json under the next ones (Windows: flaky)
            with self.assertRaises(Stop):
                main.loop()
        self.assertEqual(len(net), 6, "two rounds of 15 minutes: 6 TCP checks, not 60")

    def test_default_is_five_minutes(self):
        self.assertEqual(main.settings()["net_every"], main.NET_EVERY * 60)


@unittest.skipIf(os.name == "nt", "process groups")
class Limit(Base):
    """`claude mcp get` runs under a time limit; a run that goes over is killed with everything it started."""

    def fake_claude(self, body):
        path = os.environ["HUB_CLAUDE"]
        write(path, "#!/bin/sh\n" + body)
        os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR)

    def alive(self, pid):
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        return True

    def test_timeout_kills_the_whole_tree(self):
        pids = os.path.join(TMP, "pids")
        self.fake_claude(f'sleep 300 &\necho $! >> {pids}\n(sleep 300 & echo $! >> {pids}; wait) &\necho $$ >> {pids}\nwait\n')
        with mock.patch.object(main, "GET_TIMEOUT", 1), mock.patch.object(main, "KILL_GRACE", 0.2):
            got = REAL_GET("figma")
        self.assertEqual(got[0], 124)
        REAL_SLEEP(0.3)
        started = [int(x) for x in open(pids).read().split()]
        self.assertGreaterEqual(len(started), 3)
        for pid in started:
            self.assertFalse(self.alive(pid), f"{pid} survived")

    def test_normal_run_returns_the_output_in_memory(self):
        self.fake_claude('echo "figma:"; echo "  Status: ✓ Connected"\n')
        code, text = REAL_GET("figma")
        self.assertEqual(code, 0)
        self.assertEqual(main.parse_get(text), "connected")
        self.assertFalse(os.path.exists(os.path.join(main.HOME, "list.out")))

    def test_no_claude_is_none(self):
        os.environ["HUB_CLAUDE"] = os.path.join(TMP, "missing claude")
        try:
            with mock.patch.object(main.shutil, "which", lambda x: None):
                self.assertIsNone(REAL_GET("figma"))
        finally:
            os.environ["HUB_CLAUDE"] = os.path.join(TMP, "claude.sh")


class SessionWord(Base):
    """Where Claude Code outside the hub cannot tell (a hub catalog server), the hub's live sessions can: their Claude
    Code refreshed the login itself, under its lock, when it connected."""

    def expire(self, name):
        recs = read(os.environ["MCP_KEEPER_CREDS"])
        recs["mcpOAuth"][name + "|abc"]["expiresAt"] = (NOW - 60) * 1000
        write(os.environ["MCP_KEEPER_CREDS"], recs)
        self.gets[name] = 'No MCP server named "%s"' % name

    def test_live_session_connected_is_ok(self):
        self.expire("figma")
        self.assertEqual(self.run_once()["servers"]["figma"]["state"], "ok")

    def test_live_session_needs_auth_is_lost_after_the_second_pass(self):
        self.expire("figma")
        self.hub["sessions"]["worker"]["info"]["mcp_auth"] = ["figma"]
        res = self.run_once()
        self.assertEqual(res["servers"]["figma"]["state"], "lost")
        self.assertEqual(len(self.notes()), 1)

    def test_a_parked_session_is_no_word(self):
        self.expire("canva")
        self.hub["sessions"]["worker"]["info"]["mcp"] = []
        self.assertUnclear(self.run_once()["servers"]["canva"])   # only sleepy (parked) has it

    def test_connected_does_not_override_a_real_later(self):
        self.expire("figma")
        self.gets["figma"] = G_FAIL   # Claude Code itself could not reach it: the old word of a session is not "ok"
        self.assertNotEqual(self.run_once()["servers"]["figma"]["state"], "ok")

    def test_needs_auth_of_a_session_refreshes_nothing_itself(self):
        self.expire("figma")
        self.hub["sessions"]["worker"]["info"]["mcp_auth"] = ["figma"]
        with mock.patch.object(main.urllib.request, "urlopen", side_effect=AssertionError("network")):
            self.run_once()


class FirstLogin(Base):
    """A server the hub gives, sessions wait for its login, nobody ever logged in: no loss, one line a day to the Hub
    Developer with one login command per server."""

    def setUp(self):
        super().setUp()
        recs = read(os.environ["MCP_KEEPER_CREDS"])
        del recs["mcpOAuth"]["mobbin|abc"]
        write(os.environ["MCP_KEEPER_CREDS"], recs)
        self.hub["sessions"]["worker"]["info"]["mcp"].append("mobbin")
        self.hub["sessions"]["sleepy"]["info"]["mcp_auth"] = ["mobbin"]

    def test_first_is_shown_and_told_to_the_developer_once_a_day(self):
        res = self.run_once()
        self.assertEqual(res["servers"]["mobbin"]["state"], "first")
        self.assertEqual(self.notes(), [])
        (msg,) = self.dev()
        self.assertIn("mobbin", msg["text"])
        self.assertIn("hub mcp login hub mobbin", msg["text"])
        self.run_once(NOW + 3600)
        self.assertEqual(self.dev(), [])
        self.run_once(NOW + 86400 + 60)
        self.assertEqual(len(self.dev()), 1)
        self.assertEqual(read(os.environ["MCP_KEEPER_STATUS"])["servers"]["mobbin"]["state"], "first")

    def test_nobody_waits_nothing_is_said(self):
        self.hub["sessions"]["sleepy"]["info"]["mcp_auth"] = []
        res = self.run_once()
        self.assertEqual(res["servers"]["mobbin"], {"state": "ok"})   # connected by a session's word, nothing to fix
        self.assertEqual(self.dev(), [])

    def test_status_line_counts_it(self):
        self.run_once()
        posts = [d["text"] for t, d in self.facts if t == "ext.post"]
        self.assertTrue(posts and "never logged in" in posts[-1], posts)


class Headers(Base):
    """A headers server: its helper is run with a limit, the server is asked with those headers; a rejected key is one
    alarm a day, a hang or no network is "later". Values go nowhere."""

    def setUp(self):
        super().setUp()
        self.config({"mcp": {"buffer": {"server": {"type": "http", "url": "https://b/mcp", "headersHelper": "helper"}},
                             "icons": {"server": {"type": "http", "url": "https://i/mcp",
                                                  "headers": {"Authorization": "Bearer ${KEEPER_TEST_KEY}"}}}}})
        self.hub["sessions"]["worker"]["info"]["mcp"] = ["buffer", "icons"]
        self.helper = (0, '{"Authorization": "Bearer secret-value"}')
        p = mock.patch.object(main, "run_limited", lambda cmd, timeout, merge=True: self.helper)
        p.start()
        self.addCleanup(p.stop)

    def test_live_headers_are_ok(self):
        res = self.run_once()
        self.assertEqual((res["servers"]["buffer"]["state"], res["servers"]["icons"]["state"]), ("ok", "ok"))
        self.assertIn(("https://b/mcp", ["Authorization"]), self.keyed)

    def test_rejected_key_is_lost_and_told_once(self):
        self.probes["https://b/mcp"] = (False, 401)
        res = self.run_once()
        self.assertEqual(res["servers"]["buffer"]["state"], "lost")
        self.assertIn("401", res["servers"]["buffer"]["why"])
        self.assertIn("mcp.buffer.server", res["servers"]["buffer"]["fix"])
        self.assertEqual(len(self.notes()), 1)
        self.run_once(NOW + 3600)
        self.assertEqual(self.notes(), [])

    def test_helper_failure_is_lost(self):
        for out in [(1, ""), (0, "not json"), (0, "{}"), (0, '{"A": 5}')]:
            self.helper = out
            self.assertEqual(self.run_once()["servers"]["buffer"]["state"], "lost", out)

    def test_helper_timeout_or_no_start_is_later(self):
        for out in [(124, ""), None]:
            self.helper = out
            self.assertUnclear(self.run_once()["servers"]["buffer"], out)

    def test_server_down_is_later(self):
        self.probes["https://i/mcp"] = (None, None)
        self.assertUnclear(self.run_once()["servers"]["icons"])

    def test_header_values_are_never_stored(self):
        os.environ["KEEPER_TEST_KEY"] = "env-secret"
        self.addCleanup(os.environ.pop, "KEEPER_TEST_KEY", None)
        self.probes["https://b/mcp"] = (False, 403)
        self.run_once()
        state = read(os.path.join(main.HOME, "state.json"), raw=True)
        dump = json.dumps(self.facts) + read(os.environ["MCP_KEEPER_STATUS"], raw=True) + state
        for secret in ("secret-value", "env-secret"):
            self.assertNotIn(secret, dump)

    def test_env_is_expanded_in_static_headers(self):
        os.environ["KEEPER_TEST_KEY"] = "k"
        self.addCleanup(os.environ.pop, "KEEPER_TEST_KEY", None)
        head, why = main.helper_headers({"url": "https://i/mcp", "headers": {"Authorization": "Bearer ${KEEPER_TEST_KEY}"}})
        self.assertEqual((head, why), ({"Authorization": "Bearer k"}, None))

    def test_a_plain_header_keeps_an_oauth_server_oauth(self):
        # a client tag header is no login: the server is an OAuth one, its stored login is checked as before
        srv = {"type": "http", "url": URLS["figma"], "headers": {"X-Plugin-Bundle": "b"}}
        self.assertEqual((main.keyed(srv), main.remote(srv)), (False, True))
        self.config({"mcp": {"figma": {"server": srv}}})
        self.creds(figma={"serverUrl": URLS["figma"], "accessToken": "t", "refreshToken": "r",
                          "expiresAt": (NOW + 3600) * 1000})
        self.hub["sessions"]["worker"]["info"]["mcp"] = ["figma"]
        res = self.run_once()
        self.assertEqual(res["servers"]["figma"]["state"], "ok")
        self.assertEqual(self.keyed, [])

    def test_real_probe_sends_the_headers_only_to_a_safe_address(self):
        with mock.patch.object(main.urllib.request, "urlopen", side_effect=AssertionError("sent")):
            self.assertEqual(main.probe_with("http://evil.example.com/mcp", {"Authorization": "x"}), (None, None))


class Cover(Base):
    """cover: every login of this machine (~/.claude.json, plugins), each with its holder; a loss goes to the Hub
    Developer at the first sign and to the owner only when it stays lost for `owner_after_passes` more passes."""

    REMOTE = "https://mcp.remote.example/mcp"

    def setUp(self):
        super().setUp()
        write(os.path.join(TMP, "claude", ".claude.json"), {"mcpServers": {
            "gate": {"type": "http", "url": "https://gate.example/mcp"},
            "docs": {"type": "http", "url": "https://docs.example/mcp"},
            "desk": {"type": "http", "url": "http://127.0.0.1:3845/mcp"},
            "rem": {"type": "stdio", "command": "npx", "args": ["-y", "mcp-remote", self.REMOTE]},
            "m365": {"type": "stdio", "command": "npx", "args": ["-y", "@softeria/ms-365-mcp-server"]},
            "cli": {"type": "stdio", "command": "npx", "args": ["-y", "@canva/cli@latest", "mcp"]},
            "figma": {"type": "http", "url": URLS["figma"]}}})
        self.addCleanup(os.unlink, os.path.join(TMP, "claude", ".claude.json"))
        os.environ["MCP_REMOTE_CONFIG_DIR"] = os.path.join(TMP, "mcp-auth")
        self.addCleanup(os.environ.pop, "MCP_REMOTE_CONFIG_DIR", None)
        self.open = {"https://docs.example/mcp": True, "https://gate.example/mcp": False}
        p = mock.patch.object(main, "probe_open", lambda url: self.open.get(url))
        p.start()
        self.addCleanup(p.stop)
        self.bearer = []
        p = mock.patch.object(main, "probe_with", self.fake_with)
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(main.subprocess, "Popen", side_effect=AssertionError("a server was started"))
        p.start()
        self.addCleanup(p.stop)
        self.on()

    def gate(self):
        """A live stored login of the user server gate, next to the live catalog ones."""
        live = {n: {"serverUrl": u, "accessToken": "t", "refreshToken": "r", "expiresAt": (NOW + 3600) * 1000}
                for n, u in URLS.items()}
        self.creds(gate={"serverUrl": "https://gate.example/mcp", "accessToken": "g", "refreshToken": "r",
                         "expiresAt": (NOW + 3600) * 1000}, **live)

    def fake_with(self, url, head):
        self.bearer.append(url)
        return self.probes.get(url, (True, 200))

    def on(self, value=True, **extra):
        self.config({}, plugin={"cover": value, **extra})

    def tokens(self, expires, access="a", refresh="r"):
        import hashlib
        write(os.path.join(TMP, "mcp-auth", "mcp-remote-v1", hashlib.md5(self.REMOTE.encode()).hexdigest() + "_tokens.json"),
              {"access_token": access, "refresh_token": refresh, "expires_at": expires * 1000})

    def test_off_by_default(self):
        self.config({})
        res = self.run_once()
        for n in ("gate", "docs", "desk", "rem", "m365", "cli"):
            self.assertNotIn(n, res["servers"])

    def test_every_server_of_the_machine_has_a_state_and_a_holder(self):
        self.tokens(NOW + 3600)
        self.gate()
        s = self.run_once()["servers"]
        got = {n: (s[n]["state"], s[n].get("by")) for n in ("gate", "docs", "desk", "rem", "m365", "cli", "figma")}
        self.assertEqual(got, {"gate": ("ok", "claude"), "docs": ("ok", "none"), "desk": ("skip", "local"),
                               "rem": ("ok", "mcp-remote"), "m365": ("skip", "program"), "cli": ("skip", "program"),
                               "figma": ("ok", "claude")})
        self.assertTrue(all(s[n].get("why") for n in ("desk", "m365", "cli")))
        status = read(os.environ["MCP_KEEPER_STATUS"])
        self.assertEqual(status["servers"]["cli"]["state"], "skip")
        self.assertEqual(status["servers"]["rem"]["by"], "mcp-remote")

    def test_a_server_that_needs_no_login_is_connected_with_nothing_to_press(self):
        self.run_once()
        docs = read(os.environ["MCP_KEEPER_STATUS"])["servers"]["docs"]
        self.assertEqual(docs["state"], "ok")
        self.assertNotIn("can_login", docs)
        self.assertNotIn("why", docs)
        self.assertEqual(self.notes(), [])
        self.assertFalse([m for m in self.dev() if "docs" in m["text"]])

    def add_user_server(self, name, url):
        path = os.path.join(TMP, "claude", ".claude.json")
        cj = read(path)
        cj["mcpServers"][name] = {"type": "http", "url": url}
        write(path, cj)

    def test_a_server_that_appeared_gets_its_row_at_once_not_at_the_next_pass(self):
        self.assertFalse(main.fresh_step(), "no table yet: the first pass writes it")
        self.run_once()
        self.assertFalse(main.fresh_step())
        self.add_user_server("newsvc", "https://new.example/mcp")
        self.open["https://new.example/mcp"] = True
        self.assertTrue(main.fresh_step())
        rows = read(os.environ["MCP_KEEPER_STATUS"])["servers"]
        self.assertEqual(rows["newsvc"]["state"], "ok")
        self.assertIn("docs", rows)
        self.assertFalse(main.fresh_step(), "the same server is not asked again")

    def test_a_server_that_appeared_is_not_looked_at_while_a_pass_runs(self):
        self.run_once()
        self.add_user_server("newsvc", "https://new.example/mcp")
        with main.LOCK:
            self.assertFalse(main.fresh_step())
        self.assertTrue(main.fresh_step(), "it is looked at once the pass is over")

    def test_an_ignored_server_that_appeared_is_not_looked_at(self):
        self.on(ignore=["newsvc"])
        self.run_once()
        self.add_user_server("newsvc", "https://new.example/mcp")
        self.assertFalse(main.fresh_step())

    def test_a_list_of_machines(self):
        with mock.patch.object(main.socket, "gethostname", lambda: "Other-Box.local"):
            self.on(["Mac-One.local"])
            self.assertNotIn("gate", self.run_once()["servers"])
            self.on(["other-box"])
            self.assertIn("gate", self.run_once()["servers"])

    def test_never_logged_in_user_server_is_first_not_lost(self):
        s = self.run_once()["servers"]["gate"]
        self.assertEqual((s["state"], s["fix"]), ("first", "/mcp in Claude Code -> gate"))
        self.assertEqual(self.notes(), [])
        self.assertIn("gate: /mcp in Claude Code -> gate", self.dev()[0]["text"])

    def test_first_login_turns_ok(self):
        self.assertEqual(self.run_once()["servers"]["gate"]["state"], "first")
        self.gate()
        res = self.run_once(NOW + 900)
        self.assertEqual(res["servers"]["gate"]["state"], "ok")
        self.assertEqual((self.notes(), self.dev()), ([], []))

    def test_conduit_is_only_looked_at(self):
        self.creds(conduit={"serverUrl": "https://c/mcp", "accessToken": "c", "refreshToken": "r",
                            "expiresAt": (NOW - 600) * 1000})
        with mock.patch.object(main, "user_servers", lambda: {"conduit": {"type": "http", "url": "https://c/mcp"}}):
            s = self.run_once()["servers"]["conduit"]
        self.assertEqual((s["state"], s["by"]), ("skip", "conduit"))
        self.assertNotIn("conduit", self.asked)

    def test_mcp_remote_is_read_and_its_bearer_sent_only_to_its_address(self):
        self.tokens(NOW + 3600)
        self.probes[self.REMOTE] = (False, 401)
        s = self.run_once()["servers"]["rem"]
        self.assertEqual(s["state"], "lost")
        self.assertEqual([u for u in self.bearer if u != self.REMOTE], [])

    def test_mcp_remote_expired_with_auth_keeper_goes_to_the_developer_only(self):
        self.tokens(NOW - 600)
        cfg = read(os.environ["HUB_CONFIG"])
        cfg["extensions"]["auth-keeper"] = {"path": "x"}
        write(os.environ["HUB_CONFIG"], cfg)
        for i in range(4):
            s = self.run_once(NOW + i * 900)["servers"]["rem"]
            self.assertEqual(self.notes(), [], i)
        self.assertEqual((s["state"], s["by"]), ("lost", "auth-keeper"))

    def test_ladder_developer_first_owner_after_two_more_passes(self):
        self.gate()
        self.probes["https://gate.example/mcp"] = (False, 401)
        self.run_once()
        self.assertEqual(self.notes(), [])
        self.assertIn("gate (claude)", self.dev()[0]["text"])
        self.run_once(NOW + 900)
        self.assertEqual((self.notes(), self.dev()), ([], []))
        self.run_once(NOW + 1800)
        self.assertEqual(len(self.notes()), 1)
        self.assertTrue(self.notes()[0]["urgent"])
        self.assertEqual(self.dev(), [])
        self.run_once(NOW + 2700)
        self.assertEqual(self.notes(), [])

    def test_revived_before_the_owner_hears_tells_nobody_else(self):
        self.gate()
        self.probes["https://gate.example/mcp"] = (False, 401)
        self.run_once()
        self.probes.pop("https://gate.example/mcp")
        self.assertEqual(self.run_once(NOW + 900)["servers"]["gate"]["state"], "ok")
        self.probes["https://gate.example/mcp"] = (False, 401)
        for i in (2, 3):
            self.run_once(NOW + i * 900)
            self.assertEqual(self.notes(), [], i)

    def test_a_pass_that_cannot_tell_does_not_reset_the_ladder(self):
        self.gate()
        self.probes["https://gate.example/mcp"] = (False, 401)
        self.run_once()
        self.run_once(NOW + 900)
        self.probes["https://gate.example/mcp"] = (None, None)
        self.assertUnclear(self.run_once(NOW + 1800)["servers"]["gate"])
        self.probes["https://gate.example/mcp"] = (False, 401)
        self.run_once(NOW + 2700)
        self.assertEqual(len(self.notes()), 1)

    def test_ignore_and_hub_ignore_list(self):
        self.on(True, ignore=["cli"])
        cfg = read(os.environ["HUB_CONFIG"])
        cfg["mcp_auth_ignore"] = ["gate"]
        write(os.environ["HUB_CONFIG"], cfg)
        s = self.run_once()["servers"]
        self.assertNotIn("gate", s)
        self.assertNotIn("cli", s)

    def test_a_cli_program_is_no_duplicate_of_a_plugin_server(self):
        plug = os.path.join(TMP, "plug")
        write(os.path.join(plug, ".mcp.json"), {"mcpServers": {"canva": {"type": "http", "url": URLS["canva"]}}})
        write(os.path.join(TMP, "claude", "settings.json"), {"enabledPlugins": {"canva@m": True}})
        write(os.path.join(TMP, "claude", "plugins", "installed_plugins.json"),
              {"plugins": {"canva@m": [{"installPath": plug}]}})
        self.addCleanup(os.unlink, os.path.join(TMP, "claude", "settings.json"))
        self.addCleanup(os.unlink, os.path.join(TMP, "claude", "plugins", "installed_plugins.json"))
        self.assertEqual(main.duplicates(), [])


class ViewLogin(Base):
    """The view's "Log in" button: the owner's post "login <server>" in the plugin channel -> `hub mcp login` run by the
    process (a fake here, nothing is opened); the row says what the login is doing."""

    def setUp(self):
        super().setUp()
        recs = read(os.environ["MCP_KEEPER_CREDS"])
        del recs["mcpOAuth"]["mobbin|abc"]
        write(os.environ["MCP_KEEPER_CREDS"], recs)
        self.hub["sessions"]["worker"]["info"]["mcp"].append("mobbin")
        self.hub["sessions"]["sleepy"]["info"]["mcp_auth"] = ["mobbin"]
        self.reject("canva")
        res = self.run_once()
        self.assertEqual((res["servers"]["mobbin"]["state"], res["servers"]["canva"]["state"]), ("first", "lost"))
        self.ran = []
        self.code, self.out = 0, "login waits for the browser\n"
        self.result = None   # what the hub's waiting process wrote: (code, last line) or None - still waiting
        self.hub["feeds"] = {main.NAME: []}
        for p in (mock.patch.object(main, "hub_bin", lambda: "/hub/bin/hub"),
                  mock.patch.object(main, "login_result", lambda server: self.result),
                  mock.patch.object(main, "run_limited", self.fake_run)):
            p.start()
            self.addCleanup(p.stop)
        main.RECHECK_NOW.clear()

    def fake_run(self, cmd, timeout, merge=True):
        self.ran.append(cmd[2:])
        return self.code, self.out

    def click(self, seq, server, by="owner", ts=NOW, text=None):
        self.hub["feeds"][main.NAME].append({"id": f"p{seq}", "by": by, "text": text or f"login {server}", "ts": ts})

    def rows(self):
        return read(os.environ["MCP_KEEPER_STATUS"])["servers"]

    def test_only_a_server_that_needs_login_has_the_button(self):
        rows = self.rows()
        self.assertTrue(rows["mobbin"]["can_login"] and rows["canva"]["can_login"])
        self.assertNotIn("can_login", rows["figma"])

    def test_click_runs_the_hub_command_with_a_live_session_that_has_the_server(self):
        self.click(1, "mobbin")
        self.assertTrue(main.login_step(NOW + 1))
        self.assertEqual(self.ran, [["mcp", "login", "worker", "mobbin"]])
        self.assertEqual(self.rows()["mobbin"]["login"], {"state": "wait", "at": NOW + 1})

    def test_no_live_session_with_the_server_takes_a_sleeping_one_then_the_hub(self):
        self.hub["sessions"]["worker"]["info"]["mcp"].remove("mobbin")
        self.click(1, "mobbin")
        main.login_step(NOW + 1)
        self.assertEqual(self.ran, [["mcp", "login", "sleepy", "mobbin"]], "a sleeping session is still a session of the server")
        self.hub["sessions"]["sleepy"]["info"]["mcp_auth"] = []
        self.result = (1, "gone")
        main.login_step(NOW + 2)
        self.click(2, "mobbin")
        main.login_step(NOW + 3)
        self.assertEqual(self.ran[-1], ["mcp", "login", "hub", "mobbin"])

    def test_the_post_is_taken_once_and_only_from_the_owner_while_fresh(self):
        self.click(1, "mobbin", by="worker")
        self.click(2, "mobbin", ts=NOW - main.LOGIN_FRESH - 5)
        self.click(3, "mobbin", text="login mobbin --evil; x")
        main.login_step(NOW + 1)
        self.assertEqual(self.ran, [])
        self.click(4, "mobbin")
        main.login_step(NOW + 2)
        self.result = (1, "no")
        main.login_step(NOW + 3)
        main.login_step(NOW + 4)
        self.assertEqual(len(self.ran), 1, "the same post does not start a second login")

    def test_open_login_is_not_started_twice(self):
        self.click(1, "mobbin")
        main.login_step(NOW + 1)
        self.click(2, "mobbin")
        main.login_step(NOW + 2)
        self.assertEqual(len(self.ran), 1)

    def test_server_without_the_button_is_refused_not_run(self):
        self.click(1, "figma")
        main.login_step(NOW + 1)
        self.assertEqual(self.ran, [])
        row = self.rows()["figma"]["login"]
        self.assertEqual(row["state"], "fail")
        self.assertIn("no login", row["why"])

    def test_command_failure_is_shown_with_the_reason(self):
        self.code, self.out = 1, "no such session\nhttps://secret.example/auth?code=1\nsession x does not exist\n"
        self.click(1, "mobbin")
        main.login_step(NOW + 1)
        got = self.rows()["mobbin"]["login"]
        self.assertEqual(got["state"], "fail")
        self.assertEqual(got["why"], "session x does not exist")
        self.assertNotIn("secret", json.dumps(self.rows()))

    def test_no_hub_command_is_a_fail_not_a_crash(self):
        with mock.patch.object(main, "hub_bin", lambda: None):
            self.click(1, "mobbin")
            main.login_step(NOW + 1)
        self.assertEqual(self.rows()["mobbin"]["login"]["state"], "fail")
        self.assertEqual(self.ran, [])

    def test_waiting_ends_with_the_result_of_the_hub_process(self):
        self.click(1, "mobbin")
        main.login_step(NOW + 1)
        self.assertFalse(main.login_step(NOW + 10), "still waiting: nothing changes")
        self.assertFalse(main.RECHECK_NOW.is_set())
        self.result = (0, "logged in")
        self.assertTrue(main.login_step(NOW + 20))
        self.assertEqual(self.rows()["mobbin"]["login"]["state"], "done")
        self.assertTrue(main.RECHECK_NOW.is_set(), "the next pass checks at once, not in 15 minutes")

    def test_waiting_ends_with_a_failure_and_with_a_timeout(self):
        self.click(1, "mobbin")
        main.login_step(NOW + 1)
        self.result = (1, "the browser was closed")
        main.login_step(NOW + 20)
        self.assertEqual(self.rows()["mobbin"]["login"], {"state": "fail", "at": NOW + 20, "why": "the browser was closed"})
        self.result = None
        self.click(2, "canva")
        main.login_step(NOW + 30)
        main.login_step(NOW + 30 + main.LOGIN_LIVE + 1)
        got = self.rows()["canva"]["login"]
        self.assertEqual(got["state"], "fail")
        self.assertIn("no login within", got["why"])

    def test_login_done_at_once_is_done(self):
        self.result = (0, "")
        self.click(1, "mobbin")
        main.login_step(NOW + 1)
        self.assertEqual(self.rows()["mobbin"]["login"]["state"], "done")
        self.assertTrue(main.RECHECK_NOW.is_set())

    def test_an_old_result_goes_from_the_view(self):
        self.click(1, "mobbin")
        main.login_step(NOW + 1)
        self.result = (0, "")
        main.login_step(NOW + 2)
        self.assertIn("login", self.rows()["mobbin"])
        with mock.patch.object(main.time, "time", lambda: NOW + 2 + main.LOGIN_SHOW + 1):
            main.write_status(main.load_state())
        self.assertNotIn("login", self.rows()["mobbin"])


class ViewAdd(Base):
    """0.8.0 "Add MCP": the owner's post "add <name> <address>" -> `claude mcp add`, `hub tools import mcp`, the login. Every
    command is a fake here: nothing is added to ~/.claude.json or to a hub."""

    GOOD = "https://mcp.example.com/mcp"

    def setUp(self):
        super().setUp()
        self.run_once()
        self.ran, self.answers = [], {}
        self.hub["feeds"] = {main.NAME: []}
        self.login_done = None
        for p in (mock.patch.object(main, "hub_bin", lambda: "/hub/bin/hub"),
                  mock.patch.object(main, "claude_cmd", lambda: ["/bin/claude"]),
                  mock.patch.object(main, "login_result", lambda server: self.login_done),
                  mock.patch.object(main, "probe_open", lambda url: None),   # the network is closed
                  mock.patch.object(main, "run_limited", self.fake_run)):
            p.start()
            self.addCleanup(p.stop)
        main.RECHECK_NOW.clear()
        self.before = (read(os.environ["HUB_CONFIG"], raw=True), os.path.exists(main.claude_json()))

    def fake_run(self, cmd, timeout, merge=True):
        self.ran.append(cmd)
        got = self.answers.get(self.kind(cmd), (0, "ok\n"))
        if isinstance(got, list):
            return got.pop(0) if len(got) > 1 else got[0]
        return got() if callable(got) else got

    @staticmethod
    def kind(cmd):
        if cmd[1:3] == ["mcp", "add"]:
            return "add"
        if cmd[1:3] == ["mcp", "remove"]:
            return "remove"
        if cmd[2:4] == ["tools", "import"]:
            return "import"
        return "login" if cmd[2:4] == ["mcp", "login"] else "other"

    def kinds(self):
        return [self.kind(c) for c in self.ran]

    def post(self, seq, text, by="owner", ts=NOW):
        self.hub["feeds"][main.NAME].append({"id": f"p{seq}", "by": by, "text": text, "ts": ts})

    def status(self):
        return read(os.environ["MCP_KEEPER_STATUS"])

    def test_click_adds_the_standard_way_then_opens_the_login(self):
        self.post(1, "add newsvc " + self.GOOD)
        self.assertTrue(main.login_step(NOW + 1))
        self.assertEqual(self.ran[0], ["/bin/claude", "mcp", "add", "--transport", "http", "-s", "user", "newsvc", self.GOOD])
        self.assertEqual(self.ran[1][2:], ["tools", "import", "mcp", "newsvc", "--about", "Newsvc"])
        self.assertEqual(self.ran[2][2:], ["mcp", "login", "hub", "newsvc"])
        self.assertEqual(self.kinds(), ["add", "import", "login"])
        add = self.status()["add"]
        self.assertEqual((add["state"], add["name"], add["login"]), ("done", "newsvc", {"state": "wait"}))
        self.assertTrue(main.RECHECK_NOW.is_set(), "the new server joins the table at the next pass")

    def test_the_login_that_ended_is_shown_under_the_add(self):
        self.post(1, "add newsvc " + self.GOOD)
        main.login_step(NOW + 1)
        self.login_done = (0, "logged in")
        main.login_step(NOW + 20)
        self.assertEqual(self.status()["add"]["login"], {"state": "done"})

    def test_a_login_that_fails_leaves_the_server_added(self):
        self.answers["login"] = (1, "the browser was closed\n")
        self.post(1, "add newsvc " + self.GOOD)
        main.login_step(NOW + 1)
        add = self.status()["add"]
        self.assertEqual(add["state"], "done")
        self.assertEqual(add["login"]["state"], "fail")
        self.assertEqual(add["login"]["why"], "the browser was closed")

    def test_a_local_http_address_is_taken(self):
        self.assertEqual(main.add_server("local", "http://localhost:8080/mcp", self.hub, NOW)["state"], "done")

    def test_bad_names_and_addresses_run_nothing(self):
        for name in ("", "-x", "a;b", "a b", "a/b", "x" * 41, "имя"):
            self.assertEqual(main.add_server(name, self.GOOD, self.hub, NOW)["state"], "fail", name)
        for url in ("http://example.com/mcp", "https://user:pw@example.com/mcp", "https://example.com/mcp?key=abc",
                    "https://example.com/mcp#x", "ftp://example.com/mcp", "https://example.com/a b", "https://exa mple.com",
                    "https://example.com/mcp\"&calc", "https://", "example.com/mcp", "https://example.com/mcp%0A"[:0]):
            got = main.add_server("svc", url, self.hub, NOW)
            self.assertEqual(got["state"], "fail", url)
            self.assertNotIn("example", got["why"])
        self.assertEqual(self.ran, [])

    def test_a_name_or_an_address_that_is_there_is_refused(self):
        write(main.claude_json(), {"mcpServers": {"mine": {"type": "http", "url": "https://mcp.mine.example/mcp"}}})
        cases = (("figma", self.GOOD), ("Figma", self.GOOD), ("mine", self.GOOD),
                 ("other", URLS["figma"]), ("other", URLS["figma"] + "/"), ("other", "https://mcp.mine.example/mcp"))
        for name, url in cases:
            got = main.add_server(name, url, self.hub, NOW)
            self.assertEqual((got["state"], got["why"]), ("fail", "this name or address is already there"), (name, url))
        self.assertEqual(self.ran, [])

    def test_claude_that_fails_stops_before_the_hub(self):
        self.answers["add"] = (1, "https://secret.example/?k=1\nerror: no such transport\n")
        got = main.add_server("svc", self.GOOD, self.hub, NOW)
        self.assertEqual(got["state"], "fail")
        self.assertEqual(got["why"], "Claude Code did not add the server: error: no such transport")
        self.assertEqual(self.kinds(), ["add"])

    def test_a_hub_that_refuses_gets_the_entry_taken_back(self):
        self.answers["import"] = (1, "no such server\n")
        got = main.add_server("svc", self.GOOD, self.hub, NOW)
        self.assertEqual(got["why"], "the hub did not take the server: no such server")
        self.assertEqual(self.kinds(), ["add", "import", "remove"])
        self.assertEqual(self.ran[-1], ["/bin/claude", "mcp", "remove", "-s", "user", "svc"])

    BUSY = (1, "PermissionError: [WinError 5] Access is denied: 'hub.json.tmp' -> 'hub.json'\n")

    def test_a_busy_settings_file_is_retried_and_the_entry_stays(self):
        self.answers["import"] = [self.BUSY, self.BUSY, (0, "ok\n")]
        got = main.add_server("svc", self.GOOD, self.hub, NOW)
        self.assertEqual((got["state"], got["name"]), ("done", "svc"))
        self.assertEqual(self.kinds(), ["add", "import", "import", "import"])

    def test_busy_and_the_address_already_in_the_catalog_is_a_success(self):
        def hub_took_it():   # another process (the hub itself) imported the same server in that minute
            cfg = read(os.environ["HUB_CONFIG"])
            cfg["mcp"]["svc"] = {"server": {"type": "http", "url": self.GOOD}}
            write(os.environ["HUB_CONFIG"], cfg)
            return self.BUSY
        self.answers["import"] = hub_took_it
        got = main.add_server("svc", self.GOOD, self.hub, NOW)
        self.assertEqual(got["state"], "done")
        self.assertEqual(self.kinds(), ["add", "import"])

    def test_a_busy_file_that_stays_busy_says_so_and_takes_the_entry_back(self):
        self.answers["import"] = self.BUSY
        got = main.add_server("svc", self.GOOD, self.hub, NOW)
        self.assertEqual((got["state"], got["why"]), ("fail", "the hub was busy and did not take the server: try again"))
        self.assertEqual(self.kinds(), ["add"] + ["import"] * main.IMPORT_TRIES + ["remove"])

    def test_a_refusal_that_is_not_a_busy_file_is_not_retried(self):
        self.answers["import"] = (1, "no such server\n")
        main.add_server("svc", self.GOOD, self.hub, NOW)
        self.assertEqual(self.kinds().count("import"), 1)

    def test_a_server_that_needs_no_login_is_not_sent_to_the_login(self):
        with mock.patch.object(main, "probe_open", lambda url: True):
            self.post(1, "add newsvc " + self.GOOD)
            main.login_step(NOW + 1)
        self.assertEqual(self.kinds(), ["add", "import"])
        add = self.status()["add"]
        self.assertEqual((add["state"], add["name"]), ("done", "newsvc"))
        self.assertNotIn("login", add)

    def test_no_claude_and_no_hub_are_fails_not_crashes(self):
        with mock.patch.object(main, "claude_cmd", lambda: None):
            self.assertEqual(main.add_server("svc", self.GOOD, self.hub, NOW)["why"], "Claude Code was not found")
        with mock.patch.object(main, "hub_bin", lambda: None):
            self.assertEqual(main.add_server("svc", self.GOOD, self.hub, NOW)["state"], "fail")
        self.assertEqual(self.ran, [])

    def test_the_post_counts_once_only_from_the_owner_while_fresh(self):
        self.post(1, "add svc " + self.GOOD, by="worker")
        self.post(2, "add svc " + self.GOOD, ts=NOW - main.LOGIN_FRESH - 5)
        self.post(3, "add svc " + self.GOOD + "?key=1")
        self.post(4, "add svc")
        self.post(5, "add svc " + self.GOOD + " extra")
        main.login_step(NOW + 1)
        self.assertEqual(self.ran, [])
        self.post(6, "add svc " + self.GOOD)
        main.login_step(NOW + 2)
        main.login_step(NOW + 3)
        self.assertEqual(self.kinds().count("add"), 1, "the same post does not add a second time")

    def test_the_result_stays_an_hour_and_holds_no_address_or_key(self):
        self.answers["add"] = (1, "bad https://secret.example/?k=1 token=abc\n")
        self.post(1, "add svc " + self.GOOD)
        main.login_step(NOW + 1)
        self.assertEqual(self.status()["add"]["state"], "fail")
        text = read(os.environ["MCP_KEEPER_STATUS"], raw=True) + read(main.login_path(), raw=True)
        for secret in ("secret.example", "token=abc"):
            self.assertNotIn(secret, text)
        self.assertNotIn(self.GOOD, read(os.environ["MCP_KEEPER_STATUS"], raw=True))
        with mock.patch.object(main.time, "time", lambda: NOW + 1 + main.ADD_SHOW + 1):
            main.write_status(main.load_state())
        self.assertNotIn("add", self.status())

    def test_the_plugin_writes_neither_the_hub_settings_nor_the_claude_file_itself(self):
        self.post(1, "add svc " + self.GOOD)
        main.login_step(NOW + 1)
        self.assertEqual(self.before, (read(os.environ["HUB_CONFIG"], raw=True), os.path.exists(main.claude_json())))


class Icon(unittest.TestCase):
    """The menu draws a view icon with a mask: one colour, no background; icon.png is the cover of the market."""

    def test_menu_icon_is_a_one_colour_svg(self):
        with open(os.path.join(ROOT, "icon.svg"), encoding="utf-8") as f:
            svg = f.read()
        self.assertIn('fill="currentColor"', svg)
        self.assertNotRegex(svg, r'fill="#|stroke="#|<rect|<image|style=')
        with open(os.path.join(ROOT, "main.js"), encoding="utf-8") as f:
            self.assertIn('icon: "icon.svg"', f.read())
        self.assertTrue(os.path.isfile(os.path.join(ROOT, "icon.png")))


class Unclear(Base):
    """0.6.0, owner 28.10: the view says only live or lost. What the pass cannot judge is asked again with a pause,
    reconnected in the sessions in between; still no answer - lost with the reason, no alarm."""

    def reconnects(self):
        return [d["server"] for t, d in self.facts if t == "mcp.reconnect"]

    def test_no_later_reaches_the_view(self):
        self.probes[URLS["figma"]] = (None, None)
        self.run_once()
        got = read(os.environ["MCP_KEEPER_STATUS"])
        self.assertNotIn("later", json.dumps(got))
        self.assertEqual(got["servers"]["figma"]["state"], "lost")
        self.assertIn("no clear answer after 4", got["servers"]["figma"]["why"])
        self.assertTrue(got["auto"])

    def test_retries_then_lost_without_an_alarm(self):
        self.probes[URLS["figma"]] = (None, None)
        res = self.run_once()
        self.assertUnclear(res["servers"]["figma"])
        self.assertEqual((self.notes(), self.dev()), ([], []))
        self.assertEqual(self.reconnects(), ["figma"], "auto: once per pass, before the retries")

    def test_an_answer_on_a_retry_is_taken(self):
        calls = []

        def flaky(ent):
            calls.append(ent.get("serverUrl"))
            if ent.get("serverUrl") == URLS["figma"] and calls.count(URLS["figma"]) == 1:
                return None, None
            return True, 200
        with mock.patch.object(main, "probe", flaky):
            res = self.run_once()
        self.assertEqual(res["servers"]["figma"]["state"], "ok")
        self.assertEqual(calls.count(URLS["figma"]), 2)

    def test_an_old_later_in_the_status_file_becomes_lost(self):
        got = main.clean_status({"at": 1, "servers": {"figma": {"state": "later", "why": "timeout"}}})
        self.assertEqual(got["servers"]["figma"]["state"], "lost")
        self.assertIn("timeout", got["servers"]["figma"]["why"])

    def test_one_time_login_is_never_reconnected(self):
        self.probes[URLS["canva"]] = (None, None)
        self.run_once()
        self.assertEqual(self.reconnects(), [])

    def test_auto_off_does_not_reconnect(self):
        st = main.load_state()
        st["auto"] = False
        main.save_state(st)
        self.probes[URLS["figma"]] = (None, None)
        res = self.run_once()
        self.assertUnclear(res["servers"]["figma"])
        self.assertEqual(self.reconnects(), [])
        self.assertFalse(read(os.environ["MCP_KEEPER_STATUS"])["auto"])

    def test_auto_reconnect_not_more_often_than_ten_minutes(self):
        self.probes[URLS["figma"]] = (None, None)
        self.run_once(NOW)
        self.run_once(NOW + 300)
        self.assertEqual(self.reconnects(), [])
        self.run_once(NOW + main.AUTO_EVERY + 1)
        self.assertEqual(self.reconnects(), ["figma"])

    def test_a_lost_server_reconnects_at_start(self):
        self.reject("figma", "canva")
        self.run_once()
        self.assertEqual(self.reconnects(), ["figma"], "a pass reconnects what is lost")
        st = main.load_state()
        st["reconnected"] = {}
        main.save_state(st)
        self.facts.clear()
        main.start_auto()
        self.assertEqual(self.reconnects(), ["figma"], "canva: a one-time login is not touched")
        self.facts.clear()
        st = main.load_state()
        st["auto"], st["reconnected"] = False, {}
        main.save_state(st)
        main.start_auto()
        self.assertEqual(self.reconnects(), [])


class Curl(Base):
    """Python on Windows may not trust the proxy's certificate while curl does: the third try is curl. The login
    headers go to curl in its config on stdin, never in the command line."""

    def test_headers_go_on_stdin_not_in_argv(self):
        seen = {}

        def run(cmd, **kw):
            seen["cmd"], seen["input"] = cmd, kw.get("input")
            return subprocess.CompletedProcess(cmd, 0, "401", "")
        with mock.patch.object(main.shutil, "which", lambda n: "/bin/curl"), \
                mock.patch.object(main.subprocess, "run", run), mock.patch.object(main, "curl_status", REAL_CURL):
            self.assertEqual(main.curl_status("https://x.example/mcp", {"Authorization": "Bearer secret"}, b"{}"), 401)
        self.assertEqual(seen["cmd"], ["/bin/curl", "-K", "-"])
        self.assertNotIn("secret", " ".join(seen["cmd"]))
        self.assertIn('header = "Authorization: Bearer secret"', seen["input"])
        self.assertIn("max-redirs = 0", seen["input"])

    def test_no_curl_is_no_answer(self):
        with mock.patch.object(main.shutil, "which", lambda n: None), mock.patch.object(main, "curl_status", REAL_CURL):
            self.assertIsNone(main.curl_status("https://x.example/mcp", {}, b"{}"))

    def test_probe_falls_back_to_curl_when_urllib_cannot_connect(self):
        class Opener:
            def open(self, *a, **kw):
                raise OSError("certificate has expired")
        for code, want in ((401, (False, 401)), (200, (True, 200)), (None, (None, None))):
            with mock.patch.object(main.urllib.request, "build_opener", lambda *a: Opener()), \
                    mock.patch.object(main, "curl_status", lambda *a, c=code: c):
                self.assertEqual(main.probe_with("https://x.example/mcp", {"Authorization": "Bearer t"}), want)


class ViewReconnect(Base):
    """The view's "Reconnect" button and the "Reconnect automatically" box: the owner's posts "reconnect <server>" and
    "auto on|off" in the plugin channel."""

    def setUp(self):
        super().setUp()
        self.reject("figma")
        self.run_once()
        self.facts.clear()
        self.hub["feeds"] = {main.NAME: []}

    def post(self, seq, text, by="owner"):
        self.hub["feeds"][main.NAME].append({"id": f"p{seq}", "by": by, "text": text, "ts": NOW})

    def rows(self):
        return read(os.environ["MCP_KEEPER_STATUS"])["servers"]

    def test_reconnect_sends_the_fact_and_checks_the_server_again(self):
        self.accept("figma")
        self.post(1, "reconnect figma")
        self.assertTrue(main.login_step(NOW + 1))
        self.assertIn(("mcp.reconnect", {"server": "figma"}), self.facts)
        row = self.rows()["figma"]
        self.assertEqual(row["state"], "ok")
        self.assertEqual((row["login"]["state"], row["login"]["act"]), ("done", "reconnect"))

    def test_reconnect_that_did_not_help_says_so(self):
        self.post(1, "reconnect figma")
        main.login_step(NOW + 1)
        row = self.rows()["figma"]
        self.assertEqual((row["state"], row["login"]["state"], row["login"]["act"]), ("lost", "fail", "reconnect"))

    def test_only_the_owner_and_only_a_known_server(self):
        self.post(1, "reconnect figma", by="worker")
        self.post(2, "auto off", by="worker")
        main.login_step(NOW + 1)
        self.assertEqual(self.facts, [])
        self.assertTrue(read(os.environ["MCP_KEEPER_STATUS"])["auto"])
        self.post(3, "reconnect nosuch")
        main.login_step(NOW + 2)
        self.assertEqual(self.facts, [])
        self.assertNotIn("nosuch", self.rows())

    def test_auto_box_is_kept(self):
        self.post(1, "auto off")
        main.login_step(NOW + 1)
        self.assertFalse(main.load_state()["auto"])
        self.assertFalse(read(os.environ["MCP_KEEPER_STATUS"])["auto"])
        self.run_once(NOW + 100)
        self.assertFalse(read(os.environ["MCP_KEEPER_STATUS"])["auto"], "a pass keeps the owner's choice")
        self.post(2, "auto on")
        main.login_step(NOW + 101)
        self.assertTrue(main.load_state()["auto"])


class ViewSwitch(Base):
    """0.7.0: a connected server has "Another login" instead of "Reconnect". The plugin cannot do it itself (the
    login of Claude Code is not ours to write): the owner's click "switch <server>" opens a terminal with claude (a fake
    opener here - no window is opened)."""

    def setUp(self):
        super().setUp()
        self.reject("canva")
        self.run_once()
        self.hub["feeds"] = {main.NAME: []}
        self.opened = []
        self.opener = lambda folder, cmd, hint, server: self.opened.append((folder, cmd, hint, server))

    def post(self, seq, text, by="owner"):
        self.hub["feeds"][main.NAME].append({"id": f"p{seq}", "by": by, "text": text, "ts": NOW})

    def rows(self):
        return read(os.environ["MCP_KEEPER_STATUS"])["servers"]

    def test_only_an_active_server_with_a_login_can_switch(self):
        rows = self.rows()
        self.assertTrue(rows["figma"]["can_switch"])
        self.assertNotIn("can_switch", rows["canva"], "a lost one is reconnected or logged in, not switched")

    def test_click_opens_a_terminal_with_claude_and_a_hint(self):
        self.post(1, "switch figma")
        self.assertTrue(main.login_step(NOW + 1, opener=self.opener))
        (folder, cmd, hint, server), = self.opened
        self.assertEqual((server, cmd), ("figma", main.claude_cmd()))
        self.assertTrue(os.path.isdir(folder))
        self.assertIn("Figma", hint)
        self.assertIn("Authenticate", hint)
        self.assertEqual(self.rows()["figma"]["login"], {"state": "done", "at": NOW + 1, "act": "switch"})
        self.assertNotIn("mcp.reconnect", [t for t, _ in self.facts], "the connected server is not touched")

    def test_a_server_that_cannot_switch_is_refused(self):
        self.post(1, "switch canva")
        self.post(2, "switch nosuch")
        main.login_step(NOW + 1, opener=self.opener)
        self.assertEqual(self.opened, [])
        row = self.rows()["canva"]["login"]
        self.assertEqual((row["state"], row["act"]), ("fail", "switch"))
        self.assertNotIn("nosuch", self.rows())

    def test_only_the_owner_clicks(self):
        self.post(1, "switch figma", by="worker")
        main.login_step(NOW + 1, opener=self.opener)
        self.assertEqual(self.opened, [])

    def test_no_claude_or_no_window_is_said_in_the_row(self):
        with mock.patch.object(main, "claude_cmd", lambda: None):
            self.post(1, "switch figma")
            main.login_step(NOW + 1, opener=self.opener)
        row = self.rows()["figma"]["login"]
        self.assertEqual((row["state"], row["why"]), ("fail", main.tr("switch_noclaude")))

        def boom(*a):
            raise OSError("no terminal")
        self.post(2, "switch figma")
        main.login_step(NOW + 2, opener=boom)
        self.assertEqual(self.rows()["figma"]["login"]["why"], main.tr("switch_failed"))
        self.assertEqual(self.opened, [])

    def test_the_row_of_a_terminal_goes_away_in_five_minutes(self):
        self.post(1, "switch figma")
        main.login_step(NOW + 1, opener=self.opener)
        with mock.patch.object(main.time, "time", lambda: NOW + 1 + main.SWITCH_SHOW + 1):
            main.write_status(main.load_state())
        self.assertNotIn("login", self.rows()["figma"])

    def test_windows_script_has_no_shell_specials_from_the_hint(self):
        if os.name != "nt":
            self.skipTest("the .cmd script is a Windows thing")
        with mock.patch.object(subprocess, "Popen") as popen:
            main.launch_terminal(TMP, ["claude"], 'a & b | c "d" %e% ^f', "figma")
        script = read(os.path.join(main.HOME, "switch-figma.cmd"), raw=True)
        echo = [x for x in script.splitlines() if x.startswith("echo a")]
        self.assertEqual(len(echo), 1)
        self.assertFalse(set('&|"%^') & set(echo[0][4:]), echo)
        self.assertIn("call claude", script)
        self.assertEqual(popen.call_args[0][0][:2], ["cmd.exe", "/c"])


class AllServers(Base):
    """0.7.0: every MCP the hub gives to its sessions is in the list, with a name for a person."""

    def test_names_for_a_person(self):
        names = {"mobbin": "Mobbin", "higgsfield": "Higgsfield", "granola": "Granola", "plugin:canva:canva": "Canva",
                 "github": "GitHub", "grafana": "Grafana", "ms365": "MS365", "icons8mcp": "Icons8",
                 "parallel-search": "Parallel Search", "pencil": "Pencil", "openai-developer-docs": "OpenAI Docs",
                 "teams": "Teams", "atlassian": "Atlassian", "buffer": "Buffer", "figma": "Figma",
                 "plugin:github:github": "GitHub"}
        for name, shown in names.items():
            self.assertEqual(main.human(name), shown, name)

    def test_rows_carry_the_title_and_the_machine_name_stays_the_key(self):
        self.run_once()
        rows = read(os.environ["MCP_KEEPER_STATUS"])["servers"]
        self.assertEqual(rows["figma"]["title"], "Figma")

    def test_a_program_and_a_server_without_a_login_are_listed_by_the_sessions_word(self):
        self.config({"mcp": {"files": {"server": {"command": "npx", "args": ["-y", "files-mcp"]}},
                             "docs": {"server": {"type": "http", "url": "https://docs.example/mcp"}},
                             "pencil": {"server": {"command": "pencil"}}}})
        self.hub["sessions"]["worker"]["info"].update(mcp=["files", "docs"], mcp_auth=["pencil"])
        with mock.patch.object(subprocess, "Popen", side_effect=AssertionError("started a process")):
            res = self.run_once()["servers"]
        self.assertEqual(res["files"], {"state": "ok"})
        self.assertEqual(res["pencil"]["state"], "lost")
        self.assertTrue(res["pencil"]["unclear"], "a session's word alone raises no alarm")
        self.assertEqual((self.notes(), self.dev()), ([], []))

    def test_no_word_from_sessions_is_not_checked_with_the_reason(self):
        self.config({"mcp": {"files": {"server": {"command": "npx", "args": ["-y", "files-mcp"]}}}})
        res = self.run_once()["servers"]
        self.assertEqual(res["files"]["state"], "skip")
        self.assertIn("program", res["files"]["why"])
        self.assertIn("no word", res["files"]["why"])

    def test_ignored_server_is_a_quiet_row(self):
        self.config({"mcp_auth_ignore": ["spytrend"],
                     "mcp": {"spytrend": {"server": {"type": "http", "url": "https://s.example/mcp"}}}})
        res = self.run_once()["servers"]
        self.assertEqual(res["spytrend"], {"state": "skip", "ignored": True})
        self.assertEqual((self.notes(), self.dev()), ([], []))
        self.assertTrue(read(os.environ["MCP_KEEPER_STATUS"])["servers"]["spytrend"]["ignored"])

    def test_the_twin_of_a_catalog_server_from_a_plugin_is_not_listed_twice(self):
        twin = {"plugin:figma:figma": {"type": "http", "url": URLS["figma"]}}
        with mock.patch.object(main, "plugin_servers", lambda: twin):
            res = self.run_once()["servers"]
        self.assertNotIn("plugin:figma:figma", res)
        self.assertIn("figma", res)


class SwitchKinds(Base):
    """0.7.3: "Another login" for every connected server that has an account, and what the click does depends on
    the kind: a stored login (claude), a program with a login of its own (MS365, Teams), a key (only the words for a
    person: nothing is read or written). A server without any account has no button, the row says so."""
    KEY = "glsa_supersecret"
    HEAD = "k-secret-header"

    def setUp(self):
        super().setUp()
        servers = {
            "figma": {"type": "http", "url": URLS["figma"]},
            "grafana": {"command": "uvx", "args": ["mcp-grafana"],
                        "env": {"GRAFANA_URL": "https://g.example/", "GRAFANA_SERVICE_ACCOUNT_TOKEN": self.KEY}},
            "keyed": {"type": "http", "url": "https://k.example/mcp", "headers": {"X-Api-Key": self.HEAD}},
            "ms365": {"command": "npx", "args": ["-y", "@softeria/ms-365-mcp-server@0.158.0", "--org-mode"], "env": {}},
            "teams": {"command": "npx", "args": ["-y", "@microsoft/workiq", "mcp"], "env": {}},
            "docs": {"type": "http", "url": "https://docs.example/mcp"},
            "pencil": {"command": "pencil"},
        }
        self.config({"mcp": {n: {"server": v} for n, v in servers.items()}})
        self.hub["sessions"]["worker"]["info"]["mcp"] = list(servers)
        self.hub["feeds"] = {main.NAME: []}
        self.opened = []
        self.opener = lambda folder, cmd, hint, server, env=None: self.opened.append((folder, cmd, hint, server, env))
        p = mock.patch.object(main.shutil, "which", lambda c, *a, **k: c)   # the programs are "installed"
        p.start()
        self.addCleanup(p.stop)
        self.run_once()

    def rows(self):
        return read(os.environ["MCP_KEEPER_STATUS"])["servers"]

    def click(self, *names):
        for i, n in enumerate(names, 1):
            self.hub["feeds"][main.NAME].append({"id": f"p{i}", "by": "owner", "text": "switch " + n, "ts": NOW})
        main.login_step(NOW + 1, opener=self.opener)

    def test_every_connected_server_with_an_account_has_the_button(self):
        rows = self.rows()
        for name, kind in (("figma", "oauth"), ("grafana", "key"), ("keyed", "key"), ("ms365", "ms365"),
                           ("teams", "workiq")):
            self.assertEqual((rows[name]["state"], rows[name].get("can_switch"), rows[name].get("account")),
                             ("ok", True, kind), name)
            self.assertNotIn("no_account", rows[name], name)

    def test_a_server_without_an_account_has_no_button_and_says_why(self):
        rows = self.rows()
        for name in ("docs", "pencil"):
            self.assertTrue(rows[name]["no_account"], name)
            self.assertNotIn("can_switch", rows[name], name)
        self.click("docs", "pencil")
        self.assertEqual(self.opened, [])
        self.assertEqual(self.rows()["docs"]["login"]["state"], "fail")

    def test_a_key_server_gets_the_words_for_a_person_and_nothing_else(self):
        self.click("grafana")
        (folder, cmd, hint, server, env), = self.opened
        self.assertEqual((server, cmd, env), ("grafana", [], None), "no program, no settings")
        self.assertIn("Grafana", hint)
        self.assertIn("new key", hint)
        self.assertIn("change the Grafana key", hint)
        self.assertNotIn("hub.json", hint)
        self.assertEqual(self.rows()["grafana"]["login"], {"state": "done", "at": NOW + 1, "act": "switch"})

    def test_ms365_and_teams_sign_out_and_in_with_their_own_program(self):
        self.click("ms365", "teams")
        by = {server: (cmd, env) for _, cmd, _, server, env in self.opened}
        self.assertEqual(by["ms365"][0], [["npx", "-y", "@softeria/ms-365-mcp-server@0.158.0", "--org-mode", "--logout"],
                                          ["npx", "-y", "@softeria/ms-365-mcp-server@0.158.0", "--org-mode", "--login"]])
        self.assertEqual(by["teams"][0], [["npx", "-y", "@microsoft/workiq", "auth", "logout"],
                                          ["npx", "-y", "@microsoft/workiq", "auth", "login"]])

    def test_a_program_with_a_key_among_its_arguments_is_not_run(self):
        self.config({"mcp": {"ms365": {"server": {"command": "npx", "args": ["-y", "ms-365-mcp-server", "--token=abc"]}}}})
        self.hub["sessions"]["worker"]["info"]["mcp"] = ["ms365"]
        self.run_once()
        self.click("ms365")
        self.assertEqual(self.opened, [])
        self.assertEqual(self.rows()["ms365"]["login"]["why"], main.tr("switch_no_program"))

    def test_the_oauth_server_still_gets_claude(self):
        self.click("figma")
        (_, cmd, hint, server, env), = self.opened
        self.assertEqual((server, cmd, env), ("figma", main.claude_cmd(), None))
        self.assertIn("Authenticate", hint)

    def test_no_key_reaches_the_status_or_the_login_file(self):
        self.click("grafana", "keyed", "ms365")
        for path in (os.environ["MCP_KEEPER_STATUS"], main.login_path()):
            text = read(path, raw=True)
            self.assertNotIn(self.KEY, text)
            self.assertNotIn(self.HEAD, text)

    def test_the_script_runs_the_commands_one_after_another(self):
        if os.name != "nt":
            self.skipTest("the .cmd script is a Windows thing")
        with mock.patch.object(subprocess, "Popen"):
            main.launch_terminal(TMP, [["a", "one"], ["a", "two"]], "words", "ms365", {"ORG": "x", "BAD": "a b"})
        script = read(os.path.join(main.HOME, "switch-ms365.cmd"), raw=True)
        self.assertLess(script.index("call a one"), script.index("call a two"))
        self.assertIn('set "ORG=x"', script)
        self.assertNotIn("BAD", script, "a setting with a space or a quote is not written into a script")


class Manifest(unittest.TestCase):
    def test_version_and_wording(self):
        m = read(os.path.join(ROOT, "hub-plugin.json"))
        self.assertEqual(m["version"], "0.9.0")
        text = json.dumps(m, ensure_ascii=False).lower()
        self.assertNotIn("live", text.replace("deliver", ""))
        self.assertNotIn("жив", text)


VIEW_BUTTONS = """
import { pathToFileURL } from "node:url";
const rows = JSON.parse(process.argv[2]);
const node = (tag, cls = "", text = "") => {
  const n = { tag, cls, text, kids: [], style: {}, firstChild: null };
  n.append = (...k) => { n.kids.push(...k); n.firstChild = n.kids[0] || null; };
  n.replaceChildren = (...k) => { n.kids = []; n.firstChild = null; n.append(...k); };
  return n;
};
globalThis.fetch = async () => ({ ok: true, json: async () => ({ v: 1, at: 1, every: 15, auto: true, servers: rows }) });
const view = {};
const mod = await import(pathToFileURL(process.argv[1]).href);
mod.default({ lang: "en", el: node, postFeed() {}, addView: (v) => Object.assign(view, v) });
const root = node("div");
view.render(root, {});
await new Promise((r) => setTimeout(r, 50));
const out = {};
const walk = (n, row) => {
  if (n.tag === "tr" && n.kids[0] && n.kids[0].tag === "td") row = n.kids[0].text;
  if (n.tag === "button") (out[row] = out[row] || []).push(n.cls.replace("btn ", ""));
  if (n.cls === "mk-noacc") (out[row + ":grey"] = out[row + ":grey"] || []).push(n.text);
  n.kids.forEach((k) => k && k.tag && walk(k, row));
};
walk(root, "");
console.log(JSON.stringify(out));
"""


class ViewButtons(unittest.TestCase):
    """0.7.1: a connected server has only "Another login"; "Log in" stays for a lost one and a never-logged-in one.
    The page is run in node with a stub hub (no browser)."""

    def buttons(self, rows):
        import shutil
        node = shutil.which("node")
        if not node:
            self.skipTest("no node")
        got = subprocess.run([node, "--input-type=module", "-e", VIEW_BUTTONS, os.path.join(ROOT, "main.js"), json.dumps(rows)],
                             capture_output=True, text=True, encoding="utf-8", timeout=60)
        self.assertEqual(got.returncode, 0, got.stderr)
        return json.loads(got.stdout)

    def test_an_active_server_has_only_connect_another_and_a_lost_one_keeps_login(self):
        rows = {"figma": {"state": "ok", "title": "Figma", "can_login": True, "can_switch": True},
                "buffer": {"state": "lost", "title": "Buffer", "can_login": True},
                "mobbin": {"state": "first", "title": "Mobbin", "can_login": True}}
        got = self.buttons(rows)
        self.assertEqual(got["Figma"], ["mk-switch"])
        self.assertEqual(got["Buffer"], ["mk-reconnect", "mk-login"])
        self.assertEqual(got["Mobbin"], ["mk-reconnect", "mk-login"])

    def test_an_active_server_without_a_login_of_its_own_has_no_buttons(self):
        got = self.buttons({"canva": {"state": "ok", "title": "Canva", "can_login": True}})
        self.assertEqual(got.pop("", None), ["mk-add-open"], "0.8.0: the one button outside the table rows is \"Add MCP\"")
        self.assertEqual(got, {})

    def test_a_server_without_an_account_shows_grey_words_and_no_button(self):
        got = self.buttons({"docs": {"state": "ok", "title": "Docs", "no_account": True},
                            "figma": {"state": "ok", "title": "Figma", "can_switch": True}})
        self.assertEqual(got["Docs:grey"], ["no account"])
        self.assertNotIn("Docs", got)
        self.assertNotIn("Figma:grey", got)

    def test_the_process_never_gives_an_active_row_a_login(self):
        got = main.clean_status({"servers": {"a": {"state": "ok", "can_login": True, "can_switch": True},
                                              "b": {"state": "lost", "can_login": True}}})["servers"]
        self.assertNotIn("can_login", got["a"])
        self.assertTrue(got["a"]["can_switch"])
        self.assertTrue(got["b"]["can_login"])


VIEW_ADD = """
import { pathToFileURL } from "node:url";
const arg = JSON.parse(process.argv[2]);
const has = (n, c) => (n.cls || "").split(" ").includes(c);
const node = (tag, cls = "", text = "") => {
  const n = { tag, cls, text, kids: [], style: {}, firstChild: null, value: "" };
  n.append = (...k) => { n.kids.push(...k); n.firstChild = n.kids[0] || null; };
  n.replaceChildren = (...k) => { n.kids = []; n.firstChild = null; n.append(...k); };
  n.querySelector = (sel) => find(n, sel.slice(1));
  n.focus = () => {};
  return n;
};
const find = (n, c) => {
  if (has(n, c)) return n;
  for (const k of n.kids) { const f = k && k.tag ? find(k, c) : null; if (f) return f; }
  return null;
};
const all = (n, out = []) => { out.push(n); n.kids.forEach((k) => k && k.tag && all(k, out)); return out; };
const posts = [];
globalThis.fetch = async () => ({ ok: true, json: async () => ({ v: 1, at: 1, every: 15, auto: true, servers: {}, ...arg.extra }) });
const view = {};
const mod = await import(pathToFileURL(process.argv[1]).href);
mod.default({ lang: "en", el: node, postFeed: (t) => posts.push(t), addView: (v) => Object.assign(view, v) });
const root = node("div");
view.render(root, {});
await new Promise((r) => setTimeout(r, 30));
for (const step of arg.steps) {
  if (step.click) find(root, step.click).onclick();
  if (step.type) {
    const input = find(root, step.type[0]);
    input.onfocus();
    input.value = step.type[1];
    input.oninput();
  }
  await new Promise((r) => setTimeout(r, 10));
}
const texts = {};
for (const n of all(root)) for (const c of (n.cls || "").split(" ")) if (c.startsWith("mk-") && n.text) texts[c] = n.text;
const classes = all(root).flatMap((n) => (n.cls || "").split(" ")).filter((c) => c.startsWith("mk-add"));
console.log(JSON.stringify({ posts, texts, classes }));
process.exit(0);
"""


class ViewAddForm(unittest.TestCase):
    """0.8.0: the "Add MCP" button and form, run in node with a stub hub (no browser)."""

    def run_view(self, steps, extra=None):
        import shutil
        node = shutil.which("node")
        if not node:
            self.skipTest("no node")
        arg = {"steps": steps, "extra": extra or {}}
        got = subprocess.run([node, "--input-type=module", "-e", VIEW_ADD, os.path.join(ROOT, "main.js"), json.dumps(arg)],
                             capture_output=True, text=True, encoding="utf-8", timeout=60)
        self.assertEqual(got.returncode, 0, got.stderr)
        return json.loads(got.stdout)

    FILL = [{"click": "mk-add-open"}, {"type": ["mk-add-name", "newsvc"]}, {"type": ["mk-add-url", "https://mcp.example.com/mcp"]}]

    def test_the_button_is_under_the_table_and_opens_a_form(self):
        got = self.run_view([])
        self.assertEqual(got["texts"]["mk-add-open"], "Add MCP")
        self.assertNotIn("mk-add-form", got["classes"])
        got = self.run_view([{"click": "mk-add-open"}])
        self.assertTrue({"mk-add-name", "mk-add-url", "mk-add-go", "mk-add-cancel"} <= set(got["classes"]))
        self.assertNotIn("mk-add-bad", got["classes"])

    def test_cancel_closes_the_form_without_a_post(self):
        got = self.run_view([{"click": "mk-add-open"}, {"click": "mk-add-cancel"}])
        self.assertIn("mk-add-open", got["classes"])
        self.assertEqual(got["posts"], [])

    def test_a_good_name_and_address_post_one_add(self):
        got = self.run_view(self.FILL + [{"click": "mk-add-go"}])
        self.assertEqual(got["posts"], ["add newsvc https://mcp.example.com/mcp"])
        self.assertEqual(got["texts"]["mk-add-sent"], "adding…")
        self.assertNotIn("mk-add-open", got["classes"], "while it is added the form is not offered again")

    def test_a_bad_name_or_address_posts_nothing_and_says_why(self):
        bad = ({"type": ["mk-add-name", "a b"]}, {"type": ["mk-add-url", "http://example.com/mcp"]},
               {"type": ["mk-add-url", "https://example.com/mcp?key=abc"]}, {"type": ["mk-add-url", "https://u:p@example.com/mcp"]})
        for step in bad:
            steps = [{"click": "mk-add-open"}, {"type": ["mk-add-name", "newsvc"]}, {"type": ["mk-add-url", "https://mcp.example.com/mcp"]},
                     step, {"click": "mk-add-go"}]
            got = self.run_view(steps)
            self.assertEqual(got["posts"], [], step)
            self.assertIn("mk-add-bad", got["classes"], step)
            self.assertTrue(got["texts"]["mk-add-bad"].startswith("Check the name"), step)

    def test_the_answer_of_the_process_is_shown(self):
        add = {"state": "fail", "name": "newsvc", "at": 5, "why": "this name or address is already there"}
        got = self.run_view([], {"add": add})
        self.assertEqual(got["texts"]["mk-fail"], "Not added: this name or address is already there")
        self.assertIn("mk-add-open", got["classes"], "after a failure the owner can try again")
        add = {"state": "done", "name": "newsvc", "at": 5, "login": {"state": "wait"}}
        got = self.run_view([], {"add": add})
        self.assertEqual(got["texts"]["mk-done"], "Added: newsvc")
        self.assertNotIn("Sessions get it", got["texts"]["mk-add-login"])
        add["login"] = {"state": "done"}
        got = self.run_view([], {"add": add})
        self.assertEqual(got["texts"]["mk-add-login"], "Sessions get it after they restart.")
        add["login"] = {"state": "fail", "why": "the browser was closed"}
        got = self.run_view([], {"add": add})
        self.assertTrue(got["texts"]["mk-add-login"].endswith(": the browser was closed"))


class JsStrings(unittest.TestCase):
    def test_view_keys_match_and_cyrillic_only_in_ru(self):
        import re
        with open(os.path.join(ROOT, "main.js"), encoding="utf-8") as f:
            src = f.read()
        en, ru = src.index("  en: {"), src.index("  ru: {")
        end = src.index("\n};", ru)
        keys = lambda part: set(re.findall(r"(\w+): \"", part))
        self.assertEqual(keys(src[en:ru]), keys(src[ru:end]))
        self.assertTrue({"reconnect", "auto", "relogin", "switch", "ignored"} <= keys(src[en:ru]))
        self.assertNotIn("later", keys(src[en:ru]))
        self.assertFalse(re.search("[а-яА-ЯёЁ]", src[:ru] + src[end:]))


class ConnectedWord(unittest.TestCase):
    """0.7.2 (owner): the state of a working login is "connected" (ru: "подключён"), not "active"."""

    def test_no_active_word_in_what_the_owner_reads(self):
        import re
        for name in ("main.js", "main.py", "hub-plugin.json", "README.md"):
            with open(os.path.join(ROOT, name), encoding="utf-8") as f:
                src = f.read()
            self.assertFalse(re.search(r"активен|активны|активн|Last active|last active|active", src, re.I), name)

    def test_the_state_and_the_column_say_connected(self):
        with open(os.path.join(ROOT, "main.js"), encoding="utf-8") as f:
            src = f.read()
        self.assertIn('ok: "connected"', src)
        self.assertIn('ok_at: "Last connected"', src)
        self.assertIn('ok: "подключён"', src)
        self.assertIn("Последний раз подключён", src)
        self.assertIn("подключены {ok}", main.STR["ru"]["status"])


class AnotherLoginName(unittest.TestCase):
    """0.8.1 (owner): the button of a connected server is "Another login" (ru: "Другой логин"), in the page, the manifest
    and the README, and the old name is gone everywhere."""

    OLD_EN = "Connect another account"
    OLD_RU = "Подключить другой"   # Podklyuchit drugoy
    NEW_RU = "Другой логин"   # Drugoy login

    def src(self, name):
        with open(os.path.join(ROOT, name), encoding="utf-8") as f:
            return f.read()

    def test_the_page_labels_the_button_by_the_new_name(self):
        js = self.src("main.js")
        self.assertIn('switch: "Another login"', js)
        self.assertIn('switch: "%s"' % self.NEW_RU, js)

    def test_the_old_name_is_nowhere(self):
        for name in ("main.js", "main.py", "hub-plugin.json", "README.md"):
            text = self.src(name)
            self.assertNotIn(self.OLD_EN, text, name)
            self.assertNotIn(self.OLD_RU, text, name)

    def test_the_manifest_and_the_readme_name_the_button(self):
        m = read(os.path.join(ROOT, "hub-plugin.json"))
        self.assertIn("Another login", m["description"])
        self.assertIn(self.NEW_RU, m["i18n"]["ru"]["description"])
        readme = self.src("README.md")
        self.assertIn("**Another login**", readme)
        self.assertIn("**%s**" % self.NEW_RU, readme)


class CatalogRules(unittest.TestCase):
    """0.7.2: what the catalog of Flagman asks of a plugin - the README says what is read, written and sent (the switch
    script and the terminal included), the manifest asks for the page and the process only, `hub` is not above the
    Plugin API of the released core (1.30 in v1.5.1)."""

    def readme(self):
        with open(os.path.join(ROOT, "README.md"), encoding="utf-8") as f:
            return f.read()

    def test_readme_names_what_is_read_written_and_sent(self):
        src = self.readme()
        self.assertIn("## What it reads, writes and sends", src)
        for word in ("**Reads:**", "**Writes:**", "**Sends:**", "**Starts programs:**"):
            self.assertIn(word, src)

    def test_readme_names_the_switch_script_and_the_terminal(self):
        src = self.readme()
        for name in ("switch-<server>.cmd", "switch-<server>.command", "switch-<server>.sh", "switch-<сервер>.cmd"):
            self.assertIn(name, src)
        self.assertIn("terminal window", src)

    def test_the_script_names_in_the_readme_are_the_ones_the_code_writes(self):
        with open(os.path.join(ROOT, "main.py"), encoding="utf-8") as f:
            code = f.read()
        self.assertIn('"switch-" + safe + ".cmd"', code)
        self.assertIn('".command" if sys.platform == "darwin" else ".sh"', code)

    def test_readme_says_that_add_mcp_writes_the_claude_file_and_the_hub_settings(self):
        """0.8.0: the one place where the plugin's programs write ~/.claude.json and hub.json is said, with the commands."""
        src = self.readme()
        for word in ("**Add MCP, on a click.**", "claude mcp add --transport http -s user", "hub tools import mcp",
                     "claude mcp remove", "`~/.claude.json`", "Exception, only on your click on Add MCP",
                     "Добавить MCP"):
            self.assertIn(word, src)
        self.assertNotIn("It never writes the credential stores, `~/.claude.json`", src, "the old promise is no longer true")

    def test_the_commands_in_the_readme_are_the_ones_the_code_runs(self):
        with open(os.path.join(ROOT, "main.py"), encoding="utf-8") as f:
            code = f.read()
        self.assertIn('["mcp", "add", "--transport", "http", "-s", "user", name, url]', code)
        self.assertIn('"tools", "import", "mcp", name, "--about"', code)
        self.assertIn('["mcp", "remove", "-s", "user", name]', code)

    def test_the_manifest_asks_for_the_page_and_the_process_only(self):
        with open(os.path.join(ROOT, "hub-plugin.json"), encoding="utf-8") as f:
            man = json.load(f)
        rights = {"run", "web", "hooks", "mcp", "skills", "agents", "tools", "commands"} & set(man)
        self.assertEqual(rights, {"run", "web"})
        self.assertEqual(man["web"], "main.js")

    def test_hub_is_not_above_the_released_plugin_api(self):
        with open(os.path.join(ROOT, "hub-plugin.json"), encoding="utf-8") as f:
            man = json.load(f)
        major, minor = man["hub"].lstrip("^>=~ ").split(".")[:2]
        self.assertLessEqual((int(major), int(minor)), (1, 30))


    def test_the_description_fits_the_catalog_limit(self):
        with open(os.path.join(ROOT, "hub-plugin.json"), encoding="utf-8") as f:
            man = json.load(f)
        self.assertLessEqual(len(man["description"]), 300)
        self.assertLessEqual(len(man["i18n"]["ru"]["description"]), 300)


class Strings(unittest.TestCase):
    def test_same_keys_in_every_language(self):
        for lang in main.STR:
            self.assertEqual(set(main.STR[lang]), set(main.STR["en"]), lang)

    def test_no_cyrillic_outside_ru(self):
        import re
        with open(os.path.join(ROOT, "main.py"), encoding="utf-8") as f:
            src = f.read()
        start, end = src.index('    "ru": {'), src.index("\n}\n", src.index('    "ru": {'))
        self.assertFalse(re.search("[а-яА-ЯёЁ]", src[:start] + src[end:]))


if __name__ == "__main__":
    unittest.main()
