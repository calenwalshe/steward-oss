# Contributing

## Fork workflow

Steward is meant to be run from your own fork, with local changes kept apart
from what you send upstream.

1. Fork the upstream repo (`steward-oss`, branch `main`) and add it as a remote:
   `git remote add upstream <url>`.
2. Keep your fork's `main` an exact mirror of `upstream/main`. Never commit
   personal changes to it.
3. Run your live install from a personal branch (for example `mine`) that
   carries your own tweaks.
4. For a fix, branch `fix/<topic>` from `main`, push it to your fork, and open
   a PR against upstream `main`.
5. After it merges, bring it home: `git fetch upstream && git checkout mine &&
   git merge upstream/main`.

## Dev setup

Python 3.12+, git, Node >= 18. Runtime has no third-party dependencies; tests
need pytest and pytest-xdist:

```console
$ python -m venv .venv && . .venv/bin/activate
$ pip install pytest pytest-xdist
```

## Tests

```console
$ python -m pytest -n 8 -q
```

- New tests go in new files (`tests/test_<topic>.py`) rather than growing old ones.
- Tests must not touch your real config, Herdr, Telegram or the network; use
  tmp dirs and the fakes (`FakeWorker`, `FakeTransport`) already in the suite.
- If a test fails only under `-n`, it has an isolation bug; fix that.

## Keep personal config out of code

Names, hosts, paths, tokens and model choices belong in the config dir
(`~/.config/steward/`: `steward.toml`, `roster.toml`, `trajectory.toml`),
never in `steward/`, tests or docs. The export scan rejects personal markers.

## Before a release PR

```console
$ python3 scripts/oss_export.py --ref HEAD --out /tmp/steward-export --run-tests
```

This builds the public snapshot from `packaging/oss/export.toml`, scans for
personal markers (exits non-zero on any hit), and runs the suite inside the
exported tree. Use a fresh, empty `--out` directory.
