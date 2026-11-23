# der-opt scenario creator specification

**Version 1.0 — approved 2026-11-22**, with the recommended answer to every decision in
section 7 (1-5 agreed on draft 0.1, 6-7 on draft 0.2, which added the feeder steps).
Change it only by a new version, approved the same way.

## Purpose

Today a new scenario starts one of two ways, and neither reaches everything der-opt can
now model:

- **Templates** (`der-opt new TEMPLATE`, the app's "From a template"): four fixed
  recipes. Whatever a recipe doesn't cover is left as TODO in the YAML.
- **Library blocks** (`der-opt new --pick`, the app's "From library blocks"): a
  single home only, built from blocks at their stated sizes. It can't set a battery's
  kWh, the cost tests' comparison for each device, a DR program's delivery or event
  length, or make a study.

The five-home portfolio had to be written by hand, data script included.

This spec replaces both with **one guided flow in the local app**: a home or a study,
built step by step, with the YAML and its checks shown as you go. Its output is an
ordinary scenario or study file, the same YAML you'd write by hand, so nothing
downstream changes.

## 1. The steps

One page, steps down the left, each a short form. A step is done when its required
fields are; any step can be revisited. The YAML preview and the check (section 3) sit
on the right and update as you go. **Step 1 decides which steps appear:**

| Kind | Steps |
|---|---|
| Single home | 1, 2, 3, 5, 6, 7, 8, 10, 12, 13 |
| Portfolio (many homes, each on its own) | 1, 2, 3, 5, 6, 7, 8, 10, 12, 13 |
| Feeder (homes on a network, optimized together) | every step |
| Feeder and portfolio (the same homes, both ways) | every step |

| # | Step | A home, or a portfolio | A feeder |
|---|---|---|---|
| 1 | **What** | Single home, or Portfolio | Feeder, or Feeder and portfolio |
| 2 | **When** | the year, or a window (start, length, resolution); a cost test needs a whole calendar year, and the flow says so | the same; a planning window around a peak is a preset (three days, noon to noon) |
| 3 | **Data** | a home: a time series file (load, and optionally solar, EV and outdoor temperature) and the premise in it; a study: the premises, devices and time series files (input spec 2.0) | the same, plus the network file if there is one |
| 4 | **Network** | — | the elements: each one's name, normal rating (kW), parent (the tree is drawn as you go), and either a hard limit or an overload cost ($/kWh over normal); optionally an emergency rating, with a violation cost above it (or a hard wall) and a daily kWh budget above normal (coordination spec v1.1, section 1b). The fields say what each choice does to congestion prices: a hard limit gives exact prices but an hour that can't fit stops the run; a violation cost keeps it solvable and flags those hours. From `network.csv` or entered here. Which element each premise is on (the premises file's `element_id`, or assigned here). Load that isn't a customer, per element: a time series column, a loadcast trace (`poe10`, `poe50`, `poe90`, `max`, or a quantile), or none |
| 5 | **Devices** | a home: add devices by type, template, size and key settings (battery kW and kWh; solar kW from `pv_kw` or a size; EV pattern or plug-in data; thermostat comfort band, with outdoor temperature from the data); a study: the devices file's mix by type, and devices added to a share of homes (`add_devices`: type, template, size, share, seed, which homes) | the same, with the mix shown by element |
| 6 | **Prices and bill** | tariff, export program (NBT, NEM 2.0, none) and what to optimize (the bill, or an import and export price); in a study, defaults, each premise's tariff and program can come from the premises file | the same |
| 7 | **Meter** | import and export limits; the single meter | as defaults |
| 8 | **Today's behavior** | each device's rule-based counterfactual, defaulted by type: battery self-consumption, EV TOU timer, thermostat setpoint | the same, as defaults by type |
| 9 | **Who takes part** | — | for each home (as study-wide defaults, a premises column, or per premise): participant or not; its baseline behavior (optimizing its own bill, or rule-based); how a participant responds (optimized, or by rules); and for rule responders, each device's rules (pause in a DR event, hold to an envelope). A count by element shows who's in |
| 10 | **DR program** | none, or a program for each home: season, days, window, event length, response (incentive, commitment, import cap), delivery (per kWh or uniform), settlement basis, and the events (random, schedule, price-based, or a file) | (the portfolio's program, when the study has both) |
| 11 | **Coordination** | — | the mechanisms the feeder uses, any of: a **price signal** (from the coordinated run's congestion prices, a schedule by element and hours, or a file; paid or not); **operating envelopes** (how import and export headroom is shared: pro rata to need, equal, by service size, weights, max-min, fixed, or a file; headroom margin; nesting down the tree; per step or in blocks; minimum kW; violation penalty); **feeder DR events** (the element, a program as in step 10, triggered above a % of its rating or set as events, fixed or variable length). Then which runs to compare: each mechanism alone and all together (the default), or named combinations. The coordinated, independent and counterfactual runs are always made |
| 12 | **Value and cost tests** | screening values (an avoided cost set); cost tests: life, start year, costs, discount rates, and what each device is compared against (removed, rule-based, TOU timer, events-only, backup-only), a battery's mode required; in a study, a removed device's installed cost from the devices file | the network's own value: capacity deferral on an element ($/kW, above a threshold kW); and, with a portfolio too, its screening and cost tests |
| 13 | **Review** | the YAML in full, every check, then **Write** (and **Write and run**) | the same |

- **Presets** start the flow filled in: "a battery home in a DR program", "a solar and
  battery home on its bill", "a feeder study from files", "a battery on a wholesale
  price". They're today's four templates, as pre-filled steps.
- **Every field shows its default** and where it comes from (a block, a template, the
  data), so a step left untouched is still explained.
- **Fields name their units** (kW, kWh, $/kWh, °C, hours) and say what a value means
  in a sentence, as the templates' comments do now.

## 2. What it writes

- **One file**, never overwritten (as now): a scenario, or a study, in the project.
- **Blocks where they fit, settings where they don't.** A pick that matches a library
  block exactly is written as an `extends` of that block. A size or setting changed from
  the block is written as an override beside it (`devices: [{name: battery, params:
  {capacity_kwh: 10}}]`, as `der-opt new --use` writes now). Settings with no block
  are written out in full.
- **Commented** as the templates are: each section says what it's for, and what's
  illustrative.
- **The flow's answers aren't stored anywhere else.** The file is the record; reopening
  it goes to the file's Edit, Check and Study settings tabs (decision 3).

## 3. Checking as you go

- **Each step checks what it can** without solving: the data file has the columns the
  devices need (a thermostat without `outdoor_temp_c` says so at step 5, not at the run),
  the window lies inside the data, the tariff's dates cover the window, a cost test has a
  whole year and a mode for every battery, the DR window fits the program's hours. For
  a feeder: every premise is on an element that exists, the tree has no loop, an
  element with an envelope, a trigger or a deferral value has a rating, and the network's
  background load covers the window.
- **The Review step runs `der-opt validate`** on the file as it would be written, and
  shows its findings with the step each belongs to, so a fix is one click away.
- **Errors block Write; warnings don't.**

## 4. The command line

- `der-opt new` keeps working for scripts: `--use BLOCK` and the templates stay, as
  shortcuts to the same writer the flow uses (decision 4).
- `der-opt new FILE --pick`, the terminal picker, is retired in favor of the app.
- **As built:** `der-opt new TEMPLATE` goes through the creator's writer. With
  `--library DIR` it still writes a template's shared parts as fragment files in that
  library for later scenarios to reuse, something the flow doesn't do; that mode keeps
  today's template writer. Each preset is tested to write, setting for setting, what its
  template wrote.

## 5. What it doesn't do

- **No data generation.** The flow reads data files; it doesn't synthesize load, solar
  or weather. (The five homes' generator stays a one-off script.)
- **No editing of an existing file through the steps.** Free-form YAML can't round-trip
  through forms without losing comments and structure. Existing files keep the Edit,
  Check and Study settings tabs.
- **No new modeling.** Every field maps to a setting that exists today (the feeder
  steps to `analyses.feeder`, its `coordination`, and the population's role columns,
  docs/COORDINATION_SPEC.md).
- **No network drawing tool.** Step 4 draws the tree it's given; elements are entered as
  rows, not placed on a map.

## 6. Building it

- **The writer first:** one function from the flow's answers to YAML text, shared by
  the app and `der-opt new`, tested on its own against today's templates (each preset
  must write what its template writes now, setting for setting).
- **Then the steps,** in the app's existing style: server-rendered forms with small
  fragment updates, no build step and no framework.
- **Then presets and the retirement** of the two old paths, once the flow covers them.

## 7. Decisions

Agreed as recommended (2026-11-22):

1. **Where:** the local app only, with `der-opt new` kept for scripts. (Recommended.
   Alternative: also a step-by-step terminal wizard.)
2. **Blocks or inline:** `extends` a library block where a pick matches it, settings
   written out where they don't. (Recommended. Alternative: write every setting inline,
   with no `extends`; longer files, but each one self-contained.)
3. **Reopening:** new files only; existing files are edited in their tabs. (Recommended.
   Alternative: record the flow's answers in the file's header so the flow can reopen
   what it wrote, which only works for files nobody has edited since.)
4. **Templates:** they become the flow's presets, and `der-opt new TEMPLATE` keeps
   working through the same writer. (Recommended. Alternative: retire the templates.)
5. **Studies in scope from the start:** portfolio and feeder studies from files, with
   `add_devices`. (Recommended. Alternative: single homes first, studies later.)

Agreed for the feeder steps (0.2, 2026-11-22):

6. **The network:** from `network.csv` when there is one, otherwise entered as rows in
   step 4, and either way written into the study. (Recommended. Alternative: a network
   file only, so the flow never writes elements.)
7. **Who takes part:** set as study-wide defaults in step 9, with a premises column
   used when the file has one; changing single premises afterwards is the Study settings
   tab's job. (Recommended. Alternative: a per-premise table in step 9 itself, which
   gets long for a real feeder.)
