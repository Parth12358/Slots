export declare const MAX_WAIT_EXTENSION_MS: number;
/** For holdLock's whileWaiting: pass Playwright's `test`. */
export declare const extendTimeoutWhileWaiting: (test: {
  info(): { timeout: number; setTimeout(ms: number): void };
}) => () => () => void;
