# Brainchild

> A personal-agent daemon. One Python process, one Telegram bot, one Claude subscription, zero servers.

Brainchild runs on your laptop. It polls Telegram for your messages, invokes Claude Code on every one of them, and keeps a vault of markdown state files so it remembers you. It sends scheduled briefings (morning, midday, night), accepts voice notes, ingests context files, and pushes back when you slip.

**Cost to you:** ₹0 / $0 beyond your Claude subscription. No servers, no APIs, no telemetry.
**Requires:** macOS / Linux / Windows · Python 3.8+ · `node`/`npm` · a [Claude Code](https://docs.claude.com/en/docs/claude-code) subscription · a Telegram account.

---

## Install

```bash
# macOS / Linux
curl -fsSL https://brainchild.sh/install | bash

# Windows (PowerShell)
irm https://brainchild.sh/install.ps1 | iex
```

The installer:
1. Checks `python3`, `node`, `claude`, `git`. Hints if anything's missing.
2. Walks you through creating a Telegram bot via @BotFather (60 seconds).
3. Asks you ~10 questions about who you are, what you're working on, when you work, what to push back on, what to never bring up.
4. Lets you drop any context files — brand docs, project notes, anything — that flesh out who you are.
5. Synthesizes a personalized vault: PERSONA, LIVE, BACKLOG, SCOREBOARD, briefing recipes, push-back rules, and one folder per major workstream — derived from your actual life, not boilerplate.
6. Registers a background service (launchd / systemd-user / Task Scheduler) that survives reboots and restarts on crash.

Total time: ~10 minutes.

## Use

After install, your bot DMs you within the hour with a first read on your context. From then on:
- **Text** the bot — it responds with Claude Opus running on your machine.
- **Voice note** the bot — transcribed locally with whisper.cpp, then routed.
- **Drop files** to the bot — added to your inbox for later digestion.
- **`/pause`** stops the daemon. **`/resume`** starts it back up. **`/status`** shows what's going on.

The vault lives at `~/brainchild-vault/`. Everything is plain markdown. Edit anything by hand — the agent re-reads it.

## CLI

```
brainchild               start daemon in foreground
brainchild status        see what the daemon is doing
brainchild pause         pause processing
brainchild resume        resume processing
brainchild ask "..."     one-shot prompt
brainchild audit tail    view the audit log
brainchild rotate-token  rotate your Telegram bot token
brainchild update        pull latest, verify, restart
brainchild uninstall     remove everything (prompts before deleting vault)
```

## Security

Brainchild runs Claude Code with broad local tool access on your laptop. We mitigate with:
- A strict deny-list of dangerous commands (`rm`, `curl`, `sudo`, `ssh`, edits to `.ssh`/`.aws`/`/etc`, etc.)
- A working-directory lock to your vault
- Per-message time and tool-call ceilings (90s, 25 calls)
- A Telegram owner-only allowlist (only your chat ID can drive the agent)
- An append-only audit log of every Bash/Edit/Write
- A one-touch killswitch via `/pause`, `brainchild pause`, or `touch ~/.brainchild/PAUSE`

**We do not claim safety against:** determined attackers with physical access, zero-day prompt-injection bypasses of the deny-list, or compromise of your Claude or Telegram accounts.

Treat your vault as plaintext personal data — enable full-disk encryption (FileVault / LUKS / BitLocker). Rotate your bot token via `brainchild rotate-token` if it ever leaves your machine.

## How it works

See [PLAN.md](PLAN.md) for the full architecture. Short version: one Python process with an internal scheduler manages everything — no cron jobs to register, no system services beyond a single auto-restart entry. State lives in your vault as markdown so you own it forever.

## License

MIT. See [LICENSE](LICENSE).
