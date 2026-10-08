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

The range editor supports contiguous primary storylines made of simple asset
clips and explicit gaps, with multiple recordings and nonzero source timecodes.
It preserves clip attributes, notes, metadata, and resource definitions.
It rejects effects, markers, transitions, connected clips, compound/multicam clips,
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

The default model is `large-v3-turbo` and the default language is English. The old
`small.en` model/cache is retained for explicitly requested fast experiments;
do not silently reuse it for current speech analysis. For other
languages, select a model with `--model` and a language code or
`--language auto`. Missing models require explicit `--allow-model-download`;
this downloads model weights, not footage. `--refresh` recomputes matching analysis.
An optional `--output` writes a copy to a new path.

Transcription uses the bundled Silero speech detector, then supplies bounded audio
passages directly to `large-v3-turbo` instead of concatenating distant speech.
Default detection separates pauses of at least 650ms, uses 400ms boundary padding,
and caps long speech passages at 14s. The model is loaded once and passages are
processed in batches of four (`--batch-size` can change this). Nearby passages
share a recognition window only when their padded gap is at most 1.25s and the
combined window is at most 22s. Keep the original audio and silence within those
windows; never splice distant speech together. The cache stores original speech
passages and recognition windows. This is a single cached recognition pass, with
no additional alignment model, language model rewrite, cloud service, or per-video
manual correction loop. Reuse this profile rather than changing it for each job.

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

## Validate and deliver

The editor checks supported structure, frame alignment, source bounds, positive
range durations, coverage, and total duration. It validates against the matching
installed Apple FCPXML DTD when available and reports validation status. An
explicit `--dtd` can select a matching schema. Do not claim schema validation when
only timing checks ran.

Also check retained source starts, not just output offsets and durations. For
matching source/project frame rates, generated source starts must be whole source
frames relative to the asset start. An exported input can contain a fractional
source start; copying that phase into every new cut can cause Final Cut to insert
repair gaps on import. `validate_xml` now rejects this case even when the DTD
passes. A targeted edit path must snap those source starts to the correct source
grid, preserve the chosen output durations, and record the small timing adjustment.
Do not apply this matching-rate rule blindly to mixed-rate or retimed media.

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
