/**
 * Host process management for the browser acceptance suite.
 *
 * Starts `tests/fixtures/accounts_server.py` (a real host: Runner/RunStore/
 * EventBus/HTTP/SSE/production front-end) with a temporary data root and only
 * the execution face replaced by a scripted fake. The fixture prints one
 * `READY {json}` line; we own the child process and terminate it by PID — never
 * by a broad pattern match.
 */

import { spawn } from 'node:child_process';
import { existsSync, mkdirSync, openSync, writeSync, closeSync } from 'node:fs';
import { join } from 'node:path';
import { createInterface } from 'node:readline';

function pythonPath(repoRoot) {
  const venv = join(repoRoot, '.venv', 'bin', 'python');
  return existsSync(venv) ? venv : 'python3';
}

/** A temporary HOME/XDG keeps the fixture from ever reading the operator's pi state. */
function isolatedEnv(homeDir) {
  const env = { ...process.env };
  env.HOME = homeDir;
  env.XDG_CONFIG_HOME = join(homeDir, '.config');
  env.XDG_CACHE_HOME = join(homeDir, '.cache');
  env.XDG_DATA_HOME = join(homeDir, '.local', 'share');
  env.XDG_STATE_HOME = join(homeDir, '.local', 'state');
  return env;
}

export async function startHost({ repoRoot, mode, stateDir, logFile, requestLog }) {
  const homeDir = join(stateDir, 'home');
  mkdirSync(homeDir, { recursive: true });
  const logFd = openSync(logFile, 'a');
  const args = ['tests/fixtures/accounts_server.py', '--state-dir', stateDir, '--mode', mode];
  if (requestLog) args.push('--request-log', requestLog);
  const child = spawn(
    pythonPath(repoRoot),
    args,
    {
      cwd: repoRoot,
      stdio: ['ignore', 'pipe', 'pipe'],
      env: isolatedEnv(homeDir),
    },
  );
  const log = (chunk) => {
    try {
      writeSync(logFd, chunk);
    } catch {
      /* the fixture log is evidence, never a reason to fail the run */
    }
  };
  child.stderr.on('data', log);
  child.stdout.on('data', log);

  const lines = createInterface({ input: child.stdout });
  const ready = new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      reject(new Error(`fixture did not report READY within 30s (log: ${logFile})`));
    }, 30_000);
    lines.on('line', (line) => {
      if (line.startsWith('READY ')) {
        clearTimeout(timer);
        try {
          resolve(JSON.parse(line.slice('READY '.length)));
        } catch (error) {
          reject(new Error(`unparsable READY line: ${line}`));
        }
      }
    });
    child.on('exit', (code, signal) => {
      clearTimeout(timer);
      reject(new Error(`fixture exited before READY (code=${code} signal=${signal}, log: ${logFile})`));
    });
  });

  const state = await ready;
  let stopped = false;
  return {
    ...state,
    mode,
    stateDir,
    logFile,
    pid: child.pid,
    async stop() {
      if (stopped) return { code: null, signal: null };
      stopped = true;
      const exited = new Promise((resolve) => {
        child.once('exit', (code, signal) => resolve({ code, signal }));
      });
      child.kill('SIGTERM');
      const timer = setTimeout(() => child.kill('SIGKILL'), 8_000);
      const result = await exited;
      clearTimeout(timer);
      lines.close();
      closeSync(logFd);
      return result;
    },
  };
}
