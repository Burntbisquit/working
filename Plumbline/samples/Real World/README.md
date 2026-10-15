# Real World - sample job built from real field data

Built 2026-10-04 from a real reduced job (Edit points.fwk). **These coordinates are a real survey of a real road job** - not generated, not rounded to look tidy, and not surveyed by anyone at this desk.

Use it to see how the program behaves on data that has not been cleaned for it. Expect duplicate point numbers, descriptions that do not parse, and a check report with real findings - that is the point.

## What is in here

| Folder | Contents |
|---|---|
| `Real World.plb` | the Plumbline project - open this |
| `Field Data/Week 1/` | the download as consolidated, plus one folder per crew |
| `Field Book/` | the office's code standard (1,717 codes) |
| `Control/` | the control points all three crews shared |
| `Reports/` | the check report the office produced for this job |
| `Source/` | the files exactly as they arrived |

## The crews were worked out from the point numbers

| Crew | Initials | Date(s) | Collector | Points | Folder |
|---|---|---|---|---|---|
| 6 | ER | 2026-07-22 | GUN | 1,053 | `Crew 6` |
| 7 | AH | 2026-07-22 | GUN | 1,051 | `Crew 7` |
| 9 | AE | 2026-07-22 | GPS, TS | 1,650 | `Crew 9` |

Nothing here was typed in by hand. Crew numbers come from the point-number blocks a crew owns - crew 7 shoots 7,001-7,999 and 70,001-79,999, and nothing else. Points 1-999 are control and are shared by everyone, which is why every crew folder contains the same setup points.

## The numbers

- **3,773 rows**, 3,773 usable
- **8 duplicate point numbers** - two crews claimed the same number
- **5 source files** across 3 crews
- point numbers 600 to 94,249
- coordinate system: `EPSG:6584 NAD83(2011) / Texas North Central (USft)`

## The office standard

1,717 feature codes: 1,593 point codes and 124 that build linework, drawing on 216 layers with 133 symbols. This is the same file the crews' descriptions were written against, so the drawing comes out in the office's own layers rather than a generic one.

## What the import did

- 3,773 points imported
- 14 duplicate numbers renumbered
- 14 numbers changed in total
- 0 points without an elevation

## Linework

Built from the descriptions: strings 390.

## Things to try

1. **Survey > Data Quality Check** - the same findings the office's check report has.
2. **Layer panel** - the job draws on the office's own layers, not `TOPO-GROUND`.
3. **Survey > Project Coordinate System** - the job is on EPSG:6584 (NAD83(2011) Texas North Central, US survey feet). Under *SAF (ground scale)* try `Scale from origin (0,0) - TXDOT SOP` with a county factor and watch the coordinates move.
4. **Imagery > Add Imagery** - align an aerial against surveyed edge-of-pavement shots, then **Draw > Distance / Bearing** between a shot and the feature it is on: comparing the two is a measurement now, not a report.
5. **Survey > Fieldwork Manager** - open the same job's field data, run the duplicate check, and send the clean points back.

## Where this came from

The `Source/` folder is the untouched input. `Field Data/` is a copy split by crew. If you re-run the sample builder, every file that holds data comes out byte-identical - the build is deterministic, so it doubles as a regression check between versions. The two files that record *when* the job was built (this job's `.plb` and `Job Setup.txt`) naturally differ by their timestamp.
