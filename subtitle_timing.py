"""Recognize and trim connected subtitle titles without flattening the timeline."""
from copy import deepcopy
from fractions import Fraction
import json

from fcpxml_tools import time_value, xml_time

NOTE_PREFIX = 'final-cut-editor.subtitle.v1:'


def subtitle_data(title):
    note = title.find('note')
    if note is None or not (note.text or '').startswith(NOTE_PREFIX):
        return None
    try:
        data = json.loads(note.text[len(NOTE_PREFIX):])
        if not isinstance(data.get('words'), list):
            raise ValueError('Missing word timing.')
        return data
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError('Invalid embedded subtitle timing; no edit written.') from exc


def set_subtitle_data(title, data):
    note = title.find('note')
    if note is None:
        # note comes after params/text/style definitions, before video adjustments.
        index = next((i for i, c in enumerate(title)
                      if c.tag not in ('param', 'text', 'text-style-def')), len(title))
        import xml.etree.ElementTree as ET
        note = ET.Element('note')
        title.insert(index, note)
    note.text = NOTE_PREFIX + json.dumps(data, separators=(',', ':'))


def is_subtitle(title, resources):
    if title.tag != 'title':
        return False
    effect = resources.get(title.get('ref'))
    return (subtitle_data(title) is not None or
            'subtitle' in title.get('role', '').lower() or
            (effect is not None and effect.get('uid', '').endswith('/Subtitle.moti')))


def check_subtitle(title, resources, frame_duration):
    if not is_subtitle(title, resources):
        raise ValueError('Connected titles must be identifiable subtitles; no edit written.')
    effect = resources.get(title.get('ref'))
    if effect is None or effect.tag != 'effect':
        raise ValueError('Subtitle has no title template resource.')
    if int(title.get('lane', '0')) <= 0:
        raise ValueError('Subtitle titles require a positive connected lane.')
    duration = time_value(title.get('duration', '0s'))
    if duration <= 0 or duration / frame_duration % 1:
        raise ValueError('Subtitle duration must be positive and frame aligned.')
    for child in title:
        if child.tag not in ('param', 'text', 'text-style-def', 'note', 'metadata',
                             'adjust-transform', 'adjust-blend', 'adjust-crop'):
            raise ValueError('Subtitle contains unsupported animation/effects/nested timing.')
        if child.find('.//keyframeAnimation') is not None:
            raise ValueError('Keyframed subtitles need an animation-aware edit path.')


def connected_subtitles(timeline):
    result = []
    for clip_start, _, clip in timeline.spans:
        source = time_value(clip.get('start', '0s'))
        for title in clip.findall('title'):
            start = clip_start + time_value(title.get('offset', '0s')) - source
            result.append((start, start + time_value(title.get('duration')), title))
    return result


def title_text(title):
    return ''.join(''.join(t.itertext()) for t in title.findall('text'))


def slice_title_text(title, a, b):
    """Keep character range, retaining every remaining run's font/color references."""
    cursor = 0
    for text in title.findall('text'):
        value = text.text or ''
        text.text = value[max(0, a - cursor):max(0, min(len(value), b - cursor))]
        cursor += len(value)
        for run in list(text):
            value = run.text or ''
            run.text = value[max(0, a - cursor):max(0, min(len(value), b - cursor))]
            cursor += len(value)
            value = run.tail or ''
            run.tail = value[max(0, a - cursor):max(0, min(len(value), b - cursor))]
            cursor += len(value)
            if not run.text and not run.tail:
                text.remove(run)


def trim_title(original, original_start, a, b, warnings):
    title = deepcopy(original)
    title.set('start', xml_time(time_value(original.get('start', '0s')) + a - original_start))
    title.set('duration', xml_time(b - a))
    title.attrib.pop('id', None)
    data = subtitle_data(title)
    if data is None or data.get('text') != title_text(title):
        if a > original_start or b < original_start + time_value(original.get('duration')):
            warnings.add('Some existing subtitle text lacks matching word timing; text/style preserved at cuts for human review.')
        # A human changed this title; stale timing must never overwrite their text.
        if data is not None:
            title.remove(title.find('note'))
        return title
    relative_a, relative_b = a - original_start, b - original_start
    words = []
    for word in data['words']:
        start, end = time_value(word['start']), time_value(word['end'])
        # Word midpoint determines ownership when a cut passes through a spoken word.
        if relative_a <= (start + end) / 2 < relative_b:
            w = dict(word)
            w['start'] = xml_time(max(start, relative_a) - relative_a)
            w['end'] = xml_time(min(end, relative_b) - relative_a)
            words.append(w)
    if not words:
        return None
    first, last = words[0]['char_start'], words[-1]['char_end']
    slice_title_text(title, first, last)
    for word in words:
        word['char_start'] -= first
        word['char_end'] -= first
    set_subtitle_data(title, {**data, 'text': title_text(title), 'words': words})
    return title


def insert_connected(clip, title):
    """Place connected children before metadata, as required by Apple's DTD."""
    index = next((i for i, c in enumerate(clip) if c.tag == 'metadata'), len(clip))
    clip.insert(index, title)


def remap_subtitles(timeline, spine, ranges, warnings):
    originals = connected_subtitles(timeline)
    output_cursor = Fraction(0)
    clips = [(time_value(c.get('offset', '0s')),
              time_value(c.get('offset', '0s')) + time_value(c.get('duration')), c) for c in spine]
    for row in ranges:
        start, end = time_value(row['start']), time_value(row['end'])
        for title_start, title_end, original in originals:
            a, b = max(start, title_start), min(end, title_end)
            if a >= b:
                continue
            new_start = output_cursor + a - start
            title = trim_title(original, title_start, a, b, warnings)
            if title is None:
                continue
            clip_start, _, clip = next(c for c in clips if c[0] <= new_start < c[1])
            title.set('offset', xml_time(time_value(clip.get('start', '0s')) + new_start - clip_start))
            # Duplicating a passage also duplicates title text-style definition IDs.
            ids = {d.get('id'): f'subtitle_{len(list(spine.iter("title")))}_{i}'
                   for i, d in enumerate(title.findall('text-style-def'))}
            existing = {e.get('id') for e in spine.iter() if e.get('id')}
            for old, new in list(ids.items()):
                while new in existing:
                    new += '_'
                ids[old] = new
            for d in title.findall('text-style-def'):
                d.set('id', ids[d.get('id')])
            for run in title.findall('.//text-style'):
                if run.get('ref') in ids:
                    run.set('ref', ids[run.get('ref')])
            insert_connected(clip, title)
        output_cursor += end - start
