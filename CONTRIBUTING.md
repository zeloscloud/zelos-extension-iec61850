# Contributing

## Prerequisites

- [Zelos](https://zeloscloud.io) 26.0.10 or later
- [Zelos CLI](https://docs.zeloscloud.io/getting-started/install-cli/) 0.1.12 or later
- [just](https://github.com/casey/just)
- [uv](https://docs.astral.sh/uv/)

## Layout

`extension.toml` describes everything the extension contributes:

| Part | Files | Manifest |
| ---- | ----- | -------- |
| Agent runtime | `main.py`, the Python package, `pyproject.toml`, `config.schema.json` | `[agent]`, `[config]` |

## Commands

| Command                  | Description                                   |
| ------------------------ | --------------------------------------------- |
| `just install`           | Install dependencies and the pre-commit hooks |
| `just format`            | Format code with ruff                         |
| `just check`             | Check formatting and lint (ruff)              |
| `just test`              | Run tests (pytest)                            |
| `just package`           | Package for the Zelos marketplace             |
| `just release VERSION`   | Bump version, check, test, commit, tag        |
| `just clean`             | Remove build output, caches and packages      |
| `just run`               | Run the extension against `./config.json`     |
| `just demo`              | Replay the bundled demo capture once          |
| `just demo-pcap`         | Regenerate the bundled demo capture           |
| `just interfaces`        | List capturable NICs                          |
| `just check-permissions` | Probe capture rights and print the fix        |

`just --list` also shows the other recipes. `just install` installs the pre-commit hooks in
`.pre-commit-config.yaml`.

## The decoder

Capture and GOOSE decode are `zelos-packet`'s. This extension only builds the
keyword arguments (`zelos_extension_iec61850/decode.py`) and refuses, naming the
version it needs, on a release without the decoder. Tests stub that seam;
decode correctness is tested with the decoder, not here.

## Configuration

`config.schema.json` drives the configuration UI in the Zelos desktop app;
`config.json` is the shipped default. The interface picker uses a `ui:widget`
hint rather than an enum, because a static schema cannot enumerate a machine's
NICs. See the [configuration docs](https://docs.zeloscloud.io/sdk/how-to/develop-extensions/#configuration)
for the widget reference.

## Actions

`list_interfaces`, `auto_config`, `convert_pcap` and `demo_capture_path` are
declared `standalone=True`, so they run with the extension stopped.
`zelos extensions package` inventories them into `actions.json` and ships it
inside the archive. It is generated, never committed.

## Run it in Zelos

```bash
zelos extensions install-local .
```

A local install runs from your project directory, so you do not install again after a code
change:

- Agent runtime: stop and start the extension. After a dependency change, stop it and
  reinstall it with `zelos extensions reinstall`.
- `extension.toml`: run `zelos extensions install-local .` again.

## Packaging

```bash
just package
```

The archive lands next to `extension.toml` as `iec-61850-<version>.tar.gz`. It holds
`extension.toml`, the README, the icon, the config schema and the paths listed in
`[package] paths`, plus `pyproject.toml`, `uv.lock` and `actions.json`, the list of actions
that run while the extension is stopped.

## Release

```bash
just release 1.0.0
git push
git push origin v1.0.0   # the tag on its own: pushed together with the branch, Actions can miss it
```

The release workflow checks, tests and packages the extension, then attaches the archive to a
GitHub release. CI runs the same check, test and package steps on every push and pull request.

## License

MIT - see [LICENSE](LICENSE)
