// Playback of the generated note data with the Web Audio API.
//
// Notes are scheduled a little ahead of the clock from the same events the
// server computed, so what you hear is the arrangement itself, not a rendering
// of it. Two tracks ("source" and "result") can be loaded; switching keeps the
// musical position by bar, because a slowed arrangement is longer in seconds.
import type { Playback } from "../api/types";

const LOOKAHEAD = 0.25;      // seconds of audio scheduled ahead
const TICK_MS = 60;

export interface PlayerNote { pitch: number; onset: number; duration: number; velocity: number; staff: number; id: string; bar: number }
export interface Track { notes: PlayerNote[]; duration: number; bars: [number, number][] }
export interface PlayerState { time: number; playing: boolean; bar: number }

interface Voice { osc: OscillatorNode; gain: GainNode }

export function frequency(midi: number): number {
  return 440 * 2 ** ((midi - 69) / 12);
}

const EMPTY: Track = { notes: [], duration: 0, bars: [] };

type AudioContextFactory = () => AudioContext;

function defaultContext(): AudioContext {
  const Ctor = window.AudioContext || (window as unknown as { webkitAudioContext: typeof AudioContext }).webkitAudioContext;
  return new Ctor();
}

export class Player {
  tracks: Record<string, Track> = {};
  active: string | null = null;
  position = 0;          // seconds into the active track
  speed = 1;
  playing = false;
  private context: AudioContext | null = null;
  private timer: number | null = null;
  private cursor = 0;
  private startedAt = 0;
  private startedPosition = 0;
  private voices = new Set<Voice>();
  private listeners = new Set<(state: PlayerState) => void>();
  private makeContext: AudioContextFactory;

  constructor(makeContext: AudioContextFactory = defaultContext) {
    this.makeContext = makeContext;
  }

  subscribe(listener: (state: PlayerState) => void): () => void {
    this.listeners.add(listener);
    return () => { this.listeners.delete(listener); };
  }

  load(name: string, playback: Playback): void {
    const notes = (playback.notes || [])
      .map((n) => ({ pitch: n[0], onset: n[1], duration: n[2], velocity: n[3], staff: n[4], id: n[5], bar: n[6] }))
      .sort((a, b) => a.onset - b.onset);
    this.tracks[name] = { notes, duration: playback.duration || 0, bars: playback.bars || [] };
    if (!this.active) this.active = name;
    this.emit();
  }

  clear(): void {
    this.stop();
    this.tracks = {};
    this.active = null;
  }

  get track(): Track { return (this.active && this.tracks[this.active]) || EMPTY; }
  get duration(): number { return this.track.duration; }

  barAt(seconds: number, track: Track = this.track): number {
    let bar = track.bars.length ? track.bars[0][0] : 1;
    for (const [number, start] of track.bars) {
      if (start <= seconds + 1e-6) bar = number; else break;
    }
    return bar;
  }

  private barPosition(seconds: number, track: Track): { bar: number; fraction: number } {
    const bars = track.bars;
    if (!bars.length) return { bar: 1, fraction: 0 };
    let index = 0;
    while (index + 1 < bars.length && bars[index + 1][1] <= seconds + 1e-6) index += 1;
    const start = bars[index][1];
    const end = index + 1 < bars.length ? bars[index + 1][1] : track.duration;
    return { bar: bars[index][0], fraction: end > start ? Math.min(1, (seconds - start) / (end - start)) : 0 };
  }

  private secondsFor({ bar, fraction }: { bar: number; fraction: number }, track: Track): number {
    const bars = track.bars;
    const index = bars.findIndex(([number]) => number === bar);
    if (index < 0) return Math.min(this.position, track.duration);
    const start = bars[index][1];
    const end = index + 1 < bars.length ? bars[index + 1][1] : track.duration;
    return start + fraction * (end - start);
  }

  use(name: string): void {
    if (!this.tracks[name] || name === this.active) return;
    const wasPlaying = this.playing;
    const where = this.barPosition(this.currentTime(), this.track);
    this.pause();
    this.active = name;
    this.position = this.secondsFor(where, this.track);
    if (wasPlaying) void this.play(); else this.emit();
  }

  currentTime(): number {
    if (!this.playing || !this.context) return this.position;
    return Math.min(this.duration, this.startedPosition + (this.context.currentTime - this.startedAt) * this.speed);
  }

  async play(): Promise<void> {
    if (this.playing || !this.track.notes.length) return;
    if (!this.context) this.context = this.makeContext();
    if (this.context.state === "suspended") await this.context.resume();
    if (this.position >= this.duration - 0.01) this.position = 0;
    this.playing = true;
    this.startedAt = this.context.currentTime + 0.05;
    this.startedPosition = this.position;
    this.cursor = this.track.notes.findIndex((n) => n.onset >= this.position - 1e-6);
    if (this.cursor < 0) this.cursor = this.track.notes.length;
    this.timer = window.setInterval(() => this.tick(), TICK_MS);
    this.tick();
    this.emit();
  }

  pause(): void {
    if (!this.playing) return;
    this.position = this.currentTime();
    this.playing = false;
    if (this.timer !== null) window.clearInterval(this.timer);
    this.timer = null;
    const context = this.context;
    for (const voice of this.voices) {
      try {
        if (context) {
          voice.gain.gain.cancelScheduledValues(0);
          voice.gain.gain.setTargetAtTime(0, context.currentTime, 0.02);
          voice.osc.stop(context.currentTime + 0.1);
        }
      } catch { /* already stopped */ }
    }
    this.voices.clear();
    this.emit();
  }

  toggle(): void { if (this.playing) this.pause(); else void this.play(); }

  seek(seconds: number): void {
    const wasPlaying = this.playing;
    this.pause();
    this.position = Math.max(0, Math.min(this.duration, seconds));
    if (wasPlaying) void this.play(); else this.emit();
  }

  setSpeed(value: number | string): void {
    const wasPlaying = this.playing;
    this.pause();
    this.speed = Math.max(0.25, Math.min(1.5, Number(value) || 1));
    if (wasPlaying) void this.play(); else this.emit();
  }

  stop(): void { this.pause(); this.position = 0; this.emit(); }

  dispose(): void {
    this.pause();
    if (this.context) void this.context.close();
    this.context = null;
    this.listeners.clear();
  }

  private tick(): void {
    const now = this.currentTime();
    const notes = this.track.notes;
    const horizon = now + LOOKAHEAD * this.speed;
    while (this.cursor < notes.length && notes[this.cursor].onset <= horizon) {
      this.schedule(notes[this.cursor]);
      this.cursor += 1;
    }
    if (now >= this.duration - 0.005) {
      this.pause();
      this.position = this.duration;
    }
    this.emit();
  }

  private schedule(note: PlayerNote): void {
    const context = this.context;
    if (!context) return;
    const when = this.startedAt + (note.onset - this.startedPosition) / this.speed;
    const length = Math.max(0.08, note.duration / this.speed);
    const peak = 0.04 + 0.16 * (note.velocity / 127) ** 1.6;
    const osc = context.createOscillator();
    const overtone = context.createOscillator();
    const gain = context.createGain();
    osc.type = "triangle";
    overtone.type = "sine";
    osc.frequency.value = frequency(note.pitch);
    overtone.frequency.value = frequency(note.pitch) * 2;
    const overtoneGain = context.createGain();
    overtoneGain.gain.value = 0.25;
    overtone.connect(overtoneGain).connect(gain);
    osc.connect(gain).connect(context.destination);
    const start = Math.max(when, context.currentTime);
    gain.gain.setValueAtTime(0, start);
    gain.gain.linearRampToValueAtTime(peak, start + 0.008);
    gain.gain.exponentialRampToValueAtTime(Math.max(0.0008, peak * 0.35), start + Math.min(length, 1.2));
    gain.gain.setTargetAtTime(0, start + length, 0.06);
    osc.start(start); overtone.start(start);
    osc.stop(start + length + 0.4); overtone.stop(start + length + 0.4);
    const voice = { osc, gain };
    this.voices.add(voice);
    osc.onended = () => { this.voices.delete(voice); };
  }

  state(): PlayerState {
    const time = this.currentTime();
    return { time, playing: this.playing, bar: this.barAt(time) };
  }

  private emit(): void {
    const state = this.state();
    for (const listener of this.listeners) listener(state);
  }
}
