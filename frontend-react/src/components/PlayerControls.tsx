// Play, pause, seek, speed, and the choice between the arrangement and the
// original. The player itself is plain TypeScript; this renders its state.
import { useEffect, useState } from "react";
import { formatClock } from "../lib/format";
import type { Player, PlayerState } from "../lib/player";

interface PlayerControlsProps { player: Player; version: number }

export function PlayerControls({ player, version }: PlayerControlsProps) {
  const [state, setState] = useState<PlayerState>(() => player.state());
  const [dragging, setDragging] = useState(false);
  const [seekValue, setSeekValue] = useState(0);
  const [which, setWhich] = useState<string>(player.active || "result");
  const [speed, setSpeed] = useState(String(player.speed));

  useEffect(() => {
    setState(player.state());
    setWhich(player.active || "result");
    return player.subscribe(setState);
  }, [player, version]);

  const duration = player.duration;
  const time = dragging ? seekValue : state.time;

  return (
    <div className="player" role="group" aria-label="Playback">
      <button type="button" className="button button-primary" onClick={() => player.toggle()}>{state.playing ? "Pause" : "Play"}</button>
      <fieldset className="inline-choice">
        <legend className="visually-hidden">Listen to</legend>
        {(["result", "source"] as const).map((name) => (
          <span key={name}>
            <input
              type="radio"
              name="listen"
              id={`listen-${name}`}
              value={name}
              checked={which === name}
              disabled={!player.tracks[name]}
              onChange={() => { player.use(name); setWhich(name); }}
            />
            <label htmlFor={`listen-${name}`}>{name === "result" ? "Arrangement" : "Original"}</label>
          </span>
        ))}
      </fieldset>
      <input
        type="range"
        min="0"
        max={String(duration)}
        step="0.1"
        value={String(time)}
        aria-label="Position"
        aria-valuetext={`${formatClock(time)} of ${formatClock(duration)}, bar ${state.bar}`}
        onInput={(event) => { setDragging(true); setSeekValue(Number((event.target as HTMLInputElement).value)); }}
        onChange={(event) => { player.seek(Number(event.target.value)); setDragging(false); }}
      />
      <span className="clock" aria-live="off">{`${formatClock(state.time)} / ${formatClock(duration)} · bar ${state.bar}`}</span>
      <select aria-label="Speed" value={speed} onChange={(event) => { setSpeed(event.target.value); player.setSpeed(event.target.value); }}>
        {[0.5, 0.75, 1, 1.25].map((v) => <option key={v} value={String(v)}>{`${v}×`}</option>)}
      </select>
    </div>
  );
}
