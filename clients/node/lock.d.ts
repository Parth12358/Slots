export interface HoldOptions {
  /** Called once a wait has lasted a second, with who holds it. Default: console.warn. */
  onWait?: (holder: string) => void;
  /** Called when a wait starts; returns a function called when it ends. */
  whileWaiting?: () => () => void;
  /** How to run slots. Default: $SLOTS_BIN or "slots". */
  slots?: string;
}
export declare const holdLock: (name: string, holder: string, options?: HoldOptions) => Promise<void>;
export declare const releaseLocks: (holder?: string) => Promise<void>;
export declare const heldLocks: () => [string, string][];
