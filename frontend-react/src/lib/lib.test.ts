import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { formatBytes, formatClock, formatDate, keyName, newIdempotencyKey, pitchName } from "./format";
import { go, parseRoute, scrubHash, useHashRoute } from "./route";

describe("format", () => {
  it("formats sizes, clocks and pitches", () => {
    expect(formatBytes(512)).toBe("512 B");
    expect(formatBytes(20 * 1024)).toBe("20 KB");
    expect(formatBytes(3.5 * 1024 * 1024)).toBe("3.5 MB");
    expect(formatClock(65.9)).toBe("1:05");
    expect(formatClock(-3)).toBe("0:00");
    expect(pitchName(60)).toBe("C4");
    expect(pitchName(61)).toBe("C♯4");
    expect(pitchName(21)).toBe("A0");
  });

  it("names keys from fifths and mode", () => {
    expect(keyName({ fifths: 0, mode: "major" })).toBe("C major");
    expect(keyName({ fifths: -3, mode: "minor", estimated: true })).toBe("C minor (estimated)");
    expect(keyName({ fifths: 2, mode: "major" })).toBe("D major");
    expect(keyName(null)).toBe("unknown");
  });

  it("formats dates and tolerates junk", () => {
    expect(formatDate("not a date")).toBe("");
    expect(formatDate(null)).toBe("");
    expect(formatDate("2026-09-29T10:00:00Z")).not.toBe("");
  });

  it("makes 32-character hex idempotency keys", () => {
    const key = newIdempotencyKey();
    expect(key).toMatch(/^[0-9a-f]{32}$/);
    expect(newIdempotencyKey()).not.toBe(key);
  });
});

describe("route", () => {
  afterEach(() => { window.location.hash = ""; });

  it("parses the fragment into parts and a query", () => {
    expect(parseRoute("#/project/abc")).toMatchObject({ parts: ["project", "abc"] });
    expect(parseRoute("#/reset-password?token=t1").query.get("token")).toBe("t1");
    expect(parseRoute("").parts).toEqual([]);
  });

  it("navigates and re-renders on a same-route go", async () => {
    const { result } = renderHook(() => useHashRoute());
    expect(result.current.parts).toEqual([]);
    act(() => { go("/library"); });
    await waitFor(() => expect(result.current.parts).toEqual(["library"]));
    let fired = 0;
    window.addEventListener("hashchange", () => { fired += 1; }, { once: true });
    act(() => { go("/library"); });
    expect(fired).toBe(1);
  });

  it("scrubs a token from the address without navigating", () => {
    window.location.hash = "/verify-email?token=secret";
    scrubHash("/verify-email");
    expect(window.location.hash).toBe("#/verify-email");
  });
});
