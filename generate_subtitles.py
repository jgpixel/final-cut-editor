"""Add editable subtitle titles using cached speech timing and deterministic layout."""
import argparse
from bisect import bisect_left
from datetime import datetime
from fractions import Fraction
import json
import math
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unicodedata
import uuid
import xml.etree.ElementTree as ET

from fcpxml_tools import ROOT, load_timeline, media_path, time_value, xml_time, validate_xml, write_json, serialize_xml
from subtitle_timing import insert_connected, is_subtitle, set_subtitle_data
from analyze_recording import DEFAULT_MODEL, DEFAULT_LANGUAGE, ASR_PROFILE

SUBTITLE_UID = '.../Titles.localized/Subtitles.localized/Subtitle.localized/Subtitle.moti'
BASIC_UID = '.../Titles.localized/Bumper:Opener.localized/Basic Title.localized/Basic Title.moti'
DEFAULTS = {
    'template': 'subtitle', 'mode': 'phrase', 'animation': 'none', 'text_case': 'original',
    'font': 'Helvetica Neue', 'font_face': 'Bold', 'text_color': '#FFFFFF',
    'highlight_color': '#FFE14A', 'background_color': '#000000',
    'background_opacity': 0.6, 'outline_color': '#000000', 'outline_width': 1.5,
    'font_size': None, 'side_margin': None, 'bottom_margin': None,
    'max_words': None, 'max_lines': 2, 'max_duration': 3.5,
    'phrase_gap': 0.65, 'tail': 0.12,
}


def color(value):
    if not isinstance(value, str) or not re.fullmatch(r'#[0-9a-fA-F]{6}([0-9a-fA-F]{2})?', value):
        raise ValueError('Colors must be #RRGGBB or #RRGGBBAA.')
    value = value[1:]
    if len(value) == 6:
        value += 'FF'
    return ' '.join(f'{int(value[i:i + 2], 16) / 255:.6g}' for i in range(0, 8, 2))


def read_style(path=None):
    preset = ROOT / 'subtitle_styles' / 'default.json'
    defaults = json.loads(preset.read_text()) if preset.is_file() else {}
    overrides = {**defaults, **(json.loads(Path(path).read_text()) if path else {})}
    if not isinstance(overrides, dict) or set(overrides) - set(DEFAULTS):
        raise ValueError('Subtitle style must be an object with supported keys: ' + ', '.join(DEFAULTS))
    result = {**DEFAULTS, **overrides}
    if result['template'] not in ('subtitle', 'basic') or result['mode'] not in ('phrase', 'word'):
        raise ValueError('Supported templates: subtitle/basic; modes: phrase/word.')
    if result['animation'] not in ('none', 'fade', 'scale', 'highlight', 'fill'):
        raise ValueError('Unknown native subtitle animation.')
    if result['text_case'] not in ('original', 'uppercase'):
        raise ValueError('Supported text_case values: original/uppercase.')
    if result['template'] == 'basic' and (result['animation'] != 'none' or result['background_opacity']):
        raise ValueError('Basic Title requires animation: none and background_opacity: 0; use Subtitle for backgrounds.')
    for key in ('text_color', 'highlight_color', 'background_color', 'outline_color'):
        color(result[key])
    for key in ('background_opacity', 'side_margin', 'bottom_margin'):
        v = result[key]
        limit = 1 if key == 'background_opacity' else 0.4
        if v is not None and (isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or not 0 <= v <= limit):
            raise ValueError(f'{key} must be between 0 and {limit}.')
    for key in ('font_size', 'max_words', 'max_lines', 'max_duration', 'phrase_gap'):
        v = result[key]
        if v is not None and (isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v <= 0):
            raise ValueError(f'{key} must be positive.')
    for key in ('outline_width', 'tail'):
        v = result[key]
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v < 0:
            raise ValueError(f'{key} must be nonnegative.')
    if result['max_lines'] > 3 or int(result['max_lines']) != result['max_lines']:
        raise ValueError('max_lines must be an integer from 1 to 3.')
    if result['max_words'] is not None and int(result['max_words']) != result['max_words']:
        raise ValueError('max_words must be an integer.')
    if not all(isinstance(result[k], str) and result[k].strip() for k in ('font', 'font_face')):
        raise ValueError('Font and font_face must be nonempty strings.')
    return result


def layout_for(width, height, style):
    if width <= 0 or height <= 0:
        raise ValueError('Project must have positive width and height.')
    vertical = height > width
    side = style['side_margin'] if style['side_margin'] is not None else (0.12 if vertical else 0.08)
    bottom = style['bottom_margin'] if style['bottom_margin'] is not None else (0.22 if vertical else 0.10)
    font_pixels = style['font_size'] or min(width, height) * (0.055 if vertical else 0.045)
    # Reserve extra width for font variation, outlines, and background padding.
    usable = width * (1 - 2 * side) * 0.85
    font_pixels = min(font_pixels, usable / 2, height * 0.12)
    return {'orientation': 'vertical' if vertical else 'horizontal', 'width': width, 'height': height,
            'side_margin': side, 'bottom_margin': bottom, 'font_pixels': round(font_pixels, 4),
            'usable_width': usable, 'max_words': int(style['max_words'] or (5 if vertical else 8)),
            'max_lines': int(style['max_lines']), 'sizing': 'heuristic; human reviewed'}


def width_units(text):
    """Conservative estimate in em units; no font rendering or screenshot fitting."""
    result = 0.0
    for c in text:
        if unicodedata.combining(c):
            continue
        if c.isspace():
            result += 0.4
        elif unicodedata.east_asian_width(c) in ('W', 'F'):
            result += 1.1
        elif c in 'MW@%&#':
            result += 1.0
        elif c.isupper():
            result += 0.8
        else:
            result += 0.7
    return result


def wrap_words(words, layout):
    capacity = layout['usable_width'] / layout['font_pixels']
    lines, line = [], ''
    for w in words:
        token = w['word'].strip()
        trial = f'{line} {token}'.strip()
        if line and width_units(trial) > capacity:
            lines.append(line)
            line = token
        else:
            line = trial
    if line:
        lines.append(line)
    return lines


def group_words(words, layout, style):
    blocks, block = [], []
    for word in words:
        if block and (word.get('passage_id') != block[-1].get('passage_id') or
                      time_value(word['start']) - time_value(block[-1]['end']) > time_value(style['phrase_gap']) or
                      re.search(r'[.!?]["\u201d\u2019]*$', block[-1]['word'])):
            blocks.append(block)
            block = []
        block.append(word)
    if block:
        blocks.append(block)
    groups = []
    for block in blocks:
        groups.extend(phrase_groups(block, layout, style))
    return groups


def token(word):
    return word['word'].lower().replace('\u2019', "'").strip('.,!?;:\"\u201c\u201d()')


def boundary_cost(words, index):
    """Generic punctuation/clause hints, never rewriting the recognized words."""
    if index == len(words):
        return 0
    left, right = token(words[index - 1]), token(words[index])
    following = token(words[index + 1]) if index + 1 < len(words) else ''
    cost = 0.0
    if re.search(r'[,;:]["\u201d\u2019]*$', words[index - 1]['word']):
        cost -= 2.5
    gap = float(time_value(words[index]['start']) - time_value(words[index - 1]['end']))
    if gap >= 0.25:
        cost -= min(3.0, gap * 5)
    subjects = {"i'm", "i'll", "i've", "we're", "we'll", "we've", "you're", "you'll",
                "it's", "that's", "there's", "they're", "they'll", "let's", "he's", "she's"}
    if right in subjects or right in {'i', 'we', 'you', 'they', 'he', 'she'}:
        cost -= 2.5
    if right in {'and', 'but', 'so'} and (following in subjects or following in {'i', 'we', 'you', 'they'}):
        cost -= 2.5
    if right == 'make' and following == 'sure':
        cost -= 2.5
    # Avoid leaving a preposition, determiner, possessive, or auxiliary hanging.
    if left in {'a', 'an', 'the', 'to', 'of', 'with', 'for', 'in', 'on', 'and', 'or',
                'but', 'our', 'your', 'my', 'their', 'its', 'is', 'are', 'was', 'were',
                'will', 'would', 'can', 'could', 'should'} or left in subjects:
        cost += 6
    if left in {'make', 'take', 'get', 'find', 'see', 'have'} and right in {'a', 'an', 'the', 'our', 'your', 'my', 'it'}:
        cost += 4
    return cost


def phrase_groups(words, layout, style):
    """Choose readable boundaries in O(words * max_words), with size constraints."""
    count = len(words)
    costs = [float('inf')] * (count + 1)
    choices = [None] * count
    costs[count] = 0
    target = min(6, layout['max_words'])
    for start in range(count - 1, -1, -1):
        for end in range(start + 1, min(count, start + layout['max_words']) + 1):
            group = words[start:end]
            if end > start + 1 and (
                time_value(group[-1]['end']) - time_value(group[0]['start']) > time_value(style['max_duration']) or
                len(wrap_words(group, layout)) > layout['max_lines']):
                break
            length = end - start
            cost = 1 + 0.10 * (length - target) ** 2 + boundary_cost(words, end) + costs[end]
            if length == 1 and count > 1:
                cost += 2.5
            if cost < costs[start]:
                costs[start], choices[start] = cost, end
    result, start = [], 0
    while start < count:
        end = choices[start]
        result.append(words[start:end])
        start = end
    return result


def source_identity_matches(data, source):
    identity = data.get('identity', {})
    try:
        stat = source.stat()
        return (Path(data.get('source', '')).resolve() == source and
                Path(identity.get('source', '')).resolve() == source and
                (identity.get('size'), identity.get('mtime_ns'), identity.get('ctime_ns')) ==
                (stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns) and
                data.get('coordinate_space') == 'file_seconds')
    except OSError:
        return False


def analysis_for(source, explicit):
    if source is None:
        raise ValueError('Speech clip has no local original-media path; supply --words timeline timing.')
    source = Path(source).resolve()
    specified = []
    for path in explicit:
        data = json.loads(Path(path).read_text())
        if Path(data.get('source', '')).resolve() == source:
            if not source_identity_matches(data, source):
                raise ValueError(f'Stale or mismatched analysis: {path}')
            specified.append((data, str(path)))
    if len(specified) > 1:
        raise ValueError(f'Multiple explicit analyses for {source}; choose one.')
    if specified:
        return specified[0]
    candidates = sorted((ROOT / '.cache' / 'analysis').glob('*.json'), key=lambda p: p.stat().st_mtime_ns, reverse=True)
    for path in candidates:
        if not re.fullmatch(r'[0-9a-f]{64}', path.stem):
            continue
        data = json.loads(path.read_text())
        settings = data.get('identity', {}).get('settings', {})
        if (source_identity_matches(data, source) and settings.get('model') == DEFAULT_MODEL and
                settings.get('language') == DEFAULT_LANGUAGE and settings.get('word_timestamps') is True and
                settings.get('asr_profile') == ASR_PROFILE):
            return data, str(path)
    process = subprocess.run([sys.executable, str(ROOT / 'analyze_recording.py'), '--source', str(source)],
                             capture_output=True, text=True)
    if process.returncode:
        raise ValueError('Subtitle transcription failed: ' + process.stderr.strip())
    path = json.loads(process.stdout)['analysis']
    return json.loads(Path(path).read_text()), path


def normalize_words(rows):
    result = []
    previous = Fraction(0)
    for row in rows:
        text = row.get('word', '').strip()
        start, end = time_value(row['start']), time_value(row['end'])
        if start < 0 or end < start or start < previous:
            raise ValueError('Word timestamps must be nonnegative, ordered, and have end >= start.')
        previous = start
        if not text or start == end:
            continue
        item = {'word': text, 'start': xml_time(start), 'end': xml_time(end)}
        for key in ('passage_id', 'passage_start', 'passage_end'):
            if key in row:
                item[key] = row[key]
        result.append(item)
    return result


def analysis_words(data):
    rows = [{**{key: segment[key] for key in ('passage_id', 'passage_start', 'passage_end') if key in segment}, **w}
            for segment in data['segments'] for w in segment.get('words', [])]
    speech = data.get('speech_passages', [])
    starts = [time_value(p['start']) for p in speech]
    # Recognition can share nearby context, but captions still respect detected speech islands.
    for word in rows:
        if not speech:
            break
        midpoint = (time_value(word['start']) + time_value(word['end'])) / 2
        previous = max(0, bisect_left(starts, midpoint) - 1)
        candidates = range(previous, min(len(speech), previous + 2))
        index = min(candidates, key=lambda i: max(starts[i] - midpoint, midpoint - time_value(speech[i]['end']), 0))
        p = speech[index]
        word.update(passage_id=index, passage_start=p['start'], passage_end=p['end'])
    return rows


def words_for_timeline(timeline, explicit, supplied):
    analyses, used = {}, []
    if supplied is not None:
        if supplied.get('coordinate_space') != 'timeline':
            raise ValueError('--words must specify coordinate_space: timeline.')
        all_words = normalize_words(supplied['words'])
        if any(time_value(w['end']) > timeline.duration for w in all_words):
            raise ValueError('Timeline word timing exceeds the project duration.')
        all_words, all_midpoints = index_words(all_words)
    result = []
    for a, b, clip in timeline.spans:
        words = []
        if clip.tag == 'asset-clip' and clip.get('enabled', '1') == '1' and clip.get('srcEnable', 'all') != 'video':
            asset = timeline.resources[clip.get('ref')]
            if supplied is not None:
                rows, midpoints = all_words, all_midpoints
                shift = Fraction(0)
            elif asset.get('hasAudio') == '1':
                source = media_path(asset)
                if source not in analyses:
                    data, path = analysis_for(source, explicit)
                    analyses[source] = index_words(normalize_words(analysis_words(data)))
                    used.append(path)
                rows, midpoints = analyses[source]
                shift = a + time_value(asset.get('start', '0s')) - time_value(clip.get('start', '0s'))
            else:
                rows, midpoints, shift = [], [], Fraction(0)
            # Index speech once per source; a long recording may appear in hundreds of cuts.
            selected = rows[bisect_left(midpoints, a - shift):bisect_left(midpoints, b - shift)]
            for word in sorted(selected, key=lambda w: time_value(w['start'])):
                start, end = time_value(word['start']) + shift, time_value(word['end']) + shift
                if a <= (start + end) / 2 < b:
                    start = max(a, round(start / timeline.frame_duration) * timeline.frame_duration)
                    end = min(b, round(end / timeline.frame_duration) * timeline.frame_duration)
                    if end <= start:
                        end = min(b, start + timeline.frame_duration)
                    if end > start:
                        mapped = {**word, 'start': xml_time(start), 'end': xml_time(end)}
                        if 'passage_end' in word:
                            mapped['passage_end'] = xml_time(time_value(word['passage_end']) + shift)
                            mapped['passage_start'] = xml_time(time_value(word['passage_start']) + shift)
                        words.append(mapped)
        result.append(words)
    return result, used


def index_words(words):
    indexed = sorted(((time_value(w['start']) + time_value(w['end'])) / 2, i, w)
                     for i, w in enumerate(words))
    return [w for _, _, w in indexed], [midpoint for midpoint, _, _ in indexed]


def add_param(title, name, key, value):
    ET.SubElement(title, 'param', {'name': name, 'key': key, 'value': str(value)})


def make_title(words, start, end, clip, clip_start, effect_id, style, layout, unique):
    lines = wrap_words(words, layout)
    text = '\n'.join(lines)
    # A single unusually long token is reduced once using the same estimate.
    font_pixels = min(layout['font_pixels'], layout['usable_width'] / max(width_units(l) for l in lines))
    canvas_height = 2160 if style['template'] == 'subtitle' else 1080
    font_size = font_pixels * canvas_height / layout['height']
    if style['template'] == 'subtitle':
        font_size /= 1.2  # Built-in Subtitle text style's fixed scale, inspected locally.
    title = ET.Element('title', {'ref': effect_id, 'name': 'Subtitle: ' + text.replace('\n', ' '),
        'lane': '1', 'offset': xml_time(time_value(clip.get('start', '0s')) + start - clip_start),
        'start': '0s', 'duration': xml_time(end - start), 'role': 'Subtitles'})
    if style['template'] == 'subtitle':
        rig = '9999/3336678691/'
        add_param(title, 'Animation Style', rig + '3336692171/2/100',
                  {'none': 0, 'fade': 1, 'scale': 3, 'highlight': 4, 'fill': 5}[style['animation']])
        add_param(title, 'Animate By', rig + '3336679301/2/100', 1)
        add_param(title, 'Vertical Social Media Safe', rig + '3337013104/2/100', 0)
        add_param(title, 'Y Position Offset', rig + '3337241559/2/100',
                  round(-canvas_height * (0.5 - layout['bottom_margin']) + 840, 4))
        shape = '9999/3336674837/3336685305/3336678548/'
        add_param(title, 'Background Color', shape + '2/353/113/111', color(style['background_color']))
        add_param(title, 'Background Opacity', shape + '1/200/202', style['background_opacity'])
        add_param(title, 'Background Corner Radius', shape + '2/353/144', round(font_size * 0.25, 4))
        add_param(title, 'Highlight and Fill Color', '9999/3336674837/3337240802/2/353/113/111', color(style['highlight_color']))
        # Keep the manually wrapped text's font size; default template auto-shrink is disabled.
        add_param(title, 'Auto-Shrink', '9999/3336674837/3336674846/2/370', 0)
        half_width = layout['usable_width'] * canvas_height / layout['height'] / 2
        add_param(title, 'Left Margin', '9999/3336674837/3336674846/2/323', -half_width)
        add_param(title, 'Right Margin', '9999/3336674837/3336674846/2/324', half_width)
    text_node = ET.SubElement(title, 'text')
    style_id = f'fce_subtitle_{unique}'
    ET.SubElement(text_node, 'text-style', {'ref': style_id}).text = text
    definition = ET.SubElement(title, 'text-style-def', {'id': style_id})
    ET.SubElement(definition, 'text-style', {
        'font': style['font'], 'fontFace': style['font_face'], 'fontSize': f'{font_size:.4f}',
        'fontColor': color(style['text_color']), 'alignment': 'center', 'lineSpacing': '0',
        'strokeColor': color(style['outline_color']), 'strokeWidth': str(style['outline_width'])})
    timed_words, cursor = [], 0
    for word in words:
        i = text.index(word['word'], cursor)
        timed_words.append({'word': word['word'], 'start': xml_time(time_value(word['start']) - start),
                            'end': xml_time(time_value(word['end']) - start),
                            'char_start': i, 'char_end': i + len(word['word'])})
        cursor = i + len(word['word'])
    set_subtitle_data(title, {'text': text, 'words': timed_words})
    if style['template'] == 'basic':
        # FCPXML transform positions are percentages of project height.
        block_height = font_pixels * (1.25 * len(lines))
        y = -50 + 100 * layout['bottom_margin'] + 50 * block_height / layout['height']
        ET.SubElement(title, 'adjust-transform', {'position': f'0 {y:.5f}'})
    return title


def add_subtitles(xml, style, explicit=(), supplied=None, replace=False):
    with tempfile.TemporaryDirectory(prefix='fcp-subtitles-') as directory:
        path = Path(directory) / 'input.fcpxml'
        path.write_text(xml)
        timeline = load_timeline(path)
    timeline.check_supported()
    existing = [(clip, title) for _, _, clip in timeline.spans for title in clip.findall('title')
                if is_subtitle(title, timeline.resources)]
    if existing and not replace:
        raise ValueError('Project already has subtitles; omit generation to preserve them or explicitly use replacement.')
    for clip, title in existing:
        clip.remove(title)
    format_node = timeline.resources[timeline.sequence.get('format')]
    width, height = int(format_node.get('width', '0')), int(format_node.get('height', '0'))
    layout = layout_for(width, height, style)
    words_by_clip, used = words_for_timeline(timeline, explicit, supplied)
    # Transform display words before layout and character-offset metadata; keep cache/timing intact.
    if style['text_case'] == 'uppercase':
        words_by_clip = [[{**word, 'word': word['word'].upper()} for word in words]
                         for words in words_by_clip]
    resources = timeline.root.find('resources')
    unique = uuid.uuid4().hex[:12]
    effect_id = 'fce_subtitle_effect_' + unique
    effect = ET.SubElement(resources, 'effect', {'id': effect_id, 'name': 'Subtitle' if style['template'] == 'subtitle' else 'Basic Title',
                                      'uid': SUBTITLE_UID if style['template'] == 'subtitle' else BASIC_UID})
    timeline.resources[effect_id] = effect
    count, word_count = 0, 0
    for (clip_start, clip_end, clip), words in zip(timeline.spans, words_by_clip):
        groups = [[w] for w in words] if style['mode'] == 'word' else group_words(words, layout, style)
        for i, group in enumerate(groups):
            start = time_value(group[0]['start'])
            next_start = time_value(groups[i + 1][0]['start']) if i + 1 < len(groups) else clip_end
            end = min(clip_end, next_start, time_value(group[-1]['end']) +
                      round(time_value(style['tail']) / timeline.frame_duration) * timeline.frame_duration)
            if 'passage_end' in group[-1]:
                passage_end = time_value(group[-1]['passage_end'])
                end = min(end, (passage_end // timeline.frame_duration) * timeline.frame_duration)
            if end <= start:
                continue
            # Do not allow overlapping recognizer word intervals to escape their title.
            group = [{**w, 'end': xml_time(min(time_value(w['end']), end))} for w in group]
            title = make_title(group, start, end, clip, clip_start, effect_id, style, layout, f'{unique}_{count}')
            insert_connected(clip, title)
            count += 1
            word_count += len(group)
    warnings = []
    if not count:
        warnings.append('No speech words found in retained audio; no subtitle titles added.')
    if style['animation'] != 'none':
        warnings.append('Native template animation runs over each phrase; exact per-word reveal is reserved for the custom text stage.')
    timeline.check_supported()
    xml = serialize_xml(timeline.root)
    return xml, {'titles': count, 'words': word_count, 'style': style, 'layout': layout,
                 'analysis': used, 'warnings': warnings}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', required=True, type=Path)
    parser.add_argument('--project')
    parser.add_argument('--output', type=Path)
    parser.add_argument('--name')
    parser.add_argument('--event', default='Agent Edits')
    parser.add_argument('--style', type=Path, help='JSON style overrides')
    parser.add_argument('--analysis', action='append', type=Path, default=[])
    parser.add_argument('--words', type=Path, help='Optional timeline-relative word JSON; bypass transcription')
    parser.add_argument('--replace', action='store_true', help='Explicitly replace existing subtitle titles')
    parser.add_argument('--dtd', type=Path)
    args = parser.parse_args()
    try:
        from build_clean_edit import apply_ranges
        timeline = load_timeline(args.input, args.project)
        token = datetime.now().strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:6]
        output = (args.output or ROOT / 'outputs' / f'subtitles-{token}.fcpxml').expanduser().resolve()
        report_path = output.with_suffix('.decisions.json')
        if output.suffix != '.fcpxml' or output.exists() or report_path.exists():
            raise ValueError('Choose a new .fcpxml output path; existing outputs are never overwritten.')
        name = args.name or f'{timeline.project.get("name", "Project")} - Subtitles {token}'
        xml, manifest = apply_ranges(timeline, {'coordinate_space': 'timeline',
            'ranges': [{'start': 0, 'end': xml_time(timeline.duration)}]}, name, args.event)
        supplied = json.loads(args.words.read_text()) if args.words else None
        xml, report = add_subtitles(xml, read_style(args.style), args.analysis, supplied, args.replace)
        manifest['subtitles'] = report
        manifest['subtitle_titles'] = report['titles']
        manifest['warnings'] += report['warnings']
        manifest['validation'] = validate_xml(xml, timeline.root.get('version', ''), args.dtd)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(xml)
        write_json(report_path, manifest)
        print(json.dumps({'output': str(output), 'manifest': str(report_path), 'project': name,
            'titles': report['titles'], 'layout': report['layout'], 'validation': manifest['validation'],
            'warnings': manifest['warnings']}, indent=2))
    except (ValueError, OSError, ET.ParseError, KeyError, TypeError, ZeroDivisionError) as exc:
        parser.exit(2, f'Error: {exc}\n')


if __name__ == '__main__':
    main()
