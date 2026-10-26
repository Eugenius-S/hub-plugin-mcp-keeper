# mcp-keeper - MCP login keeper

An [Agents Hub](https://github.com/flagman/agents-hub-core) plugin: every 15 minutes it calls round every MCP server of
Claude Code - user, project and plugin servers (`plugin:<plugin>:<server>`) - and catches a lost login before a session
gets stuck on it.

- **One pass.** `claude mcp list` checks every server; Claude Code refreshes an expired OAuth access itself, under its
  own lock. The plugin never refreshes or writes logins: it only reads the Claude Code credentials (the macOS keychain
  item or `.credentials.json`). A server that answers "connected" is also asked `initialize` with its own stored access:
  some servers let a client in without a login, and a 401/403 there means the login is gone. Servers that log in by
  headers (`headersHelper`, `headers`) are judged by the list only.
- **Only real losses.** Lost is an explicit refusal: "needs authentication" twice in a row, or 401/403. A timeout, no
  network, a 5xx - "check later", nothing is said. A pass runs only after 5 minutes of a live network without sleep (a
  one-time refresh right after a wake can be lost), and a new loss is told only if a second pass 5 minutes later sees it
  too.
- **To the owner.** Only a login that worked and stopped: one urgent message with every such server of the pass, the
  most important first (one-time logins, servers a working session uses right now), each with the command to log in
  (`hub mcp login <hub session> <server>`). Repeated hourly while the server stays lost; lifted when a check sees it
  live. A stored login of a live server that disappeared is told as erased. "Connected" with a slow tool list (load
  after a hub restart) is live: the login was taken.
- **To the Hub Developer.** What blocks no work - a server never logged in, a user server with the address of a
  plugin's server (Claude Code loads the user one, and the plugin's login is never refreshed) - one line a day to the
  hub's developer session, not to the owner.
- **To sessions.** Sessions that work with a lost server right now get one line: do not loop on its tools; the hub
  reconnects it after the login.
- **A trail.** When a one-time login (its refresh token works only once) is lost, the processes of Claude Code and of the
  server at that moment are kept in the plugin state (`<hub state>/mcp-keeper/state.json`, `traces`), long tokens in
  command lines masked - to find out later who spent it.
- **View.** "MCP logins": every server, its state and when it was last live (`status.json` next to the plugin,
  rewritten every pass). The plugin channel gets one line when the picture changes: live, lost, later.

## Settings

`hub.json`, `extensions.<plugin key>`:

| Key | Default | |
|---|---|---|
| `every_minutes` | 15 | minutes between passes |
| `ignore` | `[]` | server names not to check (`hub.json` `mcp_auth_ignore` counts too) |
| `one_time` | `["canva"]` | servers whose refresh token works once |
| `warn_days` | 3 | a login without a refresh token - warn this many days before it expires |
| `quiet_until` | - | epoch or `YYYY-MM-DDTHH:MM` (hub timezone): no alarms until then |

Run one pass by hand: `PYTHONPATH=<hub code> python3 main.py --once`.

Tests: `PYTHONPATH=<hub code> python3 -m unittest discover -s tests` - no real `claude`, network or keychain.

Иконка — [3D Fluency](https://icons8.com/icons/3d-fluency) от Icons8.

## License

MIT
