# Hermes AI Budget Balancer

A local Hermes Agent plugin that chooses between GPT/Codex and Claude for each new session. It compares the remaining weekly subscription quota, time until reset, and a small task-fit classifier. The goal is practical: use both subscriptions without silently falling back to paid Anthropic API usage.

This is an early personal project by [giovannegm](https://github.com/giovannegm). It targets [Hermes Agent](https://github.com/NousResearch/hermes-agent) and is not an official Nous Research plugin.

## How it works

The plugin has four parts:

- `ai_budget_balancer.py` contains the deterministic routing policy. It classifies task size and fit, compares each provider with its expected quota trajectory, and keeps a 3% reserve.
- `scripts/saldogptclaude.py` reads weekly ChatGPT/Codex and Claude subscription usage. The collector is bundled with the project; no private helper script or absolute home path is required.
- `dashboard/plugin_api.py` exposes scoped `/status` and `/route` endpoints to Hermes. It caches quota checks for 60 seconds and writes a small decision audit log.
- `desktop/plugin.js` adds a command-palette action, a page for starting balanced conversations, and middleware that can switch models when the current quota is projected to cross the reserve floor.

A new session can favor Claude for implementation-heavy work and GPT for general reasoning. Quota pressure can override a weak task preference. Existing balanced sessions stay on their current model unless observed usage projects a floor breach before reset.

## Security model

The Claude route fails closed. It is disabled when:

- an Anthropic API credential is present;
- Claude Pro OAuth cannot be verified;
- the extra-usage setting is enabled or cannot be verified.

Subprocesses use argument arrays with `shell=False`. The audit log only accepts a fixed set of metadata fields: timestamp, provider, model, task weight, remaining percentages, and routing reason. Prompts, responses, file paths, tokens, and session content are not written to it.

The bundled collector reads existing local OAuth credentials and calls the providers' local or authenticated usage endpoints. It never prints access tokens. Treat the collector as security-sensitive code and review changes before installing them.

## Requirements

- A working Hermes Agent installation with the desktop app
- Active ChatGPT/Codex and Claude subscriptions
- Codex CLI and Claude Code authenticated through their subscription flows
- Python dependencies supplied by Hermes, including FastAPI and Pydantic
- Node.js for the desktop-plugin tests

## Installation

1. Clone or download this repository.
2. Copy the project directory to `$HERMES_HOME/plugins/ai-budget-balancer`. If `HERMES_HOME` is unset, the default location is `~/.hermes/plugins/ai-budget-balancer`.
3. Open Hermes Desktop and go to **Settings → Plugins**.
4. Enable **AI Budget Balancer**. The unified package includes both the Python backend and the desktop plugin; the desktop half is opt-in.
5. If the command does not appear after a few seconds, open the command palette and run **Reload desktop plugins**.

Do not copy local `.env` files or credential stores into the plugin directory.

## Usage

Open the command palette and select **Nova conversa balanceada**. Enter the first message for the new conversation. The plugin checks current quotas, chooses a provider, creates a model-pinned Hermes session, and submits the message.

Only sessions created through this action are tracked as balanced sessions. During later turns, the middleware keeps the current provider unless its observed burn rate is projected to cross the reserve floor before reset and a safe alternative is available.

The interface is currently written in Portuguese. Routing terms include both Portuguese and common development keywords.

## Development

Run the Python tests with the standard-library runner:

```bash
python3 -m unittest discover -s tests -v
```

Run the desktop-plugin tests directly:

```bash
node --experimental-vm-modules --test tests/test_plugin.mjs
```

The same Node test is available through:

```bash
npm test
```

The tests cover routing policy, quota trajectory, fail-closed Claude checks, audit-field filtering, bundled collector invocation, backend behavior, session creation, and session-scoped model switching.

## Limitations

- The quota collector depends on provider CLI behavior and undocumented authenticated usage endpoints, which may change.
- Task fit uses a small keyword heuristic, not a semantic model.
- The policy is tuned for the model names and weekly subscription windows present in version 0.1.0.
- The plugin has only been prepared and tested on Linux.
- It does not purchase credits, enable extra usage, or bypass provider limits.

## License

MIT. See [LICENSE](LICENSE).
