# Changelog

## 1.1.8
- Crowded tiny dimensions now get right-angled pointers: each runs along its own step of the staircase and then straight across to its label, so neighbouring pointers are separate parallel lines instead of a fan of near-identical diagonals. The step order is chosen so pointers never cross.
- Staircases are only used for dimensions too short to show two arrowheads. Dimensions long enough keep their normal arrows on the row.
- Labels keep 2 pt of clear space from lines and can slide further along the column to find a gap.

## 1.1.7
- A column of labels now goes on whichever side of its row has fewer lines (the model's side or the far side) and is pushed outward in small steps until it clears the geometry and other dimension lines. Text no longer lands on top of the model's lines.

## 1.1.6
- Staircases of tiny dimensions are mirrored between the two ends of a chain.
- Bigger steps, scaled to the room available so labels always stay clear of the model.
- Labels slide along their column, keeping order and spacing, to cross as few lines as possible.

## 1.1.4 - 1.1.5
- Much faster generation (a test project of 55 bodies / 14 drawings: 264 s down to 90 s on a first run and about 40 s on a repeat run):
  - the part-to-part gap measurement skips pairs where neither part gets a drawing, runs in parallel worker processes, and is cached in `configs/gap_cache.json` until a STEP file changes;
  - the drawing of each part runs in its own worker process.
- The staircase of tiny dimensions steps into the model; each dimension line and its text step one further than the last.
- Cancelling a generation also stops its worker processes.

## 1.1.1 - 1.1.3
- Tiny chain dimensions are laid out together as one ordered column of labels that never overlap, with their own dimension lines on separate steps, white-backed text painted last, and dots instead of overlapping arrowheads on very short dimensions.
- Every chain dimension line and pointer counts as an obstacle for later labels.

## 1.1.0
- Tolerances can be switched on or off per kind: overall and chain sizes, hole diameters, hole depths, slots, fillets and chamfers. Fillets and chamfers are off by default.
- A maximum tolerance standard can be selected (ISO 2768-1 fine, medium, coarse, very coarse). A tolerance looser than the standard allows for that size is left off.
- The version is shown in the About window.

## 1.0.0
- First public release: drawings, tolerances with thermal expansion, assembly order and illustrated assembly instructions, with single-file builds for Windows, Linux and macOS.
