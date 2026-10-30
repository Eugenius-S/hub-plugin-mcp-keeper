# mcp-keeper - MCP login keeper

An [Agents Hub](https://github.com/flagman/agents-hub-core) plugin: every 15 minutes it checks the OAuth logins of the MCP
servers **the hub gives to its sessions** and says so once, before a session gets stuck on a lost login.

## What it watches (and what it does not)

- **Only what sessions get.** The servers of the hub catalog (`hub.json` `mcp.<name>.server`) and the MCP servers of
  plugins (`plugin:<plugin>:<server>`) that live sessions carry. Hub sessions run with `--strict-mcp-config`: they never
  read `~/.claude.json`, so the MCP of a plain Claude Code outside the hub is not checked.
- **Remote servers only.** A server that runs as a program (stdio) is never started. `claude mcp list` is never run
  (it starts every stdio server).
- **Headers servers too.** A server with `headers` or `headersHelper` (an API key, no OAuth) is checked by its own
  headers: the helper runs with a 30 s limit (exit 0 and a JSON object of strings), then the server gets an
  `initialize` with those headers. 401/403 - "the server rejects the headers", a failing helper - "the helper gave no
  key"; a hang or no network - "later". A leftover OAuth record of such a server is never used. The values stay in
  memory and go only to the server's own address, by the same rules as a token.
- **No second watchdog.** The core's MCP watch already tells sessions about "needs a login" and reconnects them. The
  plugin adds what it does not do: a stored login that a server now rejects, an erased record, a token that cannot be
  refreshed - and tells the owner once.
- **A core with its own login strip (Plugin API 1.31, `mcp_waits` in the snapshot).** A server that sessions wait on is
  signed in through the strip of the hub, so for that server the plugin sends no alarm (the owner or the Hub
  Developer), shows a grey line "waits for a login: the button is in the hub's strip" instead of **Log in**, and starts
  neither an automatic reconnect nor `hub mcp login` while the core has a login of it open. Everything else stays: the
  check of all servers ahead of any session, the view, duplicates, `cover`, the steps of the alarm, **Reconnect**,
  **Another login**, **Add MCP**. A hub without that strip: the plugin works as before.

## One pass

1. The stored access token (Claude Code credentials: the macOS keychain item or `.credentials.json`, read only, never
   written) goes in an `initialize` request to the server's own address. 401/403 - the login is rejected. The token
   goes only to https (http - only to localhost / 127.0.0.1), never to an address with a login in it, never to an
   address other than the one the login was stored for, and a redirect is not followed (the Authorization header
   would travel with it): a 3xx answer is "later".
2. The access has expired and a refresh token exists: Claude Code refreshes it itself, under its own lock. The plugin asks
   `claude mcp get <server>` - this one server, at most once an hour while the stored login does not change, never for
   a one-time login (`one_time`, always includes `canva`, a setting only adds to it: a run killed in the middle of the
   exchange loses it) and not while a `~/.claude/mcp-refresh-*.lock` of Claude Code is fresh (somebody is exchanging
   right now). The plugin never exchanges a refresh token itself - only Claude Code does, under that lock. If you
   prefer that the plugin never runs `claude`, set `ask_claude` to `false`: such a server is "later".
   `claude mcp get` knows `~/.claude.json` and plugins, not the hub catalog (hub sessions run with
   `--strict-mcp-config`): "No MCP server named ..." or an answer for another address says nothing about the login.
   Then the word of the hub's live sessions counts (their Claude Code refreshed the login itself, under its lock, when
   it connected): a live session that waits for a login - lost (after the second pass); one that is connected - live
   (only when Claude Code had no word at all). Without such a session the server stays "later"; once its access has
   been expired for `stale_days` the view says so.
3. `claude mcp get` runs with a time limit (90 s). When it runs over, the whole process tree is killed (the process
   group on macOS/Linux, `taskkill /T` on Windows) - no orphans, no hung `claude`.

Lost is only an explicit refusal (401/403, "needs authentication" twice, an erased record, an expired token without a
refresh token). A timeout, no network, a 5xx - "check later", nothing is said. A pass runs only after 5 minutes of a live
network without sleep (the idle loop looks at the clock every 30 s, at the network every `net_check_minutes`), and a new loss is told only if a second pass 5 minutes later sees it too.

## Alarms

- **One alarm per server per day**, kept on disk (`<hub state>/mcp-keeper/state.json`, so a restart does not repeat
  it). A server stays "open" until a pass sees it live; it is told again only after it was seen live and lost again,
  and never within a day of its last alarm.
- **A cap for the whole plugin**: at most `max_alarms` (default 6) messages a day; the rest wait for room and go in one
  message.
- One urgent message per pass lists the newly lost servers, the most important first (one-time logins, servers a working
  session uses right now), each with `hub mcp login <hub session> <server>`. Lifted when a check sees the server live.
- Sessions that work with a lost server right now get one line (once per loss): do not loop on its tools.
- A server never logged in is never an alarm. When sessions wait for its login, the view shows it as "never logged
  in" and the Hub Developer gets one line a day with one `hub mcp login` per server (one login is enough for every
  session).

## Cover: every login of the machine (off by default)

`cover: true` (or a list of machine names: `["my-mac.local"]` - hub.json settings may travel between machines) widens
the watch from what the hub gives its sessions to every MCP server of this machine: the hub catalog, `~/.claude.json`
(user and project scope) and enabled plugins. Each server gets a state and who holds its login (`by` in the view):

| Server | State | Held by |
|---|---|---|
| OAuth (http) | live access - `initialize`; expired - `claude mcp get` (never for a one-time one) | `claude` |
| headers / headersHelper | the helper, then `initialize` with its headers | `headers` |
| read-only logins (`conduit`, `read_only` adds) | live access - `initialize`; expired with a refresh token - normal between uses, never asked of `claude` | `conduit` |
| stdio with `mcp-remote <url>` | its stored login in `~/.mcp-auth` (read only, never exchanged), live access - `initialize` | `auth-keeper` when that plugin is on, else `mcp-remote` |
| Microsoft 365 program | skipped: auth-keeper runs its `--verify-login` | `auth-keeper` |
| any other stdio program | skipped, never started; the reason is in `status.json` | `program` |
| a server on this machine (localhost) | skipped | `local` |
| one that answers without a login | connected, no **Log in** | `none` |

`mcp_auth_ignore` and `ignore` hold. A server under a second name (the same address or package) is checked once.

The alarm has steps with `cover`: the first sign of a lost login goes at once to the Hub Developer (`hub send`, what
is seen, who holds it, the fix; not again within a day); the owner gets the urgent message only when
`owner_after_passes` (2, about 30 min) more passes still see it lost, then at most one alarm per server a day. A pass
that cannot tell ("later") neither counts nor resets. A login auth-keeper holds goes to the Hub Developer only:
auth-keeper tells the owner itself.

## Refreshing ahead

The plugin never refreshes a login ahead. `claude mcp get` does not refresh a live access - it is a check, not a
renewal. Claude Code keeps its MCP logins itself: it refreshes an expired access when a session connects, under
`~/.claude/mcp-refresh-<key>.lock`, re-reading the stored pair inside the lock. A second exchange next to it (the same
minute, out of the lock) repeats a refresh token, and a one-time one is then revoked - so the plugin only reads.

## Duplicates (off by default)

A user server in `~/.claude.json` with the address of a plugin's server hides the plugin's one from Claude Code
(the login under `plugin:...` is never refreshed). It touches only Claude Code outside the hub, so it is **off by
default**; `check_duplicates: true` sends one line a day to the hub's developer session.

## What it reads, writes and sends

- **Reads:** `hub.json` (the MCP catalog, the plugin's own settings), the hub's state and journal (which sessions run,
  what they report about each server), the stored MCP logins of Claude Code (the macOS keychain item or
  `.credentials.json`; read only). With `cover` or `check_duplicates` also `~/.claude.json`, with `cover` also
  `~/.mcp-auth`: read only.
- **Writes:** only its own places. `status.json` next to the plugin (what the view shows), and in the plugin's own
  folder of the hub state `state.json` (the pass state and your choice of automatic reconnect), `login.json` (the end of
  a login you started from the view) and the switch script below. Events to the hub (a message to the owner, a line to
  the feed, a reconnect request, a line to a session) go through the hub's spool folder, the way every plugin process
  posts. It never writes the credential stores or another plugin's files, and its own code never writes
  `~/.claude.json` or `hub.json`. **Exception, only on your click on Add MCP (see below):** the programs it starts
  then write them: `claude mcp add` writes the server into `~/.claude.json`, `hub tools import mcp` writes it into the
  `mcp` section of `hub.json`. A short log goes to the process output, which the hub keeps in its own log.
- **Sends:** a server's own address gets one `initialize` request with the stored token (https, or localhost; no
  redirects, see "One pass"). Nothing goes to any other address, and the plugin has no server of its own.
- **Starts programs:** `claude mcp get <server>` (a time limit, never for a one-time login), `hub mcp login` (only on
  your click on **Log in**, or after **Add MCP**, and never for a server the core already waits on), `claude mcp add` and `claude mcp remove` (only for the server you
  add: the second one takes it back if the hub refuses it), `hub tools import mcp` (only after **Add MCP**), `curl` for a request through a proxy, `security` (macOS keychain read), `ps` or PowerShell
  (a list of processes, to tell who holds a lost one-time login), and the terminal below (on your click on **Another
  login**: `claude`, or the sign-out and sign-in of MS365 or Teams). A `headersHelper` runs only
  when the server's own catalog entry names it.
- **A terminal window, on a click.** On **Another login** the process writes a script into its own state
  folder: `switch-<server>.cmd` on Windows, `switch-<server>.command` on macOS, `switch-<server>.sh` on Linux (the
  server name is cleaned to letters, digits and `._-`; shell characters in the hint are dropped), and opens it in a new
  terminal window that stays open. What the window holds depends on the server: for a server with a login in a browser
  it starts `claude` in the login folder of that server; for MS365 and Teams it runs the server's own program, taken
  from the hub's description of the server, to sign out and sign in again (the same words the server starts with,
  plus `--logout`, then `--login`; or `auth logout`, `auth login`); for a server with a key (a header or an
  environment variable) it starts no program, only the words: where to get a new key and what to tell the hub. A
  program with an unusual word among its arguments (a key, a shell character) is not run. Only your own click in the
  view starts it; nothing opens a window by itself. The script keeps no secret: a folder, a hint and the program's
  words; no key is read, shown or written, and no new address is called.
- **Add MCP, on a click.** Under the table, **Add MCP** takes a name and an address that the service itself gives.
  The name is letters, digits, `-` and `_` (up to 40); the address is `https`, or `http` only for localhost, with no
  login, query or key in it. Your click posts `add <name> <address>` to the plugin's feed (only your own, fresh post
  counts, once). The process then does what you would type: `claude mcp add --transport http -s user <name> <address>`,
  then `hub tools import mcp <name>`, then `hub mcp login` for the browser login, the same as **Log in**. A name or an
  address that is already in the hub catalog, in the hub settings or in `~/.claude.json` is refused with the reason,
  nothing is run. If the hub's settings file is busy (another process writes it), the import is tried again up to 4
  times, 5 seconds apart; if the server is in the hub catalog by then, the add counts as done. If the hub still refuses
  the entry, `claude mcp remove` takes it back. A server that answers without a login gets no browser login. No key or token is typed, read or
  written: the login happens in the browser, in Claude Code's own store. The address is not kept in `status.json` or
  `login.json`; they hold the name, the result and a short reason. Sessions get the new server after they restart.
  A server that appears in the hub catalog, `~/.claude.json` or a plugin by other means gets its row in the view within
  about 30 seconds, not at the next pass (the same files are read, one check per new name).

The plugin asks for the page and the process, nothing more.

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
| `one_time` | `[]` | servers to add to the built-in one-time `canva`: `claude` is never run for them |
| `net_check_minutes` | 5 | minutes between TCP checks of the network in the idle loop |
| `stale_days` | 2 | an access this long expired on a server Claude Code does not know - the view says so (0 - off) |
| `warn_days` | 3 | a login without a refresh token - warn this many days before it expires |
| `max_alarms` | 6 | messages to the owner a day, the whole plugin |
| `ask_claude` | `true` | `false` - never run `claude mcp get`, an expired access is "later" |
| `check_duplicates` | `false` | one line a day about a user server that hides a plugin's one |
| `quiet_until` | - | epoch or `YYYY-MM-DDTHH:MM` (hub timezone): no alarms until then |
| `cover` | `false` | `true` or `[machine names]`: every MCP server of the machine, see "Cover" |
| `owner_after_passes` | 2 | with `cover`: more passes a loss must last before the owner hears of it |
| `read_only` | `[]` | servers to add to the built-in `conduit`: looked at, never asked of `claude` |

The view "MCP logins" lists every MCP server the hub gives to sessions (the hub catalog plus the servers of enabled
plugins; a plugin twin of a catalog server is listed once), by a name made for a person (Mobbin, Higgsfield, Granola,
GitHub, MS365, Canva, not `plugin:canva:canva`; the machine name stays in the tooltip of the row). Each row has a state
and when the server was last connected (`status.json` next to the plugin).

A row says **connected**, **lost** (with the reason) or **not checked** (grey, with the reason). The plugin starts no
program and no stdio server: a program, or a server without a login of its own, goes by the word of the running sessions
(sessions report it connected - connected, waits for a login - lost, no word - "not checked"). A server in `mcp_auth_ignore` (hub.json) is not
followed: it sits in one grey line under the table, with no buttons and no alarms. When a pass gets no clear answer (a
timeout, no network to the server), it asks again up to 3 times with a pause, reconnecting the server in the sessions in
between; still no answer - the row is "lost: no clear answer after 4 checks", with no alarm (it does not count toward
one).

Buttons of a row:

- **Another login** - on every connected server that has an account: posts `switch <server>`; the plugin
  opens a terminal, by the kind of server:
  - a login in a browser (Atlassian, Figma, Canva and the like): `claude` in the login folder of that server and a
    hint: `/mcp`, the server, "Clear authentication", then "Authenticate" and pick the other account in the browser;
  - MS365 and Teams: the server's own program signs out and then signs in again (a device code or a page of Microsoft);
  - a key in a header or an environment variable (Grafana and the like): words only, where to get a new key in the
    service and what to tell the hub in the chat ("change the Grafana key").
  A connected login is never switched by the plugin itself: Claude Code keeps its logins in a store the plugin must not
  write to, a key is not the plugin's to read or write, and `claude mcp logout` would wipe the login. A hub command for
  this would replace the terminal. The row says "terminal opened" or why it did not open.
- **no account** (grey words, no button) - a connected server without any account: a local program with no key, public
  docs.
- **Reconnect** - on a lost server, no login: posts `reconnect <server>`; the hub reconnects the server in every running
  session that has it (the event `mcp.reconnect`, needs a hub with `hub mcp reconnect`), then the plugin checks the
  server again at once and the row shows the result.
- **Log in** / **Log in again** - only for a lost server or one nobody logged in yet (a connected one has only "Another login"), and only for a server that logs in in a browser: posts `login <server>`; the process runs
  the same `hub mcp login <session> <server>` you would type in a terminal (a running session that has the server, else
  the hub's own) and your browser opens the login. The row says "login opened in the browser" until the hub reports the
  end, then the result (done, or why not); a login that went through makes the next check start at once.

The box **Reconnect automatically** under the table (on by default; your choice is kept in `state.json`): at the start
and on every pass a lost server is reconnected, at most once in 10 minutes per server, with a line in the
log. A one-time login (`one_time`) is never reconnected. Only your own posts count, only for a server in the table.

Run one pass by hand: `PYTHONPATH=<hub code> python3 main.py --once`.

Tests: `PYTHONPATH=<hub code> python3 -m unittest discover -s tests` - no real `claude`, network or keychain.

## По-русски

Вид «Входы MCP» показывает все MCP-серверы, которые хаб выдаёт сессиям (каталог хаба и плагины), по-человечески:
Mobbin, Higgsfield, Granola, GitHub, MS365, Canva. Машинное имя лежит в подсказке строки. Состояние: **подключён**,
**отвалился** (с причиной) или **не проверяется** (серым, с причиной). Плагин не запускает программы и stdio-серверы:
у программ и серверов без своего входа состояние берётся по слову работающих сессий. Серверы из `mcp_auth_ignore`
(hub.json) показаны одной серой строкой «Не следим», без кнопок и тревог.

У каждого подключённого сервера с аккаунтом кнопка **Другой логин**: плагин открывает терминал. Сервер со входом в
браузере: `claude` в папке входа и подсказка (`/mcp`, сервер, «Clear authentication», затем «Authenticate» и другой
аккаунт). MS365 и Teams: программа сервера выходит и входит заново (код устройства). Сервер с ключом (Grafana и
подобные): только слова, где в сервисе взять новый ключ и что сказать хабу («смени ключ Grafana»). У сервера без
аккаунта (локальная программа без ключа, публичные доки) кнопки нет, в строке серым «без аккаунта».
Если у ядра хаба есть своя полоса «MCP ждёт входа» (Plugin API 1.31), сервером из неё занимается она: по такому серверу
плагин не шлёт тревог, вместо **Войти** показывает серую строку «ждёт входа — кнопка в полосе хаба» и не запускает
автопереподключение и `hub mcp login`, пока у ядра идёт вход. Остальное работает как раньше. Хаб без такой полосы: плагин
ведёт себя как прежде.
Сам плагин подключённый вход не переключает: хранилище входов Claude Code ему писать нельзя, ключ он не читает и не
пишет, а `claude mcp logout` стирает вход. У отвалившегося и ещё не входившего сервера кнопки **Переподключить** и **Войти**, у подключённого только **Другой логин**. Подробнее выше, в разделе про вид.

Что плагин читает, пишет и куда шлёт: читает `hub.json`, состояние хаба и сохранённые входы MCP Claude Code (только
чтение); пишет только в свои места (`status.json` рядом с плагином, `state.json`, `login.json` и скрипт ниже в своей папке
состояния, события в хаб через его spool); входы Claude Code и чужие файлы не пишет, и сам его код не пишет
`~/.claude.json` и `hub.json`. Исключение, только по клику **Добавить MCP**: запущенные им программы пишут: `claude mcp
add` добавляет сервер в `~/.claude.json`, `hub tools import mcp` в раздел `mcp` файла `hub.json`. В сеть процесс ходит только
на собственный адрес сервера с сохранённым токеном. По клику **Другой логин** процесс пишет скрипт
`switch-<сервер>.cmd` (Windows), `.command` (macOS) или `.sh` (Linux) в свою папку состояния и открывает окно терминала:
с `claude` в папке входа сервера, либо с собственной программой MS365 или Teams (из описания сервера в хабе: выход и
вход заново), либо только со словами для сервера с ключом. Программа с необычным словом среди аргументов (ключ, символ
оболочки) не запускается. Окно открывает только ваш клик, секретов в скрипте нет, ключи не читаются и не пишутся, новых
адресов нет.

Под таблицей кнопка **Добавить MCP**: название (буквы, цифры, `-`, `_`, до 40 знаков) и адрес, который даёт сам сервис
(`https`, `http` только для localhost, без логина, параметров и ключа). Клик кладёт в ленту плагина `add <название>
<адрес>` (считается только ваш свежий пост, один раз). Процесс делает то же, что набрали бы вы: `claude mcp add --transport
http -s user`, затем `hub tools import mcp`, затем `hub mcp login` для входа в браузере, как у кнопки **Войти**. Название или
адрес, которые уже есть в каталоге хаба, настройках хаба или `~/.claude.json`, отклоняются с причиной, ничего не
запускается. Если файл настроек хаба занят (его пишет другой процесс), импорт повторяется до 4 раз с паузой 5 секунд; если
к этому времени сервер уже в каталоге хаба, добавление считается сделанным. Если хаб всё же не принял запись, `claude mcp remove` её
убирает. Сервер, который отвечает без входа, в браузер за входом не отправляется. Ключ не вводится, не читается и не пишется: вход в
браузере, в хранилище самого Claude Code. Адрес в `status.json` и `login.json` не хранится. Сессии получают сервер после
перезапуска. Сервер, который появился в каталоге хаба, `~/.claude.json` или плагине другим путём, получает строку в виде
секунд через 30, а не на следующем проходе (читаются те же файлы, одна проверка на новое имя). Права плагина: страница и процесс.

Icon: [3D Fluency](https://icons8.com/icons/3d-fluency) by Icons8.

## License

MIT
