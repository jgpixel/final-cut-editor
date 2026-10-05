"""Shared exact timing and inspection for simple FCPXML project edits."""
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from urllib.parse import unquote, urlparse
import json
import shutil
import subprocess
import tempfile
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parent
DTD_DIR = Path('/Applications/Final Cut Pro Creator Studio.app/Contents/Frameworks/Interchange.framework/Versions/A/Resources')


def time_value(value):
    """Accept decimal seconds or exact FCPXML rational seconds."""
    result = Fraction(str(value).removesuffix('s'))
    return result


def xml_time(value):
    value = Fraction(value)
    return f'{value.numerator}/{value.denominator}s' if value.denominator != 1 else f'{value.numerator}s'


def media_path(asset):
    reps = asset.findall('media-rep')
    rep = next((r for r in reps if r.get('kind') == 'original-media'), None)
    if rep is None:
        return None
    url = urlparse(rep.get('src', ''))
    if url.scheme != 'file' or url.netloc not in ('', 'localhost'):
        return None
    return str(Path(unquote(url.path)))


def write_json(path, data):
    Path(path).write_text(json.dumps(data, indent=2) + '\n')


@dataclass
class Timeline:
    path: Path
    root: ET.Element
    project: ET.Element
    sequence: ET.Element
    resources: dict
    frame_duration: Fraction
    duration: Fraction
    spans: list

    def inspect(self):
        clips = []
        errors = []
        try:
            self.check_supported()
        except ValueError as exc:
            errors.append(str(exc))
        for start, end, clip in self.spans:
            asset = self.resources.get(clip.get('ref'))
            source = time_value(clip.get('start', '0s'))
            row = {'type': clip.tag, 'name': clip.get('name'), 'ref': clip.get('ref'),
                   'timeline_start': xml_time(start), 'timeline_end': xml_time(end),
                   'source_start': xml_time(source)}
            if asset is not None and asset.tag == 'asset':
                origin = time_value(asset.get('start', '0s'))
                row.update(source_path=media_path(asset), asset_start=xml_time(origin),
                           file_start=xml_time(source - origin))
            clips.append(row)
        return {'input': str(self.path), 'project': self.project.get('name'),
                'duration': xml_time(self.duration), 'frame_duration': xml_time(self.frame_duration),
                'supported': not errors, 'limitations': errors, 'clips': clips}

    def check_supported(self):
        cursor = Fraction(0)
        for start, end, clip in self.spans:
            if clip.tag not in ('asset-clip', 'gap'):
                raise ValueError(f'Unsupported timeline element: {clip.tag}; no edit written.')
            if start != cursor or end <= start:
                raise ValueError('Timeline must be contiguous and non-overlapping; use explicit gaps.')
            if start / self.frame_duration % 1 or end / self.frame_duration % 1:
                raise ValueError('Timeline clip boundaries are not on project frames.')
            if clip.get('lane', '0') != '0':
                raise ValueError('Connected clips are not supported by this range editor.')
            if any(child.tag not in ('note', 'metadata') for child in clip):
                raise ValueError('Effects, markers, connected clips, and nested timing require a different edit path.')
            if clip.get('audioStart') is not None or clip.get('audioDuration') is not None:
                raise ValueError('Split audio/video edits are not supported.')
            if clip.tag == 'asset-clip':
                asset = self.resources.get(clip.get('ref'))
                if asset is None or asset.tag != 'asset':
                    raise ValueError(f'Missing or unsupported asset reference: {clip.get("ref")}')
                source_start = time_value(clip.get('start', '0s'))
                asset_start = time_value(asset.get('start', '0s'))
                if source_start < asset_start:
                    raise ValueError('Clip starts before its source asset.')
                if asset.get('duration') is not None:
                    if source_start + end - start > asset_start + time_value(asset.get('duration')):
                        raise ValueError('Clip extends beyond its source asset.')
            cursor = end
        if cursor != self.duration:
            raise ValueError('Sequence duration does not match its primary storyline.')


def load_timeline(path, project_name=None):
    path = Path(path).expanduser().resolve()
    if path.is_dir():
        path /= 'Info.fcpxml'
    root = ET.parse(path).getroot()
    if root.tag != 'fcpxml':
        raise ValueError('Expected an FCPXML document.')
    projects = root.findall('.//project')
    if project_name is not None:
        projects = [p for p in projects if p.get('name') == project_name]
    if len(projects) != 1:
        raise ValueError('Select exactly one project with --project; available: ' +
                         ', '.join(p.get('name', '(unnamed)') for p in root.findall('.//project')))
    project = projects[0]
    sequence = project.find('sequence')
    if sequence is None or sequence.find('spine') is None:
        raise ValueError('Project has no sequence/spine.')
    resource_node = root.find('resources')
    if resource_node is None:
        raise ValueError('Missing resources.')
    resources = {r.get('id'): r for r in resource_node}
    format_node = resources.get(sequence.get('format'))
    if format_node is None or format_node.get('frameDuration') is None:
        raise ValueError('Sequence format has no frameDuration.')
    frame_duration = time_value(format_node.get('frameDuration'))
    if frame_duration <= 0:
        raise ValueError('Invalid frame duration.')
    spans = []
    for clip in sequence.find('spine'):
        start = time_value(clip.get('offset', '0s'))
        duration = clip.get('duration')
        if duration is None:
            asset = resources.get(clip.get('ref'))
            duration = asset.get('duration') if asset is not None else None
        if duration is None:
            raise ValueError(f'Missing duration on {clip.tag}.')
        spans.append((start, start + time_value(duration), clip))
    duration = time_value(sequence.get('duration')) if sequence.get('duration') else max(
        (end for _, end, _ in spans), default=Fraction(0))
    return Timeline(path, root, project, sequence, resources, frame_duration, duration, spans)


def validate_xml(xml, version, dtd_path=None):
    """Validate before publishing outputs, when a matching DTD is available."""
    dtd = Path(dtd_path) if dtd_path else DTD_DIR / f'FCPXMLv{version.replace(".", "_")}.dtd'
    if not dtd.is_file() or not shutil.which('xmllint'):
        if dtd_path:
            raise ValueError('Requested DTD or xmllint is unavailable.')
        return 'timing checked; matching DTD or xmllint unavailable'
    with tempfile.TemporaryDirectory(prefix='fcpxml-validate-') as directory:
        directory = Path(directory)
        shutil.copyfile(dtd, directory / 'schema.dtd')
        (directory / 'edit.fcpxml').write_text(xml)
        result = subprocess.run(['xmllint', '--noout', '--nonet', '--dtdvalid',
                                 str(directory / 'schema.dtd'), str(directory / 'edit.fcpxml')],
                                capture_output=True, text=True)
        if result.returncode:
            raise ValueError('FCPXML validation failed: ' + result.stderr.strip())
    return 'matching Apple DTD and timing checked'
