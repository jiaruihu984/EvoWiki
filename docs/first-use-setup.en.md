# Local initial setup

Start EvoWiki first, then complete **Initial setup** in the Obsidian plugin. No model key is required to start the local components. Learning remains unavailable until its selected provider has a key, an endpoint and a model with configured prices.

Choose a provider, verify its API address and enter the key. An empty key field preserves the saved key. Reopening setup shows only whether a key is configured; it never retrieves the saved key or a suffix.

**Check configuration** checks local syntax, addresses, model names and price records. It makes no inference request and cannot establish key validity, account balance or model access. Real inference verification, if used after setup and budget confirmation, is a separate paid operation.

Choose a provider, then select its model from the dropdown. A configured provider's
model directory loads automatically; **Get models** refreshes it using the supplied
endpoint and API key. This reads model metadata without generating content. Other
providers' models are excluded. Saving also selects that model for the current user.
If the endpoint cannot list models, setup reports the failure and keeps the known
options. Reasoning is directly below the model selection.

Saving writes the instance's canonical `.env` and restarts an already running owned backend through its normal drain and startup sequence. Unrelated configuration and comments remain in place. The service address can change after restart. If the backend was stopped, saving leaves it stopped; start it normally afterward. A restart failure stays a failure even if the configuration was saved successfully. Reopen setup to read the saved state, then use `status` and `up` to recover.

## Secret handling

The plugin invokes a fixed local launcher command with Node's `spawn`, an argument array, `shell: false` and `windowsHide: true`. Settings travel as one JSON object on stdin. Keys are absent from command arguments, protocol responses, plugin data, notes and configuration backups. There is no HTTP endpoint that writes `.env`.

The installed plugin's nonsecret `localSetup` record contains its checkout root, absolute uv executable, and fixed isolated bootstrap arguments. The helper derives the write root from its own script location. Requests cannot choose environment variable names, commands or write paths. Optional custom price files must be TOML files inside that checkout. Credentials belong only in the `.env` of the selected installation.

The helper accepts `status`, `models`, `check` and `apply`. `models` only reads the selected provider's model metadata and does not write configuration or restart services. `apply` requires the revision returned by the latest status/check. An external edit returns `revision_conflict`; reload current settings and re-enter the intended change. The wire response contains stable error codes and field names, never the rejected value. The UI translates these codes.

The same fixed helper can change the existing global forge gate and backup directory/retention through `forge_enabled`, `backup_directory` and `backup_keep` (1–365). These map to `EVOWIKI_FORGE`, `EVOWIKI_BACKUP_DIR` and `EVOWIKI_BACKUP_KEEP`. Enabling the global forge gate does not grant a user tool or code permissions; per-user permissions remain separate. Reminder time and the Bot pause belong to existing account settings. The Bot pause also controls background memory organization; there is no separate memory switch.

## Recovery

- `invalid_env`: repair the UTF-8 `.env` syntax locally. Duplicate names, incomplete assignments and malformed quotes are rejected. Do not share the file.
- `setup_busy`: another setup operation owns `.evowiki-run/setup.lock`. Wait for it to finish. If a process crashed, confirm that no setup process is running before removing the stale empty lock file.
- `interrupted_write`: writing may have been interrupted. Startup is blocked while `.evowiki-run/setup-write.json` exists. Inspect and repair the canonical `.env` locally, re-entering credentials where necessary. Once its complete contents have been checked, remove that nonsecret marker and run `doctor`, then `up`. The marker contains a hash, not a recoverable configuration copy.
- `runtime_ownership_or_restart_failed`: use `status` to check which checkout and process own the service. Stop an unrecognized runtime from its original launcher; setup will not take it over or kill it. If a restart failed after saving, repair the reported installation/runtime problem and run `up`.
- `invalid_prices` / `missing_price`: correct the checkout-local TOML price file or choose a model with known prices. Price entries use USD per million tokens and the existing `models."provider:model"` format.

No temporary plaintext copy of `.env` is created. Writes hold the setup lock and an exclusive file lock, flush the sole secret file and verify its bytes. This avoids a second secret file, but it cannot guarantee restoration of old credentials after power loss. The interruption marker deliberately stops startup until the configuration is repaired.
