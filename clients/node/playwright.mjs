// Grow a running Playwright test's timeout while it waits for a lock, as tenantLock.ts did:
//   import { test } from '@playwright/test';
//   await holdLock('next-mock-core', holder, { whileWaiting: extendTimeoutWhileWaiting(test) });

/** A test's timeout grows by its lock waits, up to this much in all (the wait isn't the test's own time). */
export const MAX_WAIT_EXTENSION_MS = 15 * 60_000;
const EXTEND_EVERY_MS = 500;
const extensions = new WeakMap();

export const extendTimeoutWhileWaiting = (test) => () => {
  let info;
  try {
    info = test.info();
  } catch {
    return () => undefined;
  }
  let last = Date.now();
  const extend = () => {
    const now = Date.now();
    const granted = extensions.get(info) ?? 0;
    const add = Math.min(now - last, MAX_WAIT_EXTENSION_MS - granted);
    last = now;
    if (add <= 0 || info.timeout === 0) return;
    info.setTimeout(info.timeout + add);
    extensions.set(info, granted + add);
  };
  const timer = setInterval(extend, EXTEND_EVERY_MS);
  return () => {
    clearInterval(timer);
    extend();
  };
};
