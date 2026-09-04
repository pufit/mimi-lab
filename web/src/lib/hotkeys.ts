// Window-level keyboard map for the review session (design §8.4).
//
// Rules the review session depends on:
//  * `event.repeat` is ignored — a held Enter auto-repeats `keydown` and would
//    otherwise reveal a card and rate it Good within 30 ms (a held Space would
//    stutter the clip).
//  * Events coming from an input/textarea/select/contenteditable, or from
//    inside a dialog/menu, are ignored (Radix Dialog closes on Escape without
//    stopping propagation, so a window-level Esc would also leave the session —
//    the page additionally mounts this hook with `enabled={!dialogOpen}`).
//  * Any modifier other than Shift is ignored (⌘R, Ctrl+L… stay browser keys).
//  * Space and the arrow keys get `preventDefault()` so the page never scrolls
//    under the rating bar, whether or not a handler is bound. They all belong
//    to the clip (← previous sentence · → next · ↓ replay the sentence · Space
//    pause / play) and to nothing else on this page; Enter is the forward key.

import { useEffect, useRef } from "react";

export type HotkeyHandler = (event: KeyboardEvent) => void;
/** Keys are normalized names: `space`, `enter`, `esc`, `down`, `a`…`z`, `1`…`9`, `?`. */
export type HotkeyMap = Record<string, HotkeyHandler | undefined>;

const ALIASES: Record<string, string> = {
  " ": "space",
  spacebar: "space",
  arrowdown: "down",
  arrowup: "up",
  arrowleft: "left",
  arrowright: "right",
  escape: "esc",
  esc: "esc",
  return: "enter",
};

/** `KeyboardEvent.key` → the name used in a `HotkeyMap`. */
export function normalizeKey(raw: string): string {
  const k = raw.toLowerCase();
  return ALIASES[k] ?? k;
}

/** Keys whose default action (page scroll) always has to die on this page. */
const PREVENT_DEFAULT = new Set(["space", "down", "up", "left", "right"]);

const INTERACTIVE_TAGS = new Set(["INPUT", "TEXTAREA", "SELECT", "OPTION"]);
const CONTAINER_SELECTOR = "[role='dialog'], [role='alertdialog'], [role='menu'], [role='listbox']";

function isTypingTarget(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false;
  if (INTERACTIVE_TAGS.has(target.tagName)) return true;
  if (target.isContentEditable) return true;
  return target.closest(CONTAINER_SELECTOR) !== null;
}

/**
 * Bind a keyboard map for as long as the component is mounted and `enabled`.
 *
 * The map is read through a ref, so handlers may close over fresh state on
 * every render without re-binding the listener.
 */
export function useHotkeys(map: HotkeyMap, enabled = true): void {
  const mapRef = useRef(map);
  mapRef.current = map;

  useEffect(() => {
    if (!enabled) return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.repeat) return;
      if (event.ctrlKey || event.metaKey || event.altKey) return;
      if (isTypingTarget(event.target)) return;

      const key = normalizeKey(event.key);
      if (PREVENT_DEFAULT.has(key)) event.preventDefault();

      const handler = mapRef.current[key];
      if (!handler) return;
      if (!PREVENT_DEFAULT.has(key)) event.preventDefault();
      handler(event);
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [enabled]);
}
