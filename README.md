<!-- This file lists the credential shapes the redactor matches, so it trips
     the repo scanner by construction. secret-scan: allow -->

# multi-agent-orchestration

Call a phone number, tell it what you want done, and hang up. Agents run
on a desktop you are not sitting at; you get a sentence back when
something finishes, needs an answer, or goes wrong.

Underneath it is two operations:

```
ask(agent, message)  -> job_id        # "kestrel"
check(job_id)        -> {state, narration[]}
```

Everything else — the phone call, the profiles, the credential handling —
is those two with the sharp edges filed off. `check()` returns a state and
three short lines, because that reply becomes a voice model's context and
is then read out loud.

**Working today:** a real phone number answered over SIP, coding agents in
containers, an agent on the bare host, a local model, remote agents on AWS,
credential delegation from 1Password, and unprompted updates while you
talk. Discord voice was the first transport and is broken upstream — see
[the voice layer](#the-voice-layer).

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
| `stopped` | you asked it to stop | `stop()` |

The brief named six states; `stopped` is a deliberate seventh. Being told a
job "failed" when you asked it to stop is both wrong and alarming, and
would teach you to discount the word.

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
Put the token at `~/.op-token` (or point `OP_TOKEN_FILE` at it) and
everything finds it: `bootstrap.sh`, the CLI, the MCP server and the voice
bridge all look in the same places, in the same order.

That mattered more than it sounds. Only `bootstrap.sh` knew where the token
lived at first, and everything else inherited `op`'s credentials from its
environment — which for a systemd service is empty. The symptom was a voice
agent that would ask permission to share a credential, get it, and then
report that it could not find the vault at all. The token is registered
with the redactor too, since being read from a file put it beyond the reach
of `scrub_env`.

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

## Profiles and instances

A **profile** is a persistent identity: a name said out loud, a
description, its own image, and a standing set of credentials. An
**instance** is one job running as that profile, with its own container
and its own git worktree, so two instances of the same profile never step
on each other.

```
/srv/orchestration/profiles/ledger.toml     ← the profile, outside git
/srv/orchestration/profiles/ledger.Dockerfile
        ↓
ask("ledger", "…")   ask("ledger", "…")     ← two instances
   own container         own container
   own worktree          own worktree
```

**Profiles live outside the repository**, one TOML file each under
`{root}/profiles/`, named by their filename. They get personal fast — what
an agent is for, which credentials it holds, which of your projects it
touches — and none of that belongs in a git history. A profile may
`extends` anything in `config/agents.toml`, so the shipped definitions
stay the base and yours stay yours. `examples/profiles/ledger.toml` is a
worked one to copy, with a Dockerfile beside it.

A profile declares what it always holds, by vault item title:

```toml
credentials = ["Ledger DB Password", "GitHub Agent Token"]
```

`orchestrate sync-credentials` materialises those into the profile's own
secrets directory — declared once rather than granted every session, and
run by bootstrap, which is also what puts them back after a reboot since
`/run` is a tmpfs. Ad-hoc `grant()` still works on top for one-off needs.
`list_agents()` reports the titles, so the voice agent can answer "what
does Ledger have?" without a second call and without ever seeing a value.

### Making a profile

Say you want an agent called **ledger** that only ever works on the
provisioning ledger, holds that database's password, and can run two jobs
at once without them colliding.

**1. Register the repository** it works on, in `config/repos.toml`:

```toml
[repos.ledger]
url = "https://github.com/you/provision-ledger.git"
base_ref = "develop"                       # worktrees are cut from this
aliases = ["the ledger", "provisioning"]   # how you say it out loud
```

**2. Write the profile** at `/srv/orchestration/profiles/ledger.toml` —
outside the repository, because this is yours. Copy
`examples/profiles/ledger.toml` and edit:

```toml
description = """
Works on the provisioning ledger: its schema, importers and tests. Knows
that codebase and nothing else. Give it a ledger task and leave it.
"""

extends     = "claude-code"      # command, flags, timeouts, narration
image       = "orchestration/ledger:latest"
isolation   = "per_job"          # a container of its own per instance
secrets_dir = "/run/orchestration/secrets/ledger"
credentials = ["Ledger DB Password"]
default_repo = "ledger"
needs_repo  = true
max_concurrent = 2
```

The `description` is not decoration — it is what the voice model reads
when deciding who gets a job, so say what the agent is *for*.

**3. Build its image.** Start from the base so the toolchain, browser,
narration helper and agent prompt are already there, and a job costs a
container start rather than an install:

```dockerfile
FROM orchestration/claude-code:latest
USER root
RUN apt-get update && apt-get install -y --no-install-recommends \
        postgresql-client && rm -rf /var/lib/apt/lists/*
USER agent
```

```sh
docker build -f /srv/orchestration/profiles/ledger.Dockerfile \
             -t orchestration/ledger:latest /srv/orchestration/profiles
```

**4. Give it its credentials.** Add `Ledger DB Password` to the `Agent`
vault, then:

```sh
./bin/orchestrate sync-credentials
./bin/orchestrate list-agents        # ledger, and what it holds
```

**5. Use it.** By name, from the CLI or out loud:

```sh
./bin/orchestrate ask ledger "the nightly importer is dropping rows"
```

> "have ledger look at the nightly importer, and start a second one on the
> schema migration"

Two instances, two containers, two worktrees, neither aware of the other.

**Changing a profile** is editing the file. The registry is read fresh on
every command, so a new profile is available immediately; only an image
change needs a rebuild, and only new `credentials` need
`sync-credentials`. After a reboot run `bootstrap.sh`, which re-syncs them
— `/run` is a tmpfs and is emptied.

Three mechanics underneath:

**`extends`** — a second kind is a few lines, not a copy:

```toml
[agents.reviewer]
extends = "claude-code"
description = "Reads a change and reports on it. Never edits."
max_concurrent = 2
```

Anything the child names wins; the rest is inherited. Chains work, cycles
are refused rather than followed — the alternative is a hang at startup
with nothing to read.

**`isolation`** — whether concurrent instances share a container:

| | |
|---|---|
| `shared` (default) | `docker exec` into one long-lived container. Cheapest, and what the pre-baked toolchain is for. Jobs of the same kind can see each other's files. |
| `per_job` | a fresh container from `image`, removed afterwards. Costs a container *start*, not an install — the toolchain is already in the image, which is what the brief's "no reinstalling dependencies" was protecting. Concurrent jobs cannot see each other at all. |

A per-job container keeps the same posture as the long-lived one —
`cap-drop ALL`, `no-new-privileges`, no docker socket — and gets the same
identical-path bind, because worktrees resolve only if host and container
agree on the path. Stopping such a job removes the container, since the
container *is* the job.

**`max_concurrent`** — a ceiling per kind, so an enthusiastic afternoon
does not put twenty agents on one desktop. Over the limit, `ask()` refuses
and says so; it does not queue. A job that never starts and says nothing
is the failure this whole design is against.

## Concurrency

One persistent container per agent slot by default, one git worktree per
job.

Containers are long-lived and carry the toolchain, so a job costs a
`docker exec`, not an image pull and an `npm install`. `~/.claude/settings.json`
is baked into the image.

### What an agent has

Python 3 with venv and pip, Node 20, git, ripgrep, curl, jq, a compiler,
and a real browser. Network access.

Chromium is **on the path**, with Playwright installed for both Python and
Node. Two commands cover most web work:

```sh
page-text https://example.com          # what a person would see
screenshot https://example.com out.png
```

Each of those details was a lesson. Playwright hides its chromium several
directories deep, so an agent checking for a browser binary found nothing
and concluded there was none. Only the Node package was installed, so a
Python project's `pip install playwright` pulled one whose browser build
was missing and tried to download it mid-task. And Chromium's sandbox
needs privileges this container deliberately drops, so it must be launched
with `--no-sandbox` — an argument whose absence produces an error
mentioning everything except sandboxes. The prompt now says all three, and
the two helpers are short scripts an agent can read as worked examples.

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

./bin/orchestrate list-agents
./bin/orchestrate ask claude-code "fix the failing parser test" --repo ledger
# kestrel  started on claude-code in ledger (job/kestrel)

./bin/orchestrate check kestrel
# kestrel  RUNNING
#   · reading the failing test
#   · found it: the retry loop swallows the timeout

./bin/orchestrate check kestrel --tail 40     # scrubbed raw log, opt-in
./bin/orchestrate list-jobs --active
./bin/orchestrate stop kestrel
```

`--repo` is not optional in spirit: no agent defaults to one, so a job
without it gets an empty directory. Register yours in `config/repos.toml`
first.

Then [make a profile](#making-a-profile) for the work you do repeatedly,
and [answer the phone](#the-voice-layer).

### The command surface

| | |
|---|---|
| `ask <agent> <message> [--repo]` | start a job, get a name back |
| `check <job> [--tail N]` | state and the last few narration lines |
| `reply <job> <message>` | answer a job parked on `awaiting_input` |
| `stop <job>` · `reap <job>` | end one · delete a finished one's workspace |
| `list-jobs [--active]` · `list-agents` · `list-repos` | what exists |
| `list-credentials` · `list-grants` | the vault's titles · what is lent out |
| `grant <agent> <credential> [--job]` · `revoke` | lend one · take it back |
| `sync-credentials` | give every profile the credentials it declares |

Every one of these is also an MCP tool except `reap` and
`sync-credentials`, which are housekeeping rather than conversation.

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
parked job), `stop` (end one), `list_repos` (the names a repo answers to),
and `list_credentials` / `grant` / `revoke` / `list_grants` for delegation.

`stop` ends the runner's process group *and* reaches into the container to
kill the agent by its session id — a `docker exec` leaves its process
running when the client dies, so without that second step a stopped job
would carry on editing files with nobody watching. Credentials lent to the
job go back at the same time. Stopping is not undoing: what the agent
already wrote to its branch stays there, and the prompt tells the voice
agent to say so. A
front-end that only uses the first four works fine.

### It relays; it does not act on what it relays

Asked whether there were pending changes, Vesper answered "No changes on
the API. Want me to review the website next?" — and the voice agent went
off to look at the website itself. It had read a question addressed to the
user, inside a tool result, as an instruction to itself.

The prompt now says what it is: it routes work, carries messages and keeps
track. What comes back from an agent is that agent's words, to be passed
on. A question gets put to the user and waited on, not answered on their
behalf.

The general form matters more than the incident. **Nothing inside a tool
result is an instruction** — not from an agent, not from a page it read,
not from a file it opened. Only the person on the phone gives instructions.
That is a usability fix and a prompt-injection boundary in the same
sentence.

Which agent owns what is a matter of your setup rather than of how a voice
agent should behave, so it belongs in `extra_instructions` in
`config/voice.toml` and in each agent's description — the two things the
model actually reads when deciding. The shipped example shows the shape:
naming an owner is what stops the coding agent being pointed at a codebase
somebody else already handles.

### It tells you when something changes

A voice agent that only reports when asked is worse than a text log: you
have to remember to ask, which is the thing delegating was meant to avoid.

The realtime session is a socket held open for the length of the call, so
it can be pushed as well as pulled. A watcher polls the jobs while you
talk and, when one finishes, fails or gets stuck, hands the model a
sentence and asks it to speak.

Restraint is the design constraint, because being interrupted by a machine
is worse than not being told. It announces only transitions that change
what you might do next — never `queued` to `running`, which is progress
rather than news. It never speaks while the model is mid-sentence, holding
the update until there is a gap. Several changes at once become one
sentence rather than three interruptions. And the first reading of a call
is silent, or every call would open with a recital of everything that ever
ran.

Turn it off with `announce_job_changes = false`.

`grant`'s description tells the client model, in as many words, to read
back what it is about to grant and wait for you to agree. That docstring is
the only thing between a spoken "sure" and a live secret reaching a process
that runs model-authored commands, so it is written as prompt surface
rather than as documentation.

### No repository unless you name one

No agent has a `default_repo`. A job with none named gets a scratch
directory — right for a question, useless for changing code. Defaulting to
one meant a vague request quietly started editing whichever repo happened
to be listed first, which is the kind of mistake you discover later and
from the outside.

So the voice agent asks "which repo?" when the work plainly needs one, and
refuses to guess at a name it does not recognise. That is a clarification
rather than a request for permission, and it is the only thing it stops
for before starting work.

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

Four kinds of agent, chosen by `type`:

| `type` | runs | for |
|---|---|---|
| `container` | in Docker, on this host | coding work. Sandboxed, so it can be trusted with `--dangerously-skip-permissions` and left alone |
| `local` | on the bare host | work that needs the machine — services, files outside a worktree. No sandbox, so it gets `acceptEdits` and an explicit tool list instead |
| `http_openai` | wherever your model server is | questions. One request, one answer: no worktree, no tools, no file access |
| `bedrock_agentcore` | on AWS | agents that already exist elsewhere and know things this machine does not |

Config, not code. `config/agents.toml`:

```toml
[agents.reviewer]
extends = "claude-code"
description = "Reads a change and reports on it. Never edits: give it a branch and ask what is wrong."
isolation = "per_job"
image = "orchestration/claude-code:latest"
max_concurrent = 2
```

Anything named here wins; everything else — command, flags, timeouts,
secrets, the narration contract — comes from the parent. A `container`
agent using the default `isolation = "shared"` also needs a matching
service in `docker-compose.yml`; one using `per_job` needs only an image,
which is usually the simpler answer.

Unknown keys are rejected at load time rather than ignored, because a
silently-dropped `timeout_second` typo means an agent running with the
wrong timeout for weeks.

For anything you will use repeatedly, prefer a
[profile](#making-a-profile): same mechanism, but the file lives outside
this repository where personal configuration belongs.

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

**An agent on the bare host** is `type = "local"`. For work that genuinely
needs the machine — bringing a compose stack up, reaching files outside a
worktree, using something installed here and not in the image. Same
worktree, same narration, same per-agent credentials read at exec time;
what changes is that there is no boundary around it.

So it does **not** get `--dangerously-skip-permissions`. That flag is
acceptable where the blast radius is a container, and an agent with
neither a boundary nor permission checks is a different thing from what
this is. A local agent runs `--permission-mode acceptEdits` with an
explicit `--allowedTools` list, and the backend refuses the dangerous flag
outright rather than honouring it quietly. `config/agents.toml` has a
commented example.

**An agent that lives somewhere else** is `type = "bedrock_agentcore"` —
a remote agent on AWS, given a message and asked for an answer. Nothing is
checked out and nothing runs here. The job's session id is reused as the
runtime session, so `reply()` continues the same conversation rather than
starting a new one. Needs `pip install -e '.[aws]'`.

Its IAM user should be able to do exactly one thing:

```json
{ "Effect": "Allow",
  "Action": "bedrock-agentcore:InvokeHarness",
  "Resource": [
    "arn:aws:bedrock-agentcore:us-east-1:<account>:harness/Soren-...",
    "arn:aws:bedrock-agentcore:us-east-1:<account>:harness/Vesper-..."
  ] }
```

Both keys go in the vault; half a credential is refused outright, because
boto3 would otherwise fall back to ambient credentials and silently act as
somebody else.

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
.venv/bin/python -m pytest -q          # 641 tests
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
5. **For the phone**: a SIP trunk pointed at
   `sip:$PROJECT_ID@sip.api.openai.com;transport=tls`, a tunnel so OpenAI
   can reach the webhook, and your own number in `allowed_callers`. See
   [SIP](#sip-a-real-phone-number).
6. **For agents on AWS**: an IAM user scoped to the harnesses you want
   invoked, and `pip install -e '.[aws]'`.

## Known limits

- With the default `isolation = "shared"`, jobs of the same agent share a
  container and can read each other's worktrees. Set
  `isolation = "per_job"` — which profiles do by default — and each
  instance gets a container of its own.
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

### SIP: a real phone number

The primary voice path. Point a SIP trunk at
`sip:$PROJECT_ID@sip.api.openai.com;transport=tls`, and OpenAI posts a
`realtime.call.incoming` webhook when the number is dialled.

**OpenAI bridges the audio itself.** For a SIP call this process never
touches a sample — no resampling, no jitter buffer, no undocumented
surface. `audio.py` and `playback.py` exist solely to feed Discord. What
runs here is the door and the tool calls.

```sh
./scripts/install-voice-service.sh     # run it as a service (recommended)
./bin/orchestrate-voice --transport sip   # or in the foreground, to watch it
```

**Run it as a service.** The listener waits for the phone to ring and never
exits, so anything running it in the foreground is holding a process
forever — and an agent relaying commands over chat cannot: its tool call
times out, the process dies with it, and the symptom is a phone that rings
while the webhook returns 502 from a tunnel whose target has gone. As a
service it survives that, restarts on failure, and logs to journald where
it can be read without holding anything:

```sh
journalctl --user -u orchestrator-voice -f
```

It restarts indefinitely on purpose. `/run` is a tmpfs, so after a reboot
the credentials are gone until `bootstrap.sh` refills them; the listener
backs off and retries rather than giving up, and the phone starts working
again on its own. Enable lingering (`sudo loginctl enable-linger $USER`) or
the service stops when your session ends.

**Only numbers you list are answered.** Three layers, because caller ID
alone is not a strong boundary — a SIP `From` header can be forged, and
while a real telco trunk makes that hard for PSTN-originated calls, it is
not proof:

1. **Webhook signature.** The endpoint must be publicly reachable for
   calls to arrive, so it will be found and poked. Anything unsigned or
   replayed is refused, and the reply reveals nothing about the call.
2. **Caller whitelist**, default-deny. An empty `allowed_callers` answers
   nobody — a misconfiguration leaves the phone silent, never open.
   Formatting is irrelevant: `+1 (425) 555-1212` and `4255551212` are the
   same phone.
3. **Filter at the trunk too.** Twilio can drop non-whitelisted callers
   before they ever reach OpenAI, so your endpoint is not even touched.

Unwanted calls get SIP `603 Decline` before a word is exchanged, and are
never billed. And granting a credential still needs a spoken confirmation
regardless of who is calling.

Bind the webhook to loopback and put a tunnel in front — Cloudflare Tunnel
or Tailscale Funnel — rather than opening a port. The only thing that needs
to reach it is OpenAI.

### Discord voice receive is broken upstream

It has now happened. **Discord voice reception does not work**, and not
through any fault of this code:

- Discord made DAVE, its end-to-end voice encryption, **mandatory on 2
  March 2026**. A client that advertises no support is rejected with close
  code 4017.
- py-cord 2.8 added DAVE for *sending*, which is why the bot connects and
  can speak. Receiving from a DAVE call is not implemented — the library
  says so itself at runtime: *"voice reception is currently broken due to
  Discord's DAVE protocol"*, [Pycord issue 3139](https://github.com/Pycord-Development/pycord/issues/3139).

So the bot can talk but cannot hear, and there is no configuration that
changes that. Declining DAVE was investigated and does not work: sending
`max_dave_protocol_version: 0` is exactly what gets close code 4017.

Use `--transport sip`. The Discord adapter is kept because it is a few
lines and would work again if Pycord ships its rework.

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

### Sharing a credential

One step: ask, and it happens. The control is not a confirmation but a
**report** — the tool description requires the model to say what it
granted, to whom, and for how long, every time and unprompted. That
sentence is how a misheard request gets caught, and it is the only chance
to catch one.

That is a deliberate change from the two-step it shipped with. Living with
it, the read-back-and-confirm exchange was rote when you trust the model to
interpret "give the coding agent the staging password" correctly, and the
half that actually catches a mistake is being told what happened. Set
`confirm_grants = true` in `config/voice.toml` to put the confirmation
back: the first `grant` then returns a sentence to say and waits for an
identical call within two minutes, where confirming staging for `hermes`
does not release production or the same key to another agent.

`revoke` is not gated either way. Withdrawing access is the safe direction
and the one thing you might need in a hurry.

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
