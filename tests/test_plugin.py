"""mcp-keeper: no real claude, network or keychain - a fake `claude` script, a credentials file and patched probes."""
import json, os, signal, stat, subprocess, sys, tempfile, time, unittest
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
        patches = [
            mock.patch.object(main, "put", lambda t, d, entity=None: self.facts.append((t, d))),
            mock.patch.object(main, "mcp_get", self.fake_get),
            mock.patch.object(main, "probe", lambda ent: self.probes.get(ent.get("serverUrl"), (True, 200))),
            mock.patch.object(main, "net_up", lambda: True),
            mock.patch.object(main, "processes", lambda name: [{"pid": 1, "started": "x", "kind": "claude"}]),
            mock.patch.object(main, "lang", lambda: "en"),
            mock.patch.object(main.time, "sleep", self.tick),
            mock.patch.object(main, "mono", lambda: self.clock),
            mock.patch.object(main, "drift", lambda: 0.0),
            mock.patch.object(main, "hub_state", lambda: self.hub),
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
        self.assertIsNone(main.parse_get("No MCP server found with name: figma"))
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
        self.assertEqual(res["servers"], {})
        self.assertEqual(self.asked, [])

    def test_header_server_is_not_judged_by_a_leftover_login(self):
        self.config({"mcp": {"buffer": {"server": {"type": "http", "url": "https://b/mcp", "headers": {"X": "y"}}}}})
        recs = read(os.environ["MCP_KEEPER_CREDS"])
        recs["mcpOAuth"]["buffer|x"] = {"serverName": "buffer", "serverUrl": "https://b/mcp", "accessToken": "old"}
        write(os.environ["MCP_KEEPER_CREDS"], recs)
        self.assertNotIn("buffer", self.run_once()["servers"])


class Pass(Base):
    def test_all_live_no_alarm_and_status_post(self):
        res = self.run_once()
        self.assertEqual({s["state"] for s in res["servers"].values()}, {"ok"})
        self.assertEqual(self.notes(), [])
        posts = [d for t, d in self.facts if t == "ext.post"]
        self.assertEqual(posts, [{"plugin": "mcp-keeper", "text": "MCP: 4 live, 0 lost, 0 later"}])   # a human line only
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
        self.assertEqual(res["servers"]["figma"]["state"], "later")
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
        self.assertEqual(res["servers"]["figma"]["state"], "later")
        self.run_once(NOW + 3700)
        self.assertGreater(len(self.asked), n)

    def test_one_time_login_never_goes_through_claude(self):
        self.expire("canva")
        res = self.run_once()
        self.assertNotIn("canva", self.asked)
        self.assertEqual(res["servers"]["canva"]["state"], "later")

    def test_ask_claude_off(self):
        self.config({}, {"ask_claude": False})
        self.expire("figma")
        res = self.run_once()
        self.assertEqual(self.asked, [])
        self.assertEqual(res["servers"]["figma"]["state"], "later")

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
        self.assertNotIn("figma", res["servers"])
        self.assertEqual((self.notes(), self.dev()), ([], []))

    def test_ignore_lists(self):
        self.config({"mcp_auth_ignore": ["canva"]}, {"ignore": ["figma"]})
        self.reject("canva", "figma")
        res = self.run_once(NOW)
        self.assertEqual(sorted(res["servers"]), ["mobbin", "notion"])

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
        self.assertEqual(got["servers"], {"a": {"state": "ok", "ok_at": 2}})


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
