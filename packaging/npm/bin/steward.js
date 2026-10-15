#!/usr/bin/env node
'use strict';
// The Steward launcher: find a Python >= 3.12, set the environment the rest of the package expects, run
// `<python> -P -m steward.cli <args>` with inherited stdio and exit with its status. No dependencies. All logic past
// Python discovery lives in Python.
//
// -P keeps a `steward/` directory in the user's cwd from shadowing the vendored package; the vendored root is also
// first on PYTHONPATH. Nothing here installs anything on its own: `uv python install 3.12` is offered on a terminal,
// answered with one key, and never run without that answer (never with sudo).

const fs = require('fs');
const os = require('os');
const path = require('path');
const cp = require('child_process');

const MIN = [3, 12];
const NAMES = ['python3.13', 'python3.12', 'python3', 'python'];
const ROOT = path.resolve(__dirname, '..');
const SHIM = __filename;

const PROBE = 'import sys; print("%d.%d" % sys.version_info[:2])';
const MISSING =
  'steward: Python 3.12 or newer is required and was not found. ' +
  'Fix: install Python 3.12 or uv (https://docs.astral.sh/uv/), then run steward again.';

function isExecutable(file) {
  try {
    if (!fs.statSync(file).isFile()) return false;
    fs.accessSync(file, fs.constants.X_OK);
    return true;
  } catch (e) {
    return false;
  }
}

function pathDirs(env) {
  return (env.PATH || '').split(path.delimiter).filter(Boolean);
}

// Absolute path of `name` on PATH (or `name` itself when it already contains a separator), else ''.
function which(name, env) {
  if (name.includes('/')) return isExecutable(name) ? path.resolve(name) : '';
  for (const dir of pathDirs(env)) {
    const candidate = path.join(dir, name);
    if (isExecutable(candidate)) return candidate;
  }
  return '';
}

// True when `file` runs and reports Python >= 3.12.
function goodPython(file, env) {
  const res = cp.spawnSync(file, ['-c', PROBE], { env, encoding: 'utf8', timeout: 20000 });
  if (res.error || res.status !== 0) return false;
  const hit = /^(\d+)\.(\d+)/.exec((res.stdout || '').trim());
  if (!hit) return false;
  const major = Number(hit[1]);
  const minor = Number(hit[2]);
  return major > MIN[0] || (major === MIN[0] && minor >= MIN[1]);
}

function uvFind(env) {
  const uv = which('uv', env);
  if (!uv) return '';
  const res = cp.spawnSync(uv, ['python', 'find', '3.12'], { env, encoding: 'utf8', timeout: 30000 });
  if (res.error || res.status !== 0) return '';
  const found = (res.stdout || '').trim().split('\n').pop().trim();
  return found && goodPython(found, env) ? found : '';
}

function findPython(env) {
  const wanted = (env.STEWARD_PYTHON || '').trim();
  const candidates = wanted ? [wanted, ...NAMES] : NAMES;
  for (const name of candidates) {
    const file = which(name, env);
    if (file && goodPython(file, env)) return file;
  }
  return uvFind(env);
}

// One keypress from the terminal, without needing Enter. Falls back to a line when `stty` is unavailable.
function readKey() {
  const stty = (args) => cp.spawnSync('stty', args, { stdio: ['inherit', 'pipe', 'ignore'], encoding: 'utf8' });
  const saved = stty(['-g']);
  const raw = saved.status === 0 ? stty(['-icanon', '-echo', 'min', '1']) : null;
  const buf = Buffer.alloc(16);
  let n = 0;
  try {
    n = fs.readSync(0, buf, 0, buf.length, null);
  } catch (e) {
    n = 0;
  } finally {
    if (raw && raw.status === 0) stty([saved.stdout.trim()]);
  }
  return buf.toString('utf8', 0, n).trim().charAt(0).toLowerCase();
}

function offerUv(env) {
  if (!process.stdin.isTTY || !process.stderr.isTTY) return false;
  const uv = which('uv', env);
  if (!uv) return false;
  process.stderr.write(
    'steward: Python 3.12 or newer was not found. Install it with uv (`uv python install 3.12`, no sudo)? [y/N] '
  );
  const key = readKey();
  process.stderr.write(key ? key + '\n' : '\n');
  if (key !== 'y') return false;
  const res = cp.spawnSync(uv, ['python', 'install', '3.12'], { env, stdio: ['ignore', 2, 2] });
  return !res.error && res.status === 0;
}

function realpath(file) {
  try {
    return fs.realpathSync(file);
  } catch (e) {
    return file;
  }
}

// One stderr line when a different `steward` is on PATH. Links to this very shim are not a conflict.
function announceConflict(env) {
  if (env.STEWARD_SHIM_QUIET) return;
  const mine = realpath(SHIM);
  for (const dir of pathDirs(env)) {
    const candidate = path.join(dir, 'steward');
    if (isExecutable(candidate) && realpath(candidate) !== mine) {
      process.stderr.write(
        `steward: running ${SHIM} (install root ${ROOT}); another steward on PATH: ${candidate}\n`
      );
      return;
    }
  }
}

function cacheRoot(env) {
  return (env.XDG_CACHE_HOME || '').trim() || path.join(env.HOME || os.homedir(), '.cache');
}

function main() {
  const env = Object.assign({}, process.env);
  let python = findPython(env);
  if (!python && offerUv(env)) python = findPython(env);
  if (!python) {
    process.stderr.write(MISSING + '\n');
    return 1;
  }
  announceConflict(env);

  env.STEWARD_INSTALL_ROOT = ROOT;
  env.STEWARD_SHIM = SHIM;
  env.STEWARD_NODE = process.execPath;
  env.STEWARD_PYTHON = python;
  env.PYTHONPATH = env.PYTHONPATH ? ROOT + path.delimiter + env.PYTHONPATH : ROOT;
  if (!(env.PYTHONPYCACHEPREFIX || '').trim()) {
    env.PYTHONPYCACHEPREFIX = path.join(cacheRoot(env), 'steward', 'pycache');
  }

  const child = cp.spawn(python, ['-P', '-m', 'steward.cli', ...process.argv.slice(2)], { env, stdio: 'inherit' });
  // The terminal already delivers ^C to the whole foreground group; forward only the signals it would not.
  process.on('SIGINT', () => {});
  for (const sig of ['SIGTERM', 'SIGHUP']) process.on(sig, () => child.kill(sig));
  child.on('error', (err) => {
    process.stderr.write(`steward: could not run ${python}: ${err.message}\n`);
    process.exit(1);
  });
  child.on('exit', (code, signal) => {
    if (signal) {
      process.removeAllListeners(signal);
      process.kill(process.pid, signal);
      // a signal that does not terminate (SIGINT is ignored above) still ends the shim
      setTimeout(() => process.exit(128 + (os.constants.signals[signal] || 0)), 50);
      return;
    }
    process.exit(code === null ? 1 : code);
  });
  return null;
}

const status = main();
if (status !== null) process.exit(status);
