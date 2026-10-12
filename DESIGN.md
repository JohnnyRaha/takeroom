# Interface notes

The subject is a single-operator studio tool, used by one engineer with a DAW
open on the other half of the screen. It is not a product page and has no
audience to persuade, so there is no hero: the work surface *is* the first
screen. Every pixel either takes input or reports state.

The vernacular is studio hardware — painted steel panels, silkscreened
lettering, indicator lamps — rather than web app. That decides the palette and
the density.

## Colour

| token | value | role |
| --- | --- | --- |
| `--panel` | `#191C1A` | chassis; the page ground |
| `--surface` | `#212522` | raised panel, where inputs sit |
| `--surface-hi` | `#2A2F2B` | hover / active panel |
| `--rule` | `#343A35` | hairlines between sections |
| `--ink` | `#E6E4DB` | silkscreen white, slightly warm |
| `--ink-dim` | `#8A9089` | secondary lettering |
| `--lamp` | `#E39B2F` | amber: armed, queued, working |
| `--signal` | `#7FB7A8` | teal: signal present, ready, playing |
| `--fault` | `#C4604A` | fault |

Two indicator colours, used only as state. No accent colour is applied
decoratively anywhere.

## Type

Space Grotesk throughout — it has the slightly mechanical, wide-aperture feel of
panel lettering without being a novelty face. IBM Plex Mono only for values that
are genuinely machine-readable and need to align in a column: seed, LUFS, peak,
duration, render time. Labels stay in the sans, in sentence case. No all-caps.

## Layout

```
┌──────────────────────────────────────────────────────────────┐
│ ● acestep  ● writer  ○ stems          queue 2    -11.0 LUFS  │  status strip
├───────────────────────────┬──────────────────────────────────┤
│ brief                     │  4  Night drive through the...   │
│ ┌───────────────────────┐ │     ●ready  2:04  seed 88421037  │
│ │                       │ │     ▸ ────────────────────────   │
│ └───────────────────────┘ │                                  │
│ style prompt              │  3  Night drive through the...   │
│ lyrics                    │     ●working · mastering         │
│ duration ─────○──── 2:00  │                                  │
│ steps  guidance  takes    │  2  Slow gospel organ, 68 bpm    │
│ [ Generate ]              │     ●ready  1:30  seed 11903755  │
└───────────────────────────┴──────────────────────────────────┘
```

Left column fixed at 26rem — it is a control panel and shouldn't reflow. Right
column scrolls. Takes are numbered because they genuinely are a sequence: take
1, take 2, the same way a session log numbers them.

## Motion

One moving thing: the lamp on a working take pulses. Playback progress moves
because it is data. Nothing fades or slides in.
