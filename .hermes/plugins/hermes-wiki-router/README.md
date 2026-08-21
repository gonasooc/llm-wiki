# Hermes Wiki Router

This plugin owns explicit-mention thread placement for one Discord parent channel and reuses the mapped thread for duplicate YouTube video IDs.

## Required configuration

Set the plugin parent channel and grant the declared platform-action capability. The same parent channel must be listed in `discord.no_thread_channels`; otherwise Discord's native adapter creates a thread before `gateway_message_route` runs and the router correctly ignores the already-threaded event.

Install this project artifact into the profile's user-plugin directory, then
enable it. This path is recognized by both runtime discovery and the plugin CLI:

```bash
PLUGIN_HOME="${HERMES_HOME:-$HOME/.hermes}"
mkdir -p "$PLUGIN_HOME/plugins"
cp -R .hermes/plugins/hermes-wiki-router "$PLUGIN_HOME/plugins/"
HERMES_HOME="$PLUGIN_HOME" hermes plugins enable hermes-wiki-router --no-allow-tool-override
```

The copy command above is for a first install. For an update, replace the
existing `$PLUGIN_HOME/plugins/hermes-wiki-router` directory atomically before
restarting the Gateway.

For channel `1539904226333954098`, apply settings with `hermes config set`:

```bash
hermes config set plugins.entries.hermes-wiki-router.settings.parent_channel_id 1539904226333954098
hermes config set plugins.entries.hermes-wiki-router.allow_platform_actions true
hermes config get discord.no_thread_channels
hermes config set discord.no_thread_channels '["1539904226333954098"]'
hermes config set discord.auto_thread_mentions_in_free_response false
```

`discord.no_thread_channels` is list-valued. The example array is correct when
the current value printed by `config get` is empty. If it already contains
channel IDs, include every existing ID plus `1539904226333954098` in the JSON
array passed to `config set`; the command replaces the whole list.

The final setting disables the Phase 1 native mention lane for the managed channel. Ambient messages remain inline because it is still a free-response channel; explicit mentions are routed by this plugin.

## Data

The default SQLite database is profile-owned:

```text
$HERMES_HOME/plugin-data/hermes-wiki-router/youtube_threads.db
```

The mapping key is `(profile, guild, parent channel, video ID)`. SQL parameters are bound, and the database is not shared across profiles unless `db_path` is explicitly configured to a shared location.
