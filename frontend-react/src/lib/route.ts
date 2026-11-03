// Hash routing, as in the original app: the route is the fragment, so the
// app is one static file on any host, and emailed tokens
// (#/reset-password?token=...) never reach a server.
import { useEffect, useState } from "react";

export interface Route {
  parts: string[];
  query: URLSearchParams;
}

export function parseRoute(hash: string = window.location.hash): Route {
  const cleaned = hash.replace(/^#\/?/, "");
  const [path, queryString] = cleaned.split("?");
  return { parts: path.split("/").filter(Boolean), query: new URLSearchParams(queryString || "") };
}

export function go(path: string): void {
  if (window.location.hash === `#${path}`) {
    window.dispatchEvent(new HashChangeEvent("hashchange"));
  } else {
    window.location.hash = path;
  }
}

// Replaces the address without navigating: used to drop a token from the bar.
export function scrubHash(path: string): void {
  window.history.replaceState(null, "", `${window.location.pathname}#${path}`);
}

export function useHashRoute(): Route {
  const [route, setRoute] = useState<Route>(() => parseRoute());
  useEffect(() => {
    const onChange = () => setRoute(parseRoute());
    window.addEventListener("hashchange", onChange);
    return () => window.removeEventListener("hashchange", onChange);
  }, []);
  return route;
}
