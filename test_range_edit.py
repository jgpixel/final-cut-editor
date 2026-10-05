"""Verify timing and preservation across sources, frame rates, and edit orders."""
from copy import deepcopy
from fractions import Fraction
from pathlib import Path
import tempfile
import unittest
import xml.etree.ElementTree as ET

from build_clean_edit import apply_ranges
from fcpxml_tools import load_timeline, time_value, xml_time, validate_xml


class RangeEditingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'input.fcpxml'
        self.fd = Fraction(1001, 24000)
        self.origin = Fraction(3600)
        self.root = ET.fromstring('''<fcpxml version="1.14"><resources>
          <format id="format" frameDuration="1001/24000s" width="1920" height="1080"/>
          <asset id="first" start="3600s" duration="10s" hasVideo="1" hasAudio="1" format="format">
            <media-rep kind="original-media" src="file:///tmp/first%20clip.mov"/>
          </asset>
          <asset id="second" start="7200s" duration="10s" hasVideo="1" hasAudio="1" format="format">
            <media-rep kind="original-media" src="file:///tmp/second.mov"/>
          </asset></resources><library><event><project name="Input"><sequence format="format">
          <spine><asset-clip ref="first"/><asset-clip ref="second"/></spine>
          </sequence></project></event></library></fcpxml>''')
        sequence = self.root.find('.//sequence')
        sequence.set('duration', xml_time(10 * self.fd))
        first, second = list(sequence.find('spine'))
        first.attrib.update(offset='0s', start=xml_time(self.origin + 2 * self.fd), duration=xml_time(4 * self.fd))
        second.attrib.update(offset=xml_time(4 * self.fd), start='7200s', duration=xml_time(6 * self.fd))

    def timeline(self):
        ET.ElementTree(self.root).write(self.path)
        return load_timeline(self.path)

    def edit(self, ranges):
        return apply_ranges(self.timeline(), {'coordinate_space': 'timeline', 'ranges': ranges}, 'Revision', 'Edits')

    def test_cross_source_trim_and_nonzero_media_timecode(self):
        xml, manifest = self.edit([{'start': xml_time(2 * self.fd), 'end': xml_time(7 * self.fd)}])
        a, b = manifest['segments']
        self.assertEqual(a['ref'], 'first')
        self.assertEqual(time_value(a['file_start']), 4 * self.fd)
        self.assertEqual(a['source_path'], '/tmp/first clip.mov')
        self.assertEqual(b['ref'], 'second')
        self.assertEqual(time_value(b['timeline_start']), 2 * self.fd)
        self.assertEqual(time_value(manifest['edited_duration']), 5 * self.fd)
        self.assertEqual(validate_xml(xml, '1.14'), 'matching Apple DTD and timing checked')

    def test_reorder_and_repeat_selected_passages(self):
        _, manifest = self.edit([{'start': xml_time(4 * self.fd), 'end': xml_time(10 * self.fd)},
                                 {'start': 0, 'end': xml_time(4 * self.fd)},
                                 {'start': 0, 'end': xml_time(4 * self.fd)}])
        self.assertEqual([s['ref'] for s in manifest['segments']], ['second', 'first', 'first'])
        self.assertEqual(time_value(manifest['edited_duration']), 14 * self.fd)

    def test_full_range_has_no_automatic_cuts_and_preserves_metadata(self):
        clip = self.root.find('.//asset-clip')
        ET.SubElement(clip, 'metadata')
        xml, manifest = self.edit([{'start': 0, 'end': xml_time(10 * self.fd)}])
        edited = ET.fromstring(xml)
        self.assertEqual(time_value(manifest['edited_duration']), 10 * self.fd)
        self.assertIsNotNone(edited.find('.//asset-clip/metadata'))
        self.assertNotEqual(edited.find('.//project').get('uid'), self.root.find('.//project').get('uid'))
        self.assertEqual(
            ET.canonicalize(ET.tostring(edited.find('resources'), encoding='unicode'), strip_text=True),
            ET.canonicalize(ET.tostring(self.root.find('resources'), encoding='unicode'), strip_text=True))

    def test_out_of_bounds_and_wrong_coordinate_space_rejected(self):
        with self.assertRaisesRegex(ValueError, 'Out-of-bounds'):
            self.edit([{'start': 0, 'end': 200}])
        with self.assertRaisesRegex(ValueError, 'coordinate_space'):
            apply_ranges(self.timeline(), {'ranges': [{'start': 0, 'end': 1}]}, 'Edit', 'Edits')

    def test_nested_clip_rejected_instead_of_discarded(self):
        ET.SubElement(self.root.find('.//asset-clip'), 'asset-clip', ref='second', duration='1s')
        with self.assertRaisesRegex(ValueError, 'nested timing'):
            self.edit([{'start': 0, 'end': xml_time(4 * self.fd)}])

    def test_gap_is_preserved(self):
        gap = self.root.find('.//spine')[1]
        gap.tag = 'gap'
        gap.attrib.pop('ref')
        gap.set('start', '0s')
        _, manifest = self.edit([{'start': 0, 'end': xml_time(10 * self.fd)}])
        self.assertEqual([s['type'] for s in manifest['segments']], ['asset-clip', 'gap'])

    def test_multiple_projects_require_selection(self):
        event = self.root.find('.//event')
        other = deepcopy(event.find('project'))
        other.set('name', 'Other')
        event.append(other)
        with self.assertRaisesRegex(ValueError, 'Select exactly one'):
            self.timeline()
        self.assertEqual(load_timeline(self.path, 'Other').project.get('name'), 'Other')

    def test_supports_other_project_frame_rates(self):
        for fd in (Fraction(1, 25), Fraction(1001, 30000), Fraction(1, 60)):
            self.root.find('.//format').set('frameDuration', xml_time(fd))
            sequence = self.root.find('.//sequence')
            sequence.set('duration', xml_time(10 * fd))
            first, second = list(sequence.find('spine'))
            first.set('duration', xml_time(4 * fd))
            second.set('duration', xml_time(6 * fd))
            second.set('offset', xml_time(4 * fd))
            _, manifest = self.edit([{'start': xml_time(1 * fd), 'end': xml_time(9 * fd)}])
            self.assertEqual(time_value(manifest['edited_duration']), 8 * fd)


if __name__ == '__main__':
    unittest.main()
