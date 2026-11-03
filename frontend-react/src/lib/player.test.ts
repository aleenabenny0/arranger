import { describe, expect, it, vi } from "vitest";
import type { Playback } from "../api/types";
import { frequency, Player } from "./player";

function fakeContext(): AudioContext {
  const param = () => ({ value: 0, setValueAtTime: vi.fn(), linearRampToValueAtTime: vi.fn(), exponentialRampToValueAtTime: vi.fn(), setTargetAtTime: vi.fn(), cancelScheduledValues: vi.fn() });
  const node = () => ({ connect: vi.fn().mockReturnThis(), gain: param(), frequency: param(), type: "sine", start: vi.fn(), stop: vi.fn(), onended: null });
  return {
    currentTime: 0, state: "running", destination: {}, resume: vi.fn(async () => undefined), close: vi.fn(async () => undefined),
    createOscillator: vi.fn(node), createGain: vi.fn(node),
  } as unknown as AudioContext;
}

const PLAYBACK: Playback = {
  notes: [[60, 0, 0.5, 80, 1, "n1", 1], [64, 0.5, 0.5, 80, 1, "n2", 1], [67, 1, 0.5, 80, 1, "n3", 2], [72, 1.5, 0.5, 80, 1, "n4", 2]],
  duration: 2,
  bars: [[1, 0], [2, 1]],
};

describe("Player", () => {
  it("loads tracks sorted by onset and reports the bar at a time", () => {
    const player = new Player(fakeContext);
    player.load("result", { ...PLAYBACK, notes: [...PLAYBACK.notes].reverse() });
    expect(player.track.notes.map((n) => n.id)).toEqual(["n1", "n2", "n3", "n4"]);
    expect(player.duration).toBe(2);
    expect(player.barAt(0.2)).toBe(1);
    expect(player.barAt(1.2)).toBe(2);
  });

  it("keeps the musical position when switching to a track of another length", () => {
    const player = new Player(fakeContext);
    player.load("result", PLAYBACK);
    player.load("source", { ...PLAYBACK, duration: 4, bars: [[1, 0], [2, 2]] });
    player.seek(1.5);                  // halfway through bar 2 of the result
    player.use("source");
    expect(player.active).toBe("source");
    expect(player.position).toBeCloseTo(3, 5);   // halfway through bar 2 of the source
  });

  it("plays, schedules voices, pauses and notifies subscribers", async () => {
    vi.useFakeTimers();
    const context = fakeContext();
    const player = new Player(() => context);
    player.load("result", PLAYBACK);
    const states: boolean[] = [];
    player.subscribe((s) => states.push(s.playing));
    await player.play();
    expect(player.playing).toBe(true);
    expect((context.createOscillator as ReturnType<typeof vi.fn>).mock.calls.length).toBeGreaterThan(0);
    (context as { currentTime: number }).currentTime = 0.5;
    vi.advanceTimersByTime(70);
    player.pause();
    expect(player.playing).toBe(false);
    expect(player.position).toBeGreaterThan(0);
    expect(states).toContain(true);
    expect(states[states.length - 1]).toBe(false);
    player.setSpeed(0.5);
    expect(player.speed).toBe(0.5);
    player.setSpeed("junk");
    expect(player.speed).toBe(1);
    player.stop();
    expect(player.position).toBe(0);
    player.dispose();
    expect(context.close).toHaveBeenCalled();
    vi.useRealTimers();
  });

  it("does nothing without notes", async () => {
    const player = new Player(fakeContext);
    await player.play();
    expect(player.playing).toBe(false);
    expect(frequency(69)).toBe(440);
    expect(frequency(81)).toBeCloseTo(880, 5);
  });
});
