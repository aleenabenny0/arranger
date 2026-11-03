// Move focus to the new view's heading so keyboard and screen-reader users land
// where the content changed, the way a page load would. Not on the first view:
// a real page load starts at the top, where the skip link is the first Tab stop.
import { useEffect } from "react";

let firstView = true;

export function resetFocusTracking(): void {
  firstView = true;
}

export function focusHeading(): void {
  if (firstView) { firstView = false; return; }
  const main = document.getElementById("main");
  const heading = main ? main.querySelector<HTMLElement>("h1") : null;
  if (heading) { heading.tabIndex = -1; heading.focus({ preventScroll: false }); }
}

// `ready` is true once the view's heading is on the page; the key changes
// with the route so a new view of the same component still moves focus.
export function useFocusHeading(ready: boolean, key: string): void {
  useEffect(() => {
    if (ready) focusHeading();
  }, [ready, key]);
}

export function setTitle(text: string): void {
  document.title = `${text} · Arranger`;
}
