"""mcp-keeper: no real claude, network or keychain - a fake `claude` script, a credentials file and patched probes."""
import json, os, sys, tempfile, time, unittest
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

NOW = 2_000_000_000.0


def write(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(data if isinstance(data, str) else json.dumps(data))


class Base(unittest.TestCase):
    def setUp(self):
        for root, dirs, files in os.walk(TMP, topdown=False):
            for f in files:
                os.unlink(os.path.join(root, f))
        write(os.environ["HUB_CONFIG"], {"language": "en"})
        os.makedirs(os.path.join(TMP, "claude"), exist_ok=True)
        self.facts = []
        self.lists = []   # outputs of `claude mcp list`, one per call; the last repeats
        patches = [
            mock.patch.object(main, "put", lambda t, d, entity=None: self.facts.append((t, d))),
            mock.patch.object(main, "mcp_list", self.fake_list),
            mock.patch.object(main, "probe", lambda ent: self.probes.get(ent.get("serverUrl"), (True, 200))),
            mock.patch.object(main, "net_up", lambda: True),
            mock.patch.object(main, "processes", lambda name: [{"pid": 1, "started": "x", "cmd": "claude"}]),
            mock.patch.object(main, "lang", lambda: "en"),
            mock.patch.object(main.time, "sleep", self.tick),
            mock.patch.object(main, "mono", lambda: self.clock),
            mock.patch.object(main, "drift", lambda: 0.0),
            mock.patch.object(main, "hub_state", lambda: self.hub),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.probes = {}
        self.clock = 100_000.0
        self.hub = {"system": ["hub-dev"], "sessions": {"hub": {"kind": "hub", "state": "idle"},
                                 "hub-dev": {"state": "idle"},
                                 "worker": {"state": "busy", "info": {"mcp": ["canva", "figma"]}},
                                 "sleepy": {"state": "parked", "info": {"mcp": ["canva"]}}}}
        main.CALM.shaken = self.clock - 10 * main.COOLDOWN   # a settled machine
        main.CALM.down = False
        main.CALM.mark = 0.0

    def tick(self, seconds):
        self.clock += seconds

    def fake_list(self):
        out = self.lists.pop(0) if len(self.lists) > 1 else self.lists[0]
        return 0, out

    def creds(self, **entries):
        write(os.environ["MCP_KEEPER_CREDS"], {"mcpOAuth": {f"{n}|abc": {"serverName": n, **e} for n, e in entries.items()}})

    def notes(self):
        return [d for t, d in self.facts if t == "owner.notify"]

    def dev(self):
        return [d for t, d in self.facts if t == "message.received" and d["to"] == "hub-dev"]

    def run_once(self, now=NOW):
        self.facts.clear()
        return main.once(now)


L_OK = "canva: https://mcp.canva.com/mcp (HTTP) - ✓ Connected\nfigma: https://mcp.figma.com/mcp (HTTP) - ✓ Connected\n"
L_CANVA_AUTH = "canva: https://mcp.canva.com/mcp (HTTP) - ! Needs authentication\nfigma: https://mcp.figma.com/mcp (HTTP) - ✓ Connected\n"


class Parse(Base):
    def test_list_lines(self):
        got = main.parse_list("Checking MCP server health...\n\n" + L_CANVA_AUTH
                              + "plugin:slack:slack: https://x (HTTP) - ✗ Failed to connect\n"
                              + "old: npx old (stdio) - Disabled\n")
        self.assertEqual(got, {"canva": "auth", "figma": "connected", "plugin:slack:slack": "failed"})

    def test_connected_with_slow_tool_list_is_live(self):
        # after a hub restart: the login was taken, only the tool list timed out - live, not "does not connect"
        got = main.parse_list("github: https://g/mcp/ (HTTP) - ! Connected · tools fetch failed — Request timed out\n"
                              "x: npx -y mcp-remote https://x - ✘ Failed to connect — MCP server \"x\" connection "
                              "timed out after 30000ms\n")
        self.assertEqual(got, {"github": "connected", "x": "failed"})
        self.lists = ["canva: https://c (HTTP) - ! Connected · tools fetch failed — Request timed out\n"]
        self.assertEqual(self.run_once()["servers"]["canva"]["state"], "ok")


class Pass(Base):
    def test_all_live_no_alarm_and_status_post(self):
        self.lists = [L_OK]
        res = self.run_once()
        self.assertEqual({n: s["state"] for n, s in res["servers"].items()}, {"canva": "ok", "figma": "ok"})
        self.assertEqual(self.notes(), [])
        posts = [d for t, d in self.facts if t == "ext.post"]
        self.assertEqual(posts, [{"plugin": "mcp-keeper", "text": "MCP: 2 live, 0 lost, 0 later"}])   # a human line only
        with open(os.environ["MCP_KEEPER_STATUS"], encoding="utf-8") as f:
            self.assertEqual(json.load(f)["servers"]["canva"]["state"], "ok")   # the view's data - the file
        self.run_once(NOW + 900)
        self.assertEqual([t for t, d in self.facts if t == "ext.post"], [])   # same picture - no new post

    def test_first_login_is_not_counted_as_lost(self):
        self.lists = [L_OK + "mobbin: https://m (HTTP) - ! Needs authentication\n"]
        self.run_once()
        self.assertEqual([d["text"] for t, d in self.facts if t == "ext.post"], ["MCP: 2 live, 0 lost, 0 later"])

    def test_lost_after_live_is_urgent_one_message_and_repeats_hourly(self):
        self.lists = [L_OK]
        self.run_once(NOW)
        self.lists = [L_CANVA_AUTH]
        self.run_once(NOW + 900)
        notes = self.notes()
        self.assertEqual(len(notes), 1)
        self.assertTrue(notes[0]["urgent"])
        self.assertIn("hub mcp login hub canva", notes[0]["text"])
        sent = [d for t, d in self.facts if t == "message.received"]
        self.assertEqual([d["to"] for d in sent], ["worker"])   # busy with canva; the parked one is not woken
        self.run_once(NOW + 1800)
        self.assertEqual(self.notes(), [], "not again within the hour")
        self.assertEqual([t for t, d in self.facts if t == "message.received"], [], "sessions told once")
        self.run_once(NOW + 900 + 3600)
        self.assertEqual(len(self.notes()), 1, "hourly while lost")

    def test_live_again_lifts_the_alarm(self):
        self.lists = [L_OK]
        self.run_once(NOW)
        self.lists = [L_CANVA_AUTH]
        self.run_once(NOW + 900)
        self.lists = [L_OK]
        self.run_once(NOW + 1800)
        self.assertIn(("owner.notify_resolved", {"id": "mcp"}), self.facts)

    def test_single_auth_answer_is_rechecked(self):
        self.lists = [L_OK]
        self.run_once(NOW)
        self.lists = [L_CANVA_AUTH, L_OK]   # the second ask says connected
        res = self.run_once(NOW + 900)
        self.assertEqual(res["servers"]["canva"]["state"], "ok")
        self.assertEqual(self.notes(), [])

    def test_rejected_access_on_connected_server(self):
        self.creds(figma={"serverUrl": "https://mcp.figma.com/mcp", "accessToken": "t", "refreshToken": "r",
                          "expiresAt": (NOW + 3600) * 1000})
        self.probes["https://mcp.figma.com/mcp"] = (False, 401)
        with mock.patch.object(main.time, "time", lambda: NOW):
            self.lists = [L_OK]
            res = self.run_once(NOW)
        self.assertEqual(res["servers"]["figma"]["state"], "lost")
        self.assertIn("HTTP 401", self.notes()[0]["text"])

    def test_network_doubt_is_not_lost(self):
        self.creds(figma={"serverUrl": "https://mcp.figma.com/mcp", "accessToken": "t", "refreshToken": "r",
                          "expiresAt": (NOW + 3600) * 1000})
        self.probes["https://mcp.figma.com/mcp"] = (None, None)
        with mock.patch.object(main.time, "time", lambda: NOW):
            self.lists = [L_OK]
            res = self.run_once(NOW)
        self.assertEqual(res["servers"]["figma"]["state"], "ok")
        self.assertEqual(self.notes(), [])

    def test_wiped_record_of_a_live_server(self):
        self.creds(canva={"serverUrl": "https://mcp.canva.com/mcp", "accessToken": "t", "refreshToken": "r",
                          "expiresAt": (NOW + 3600) * 1000})
        with mock.patch.object(main.time, "time", lambda: NOW):
            self.lists = [L_OK]
            self.run_once(NOW)
            self.creds(canva={"serverUrl": "https://mcp.canva.com/mcp"})   # the record lost its tokens
            res = self.run_once(NOW + 900)
        self.assertEqual(res["servers"]["canva"]["state"], "lost")
        self.assertIn("erased", self.notes()[0]["text"])

    def test_one_time_loss_keeps_process_trace(self):
        self.lists = [L_OK]
        self.run_once(NOW)
        self.lists = [L_CANVA_AUTH]
        self.run_once(NOW + 900)
        trace = main.load_state()["traces"]["canva"]
        self.assertEqual(trace["at"], NOW + 900)
        self.assertEqual(trace["processes"][0]["pid"], 1)

    def test_most_important_first_in_one_message(self):
        self.lists = ["figma: https://f (HTTP) - ✓ Connected\nmobbin: https://m (HTTP) - ✓ Connected\n"
                      "canva: https://c (HTTP) - ✓ Connected\n"]
        self.run_once(NOW)
        self.lists = ["figma: https://f (HTTP) - ! Needs authentication\nmobbin: https://m (HTTP) - ! Needs authentication\n"
                      "canva: https://c (HTTP) - ! Needs authentication\n"]
        self.run_once(NOW + 900)
        notes = self.notes()
        self.assertEqual(len(notes), 1)
        lines = notes[0]["text"].splitlines()
        self.assertTrue(lines[1].startswith("• canva"), lines)    # one-time login first
        self.assertTrue(lines[2].startswith("• figma"), lines)    # then the one a working session uses

    def test_first_login_goes_to_hub_developer_once_a_day_and_plugin_server_is_silent(self):
        self.lists = ["newone: https://n (HTTP) - ! Needs authentication\n"
                      "plugin:x:x: https://p (HTTP) - ! Needs authentication\n"]
        res = self.run_once(NOW)
        self.assertEqual(res["servers"]["plugin:x:x"]["state"], "idle")
        self.assertEqual(self.notes(), [])   # blocks no work: not the owner
        dev = self.dev()
        self.assertEqual(len(dev), 1)
        self.assertIn("newone", dev[0]["text"])
        self.assertNotIn("plugin:x:x", dev[0]["text"])
        self.run_once(NOW + 3600)
        self.assertEqual(self.dev(), [])
        self.run_once(NOW + main.DAY + 1)
        self.assertEqual(len(self.dev()), 1)

    def test_old_owner_notice_of_0_1_0_is_lifted_once(self):
        write(os.path.join(main.HOME, "state.json"), {"told_dup": ["figma"], "told_first": ["mobbin"]})
        self.lists = [L_OK]
        self.run_once(NOW)
        self.assertIn(("owner.notify_resolved", {"id": "mcp"}), self.facts)
        self.run_once(NOW + 900)
        self.assertNotIn(("owner.notify_resolved", {"id": "mcp"}), self.facts)

    def test_ignore_lists(self):
        write(os.environ["HUB_CONFIG"], {"mcp_auth_ignore": ["canva"],
                                         "extensions": {"mcp-keeper": {"ignore": ["figma"]}}})
        self.lists = [L_CANVA_AUTH]
        res = self.run_once(NOW)
        self.assertEqual(res["servers"], {})

    def test_unsettled_machine_does_not_run_the_list(self):
        main.CALM.shaken = self.clock   # just woke up
        called = []
        with mock.patch.object(main, "mcp_list", lambda: called.append(1) or (0, L_OK)), \
                mock.patch.object(main, "MAX_WAIT", 0):
            res = self.run_once(NOW)
        self.assertTrue(res["later_all"])
        self.assertEqual(called, [])

    def test_quiet_until_holds_alarms(self):
        write(os.environ["HUB_CONFIG"], {"extensions": {"mcp-keeper": {"quiet_until": NOW + 10_000}}})
        self.lists = [L_OK]
        self.run_once(NOW)
        self.lists = [L_CANVA_AUTH]
        self.run_once(NOW + 900)
        self.assertEqual(self.notes(), [])

    def test_duplicate_of_plugin_server(self):
        d = os.environ["CLAUDE_CONFIG_DIR"]
        inst = os.path.join(TMP, "plug")
        write(os.path.join(inst, ".mcp.json"), {"mcpServers": {"canva": {"type": "http", "url": "https://mcp.canva.com/mcp"}}})
        write(os.path.join(d, "settings.json"), {"enabledPlugins": {"canva@market": True}})
        write(os.path.join(d, "plugins", "installed_plugins.json"), {"plugins": {"canva@market": [{"installPath": inst}]}})
        write(os.path.join(d, ".claude.json"), {"mcpServers": {"canva": {"type": "http", "url": "https://mcp.canva.com/mcp/"}}})
        self.assertEqual(main.duplicates(), [{"name": "canva", "plugin": "plugin:canva:canva"}])
        self.lists = [L_OK]
        self.run_once(NOW)
        self.assertEqual(self.notes(), [])   # a duplicate is the Hub Developer's, not an urgent alarm to the owner
        self.assertIn("claude mcp remove canva -s user", self.dev()[0]["text"])
        self.run_once(NOW + 900)
        self.assertEqual(self.dev(), [])   # once a day


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
