// Playback of the generated note data with the Web Audio API.
//
// Notes are scheduled a little ahead of the clock from the same events the
// server computed, so what you hear is the arrangement itself, not a rendering
// of it. Two tracks ("source" and "result") can be loaded; switching keeps the
// musical position by bar, because a slowed arrangement is longer in seconds.

const LOOKAHEAD = 0.25;      // seconds of audio scheduled ahead
const TICK_MS = 60;

function frequency(midi) {
  return 440 * 2 ** ((midi - 69) / 12);
}

export class Player extends EventTarget {
  constructor() {
    super();
    this.tracks = {};
    this.active = null;
    this.position = 0;          // seconds into the active track
    this.speed = 1;
    this.playing = false;
    this._context = null;
    this._timer = null;
    this._cursor = 0;
    this._startedAt = 0;
    this._startedPosition = 0;
    this._voices = new Set();
  }

  load(name, playback) {
    const notes = (playback.notes || []).map((n) => ({ pitch: n[0], onset: n[1], duration: n[2], velocity: n[3], staff: n[4], id: n[5], bar: n[6] }))
      .sort((a, b) => a.onset - b.onset);
    this.tracks[name] = { notes, duration: playback.duration || 0, bars: playback.bars || [] };
    if (!this.active) this.active = name;
    this._emit();
  }

  get track() { return this.tracks[this.active] || { notes: [], duration: 0, bars: [] }; }
  get duration() { return this.track.duration; }

  barAt(seconds, track = this.track) {
    let bar = track.bars.length ? track.bars[0][0] : 1;
    for (const [number, start] of track.bars) {
      if (start <= seconds + 1e-6) bar = number; else break;
    }
    return bar;
  }

  _barPosition(seconds, track) {
    const bars = track.bars;
    if (!bars.length) return { bar: 1, fraction: 0 };
    let index = 0;
    while (index + 1 < bars.length && bars[index + 1][1] <= seconds + 1e-6) index += 1;
    const start = bars[index][1];
    const end = index + 1 < bars.length ? bars[index + 1][1] : track.duration;
    return { bar: bars[index][0], fraction: end > start ? Math.min(1, (seconds - start) / (end - start)) : 0 };
  }

  _secondsFor({ bar, fraction }, track) {
    const bars = track.bars;
    const index = bars.findIndex(([number]) => number === bar);
    if (index < 0) return Math.min(this.position, track.duration);
    const start = bars[index][1];
    const end = index + 1 < bars.length ? bars[index + 1][1] : track.duration;
    return start + fraction * (end - start);
  }

  use(name) {
    if (!this.tracks[name] || name === this.active) return;
    const wasPlaying = this.playing;
    const where = this._barPosition(this.currentTime(), this.track);
    this.pause();
    this.active = name;
    this.position = this._secondsFor(where, this.track);
    if (wasPlaying) this.play(); else this._emit();
  }

  currentTime() {
    if (!this.playing || !this._context) return this.position;
    return Math.min(this.duration, this._startedPosition + (this._context.currentTime - this._startedAt) * this.speed);
  }

  async play() {
    if (this.playing || !this.track.notes.length) return;
    if (!this._context) this._context = new (window.AudioContext || window.webkitAudioContext)();
    if (this._context.state === "suspended") await this._context.resume();
    if (this.position >= this.duration - 0.01) this.position = 0;
    this.playing = true;
    this._startedAt = this._context.currentTime + 0.05;
    this._startedPosition = this.position;
    this._cursor = this.track.notes.findIndex((n) => n.onset >= this.position - 1e-6);
    if (this._cursor < 0) this._cursor = this.track.notes.length;
    this._timer = window.setInterval(() => this._tick(), TICK_MS);
    this._tick();
    this._emit();
  }

  pause() {
    if (!this.playing) return;
    this.position = this.currentTime();
    this.playing = false;
    window.clearInterval(this._timer);
    for (const voice of this._voices) {
      try { voice.gain.gain.cancelScheduledValues(0); voice.gain.gain.setTargetAtTime(0, this._context.currentTime, 0.02); voice.osc.stop(this._context.currentTime + 0.1); } catch { /* already stopped */ }
    }
    this._voices.clear();
    this._emit();
  }

  toggle() { return this.playing ? this.pause() : this.play(); }

  seek(seconds) {
    const wasPlaying = this.playing;
    this.pause();
    this.position = Math.max(0, Math.min(this.duration, seconds));
    if (wasPlaying) this.play(); else this._emit();
  }

  setSpeed(value) {
    const wasPlaying = this.playing;
    this.pause();
    this.speed = Math.max(0.25, Math.min(1.5, Number(value) || 1));
    if (wasPlaying) this.play(); else this._emit();
  }

  stop() { this.pause(); this.position = 0; this._emit(); }

  dispose() {
    this.pause();
    if (this._context) this._context.close();
    this._context = null;
  }

  _tick() {
    const now = this.currentTime();
    const notes = this.track.notes;
    const horizon = now + LOOKAHEAD * this.speed;
    while (this._cursor < notes.length && notes[this._cursor].onset <= horizon) {
      this._schedule(notes[this._cursor]);
      this._cursor += 1;
    }
    if (now >= this.duration - 0.005) {
      this.pause();
      this.position = this.duration;
    }
    this._emit();
  }

  _schedule(note) {
    const context = this._context;
    const when = this._startedAt + (note.onset - this._startedPosition) / this.speed;
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
    this._voices.add(voice);
    osc.onended = () => this._voices.delete(voice);
  }

  _emit() {
    this.dispatchEvent(new CustomEvent("change", { detail: { time: this.currentTime(), playing: this.playing, bar: this.barAt(this.currentTime()) } }));
  }
}
