# Using the Final Cut editing workflow

## Purpose

Use the local FCPXML workflow in this folder to carry out the user's requested
changes and return an editable Final Cut Pro project revision. Follow the current
request. Existing edits are examples, not instructions for the next video.

Do not assume a task requires silence removal, repeated-take removal, reordering,
transcription, or a rendered video. Perform requested operations and preserve
unrelated content. Clarify ambiguity that would materially change the result;
otherwise use reasonable editorial judgment.

## Default delivery: rough cut

The user wants the base edit and will finish it in Final Cut. Make one editorial
pass, apply the decisions once, run the inexpensive structural/timing checks,
and deliver the XML. The user imports it themselves by default. Do not open Final
Cut, automate import, render a preview, or perform a second editorial review pass
unless requested. Do not refine the result merely to polish a rough cut.

Use additional analysis only for a concrete issue that prevents the requested
base edit, such as analysis missing an essential passage or unsupported timing.
Keep that work targeted. Fix technical errors that prevent a valid export; the
rough-cut preference does not mean delivering broken XML. Briefly report material
uncertainty for the user to check rather than spending time on optional polish.

## Tools and supported projects

Run commands from this folder with `.venv/bin/python`. The tools accept inputs
through command-line arguments; do not edit their source code to configure a task.
Use `--help` for options. Reuse installed software and downloaded models.

- `build_clean_edit.py`: inspects a project or applies ordered timeline ranges to
  create a new FCPXML revision and a matching `.decisions.json` manifest. It reads
  frame rate, source references, and timing from the export. It never chooses
  editorial cuts automatically.
- `analyze_recording.py`: transcribes a specified media file locally, with word
  timestamps and optional acoustic silence detection. Analysis is cached by
  source identity and settings under `.cache/analysis/`. Intermediate audio is
  temporary; matching subsequent requests reuse the cached result.
- `render_preview.py`: optionally renders an editor-generated manifest as a review
  MP4. It uses project frame rate and aspect ratio, supports multiple sources,
  and detects HLG/PQ for SDR review conversion. It does not change the XML.
- `fcpxml_tools.py`: shared XML inspection, rational timing, and DTD validation.
- `generate_subtitles.py`: adds editable connected title subtitles, reuses source
  analysis or transcribes missing sources locally, and applies a saved style.
- `subtitle_timing.py`: preserves and remaps recognized subtitle titles through
  cuts and reordering, using embedded word timing when available.

The range editor supports contiguous primary storylines made of simple asset
clips and explicit gaps, with multiple recordings and nonzero source timecodes.
It preserves clip attributes, notes, metadata, resource definitions, and recognized
connected subtitle titles. It rejects effects, markers, transitions, other connected clips, compound/multicam clips,
retiming, split audio/video edits, and overlapping timelines before writing an
edit. Use inspection to identify these limits. Do not flatten or discard those
structures to force a project through this tool; explain the limitation and use
an appropriate editing method that preserves the requested project structure.

## Inspect the current project

Identify the user's `.fcpxml` or `.fcpxmld` export. If the request concerns a project
open in Final Cut, obtain its export first. A bundle contains `Info.fcpxml`.

```sh
.venv/bin/python build_clean_edit.py --input "/path/to/project.fcpxmld" --inspect
```

Inspection reports project duration, exact frame duration, source references,
media paths, timeline boundaries, source starts, file starts, and support status.
Use `--project "Project Name"` when an export contains multiple projects.

## Analyze only what the request needs

For spoken-content decisions, analyze each distinct recording once. For purely
specified time cuts, transcription may be unnecessary. For visual selections,
inspect relevant shots; a transcript alone cannot establish visual quality.

```sh
.venv/bin/python analyze_recording.py --source "/path/to/recording.mov"
```

The command reports the analysis JSON path and whether it reused a cache. Add
`--detect-silence` only when acoustic gaps matter to the request. `--silence-db`
and `--silence-min` configure detection; they do not remove anything. The cached
JSON contains timestamped words, optional silence intervals, and source identity.
Do not use legacy top-level analysis without verifying its source and settings.

The downloaded model is `small.en` and the default language is English. For other
languages, select a multilingual model with `--model` and a language code or
`--language auto`. Missing models require explicit `--allow-model-download`;
this downloads model weights, not footage. `--refresh` recomputes matching analysis.
An optional `--output` writes a copy to a new path.

Transcription timestamps are **file-relative seconds**, while edit ranges are
**project timeline seconds**. For a word or gap within a particular clip:

`timeline time = clip timeline start + file time + asset start - clip source start`

Use inspection's mapping and clip bounds. A recording may appear more than once
or only as selected excerpts; never assume source time equals timeline time.

## Specify and apply the requested edit

Write a task-specific JSON decision file under `.cache/decisions/<job>/`.
It must specify `coordinate_space: "timeline"` and a `ranges` list. Each range
needs `start` and `end`; `reason` is optional. Values may be decimal seconds or
exact FCPXML rational strings. Ranges are retained and concatenated **in list
order**, so reordering the list reorders the selected passages. Range ends are
exclusive. The following is a schema example, not an editing preset:

```json
{
  "coordinate_space": "timeline",
  "ranges": [
    {"start": 10, "end": 15, "reason": "Passage requested first"},
    {"start": 0, "end": 5, "reason": "Passage requested second"}
  ]
}
```

```sh
.venv/bin/python build_clean_edit.py \
  --input "/path/to/project.fcpxmld" \
  --decisions "/path/to/decisions.json"
```

The default output is a uniquely named revision in `outputs/`, with a matching
manifest recording actual applied timing and media mapping. Optional `--output`,
`--name`, and `--event` set the destination and names. Existing output files are
never overwritten. Cuts snap to the nearest project frame; check the resulting
manifest when exact boundaries matter. The editor splits ranges crossing clips
and recomputes output offsets while retaining the appropriate source timings.
For agent-run edits, use `--output outputs/<job>/rough-cut-<revision>.fcpxml` to
keep each job's results together; choose a new revision filename for each edit.

## Guidance for particular requests

- **Remove silence or shorten pauses:** detect candidate gaps, check speech
  boundaries and visual context, and select ranges excluding only the requested
  gaps. Choose retained breathing room appropriate to the pacing brief.
- **Remove repetitions or alternate takes:** compare passages in context, retain
  takes satisfying the brief, and check for unique information in removed takes.
  Preserve intentional repetition unless asked otherwise.
- **Reorder sections:** arrange the selected ranges in the requested order. Check
  continuity and references depending on earlier context. Do not also tighten
  pauses unless requested.
- **Highlights, sorting, or separate clips:** select by the user's criteria and
  inspect relevant visuals. Generate separate revisions when requested.
- **Revise an edit:** reuse unchanged source analysis and adjust the decision file.
  Reapply to the appropriate input export without retranscribing unchanged media.

## Generate and preserve subtitles

Subtitles here are editable **title clips**, not closed-caption tracks. The local
transcription tool supplies their words/timing; Final Cut renders the title template.
Generate subtitles only when requested. For cuts plus subtitles in one delivery,
append `--subtitles` to the existing editing command:

```sh
.venv/bin/python build_clean_edit.py \
  --input "/path/to/project.fcpxmld" \
  --decisions "/path/to/decisions.json" \
  --subtitles --output "outputs/<job>/rough-cut-subtitles-01.fcpxml"
```

To add subtitles to an already edited project without changing its video:

```sh
.venv/bin/python generate_subtitles.py \
  --input "/path/to/edited.fcpxml" \
  --output "outputs/<job>/subtitles-01.fcpxml"
```

Both commands reuse analysis automatically if source path, size, mtime and ctime
match; missing analysis runs the existing local transcriber once per source. No
model downloads are silently authorized. Explicit caches can be selected with
repeatable `--subtitle-analysis` (range editor) or `--analysis` (standalone).
Cache timestamps are file-relative; the generator maps each retained source
excerpt, including reordered/repeated excerpts and nonzero source timecodes.
Disabled/video-only clips and gaps get no generated subtitles.

Use `subtitle_styles/default.json` as the reusable default. Request-specific
overrides go in a new JSON file, passed with `--subtitle-style` or standalone
`--style`; they merge over the default preset. Do not change the global preset
merely to configure one job. Included alternatives are `subtitle_styles/word.json`
(one word replaces the previous word) and `subtitle_styles/basic.json` (plain
Basic Title without a background). Default template is Apple's built-in Subtitle,
available in Final Cut 12.3; Basic Title is the alternative for older versions.

**One-pass layout rules, applied automatically:**

| Setting | Horizontal / square | Vertical |
| --- | --- | --- |
| Left/right margin | 8% each | 12% each |
| Bottom clearance | 10% of height | 22% of height |
| Font size | 4.5% of shorter dimension | 5.5% of shorter dimension |
| Maximum words per phrase | 8 | 5 |
| Maximum lines | 2 | 2 |

Sizes are converted to the title template's reference canvas, so 4K and 1080p
keep comparable proportions. Reserve another 15% of usable width for font
variation, outlines, and background padding. Estimate character widths, wrap
once, and split phrases that would exceed the line limit. A single unusually
long word is reduced once to fit the estimate. Stop phrases at sentence endings,
gaps over 0.65s, clip boundaries, or 3.5s; allow a 0.12s trailing hold without
overlapping the next title or extending past the clip. These are practical
heuristics, not a guarantee about fonts or every platform's controls.

Style fields: `template` (`subtitle`/`basic`), `mode` (`phrase`/`word`), `animation`,
`font`, `font_face`, `text_color`, `highlight_color`, `background_color`,
`background_opacity` (0–1), `outline_color`, `outline_width`, `font_size` (target
project pixels), `side_margin`, `bottom_margin` (fractions), `max_words`,
`max_lines` (1–3), `max_duration`, `phrase_gap`, and `tail`. Colors are
`#RRGGBB` or `#RRGGBBAA`. Default is bold white phrase text with a translucent
black background, black outline, and no animation. Native Subtitle animations
`fade`, `scale`, `highlight`, and `fill` are selectable; their sequence follows
template duration, not exact word timestamps. Precise cumulative word reveals
and large mixed-font arrangements belong to the next custom-text stage.

For corrected words without retranscription, optionally provide JSON with
`coordinate_space: "timeline"` and `words: [{"word": "Hello", "start": 1,
"end": 1.4}]`, using standalone `--words` or range-editor `--subtitle-words`.
For the range editor these times refer to the **edited output timeline**.

When editing an existing subtitled project, omit `--subtitles`: the range editor
preserves and remaps identifiable subtitle titles automatically. It recognizes
embedded workflow timing, a subtitle role, or Apple's Subtitle template. Other
connected graphics, nested title structures, filters, and keyframed titles remain
unsupported. A title can cross primary-clip boundaries; its intersection with
each retained passage is connected to the appropriate output clip.

Generated titles store word timing and text character ranges in their clip notes.
Keep those notes when exporting from Final Cut. At cuts, word midpoints determine
which words survive; remaining text keeps its style runs. If notes are absent
(including ordinary Final Cut-generated subtitles) or text was changed by a human,
preserve text/style and trim timing, report partial titles for human review, and
never overwrite human wording with stale timing. Do not claim automatic word-level
text trimming for such titles. Generating over existing subtitles requires explicit
`--subtitle-replace` or standalone `--replace`; otherwise it stops to avoid duplicates.

**Subtitle delivery is deliberately one pass.** Use the preset and heuristics,
run cheap timing/XML validation, and deliver. Do not take screenshots, render
previews, inspect every phrase visually, add a refinement loop, or perform a
second transcription/review pass to perfect wording, spacing, or synchronization.
The user reviews and adjusts the result in Final Cut. Only fix technical failures
that prevent valid output. Report material limitations briefly. DTD checks do not
confirm template rendering or a successful Final Cut import. The preview script
does not render title graphics and refuses manifests containing subtitles.

## Validate and deliver

The editor checks supported structure, frame alignment, source bounds, positive
range durations, coverage, and total duration. It validates against the matching
installed Apple FCPXML DTD when available and reports validation status. An
explicit `--dtd` can select a matching schema. Do not claim schema validation when
only timing checks ran.

Final Cut is installed at `/Applications/Final Cut Pro Creator Studio.app`, bundle
ID `com.apple.FinalCutApp`. DTDs are under that app's
`Contents/Frameworks/Interchange.framework/Versions/A/Resources/` directory.

Deliver a link to the generated XML with its duration and project name. The user
can choose File > Import > XML in Final Cut, select the file, and choose a library
if prompted. Only automate import when the user requests it; then verify the new
project, duration, and linked media. XML validation alone does not prove import
succeeded. Preserve source media, original exports, and original projects; never
modify Final Cut library databases directly.

Render only if requested or needed for a concrete verification:

```sh
.venv/bin/python render_preview.py \
  --manifest "/path/to/revision.decisions.json" \
  --output "/path/to/new-preview.mp4"
```

This is a review render of the supported simple timeline. Final Cut remains the
place for final-quality exports. `--max-side` sets preview resolution and
`--tone-map none` disables automatic HDR conversion.

## Project organization and cleanup

Reuse this workspace across jobs so the environment and model cache remain
available. The suggested layout is `inputs/<job>/` for user XML exports,
`outputs/<job>/` for editable revisions and manifests, and
`.cache/decisions/<job>/` for editorial decisions. Analysis stays in the shared
source-keyed `.cache/analysis/`. A job can use any source paths in its XML; footage
does not need to be copied into this folder. Existing exports in the repository
root are valid inputs; do not move them just to enforce this layout.

When a new export arrives, identify its job and use separate decision/output
paths. Do not automatically delete earlier jobs or clear analysis. A new export
may be another revision using the same recordings.

Cleanup is an explicit operation after a job is finished, not a trigger on new
files. When requested, archive the named job's exports, outputs, and decisions
together under `archives/<job>-<date>/`, or remove those specific files if the
user explicitly requests deletion. Preserve shared analysis by default; prune it
only when asked. Never include original footage, Final Cut libraries, `.venv`,
or `.cache/models` in routine job cleanup. Do not execute cleanup merely because
this section exists. A cleanup script is not required for this layout.

If version control is used, track tools and instructions and keep job files,
footage, caches, and the virtual environment out of Git. Do not publish a repository
as an incidental editing step.

Skip unnecessary previews, contact sheets, and retranscription of renders. Keep
context across sections of long recordings. Process locally; do not upload footage
as an incidental tool choice. CommandPost is outside this workflow. Report output
location, project name, meaningful changes, duration, and unresolved limitations
concisely. Only claim playback, analysis, validation, or import actually performed.
