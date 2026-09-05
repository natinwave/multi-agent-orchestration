<!-- This file lists the credential shapes the redactor matches, so it trips
     the repo scanner by construction. secret-scan: allow -->

# multi-agent-orchestration

A host-side supervisor that hands work to background coding agents and
reports back in a few short sentences.

Two operations, meant for a voice front-end that does not exist yet:

```
ask(agent, message)  -> job_id        # "kestrel"
check(job_id)        -> {state, narration[]}
```

Phase one is the orchestration core. There is no audio here, on purpose:
`ask`/`check` have to be demonstrably right over text before anything reads
them out loud.

---

## Where this runs

**On an Ubuntu desktop with native Docker** — 22.04 and 24.04 both work.
Not on a Mac.

That is not a preference. Several load-bearing behaviours differ or vanish
under Docker Desktop on macOS: bind-mount UID passthrough, identical-path
binds, `nvidia-smi`, and the whole secrets-on-tmpfs arrangement. A green
run on a laptop proves the pure logic and nothing else. See
[What cannot be tested off the target host](#what-cannot-be-tested-off-the-target-host).

---

## Architecture

```
Ubuntu 24.04 host
┌─────────────────────────────────────────────────────────────────────┐
│                                                                     │
│  MCP client  (OpenAI Agents SDK, client-side)                       │
│       │ stdio — no port, no TLS, nothing publicly reachable         │
│       ▼                                                             │
│  mcp_server.py ──► Supervisor ──► redaction ──► reply               │
│                        │                                            │
│                        │ spawns a detached runner per job           │
│                        ▼                                            │
│                    runner.py ─────┬──────────────────┐              │
│                                   │                  │              │
│  ═══════ trust boundary ══════════╪══════════════════╪═══════       │
│                                   ▼                  ▼              │
│                          docker exec           HTTP loopback        │
│                        orch-claude-code          hermes             │
│                        (persistent)            (local model)        │
│                                                                     │
│  /srv/orchestration          bind-mounted at the SAME path          │
│  /run/orchestration/secrets  tmpfs, 0700, one dir per agent, ro     │
└─────────────────────────────────────────────────────────────────────┘
```

The supervisor is the only privileged component. It talks to the Docker
daemon; nothing it launches can. **No agent container gets
`/var/run/docker.sock`** — that socket is host root, and an agent holding
it could read every other agent's credentials, which would make the
per-agent scoping below decorative.

### The supervisor holds no state

`ask()` creates a job directory, spawns a runner with `start_new_session`,
and returns. Everything else reads the job directory back. That has three
consequences worth knowing:

- Jobs outlive the MCP client. The transport is stdio, so the client
  process comes and goes; the work does not.
- The CLI and the MCP server are the same supervisor. You can start a job
  from one and check it from the other.
- A runner killed by the OOM killer or a reboot leaves `running` on disk
  forever, so `check()` reconciles: if the PID is gone, the job is `failed`.

### Job state

```
/srv/orchestration/
├── repos/<name>/              main clone, worktrees are cut from here
├── worktrees/<job>/           one per job, on branch job/<name>
├── scratch/<job>/             for jobs that need no repository
└── jobs/<job>/
    ├── meta.json              agent, repo, session uuid, workdir
    ├── status.json            state, exit code, runner pid
    ├── narration.log          what check() returns
    ├── prompt.txt             the message (never on a command line)
    └── raw.log                everything, returned only on tail=N
```

`status.json` has exactly one writer — the runner — so nothing is locked,
and every write is a same-directory temp file plus `os.replace`, so a
reader never sees a torn file.

### States

| state | means | set by |
|---|---|---|
| `queued` | job exists, runner not up yet | `ask()` |
| `running` | working | runner |
| `blocked` | stuck on something nobody can answer in a sentence | agent, via `narrate` |
| `awaiting_input` | asked you a question, parked | agent, via `narrate` |
| `done` | finished cleanly | exit code 0 |
| `failed` | crashed, timed out, or the runner vanished | non-zero exit |

One rule is worth stating: `claude -p` **exits 0 when it stops to ask a
question**. Trusting the exit code alone would report `done` for a job that
never started the work, so a last-narrated `blocked`/`awaiting_input` wins
over a clean exit. A non-zero exit always wins over both.

---

## The narration channel

`check()` returns a state and the last three narration lines. Nothing else,
unless you pass `tail=N` explicitly.

This is a hard constraint, not a default: the reply becomes a cloud model's
context and is then spoken out loud. Raw agent logs are enormous, mostly
tool output, and would be both expensive and unlistenable.

Agents narrate with a helper baked into the image:

```sh
narrate "suite green, 42 passed"
narrate --state awaiting_input "which staging database?"
```

The contract is in `docker/agent-prompt.md`, injected with
`--append-system-prompt-file`. It tells the agent that nobody reads its
stdout, that ten minutes of silence reads as a hung job, and to write for
the ear — "the tests pass now", not `✅ pytest tests/ -q → 42 passed`.

An agent may set `running`, `blocked` and `awaiting_input`. It may not
declare itself `done` or `failed`; those come from how its process exits.

---

## Redaction

Everything leaving the supervisor passes through `redaction.py`. Three
layers, applied in order:

1. **Registered values** — secrets this process can actually see: the
   per-agent secret files, the environment variables named in
   `config/orchestrator.toml`. Matched literally, plus base64 and
   percent-encoded forms.
2. **Pattern rules** — `sk-ant-`, `ghp_`, `github_pat_`, AWS, Google,
   Slack, Stripe, JWTs, PEM blocks, `Authorization:` headers, and generic
   `token = <value>` assignments.
3. **Entropy fallback** — any long opaque run whose Shannon entropy is high
   enough to look like a credential nothing has a pattern for.

Layer 3 needs an allowlist or it eats the git SHAs, UUIDs and paths agents
emit constantly. That matters more than it sounds: redacting those destroys
the narration *and* teaches the voice model that `[redacted]` is noise to
be ignored. `tests/test_redaction.py` pins the behaviour in both
directions, and the two holes the allowlist opens are asserted explicitly
as documented limits.

Enforcement is structural. Every public supervisor method returns through
one chokepoint, `_out()`, and a test reads `supervisor.py`'s own AST to
prove it — because a single plain `return {...}` added later would pipe an
unscrubbed credential to a cloud model and then to a speaker.

The same patterns guard the repo: `scripts/check-no-secrets.sh` fails on
anything credential-shaped in the tree. `op://` references are recognised
by shape and allowed; values are not.

---

## Secrets

**No secret values in this repo, ever.** `.env.example` holds 1Password
references only.

Delivery, host to container:

```
1Password ──op run/op read──► /run/orchestration/secrets/<agent>/   (tmpfs, 0700, 0400)
                                          │
                                          │ mounted read-only, ONE agent's dir
                                          ▼
                              container:/run/secrets/
                                          │
                                          │ read at exec time
                                          ▼
                    CLAUDE_CODE_OAUTH_TOKEN="$(cat …)" exec claude -p …
```

The value exists in the environment of one `claude` process for as long as
that process lives. It is never in the image, never in the container's
environment block, and never in `docker inspect`. `claude-code` and
`hermes` mount different directories, so neither can read the other's
identity.

**`/run` is tmpfs, so this is deliberately lost on reboot.** After the
machine restarts, re-run bootstrap to put the credentials back; jobs fail
with a clear "no Claude credential in /run/secrets" until you do. That is
the intended trade: credentials never touch a disk.

The *directory* should not need recreating by hand each time, though.
`sudo ./scripts/root-setup.sh` installs a `systemd-tmpfiles` rule so
systemd recreates it with the right owner and mode before anything else
runs, and bootstrap never needs root again.

Without it, a directory left behind by an earlier run as `root` makes every
credential write fail one confusing layer later. bootstrap repairs what it
can — a directory it owns with the wrong mode is its own to `chmod` — and
points at `root-setup.sh` for anything needing a change of owner.

For a machine you are not sitting at, use a 1Password **service account**
rather than `op signin` — the interactive sign-in expects the desktop app.
Set `OP_SERVICE_ACCOUNT_TOKEN` and `op run` works headlessly. That token is
itself a secret and has to live somewhere; a root-owned file sourced by the
operator, or a systemd credential, is the usual answer.

`--dangerously-skip-permissions` is used **inside the container and nowhere
else**. Anything invoking `claude` on the bare host uses
`--permission-mode acceptEdits` with a scoped `--allowedTools`. A test
enforces that the dangerous flag never appears in a host-side script.

### Delegating credentials

Everything above covers an agent's *own* identity. Anything else it might
need — a staging database password, a third-party API key — is delegated
deliberately, one grant at a time.

The `Agent` vault in 1Password is the pool you are **willing** to share.
Nothing in it reaches an agent until you say so:

```sh
./bin/orchestrate list-credentials              # the menu, titles only
./bin/orchestrate grant claude-code the staging password --job kestrel
# granted Staging DB Password to claude-code as $STAGING_DB_PASSWORD (this job only)
./bin/orchestrate list-grants                   # what each agent can reach
./bin/orchestrate revoke claude-code "Staging DB Password"
```

Three properties make this cheap rather than elaborate:

- **A grant takes effect with no restart.** `secrets/<agent>/` is already
  bind-mounted into that agent's container, so a new file inside it simply
  appears.
- **Scoping is structural.** Each container mounts only its own directory,
  so a grant to `claude-code` is invisible to `hermes` — not by policy, by
  the mount table.
- **No value ever passes through the supervisor's replies.** These calls
  return titles and the environment variable name, and they go through the
  same `_out()` chokepoint as everything else.

The agent never reads `/run/secrets` itself. Each granted file is exported
into the job's environment under its uppercased name, so `Staging DB
Password` arrives as `$STAGING_DB_PASSWORD` — which is what you tell the
agent to use. `docker/agent-prompt.md` instructs it to park with
`awaiting_input` if a credential it needs is missing, rather than hunting
for one or working around it.

`--job` ties a grant to one job, and the runner drops it when that job
ends. Every grant and revoke appends to `logs/grants.log`.

**The honest limit:** revoking stops *future* reads. A job that already
read the value holds it in memory until it exits. If you need it back right
now, end the job.

---

## Concurrency

One persistent container per agent slot, one git worktree per job.

Containers are long-lived and carry the toolchain, so a job costs a
`docker exec`, not an image pull and an `npm install`. `~/.claude/settings.json`
is baked into the image.

### What an agent has

Python 3 with venv and pip, Node 20, git, ripgrep, curl, jq, a compiler,
and Chromium via Playwright. Network access. Browsers install to a shared
`/opt/playwright` so the Python and Node packages find the same binaries
instead of downloading one copy each.

Ubuntu marks the system Python externally-managed, so `PIP_BREAK_SYSTEM_PACKAGES`
is set: in a disposable container there is nothing to protect, and the PEP
668 error otherwise sends an agent hunting for a problem that is not there.
The agent prompt still asks for the project's own environment on real work,
where its pinned versions are what the tests run against.

### What an agent deliberately does not have

**Docker.** Not the socket, not privileged docker-in-docker, not a relaxed
`cap_drop`. Every route to running containers inside a container hands back
the isolation this design rests on — the socket is host root, and
`--privileged` is host root by another name.

The need behind "I want Docker" is almost always *reaching* a service, not
running one, so agents get `host.docker.internal` instead: a database, a
dev server or the local model running on the host is addressable by name.
That grants nothing new — the bridge gateway was always reachable by
address — it just means an agent need not guess an IP.

If an agent genuinely needs a container brought up, it should say so and
stop. A human or the relay agent starts it on the host.

Each job gets `worktrees/<job>` on branch `job/<job>`, cut fresh from the
base ref, so four concurrent sessions cannot collide on branches or files.
Jobs needing no repository get a scratch directory instead — plenty of
questions need no checkout.

### Why the identical-path bind

A git worktree stores an **absolute** path back to the main clone's `.git`.
Git 2.48 added `worktree --relative-paths`; Ubuntu 24.04 ships 2.43 and
22.04 ships 2.34, so **neither has it**. `/srv/orchestration` is
bind-mounted into the container at
`/srv/orchestration` — the same string — and worktrees resolve. Change the
root in `config/orchestrator.toml` and you must change
`docker-compose.yml` to match. `bootstrap.sh` verifies this rather than
assuming it, because it fails silently.

`/workspace` exists too, as a second bind of the same worktrees directory.
Double-mounting one host directory at two container paths is legal; the
identical-path one is the one that has to be there.

### Why AGENT_UID matters

On native Linux a bind mount passes UIDs straight through. If the
container's `agent` user is not the host user's UID, every file the agent
writes comes back owned by someone else. `bootstrap.sh` builds with
`AGENT_UID=$(id -u)` and then verifies it by having the container touch a
file and checking who owns it.

Note that `ubuntu:24.04` already ships a `ubuntu` user sitting on UID 1000,
which is usually the UID we want — the Dockerfile removes it first.

---

## Getting started

On the Ubuntu host. Exactly one step needs root, and it is one reviewable
script rather than a handful of commands pasted from here:

```sh
git clone https://github.com/natinwave/multi-agent-orchestration.git
cd multi-agent-orchestration
cp -f .env.example .env                 # references only; .env is gitignored

./scripts/preflight.sh                  # changes nothing; relay the output

sudo ./scripts/root-setup.sh            # once per machine, the only root step

./scripts/bootstrap.sh --selftest       # finds ~/.op-token by itself

./bin/orchestrate ask claude-code "fix the failing parser test"
# kestrel  started on claude-code in main (job/kestrel)

./bin/orchestrate check kestrel
# kestrel  RUNNING
#   · reading the failing test
#   · found it: the retry loop swallows the timeout

./bin/orchestrate check kestrel --tail 40     # scrubbed raw log, opt-in
./bin/orchestrate list-jobs --active
```

`preflight.sh` is the first thing to run and its whole output is meant to
be pasted into chat: one line per check, one summary line.

Two environment variables override config, which is how you run a second
instance against a scratch root without touching the real one:

```sh
ORCH_CONFIG_DIR=/path/to/other/config ORCH_ROOT=/tmp/orch-test ./bin/orchestrate list-jobs
```

The supervisor passes both to each runner it spawns, so a job cannot drift
onto a different registry mid-flight.

### Wiring the MCP client

`bootstrap.sh` prints the config. The transport is stdio, so the client
spawns the server itself:

```json
{
  "mcpServers": {
    "orchestrator": {
      "command": "/path/to/multi-agent-orchestration/bin/orchestrate-mcp",
      "args": []
    }
  }
}
```

Tools: `ask`, `check`, `list_agents`, `list_jobs`, plus `reply` (answer a
parked job), `list_repos` (the names a repo answers to), and
`list_credentials` / `grant` / `revoke` / `list_grants` for delegation. A
front-end that only uses the first four works fine.

`grant`'s description tells the client model, in as many words, to read
back what it is about to grant and wait for you to agree. That docstring is
the only thing between a spoken "sure" and a live secret reaching a process
that runs model-authored commands, so it is written as prompt surface
rather than as documentation.

### Naming a repo out loud

`ask()` takes an optional free-text repo string, resolved against
`config/repos.toml` by name, then alias, then token overlap against the
aliases and the clone URL's slug. "the kiln one", "pottery" and "kiln
controller" all reach the same repo.

A tie returns `{"error": "ambiguous_repo", "candidates": [...]}` rather
than guessing, because guessing means an agent committing to the wrong
repository. Omit it entirely and the job gets the agent's default, or a
scratch directory if it has none.

---

## Adding an agent

Config, not code. `config/agents.toml`:

```toml
[agents.reviewer]
type = "container"
container = "orch-reviewer"
command = ["claude", "-p", "--dangerously-skip-permissions",
           "--output-format", "stream-json", "--verbose"]
secrets_dir = "/run/orchestration/secrets/reviewer"
default_repo = "main"
needs_repo = true
```

Add a matching service to `docker-compose.yml` — copy the `claude-code`
one, change the container name and the secrets mount — and re-run
bootstrap. Unknown keys are rejected at load time rather than ignored,
because a silently-dropped `timeout_second` typo means an agent running
with the wrong timeout for weeks.

**A different CLI is also config.** Each agent declares how its tool spells
"start a session", "resume one" and "take a system prompt":

```toml
session_flags       = ["--session-id", "{session_id}"]
resume_flags        = ["--resume", "{session_id}"]
system_prompt_flags = ["--append-system-prompt-file", "{agent_prompt}"]
```

An agent with no `resume_flags` simply has no sessions — `reply()` says so
plainly rather than failing strangely. A test asserts no tool-specific flag
creeps back into the backend, because that is what made adding a second
harness a code change the first time.

`config/agents.toml` carries a commented `codex` entry as a worked example.

**An OpenAI-compatible API needs nothing at all** — `base_url`, `model`,
done, like `hermes`. But that backend is one request and one answer: no
worktree, no tools, no file access. It answers questions; it does not do
coding work.

---

## What cannot be tested off the target host

Everything below needs the Ubuntu box. A passing run on a Mac says nothing
about any of it:

- **Image build with a matching UID.** Docker Desktop's virtiofs layer
  translates ownership; native Linux does not.
- **Bind-mount ownership.** The failure mode — agent writes files the host
  user cannot read — simply does not reproduce on macOS.
- **The identical-path bind and worktree resolution.** Verified by
  `bootstrap.sh` on the host.
- **`nvidia-smi` and VRAM.** No GPU check is meaningful off that machine.
- **`claude setup-token` and container OAuth.** See below.
- **Docker group membership**, the `permission denied` path in preflight.
- **tmpfs secret mounts** and their 0400/0700 modes.
- **`op run`** against your actual vault.
- **Reachability of the hermes endpoint**, which is a stub URL until you
  wire the real one.

What *is* covered off-host, and is worth running before you push:

```sh
.venv/bin/python -m pytest -q          # 450 tests
./scripts/check-no-secrets.sh
docker compose -f docker/docker-compose.yml config     # schema only
docker run --rm -v "$PWD:/mnt" -w /mnt koalaman/shellcheck:stable \
    scripts/*.sh scripts/lib/*.sh docker/bin/narrate
```

That covers redaction, id allocation, the state machine, narration parsing
(including the shell writer against the Python reader), registry
validation and repo resolution, the docker/compose invariants, and a real
end-to-end `ask` → detached runner → `check` against a stub model server.

---

## What remains manual

Four things, and the first one is the one that bites:

1. **A Claude credential**, one of two ways:

   - `claude setup-token` produces a long-lived subscription token. The
     onboarding wizard otherwise appears in containers even with valid
     credentials and waits for a keypress forever, which is what this
     avoids. It is interactive, so **it cannot be run over chat, by an
     agent, or over ssh without a terminal** — a human has to be at a
     terminal, though not necessarily *this* machine. Store the result in
     1Password under the reference in `.env.example`.
   - `ANTHROPIC_API_KEY` bills per token and needs nobody at a keyboard.
     For an always-on box this is the simpler story, and it keeps a
     subscription token off a machine that runs unattended.

   Either satisfies the container. `preflight.sh` says which, if any, it
   found rather than failing sideways.
2. **A 1Password service account token** on the host, at `~/.op-token`.
   bootstrap finds it on its own and resolves `.env` without an `op run`
   wrapper.
3. **`gh auth login`**, if you want agents opening pull requests.
4. **The hermes endpoint.** `base_url` in `config/agents.toml` is a
   placeholder port. Point it at the real server; nothing else changes.

## Known limits

- Jobs belonging to the same agent share a container, so one job can read
  another's worktree. That boundary is the *agent identity*, not the job.
  Separate identities need separate containers and separate secret
  directories.
- Container egress is unrestricted, because Claude Code needs the API. So
  `--dangerously-skip-permissions` is bounded by the container, not by the
  network. An egress-limited network is future work.
- Job names are recycled once every word in `data/wordlist.txt` is used;
  reaping a job frees its name.
- The redactor will not match a secret split across a line break. Matching
  across arbitrary wrapping would produce constant false positives; layers
  2 and 3 still see each fragment.

## The voice layer

Phase two. A Discord bot on this host carries audio between your phone and
`gpt-realtime`, which calls the supervisor's tools.

```
phone (Discord app, anywhere)
   |  voice channel
Discord  <->  bot on this host  <->  gpt-realtime
                    |
             in-process MCP -> supervisor -> agents
```

The session lives on the host, not the phone. That keeps the MCP server on
stdio with no port open, and means there is no app to maintain — your phone
is a microphone and a speaker.

Discord rather than a web page because **the phone should be able to be in
a pocket.** A browser tab needs the screen on and the page in front; a
native app has background-audio privileges and push notifications.

### Discord voice receive is an unsupported surface

Worth being clear-eyed about: **Discord has never officially supported
receiving voice.** py-cord's recording support reads a surface Discord does
not document, has broken before, and is actively changing — their
end-to-end encrypted voice protocol is exactly the kind of change that
ends it.

For one person's own bot that is an acceptable trade, because nothing else
gives you push-to-talk from a pocket this cheaply. But it is a trade with a
known failure mode, so the seam is real rather than notional: a transport
owns its own audio formats and hands the session 24 kHz mono PCM16, and
nothing above `voice/transport/` knows Discord exists. A test enforces
that.

If receive breaks, two replacements are already identified:

- **SIP.** The Realtime API accepts calls over SIP directly — point a trunk
  at `sip:$PROJECT_ID@sip.api.openai.com;transport=tls` and accept the call
  from a webhook. A real phone number, on a supported API, working with no
  data connection at all. The costs are per-minute billing and a publicly
  reachable webhook, which this design has so far avoided needing.
- **A WebSocket page over Tailscale.** No third party in the audio path,
  but a browser tab needs the screen on — which is why Discord won.

Either is one file in `voice/transport/`. `LoopbackTransport` is a third,
used by the tests, which is how the session is exercised end to end without
Discord, a bot token, or a microphone.

### Setting up the Discord bot

In the Developer Portal, create an application, then:

| Setting | Value | Why |
|---|---|---|
| Installation → **Guild Install** | on | A user-installed app has no bot member in the server, and joining a voice channel needs one. User Install can stay off. |
| Bot → Privileged intents | **all off** | The bot never reads messages. `Intents.default()` already covers voice states, which is not privileged. |
| Bot → Public bot | off | Nobody else should be able to add it. |
| OAuth2 → URL Generator → scope | `bot` | |
| OAuth2 → URL Generator → permissions | View Channel, Connect, Speak | The minimum to join and be heard. |

Open the generated URL, pick your server. Then turn on Developer Mode
(Settings → Advanced), right-click the voice channel, Copy Channel ID, and
put it in `config/voice.toml` — the copy, not the `.example`, which stays
tracked so your channel id never conflicts on a pull.

Three things that commonly go wrong, all of them caught up front now
rather than after the bot is connected and idle:

- The bot needs **View Channel on that specific channel**, not only
  server-wide — a category override can deny what a server-wide grant
  gave.
- The id must be the **voice** channel. Text and voice channels are often
  named the same and look identical in the copy-id menu.
- **py-cord with its `[voice]` extra.** Plain `py-cord` imports fine and
  then refuses to join a channel; `discord.py` installs under the same
  name and cannot receive voice at all. The extra pulls PyNaCl for voice
  encryption and `davey` for DAVE, Discord's end-to-end encrypted voice
  protocol — which is the same protocol change that could one day end
  voice receive, so its presence here is mildly reassuring.

`tests/test_voice_pycord.py` pins the py-cord API this depends on. It
skips where py-cord is absent and runs on the host, so an upgrade that
moves that ground is reported by the test suite rather than discovered
halfway through a call.

### Setup

Two more items in the `Agent` vault — `OpenAI API Key` and
`Discord Bot Token` — then set the channel id in `config/voice.toml`:

```sh
.venv/bin/pip install -e '.[voice]'              # py-cord[voice]: PyNaCl + davey
cp config/voice.toml.example config/voice.toml   # then set discord_channel_id
./scripts/bootstrap.sh                           # materialises the voice identity

./bin/orchestrate-voice --transport loopback     # credentials + API only
./bin/orchestrate-voice                          # the real thing
```

Run the loopback transport first. It checks the credentials and the
Realtime connection with Discord entirely out of the picture, so if
something is wrong you know which half it is.

The bridge is **its own identity**: the coding agents never see the OpenAI
key, and it never sees their Claude token. Same per-directory scoping as
everything else.

### What is enforced rather than prompted

You chose to let it start jobs freely but confirm every credential. A
prompt is not a control, so `GrantGuard` makes the **first** call to
`grant` never grant anything — it returns the sentence to say and waits for
an identical call within two minutes. Confirming staging for `hermes` does
not release production, or the same key to a different agent, and an
approval is consumed rather than reusable.

`revoke` is deliberately *not* gated. Withdrawing access is the safe
direction and is the one thing you might need in a hurry.

### Audio

Discord is 48 kHz stereo, the API is 24 kHz mono. The exact 2:1 ratio makes
this arithmetic rather than a dependency — written against `array` because
`audioop` is removed in 3.13. Both converters carry remainders at the byte
level; dropping an odd trailing byte would shift every later sample and
turn the rest of the call into noise.

Barge-in cancels generation **and** drops buffered playback. Cancelling
alone leaves a second of speech still queued over the person interrupting,
which is the thing that makes a voice assistant infuriating.

### What cannot be tested off the host

The Discord glue and the WebSocket transport need a bot token, a server and
a live key, so `discord_bot.py` is deliberately thin. Everything decidable
offline is covered: audio conversion, the playback buffer, the grant guard,
tool dispatch, and the realtime event loop — `handle_event` takes a plain
dictionary precisely so it can be driven without a socket.

`protocol.py` holds every wire constant in one place. It was checked against
the current docs rather than memory, which corrected three things: the model
is `gpt-realtime-2.1`, audio deltas are `response.output_audio.delta`, and
the `OpenAI-Beta` header is dropped at GA. When something breaks after an
API change, read that file first.

## Out of scope

No TTS/STT pipeline and no ElevenLabs: `gpt-realtime` is speech-to-speech,
so there is nothing to stitch together.
