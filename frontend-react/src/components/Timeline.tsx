// The piece as a row of bars. Each tile says how many findings its bar has,
// with a shape and a word as well as a colour; choosing one moves the player
// there and shows the first finding on the score.
import type { Finding } from "../api/types";

export interface BarTile { bar: number; start: number; hard: number; strain: number }

export function barTiles(bars: [number, number][], findings: Finding[]): BarTile[] {
  const counts = new Map<number, { hard: number; strain: number }>();
  for (const finding of findings) {
    if (finding.bar === null || finding.bar === undefined) continue;
    const entry = counts.get(finding.bar) || { hard: 0, strain: 0 };
    if (finding.severity === "hard") entry.hard += 1; else entry.strain += 1;
    counts.set(finding.bar, entry);
  }
  return bars.map(([bar, start]) => ({ bar, start, ...(counts.get(bar) || { hard: 0, strain: 0 }) }));
}

export function tileLabel(tile: BarTile): string {
  const parts = [`Bar ${tile.bar}`];
  if (tile.hard) parts.push(`${tile.hard} beyond your limits`);
  if (tile.strain) parts.push(`${tile.strain} ${tile.strain === 1 ? "stretch" : "stretches"}`);
  if (!tile.hard && !tile.strain) parts.push("fine");
  return parts.join(", ");
}

interface TimelineProps {
  bars: [number, number][];
  findings: Finding[];
  currentBar: number;
  onSelect: (tile: BarTile) => void;
}

export function Timeline({ bars, findings, currentBar, onSelect }: TimelineProps) {
  const tiles = barTiles(bars, findings);
  if (!tiles.length) return null;
  return (
    <ol className="timeline" aria-label="Bars" data-testid="timeline">
      {tiles.map((tile) => {
        const kind = tile.hard ? "hard" : tile.strain ? "strain" : "clear";
        return (
          <li key={tile.bar}>
            <button
              type="button"
              className={`bar-tile bar-tile-${kind}`}
              aria-label={tileLabel(tile)}
              aria-current={tile.bar === currentBar ? "true" : undefined}
              data-testid={`bar-${tile.bar}`}
              onClick={() => onSelect(tile)}
            >
              <span className="bar-number">{tile.bar}</span>
              <span className="bar-mark" aria-hidden="true">{tile.hard ? "!" : tile.strain ? "~" : ""}</span>
            </button>
          </li>
        );
      })}
    </ol>
  );
}
