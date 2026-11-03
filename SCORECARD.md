# Scorecard

23 piece(s), profile `me`. Brute force searches every skill-legal whole-piece figure at 1-3 voices and 3 fold windows.

Cost = hard violations + a penalty for falling below the fidelity floor;
lower is better and 0 means playable with the music intact. Quality ranks
cost, then tempo, then strain, then fidelity above the floor, in that order
(`arranger.engine.quality_key`). A win on quality at equal cost means the
arrangement plays with less strain, or keeps more of the piece, than the
best uniform plan.

## Deterministic engine vs brute force

| piece | notes | brute force cost / strain / fidelity (plan) | engine cost / strain / fidelity (plan) | on cost | on quality | brute force time | engine time |
|---|---|---|---|---|---|---|---|
| andre-sonatine-op34-1 | 571 | 0.00 / 0 / 0.985 (arpeggio/v2/f0) | 0.00 / 0 / 0.989 (arpeggio/v2/f0) | tie | tie | 2.1s | 4.3s |
| bach-invention-04-bwv775 | 443 | 0.00 / 0 / 0.990 (alberti/v2/f0) | 0.00 / 0 / 0.990 (alberti/v2/f0) | tie | tie | 2.4s | 4.9s |
| beethoven-fur-elise-woo59 | 905 | 0.00 / 2 / 0.995 (alberti/v2/f0) | 0.00 / 2 / 0.995 (alberti/v2/f0) | tie | tie | 5.9s | 9.4s |
| beethoven-piano-concerto3-1 | 9951 | 1.00 / 22 / 0.959 (arpeggio/v3/f0) | 0.00 / 0 / 0.943 (block/v2/f12 @40%) | win | win | 56.1s | 96.7s |
| beethoven-violin-concerto-1 | 13541 | 0.00 / 2 / 0.948 (arpeggio/v3/f16) | 0.00 / 2 / 0.962 (arpeggio/v3/f16) | tie | win | 69.9s | 260.0s |
| brahms-waltz-op39-15 | 720 | 0.00 / 0 / 0.995 (arpeggio/v2/f0) | 0.00 / 0 / 0.995 (arpeggio/v2/f0) | tie | tie | 8.6s | 14.9s |
| burgmuller-op100-01-la-candeur | 241 | 0.00 / 0 / 0.992 (arpeggio/v2/f0) | 0.00 / 0 / 0.992 (arpeggio/v2/f0) | tie | tie | 6.6s | 10.7s |
| burgmuller-op100-02-arabesque | 283 | 0.00 / 0 / 0.990 (arpeggio/v2/f0) | 0.00 / 0 / 0.990 (arpeggio/v2/f0) | tie | tie | 5.5s | 9.0s |
| burgmuller-op100-05-innocence | 215 | 0.00 / 0 / 0.988 (alberti/v2/f0) | 0.00 / 0 / 0.995 (alberti/v2/f0) | tie | win | 4.3s | 7.2s |
| burgmuller-op100-08-la-gracieuse | 318 | 0.00 / 0 / 0.991 (arpeggio/v2/f0) | 0.00 / 0 / 0.995 (arpeggio/v2/f0) | tie | tie | 4.9s | 8.0s |
| burgmuller-op100-10-tendre-fleur | 269 | 0.00 / 0 / 0.967 (arpeggio/v2/f0) | 0.00 / 0 / 1.000 (arpeggio/v2/f0) | tie | win | 6.5s | 11.2s |
| burgmuller-op100-12-ladieu | 601 | 0.00 / 8 / 0.937 (arpeggio/v2/f0) | 0.00 / 8 / 0.969 (arpeggio/v2/f0) | tie | win | 13.7s | 22.7s |
| burgmuller-op100-15-ballade | 756 | 0.00 / 0 / 0.982 (alberti/v2/f0) | 0.00 / 0 / 0.982 (alberti/v2/f0) | tie | tie | 12.1s | 19.6s |
| chopin-prelude-op28-20 | 286 | 0.00 / 0 / 0.962 (arpeggio/v2/f0) | 0.00 / 0 / 0.971 (arpeggio/v2/f0) | tie | win | 3.6s | 5.8s |
| field-nocturne-5-h37 | 1074 | 0.00 / 0 / 0.983 (arpeggio/v3/f0) | 0.00 / 0 / 0.986 (arpeggio/v3/f0) | tie | win | 14.9s | 23.2s |
| joplin-solace | 2164 | 0.00 / 4 / 0.984 (arpeggio/v2/f0) | 0.00 / 4 / 0.984 (arpeggio/v2/f0) | tie | tie | 31.2s | 49.0s |
| mozart-k545-1-allegro | 1288 | 0.00 / 4 / 0.962 (arpeggio/v2/f0) | 0.00 / 4 / 0.987 (arpeggio/v2/f0) | tie | win | 22.3s | 34.3s |
| satie-gymnopedie-2 | 371 | 0.00 / 0 / 0.992 (arpeggio/v2/f0) | 0.00 / 0 / 0.992 (arpeggio/v2/f0) | tie | tie | 8.5s | 12.0s |
| schubert-impromptu-d899-3 | 2581 | 0.00 / 0 / 0.956 (arpeggio/v3/f0) | 0.00 / 0 / 0.963 (arpeggio/v3/f0) | tie | tie | 31.9s | 49.2s |
| schumann-op15-07-traumerei | 339 | 0.00 / 0 / 0.907 (alberti/v3/f0) | 0.00 / 0 / 0.907 (alberti/v3/f0) | tie | tie | 7.0s | 10.3s |
| tchaikovsky-op35-2 | 9306 | 4.00 / 75 / 0.984 (alberti/v2/f0) | 2.00 / 78 / 0.938 (block/v2/f10) | win | win | 148.7s | 182.0s |
| tchaikovsky-op39-01-morning-prayer | 246 | 0.00 / 0 / 0.990 (arpeggio/v2/f0) | 0.00 / 0 / 0.990 (arpeggio/v2/f0) | tie | tie | 3.0s | 5.1s |
| tchaikovsky-op39-16-old-french-song | 188 | 0.00 / 0 / 0.987 (arpeggio/v2/f0) | 0.00 / 0 / 0.987 (arpeggio/v2/f0) | tie | tie | 3.8s | 6.7s |

**On cost: 2 wins, 21 ties, 0 losses.** **On quality: 9 wins, 14 ties, 0 losses.**

The engine never lost to brute force on quality.

## Reading this

Pieces whose baseline cost is already 0 cannot demonstrate anything
on cost: a perfect score is available without a model. They can still
be won on quality, which is the comparison that shows musical judgement.
The deterministic engine is run once per piece because it is
deterministic; its numbers are exact, not a sample.
