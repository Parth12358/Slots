// Hold a slots lock (or pool slot) from Node until released or until this process exits.
//
// Each lock is held by a child `slots run <name> -- sh -c 'echo held; exec cat'`: the lease
// belongs to that child's connection to the slots daemon, and the child lives as long as our
// pipe to it, so a crash or kill -9 of this process releases the lock at once.
import { spawn } from 'node:child_process';

/** Say who holds it once a wait has lasted this long. */
const SAY_WAITING_AFTER_MS = 1_000;

const held = new Map();
const keyOf = (holder, name) => `${holder}\u0000${name}`;

/**
 * Hold `name` for `holder` until releaseLocks; waits in line while someone else has it. No-op if
 * already held. `whileWaiting()` is called when the wait starts and returns a function called when
 * it ends (see ./playwright.mjs for growing a test's timeout).
 */
export const holdLock = async (name, holder, { onWait, whileWaiting, slots = process.env.SLOTS_BIN ?? 'slots' } = {}) => {
  const key = keyOf(holder, name);
  if (held.has(key)) return;
  const [cmd, ...pre] = slots.split(' ');
  const child = spawn(cmd, [...pre, 'run', name, '--label', holder, '--', '/bin/sh', '-c', 'echo held; exec cat >/dev/null'], {
    stdio: ['pipe', 'pipe', 'pipe'],
  });
  const stop = whileWaiting?.() ?? (() => undefined);
  let waitingOn = 'another run';
  const sayWaiting = setTimeout(
    () => (onWait ?? ((who) => console.warn(`waiting for ${name} (held by ${who})`)))(waitingOn),
    SAY_WAITING_AFTER_MS
  );
  try {
    await new Promise((done, fail) => {
      let out = '';
      let errText = '';
      child.stdout.on('data', (chunk) => {
        out += String(chunk);
        if (out.includes('held')) done();
      });
      child.stderr.on('data', (chunk) => {
        errText += String(chunk);
        const m = /waiting for \S+ \(\d+\/\d+ in use: ([^)]*)\)/.exec(errText);
        if (m) waitingOn = m[1];
      });
      child.on('error', (e) => fail(new Error(`${name} lock failed (${e.message})`)));
      child.on('exit', (code) => fail(new Error(`${name} lock failed (slots exit ${code}): ${errText.trim()}`)));
    });
  } finally {
    clearTimeout(sayWaiting);
    stop();
  }
  held.set(key, child);
};

/** Release the locks `holder` holds (all of this process's, without a holder). */
export const releaseLocks = async (holder) => {
  for (const [key, child] of [...held]) {
    if (holder !== undefined && !key.startsWith(`${holder}\u0000`)) continue;
    held.delete(key);
    if (child.exitCode !== null) continue;
    const exited = new Promise((done) => child.once('exit', done));
    child.stdin.end();
    await exited;
  }
};

/** The locks this process holds, as [holder, name] pairs. */
export const heldLocks = () => [...held.keys()].map((k) => k.split('\u0000'));
