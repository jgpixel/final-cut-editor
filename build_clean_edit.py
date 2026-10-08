"""Apply ordered project-timeline ranges to an exported FCPXML project."""
import argparse
from copy import deepcopy
from datetime import datetime
from fractions import Fraction
import json
from pathlib import Path
import uuid
import xml.etree.ElementTree as ET

from fcpxml_tools import ROOT, load_timeline, media_path, time_value, xml_time, validate_xml, write_json


def apply_ranges(timeline, decisions, project_name, event_name):
    timeline.check_supported()
    if decisions.get('coordinate_space') != 'timeline':
        raise ValueError('Decisions must specify coordinate_space: "timeline".')
    ranges = decisions.get('ranges')
    if not isinstance(ranges, list) or not ranges:
        raise ValueError('Provide a nonempty ranges list in desired output order.')
    root = ET.Element('fcpxml', dict(timeline.root.attrib))
    for tag in ('import-options', 'resources'):
        node = timeline.root.find(tag)
        if node is not None:
            root.append(deepcopy(node))
    old_library = timeline.root.find('library')
    if old_library is not None:
        parent = ET.SubElement(root, 'library', dict(old_library.attrib))
    else:
        parent = root
    event = ET.SubElement(parent, 'event', {'name': event_name, 'uid': str(uuid.uuid4()).upper()})
    project = deepcopy(timeline.project)
    project.set('name', project_name)
    project.set('uid', str(uuid.uuid4()).upper())
    for attribute in ('id', 'modDate'):
        project.attrib.pop(attribute, None)
    event.append(project)
    sequence = project.find('sequence')
    spine = sequence.find('spine')
    spine.clear()
    cursor = Fraction(0)
    segments = []
    applied_ranges = []
    for row in ranges:
        if not isinstance(row, dict) or 'start' not in row or 'end' not in row:
            raise ValueError('Each range needs start and end in project timeline seconds.')
        requested_start, requested_end = time_value(row['start']), time_value(row['end'])
        if not 0 <= requested_start < requested_end <= timeline.duration:
            raise ValueError(f'Out-of-bounds or empty timeline range: {row}')
        start = round(requested_start / timeline.frame_duration) * timeline.frame_duration
        end = round(requested_end / timeline.frame_duration) * timeline.frame_duration
        if start >= end:
            raise ValueError('Range becomes empty after alignment to project frames.')
        applied_ranges.append({'requested_start': str(row['start']), 'requested_end': str(row['end']),
                               'start': xml_time(start), 'end': xml_time(end),
                               'reason': row.get('reason', '')})
        covered = Fraction(0)
        for clip_start, clip_end, original in timeline.spans:
            a, b = max(start, clip_start), min(end, clip_end)
            if a >= b:
                continue
            clip = deepcopy(original)
            source_start = time_value(original.get('start', '0s')) + a - clip_start
            duration = b - a
            clip.set('offset', xml_time(cursor))
            clip.set('start', xml_time(source_start))
            clip.set('duration', xml_time(duration))
            spine.append(clip)
            segment = {'type': clip.tag, 'ref': clip.get('ref'), 'name': clip.get('name'),
                       'original_timeline_start': xml_time(a), 'timeline_start': xml_time(cursor),
                       'source_start': xml_time(source_start), 'source_end': xml_time(source_start + duration),
                       'duration': xml_time(duration), 'reason': row.get('reason', ''),
                       'enabled': clip.get('enabled', '1') == '1', 'src_enable': clip.get('srcEnable', 'all')}
            if clip.tag == 'asset-clip':
                asset = timeline.resources[clip.get('ref')]
                origin = time_value(asset.get('start', '0s'))
                segment.update(source_path=media_path(asset), asset_start=xml_time(origin),
                               file_start=xml_time(source_start - origin),
                               has_audio=asset.get('hasAudio') == '1', has_video=asset.get('hasVideo') == '1')
            segments.append(segment)
            cursor += duration
            covered += duration
        if covered != end - start:
            raise ValueError('Selected range is not fully covered by the timeline.')
    sequence.set('duration', xml_time(cursor))
    ET.indent(root, space='    ')
    xml = '<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE fcpxml>\n' + ET.tostring(root, encoding='unicode') + '\n'
    format_node = timeline.resources[timeline.sequence.get('format')]
    manifest = {'schema_version': 1, 'input': str(timeline.path), 'project': project_name,
                'frame_duration': xml_time(timeline.frame_duration),
                'original_duration': xml_time(timeline.duration), 'edited_duration': xml_time(cursor),
                'video_format': {k: format_node.get(k) for k in ('width', 'height', 'colorSpace')},
                'ranges': applied_ranges, 'segments': segments}
    return xml, manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', required=True, type=Path, help='.fcpxml or .fcpxmld bundle')
    parser.add_argument('--project', help='Select project when export contains several')
    parser.add_argument('--inspect', action='store_true', help='Print project timing/media; no edit')
    parser.add_argument('--decisions', type=Path, help='JSON containing ordered timeline ranges')
    parser.add_argument('--output', type=Path, help='New .fcpxml path; never overwritten')
    parser.add_argument('--name', help='Revision project name')
    parser.add_argument('--event', default='Agent Edits', help='Destination event name')
    parser.add_argument('--dtd', type=Path, help='Optional explicit matching Apple DTD')
    args = parser.parse_args()
    try:
        timeline = load_timeline(args.input, args.project)
        if args.inspect:
            print(json.dumps(timeline.inspect(), indent=2))
            return
        if not args.decisions:
            raise ValueError('--decisions is required for editing; no automatic cuts are applied.')
        token = datetime.now().strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:6]
        output = (args.output or ROOT / 'outputs' / f'edit-{token}.fcpxml').expanduser().resolve()
        manifest_path = output.with_suffix('.decisions.json')
        if output.suffix != '.fcpxml':
            raise ValueError('--output must end in .fcpxml.')
        if output.exists() or manifest_path.exists():
            raise ValueError('Output already exists; choose a new path to preserve previous revisions.')
        decisions = json.loads(args.decisions.read_text())
        name = args.name or f'{timeline.project.get("name", "Project")} - Edit {token}'
        xml, manifest = apply_ranges(timeline, decisions, name, args.event)
        manifest['validation'] = validate_xml(xml, timeline.root.get('version', ''), args.dtd)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(xml)
        write_json(manifest_path, manifest)
        print(json.dumps({'output': str(output), 'manifest': str(manifest_path), 'project': name,
                          'duration_seconds': float(time_value(manifest['edited_duration'])),
                          'segments': len(manifest['segments']), 'validation': manifest['validation']}, indent=2))
    except (ValueError, OSError, ET.ParseError, KeyError, TypeError) as exc:
        parser.exit(2, f'Error: {exc}\n')


if __name__ == '__main__':
    main()
