# mcp-keeper - MCP login keeper

An [Agents Hub](https://github.com/flagman/agents-hub-core) plugin: every 15 minutes it checks the OAuth logins of the MCP
servers **the hub gives to its sessions** and says so once, before a session gets stuck on a lost login.

## What it watches (and what it does not)

- **Only what sessions get.** The servers of the hub catalog (`hub.json` `mcp.<name>.server`) and the MCP servers of
  plugins (`plugin:<plugin>:<server>`) that live sessions carry. Hub sessions run with `--strict-mcp-config`: they never
  read `~/.claude.json`, so the MCP of a plain Claude Code outside the hub is not checked.
- **Remote servers only.** A server that runs as a program (stdio) is never started. `claude mcp list` is never run
  (it starts every stdio server). A server with `headers`/`headersHelper` logs in by itself and is skipped.
- **No second watchdog.** The core's MCP watch already tells sessions about "needs a login" and reconnects them. The
  plugin adds what it does not do: a stored login that a server now rejects, an erased record, a token that cannot be
  refreshed - and tells the owner once.

## One pass

1. The stored access token (Claude Code credentials: the macOS keychain item or `.credentials.json`, read only, never
   written) goes in an `initialize` request to the server's own address. 401/403 - the login is rejected.
2. The access has expired and a refresh token exists: Claude Code refreshes it itself, under its own lock. The plugin asks
   `claude mcp get <server>` - this one server, at most once an hour while the stored login does not change, never for
   a one-time login (`one_time`, default `canva`: a run killed in the middle of the exchange loses it). If you prefer
   that the plugin never runs `claude`, set `ask_claude` to `false`: such a server is "later".
3. `claude mcp get` runs with a time limit (90 s). When it runs over, the whole process tree is killed (the process
   group on macOS/Linux, `taskkill /T` on Windows) - no orphans, no hung `claude`.

Lost is only an explicit refusal (401/403, "needs authentication" twice, an erased record, an expired token without a
refresh token). A timeout, no network, a 5xx - "check later", nothing is said. A pass runs only after 5 minutes of a live
network without sleep, and a new loss is told only if a second pass 5 minutes later sees it too.

## Alarms

- **One alarm per server per day**, kept on disk (`<hub state>/mcp-keeper/state.json`, so a restart does not repeat
  it). A server stays "open" until a pass sees it live; it is told again only after it was seen live and lost again,
  and never within a day of its last alarm.
- **A cap for the whole plugin**: at most `max_alarms` (default 6) messages a day; the rest wait for room and go in one
  message.
- One urgent message per pass lists the newly lost servers, the most important first (one-time logins, servers a working
  session uses right now), each with `hub mcp login <hub session> <server>`. Lifted when a check sees the server live.
- Sessions that work with a lost server right now get one line (once per loss): do not loop on its tools.
- A server never logged in is never an alarm.

## Duplicates (off by default)

A user server in `~/.claude.json` with the address of a plugin's server hides the plugin's one from Claude Code
(the login under `plugin:...` is never refreshed). It touches only Claude Code outside the hub, so it is **off by
default**; `check_duplicates: true` sends one line a day to the hub's developer session.

## What is stored

Server names, states and times - nothing else. No command lines, arguments, headers, addresses or query strings: the
`claude mcp get` output is parsed in memory and dropped; the trail kept when a one-time login is lost holds only
`{pid, started, kind}` of the processes of that moment. Files written by older versions (the raw `claude mcp list`
output, command lines, free-form status rows) are removed or rewritten at start.

## Settings

`hub.json`, `extensions.<plugin key>`:

| Key | Default | |
|---|---|---|
| `every_minutes` | 15 | minutes between passes |
| `ignore` | `[]` | server names not to check (`hub.json` `mcp_auth_ignore` counts too) |
| `one_time` | `["canva"]` | servers whose refresh token works once: `claude` is never run for them |
| `warn_days` | 3 | a login without a refresh token - warn this many days before it expires |
| `max_alarms` | 6 | messages to the owner a day, the whole plugin |
| `ask_claude` | `true` | `false` - never run `claude mcp get`, an expired access is "later" |
| `check_duplicates` | `false` | one line a day about a user server that hides a plugin's one |
| `quiet_until` | - | epoch or `YYYY-MM-DDTHH:MM` (hub timezone): no alarms until then |

The view "MCP logins" shows every server, its state and when it was last live (`status.json` next to the plugin).

Run one pass by hand: `PYTHONPATH=<hub code> python3 main.py --once`.

Tests: `PYTHONPATH=<hub code> python3 -m unittest discover -s tests` - no real `claude`, network or keychain.

Icon: [3D Fluency](https://icons8.com/icons/3d-fluency) by Icons8.

## License

MIT
