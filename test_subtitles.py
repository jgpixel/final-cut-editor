"""Exercise speech mapping, subtitle ownership at cuts, layout, and XML structure."""
from fractions import Fraction
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

from build_clean_edit import apply_ranges
from fcpxml_tools import load_timeline, time_value, validate_xml, xml_time
from generate_subtitles import add_subtitles, analysis_for, layout_for, read_style, source_identity_matches, wrap_words, width_units, group_words, analysis_words
from analyze_recording import DEFAULT_MODEL, DEFAULT_LANGUAGE, ASR_PROFILE, passage_rows, recognition_windows
from subtitle_timing import connected_subtitles, subtitle_data, title_text


class SubtitleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.path = self.directory / 'input.fcpxml'
        self.xml = '''<fcpxml version="1.14"><resources>
          <format id="r1" frameDuration="1/25s" width="1920" height="1080"/>
          <asset id="r2" start="3600s" duration="100s" hasVideo="1" hasAudio="1" format="r1">
            <media-rep kind="original-media" src="file:///tmp/source.mov"/>
          </asset></resources><library><event><project name="Input">
          <sequence format="r1" duration="8s" tcStart="7200s"><spine>
            <asset-clip ref="r2" offset="0s" start="3610s" duration="4s"><metadata/></asset-clip>
            <asset-clip ref="r2" offset="4s" start="3620s" duration="4s"/>
          </spine></sequence></project></event></library></fcpxml>'''
        self.words = {'coordinate_space': 'timeline', 'words': [
            {'word': 'I', 'start': 1, 'end': 1.2}, {'word': 'went', 'start': 1.3, 'end': 1.6},
            {'word': 'to', 'start': 1.7, 'end': 1.9}, {'word': 'the', 'start': 2, 'end': 2.2},
            {'word': 'store.', 'start': 2.3, 'end': 2.8},
            {'word': 'Again.', 'start': 5, 'end': 5.8}]}

    def timeline(self, xml):
        self.path.write_text(xml)
        return load_timeline(self.path)

    def generated(self, style=None):
        return add_subtitles(self.xml, style or read_style(), supplied=self.words)

    def test_titles_connected_in_source_coordinates_and_dtd_valid(self):
        xml, report = self.generated()
        timeline = self.timeline(xml)
        timeline.check_supported()
        titles = connected_subtitles(timeline)
        self.assertEqual([t[0] for t in titles], [Fraction(1), Fraction(5)])
        self.assertEqual(titles[0][2].get('offset'), '3611s')
        self.assertEqual(report['words'], 6)
        self.assertIn('matching Apple DTD', validate_xml(xml, '1.14'))
        self.assertEqual(list(timeline.spans[0][2])[-1].tag, 'metadata')

    def test_cut_through_phrase_removes_words_and_rebases_embedded_timing(self):
        xml, _ = self.generated()
        edited, report = apply_ranges(self.timeline(xml), {'coordinate_space': 'timeline',
            'ranges': [{'start': 2, 'end': 4}]}, 'Cut', 'Edits')
        timeline = self.timeline(edited)
        title = connected_subtitles(timeline)[0][2]
        self.assertEqual(title_text(title), 'the store.')
        self.assertEqual(time_value(subtitle_data(title)['words'][0]['start']), 0)
        self.assertEqual(report['warnings'], [])
        self.assertIn('matching Apple DTD', validate_xml(edited, '1.14'))

    def test_reorder_repeat_deletion_and_unique_style_ids(self):
        xml, _ = self.generated()
        edited, _ = apply_ranges(self.timeline(xml), {'coordinate_space': 'timeline', 'ranges': [
            {'start': 4, 'end': 8}, {'start': 0, 'end': 4}, {'start': 0, 'end': 4}]}, 'Reorder', 'Edits')
        titles = connected_subtitles(self.timeline(edited))
        self.assertEqual([t[0] for t in titles], [Fraction(1), Fraction(5), Fraction(9)])
        self.assertEqual([title_text(t[2]) for t in titles], ['Again.', 'I went to the store.', 'I went to the store.'])
        ids = [d.get('id') for d in ET.fromstring(edited).iter('text-style-def')]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertIn('matching Apple DTD', validate_xml(edited, '1.14'))
        deleted, _ = apply_ranges(self.timeline(xml), {'coordinate_space': 'timeline',
            'ranges': [{'start': 3, 'end': 4}]}, 'Delete', 'Edits')
        self.assertFalse(connected_subtitles(self.timeline(deleted)))

    def test_external_subtitles_preserve_styling_without_inventing_word_timing(self):
        xml, _ = self.generated()
        root = ET.fromstring(xml)
        title = root.find('.//title')
        title.remove(title.find('note'))
        title.find('text-style-def/text-style').set('fontColor', '1 0 0 1')
        edited, report = apply_ranges(self.timeline(ET.tostring(root, encoding='unicode')),
            {'coordinate_space': 'timeline', 'ranges': [{'start': 2, 'end': 4}]}, 'Cut', 'Edits')
        title = connected_subtitles(self.timeline(edited))[0][2]
        self.assertEqual(title_text(title), 'I went to the store.')
        self.assertEqual(title.find('text-style-def/text-style').get('fontColor'), '1 0 0 1')
        self.assertEqual(len(report['warnings']), 1)

    def test_human_text_change_is_not_overwritten_by_stale_note(self):
        xml, _ = self.generated()
        root = ET.fromstring(xml)
        root.find('.//title/text/text-style').text = 'My corrected wording'
        edited, report = apply_ranges(self.timeline(ET.tostring(root, encoding='unicode')),
            {'coordinate_space': 'timeline', 'ranges': [{'start': 2, 'end': 4}]}, 'Cut', 'Edits')
        title = connected_subtitles(self.timeline(edited))[0][2]
        self.assertEqual(title_text(title), 'My corrected wording')
        self.assertIsNone(subtitle_data(title))
        self.assertTrue(report['warnings'])

    def test_mixed_fonts_survive_text_trim_and_fractional_frame_rate(self):
        xml, _ = self.generated()
        root = ET.fromstring(xml)
        title = root.find('.//title')
        title.find('text/text-style').text = 'I went to the '
        ET.SubElement(title.find('text'), 'text-style', {'ref': 'emphasis'}).text = 'store.'
        # Keep DTD ordering: style definitions precede note/adjustments.
        definition = ET.Element('text-style-def', {'id': 'emphasis'})
        ET.SubElement(definition, 'text-style', {'font': 'Georgia', 'fontColor': '1 0 0 1'})
        title.insert(list(title).index(title.find('note')), definition)
        edited, _ = apply_ranges(self.timeline(ET.tostring(root, encoding='unicode')),
            {'coordinate_space': 'timeline', 'ranges': [{'start': 2, 'end': 4}]}, 'Cut', 'Edits')
        title = connected_subtitles(self.timeline(edited))[0][2]
        self.assertEqual(title_text(title), 'the store.')
        runs = title.findall('text/text-style')
        ref = runs[-1].get('ref')
        self.assertEqual(title.find(f'text-style-def[@id="{ref}"]/text-style').get('font'), 'Georgia')
        self.assertIn('matching Apple DTD', validate_xml(edited, '1.14'))
        root = ET.fromstring(self.xml)
        fd = Fraction(1001, 60000)
        root.find('resources/format').set('frameDuration', xml_time(fd))
        for i, clip in enumerate(root.findall('.//asset-clip')):
            clip.set('duration', xml_time(240 * fd))
            clip.set('offset', xml_time(i * 240 * fd))
        root.find('.//sequence').set('duration', xml_time(480 * fd))
        generated, _ = add_subtitles(ET.tostring(root, encoding='unicode'), read_style(), supplied=self.words)
        timeline = self.timeline(generated)
        timeline.check_supported()
        for start, end, _ in connected_subtitles(timeline):
            self.assertEqual(start / fd % 1, 0)
            self.assertEqual(end / fd % 1, 0)

    def test_subtitle_crossing_source_boundary_is_preserved_and_reanchored(self):
        xml, _ = self.generated()
        root = ET.fromstring(xml)
        title = root.find('.//title')
        title.remove(title.find('note'))
        title.set('offset', '3613s')
        title.set('duration', '3s')
        second = root.findall('.//asset-clip')[1]
        second.remove(second.find('title'))
        edited, _ = apply_ranges(self.timeline(ET.tostring(root, encoding='unicode')),
            {'coordinate_space': 'timeline', 'ranges': [{'start': 4, 'end': 8}]}, 'Cut', 'Edits')
        start, end, title = connected_subtitles(self.timeline(edited))[0]
        self.assertEqual((start, end), (0, 2))
        self.assertEqual(title.get('offset'), '3620s')

    def test_source_timestamps_nonzero_timecodes_repeated_source_and_cache_reuse(self):
        source = self.directory / 'source.mov'
        source.write_bytes(b'fixture')
        stat = source.stat()
        data = {'source': str(source), 'coordinate_space': 'file_seconds',
                'identity': {'source': str(source), 'size': stat.st_size,
                             'mtime_ns': stat.st_mtime_ns, 'ctime_ns': stat.st_ctime_ns},
                'segments': [{'words': [{'word': 'First.', 'start': 11, 'end': 12},
                                        {'word': 'Second.', 'start': 21, 'end': 22}]}]}
        analysis = self.directory / 'analysis.json'
        analysis.write_text(json.dumps(data))
        xml = self.xml.replace('file:///tmp/source.mov', source.as_uri())
        result, report = add_subtitles(xml, read_style(), explicit=[analysis])
        titles = connected_subtitles(self.timeline(result))
        self.assertEqual([title_text(t[2]) for t in titles], ['First.', 'Second.'])
        self.assertEqual([t[0] for t in titles], [1, 5])
        self.assertEqual(report['analysis'], [str(analysis)])
        source.write_bytes(b'changed')
        self.assertFalse(source_identity_matches(data, source))
        with self.assertRaisesRegex(ValueError, 'Stale'):
            add_subtitles(xml, read_style(), explicit=[analysis])

    def test_automatic_cache_selection_requires_default_model_even_if_old_model_is_newer(self):
        source = (self.directory / 'source.mov').resolve()
        source.write_bytes(b'fixture')
        stat = source.stat()
        identity = {'source': str(source), 'size': stat.st_size,
                    'mtime_ns': stat.st_mtime_ns, 'ctime_ns': stat.st_ctime_ns}
        cache = self.directory / '.cache' / 'analysis'
        cache.mkdir(parents=True)
        turbo = cache / ('a' * 64 + '.json')
        small = cache / ('b' * 64 + '.json')
        for path, model in ((turbo, DEFAULT_MODEL), (small, 'small.en')):
            path.write_text(json.dumps({'source': str(source), 'coordinate_space': 'file_seconds',
                'identity': {**identity, 'settings': {'model': model, 'language': DEFAULT_LANGUAGE,
                                                     'word_timestamps': True, 'asr_profile': ASR_PROFILE}}}))
        with patch('generate_subtitles.ROOT', self.directory), patch('generate_subtitles.subprocess.run') as process:
            data, path = analysis_for(str(source), [])
            self.assertEqual(path, str(turbo))
            self.assertEqual(data['identity']['settings']['model'], DEFAULT_MODEL)
            process.assert_not_called()
            turbo.unlink()
            process.side_effect = RuntimeError('New transcription required')
            with self.assertRaisesRegex(RuntimeError, 'New transcription required'):
                analysis_for(str(source), [])

    def test_new_profile_does_not_reuse_unbounded_turbo_transcript(self):
        source = (self.directory / 'source.mov').resolve()
        source.write_bytes(b'fixture')
        stat = source.stat()
        cache = self.directory / '.cache' / 'analysis'
        cache.mkdir(parents=True)
        (cache / ('c' * 64 + '.json')).write_text(json.dumps({'source': str(source),
            'coordinate_space': 'file_seconds', 'identity': {'source': str(source),
            'size': stat.st_size, 'mtime_ns': stat.st_mtime_ns, 'ctime_ns': stat.st_ctime_ns,
            'settings': {'model': DEFAULT_MODEL, 'language': DEFAULT_LANGUAGE, 'word_timestamps': True}}}))
        with patch('generate_subtitles.ROOT', self.directory), patch('generate_subtitles.subprocess.run',
                side_effect=RuntimeError('New profile needed')):
            with self.assertRaisesRegex(RuntimeError, 'New profile needed'):
                analysis_for(str(source), [])

    def test_natural_phrase_boundaries_preserve_clauses_and_words(self):
        style = read_style()
        layout = layout_for(1920, 1080, style)
        for text, expected in [
            ("Let's get to our starting location make sure there's no tree around me",
             ["Let's get to our starting location", "make sure there's no tree around me"]),
            ("line up with the center here we'll make our dive",
             ['line up with the center here', "we'll make our dive"]),
        ]:
            words = [{'word': word, 'start': i * 0.18, 'end': (i + 1) * 0.18}
                     for i, word in enumerate(text.split())]
            groups = group_words(words, layout, style)
            self.assertEqual([' '.join(w['word'] for w in group) for group in groups], expected)
            self.assertEqual([w for group in groups for w in group], words)

    def test_speech_boundary_is_hard_even_when_word_timestamps_touch(self):
        words = [{'word': word, 'start': i * 0.2, 'end': (i + 1) * 0.2,
                  'passage_id': 0 if i < 2 else 1}
                 for i, word in enumerate(["let's", 'go', "that's", 'not', 'good'])]
        groups = group_words(words, layout_for(1920, 1080, read_style()), read_style())
        self.assertEqual([' '.join(w['word'] for w in g) for g in groups], ["let's go", "that's not good"])

    def test_recognizer_words_are_clamped_to_actual_audio_window(self):
        passages = [{'start': 81.0, 'end': 82.2}, {'start': 93.5, 'end': 94.8}]
        segments = [SimpleNamespace(seek=9350, words=[
            SimpleNamespace(start=81.7, end=93.8, word=" that's", probability=0.9),
            SimpleNamespace(start=94, end=95.5, word=' good', probability=0.9)])]
        row = passage_rows(segments, passages)[0]
        self.assertEqual(row['passage_id'], 1)
        self.assertEqual(row['words'][0]['start'], 93.5)
        self.assertEqual(row['words'][-1]['end'], 94.8)
        with self.assertRaisesRegex(ValueError, 'unknown speech passage'):
            passage_rows([SimpleNamespace(seek=8200, words=[])], passages)

    def test_shared_context_preserves_real_silence_and_caption_speech_boundaries(self):
        speech = [{'start': 0, 'end': 2}, {'start': 3, 'end': 4}, {'start': 8, 'end': 10}]
        self.assertEqual(recognition_windows(speech), [{'start': 0, 'end': 4}, {'start': 8, 'end': 10}])
        data = {'speech_passages': speech, 'segments': [{'passage_id': 0, 'passage_start': 0,
            'passage_end': 4, 'words': [{'word': 'First', 'start': 1, 'end': 2},
                                      {'word': 'Second', 'start': 3, 'end': 4}]}]}
        words = analysis_words(data)
        self.assertEqual([w['passage_id'] for w in words], [0, 1])
        self.assertEqual([w['passage_end'] for w in words], [2, 4])

    def test_vertical_horizontal_square_resolution_and_long_words(self):
        style = read_style()
        for width, height in ((1920, 1080), (3840, 2160), (1080, 1920), (2160, 3840), (1080, 1080), (320, 568)):
            root = ET.fromstring(self.xml)
            root.find('.//format').set('width', str(width))
            root.find('.//format').set('height', str(height))
            xml, report = add_subtitles(ET.tostring(root, encoding='unicode'), style, supplied=self.words)
            self.assertLessEqual(report['layout']['font_pixels'], min(width, height) * 0.055 + 0.001)
            for title in ET.fromstring(xml).iter('title'):
                self.assertLessEqual(len(title_text(title).splitlines()), 2)
            self.assertIn('matching Apple DTD', validate_xml(xml, '1.14'))
        landscape = layout_for(1920, 1080, style)
        larger = layout_for(3840, 2160, style)
        self.assertEqual(larger['font_pixels'], landscape['font_pixels'] * 2)
        portrait = layout_for(1080, 1920, style)
        self.assertGreater(portrait['bottom_margin'], landscape['bottom_margin'])
        words = {'coordinate_space': 'timeline', 'words': [{'word': 'W' * 100, 'start': 1, 'end': 2}]}
        xml, _ = add_subtitles(self.xml, style, supplied=words)
        title = ET.fromstring(xml).find('.//title')
        size = float(title.find('text-style-def/text-style').get('fontSize')) * 1.2 * 1080 / 2160
        self.assertLessEqual(width_units(title_text(title)) * size, landscape['usable_width'] + 0.01)

    def test_word_mode_does_not_overlap_and_respects_mute_and_gap(self):
        style = {**read_style(), 'mode': 'word'}
        xml, report = self.generated(style)
        self.assertEqual(report['titles'], 6)
        titles = connected_subtitles(self.timeline(xml))
        self.assertTrue(all(a[1] <= b[0] for a, b in zip(titles, titles[1:])))
        muted = self.xml.replace('start="3610s"', 'srcEnable="video" start="3610s"')
        _, report = add_subtitles(muted, style, supplied=self.words)
        self.assertEqual(report['titles'], 1)

    def test_caps_preset_preserves_word_timing_offsets_and_outline(self):
        style = read_style(Path(__file__).parent / 'subtitle_styles' / 'word-caps-black.json')
        words = {'coordinate_space': 'timeline', 'words': [
            {'word': 'Straße', 'start': 1, 'end': 1.2},
            {'word': 'today.', 'start': 2.6, 'end': 2.8}]}
        original = json.dumps(words)
        xml, report = add_subtitles(self.xml, style, supplied=words)
        titles = connected_subtitles(self.timeline(xml))
        self.assertEqual([title_text(t[2]) for t in titles], ['STRASSE', 'TODAY.'])
        self.assertEqual([t[0] for t in titles], [Fraction(1), Fraction(13, 5)])
        self.assertLess(titles[0][1], titles[1][0])
        first = titles[0][2]
        self.assertEqual(subtitle_data(first)['words'][0]['char_end'], 7)
        self.assertEqual(first.find('text-style-def/text-style').get('strokeWidth'), '1.5')
        self.assertEqual(first.find('text-style-def/text-style').get('strokeColor'), '0 0 0 1')
        self.assertEqual(first.find('param[@name="Background Opacity"]').get('value'), '1')
        self.assertEqual(json.dumps(words), original)
        self.assertEqual(report['style']['text_case'], 'uppercase')
        self.assertIn('matching Apple DTD', validate_xml(xml, '1.14'))

    def test_generation_requires_explicit_replacement_and_rejects_unrelated_titles(self):
        xml, _ = self.generated()
        with self.assertRaisesRegex(ValueError, 'already has subtitles'):
            add_subtitles(xml, read_style(), supplied=self.words)
        replaced, report = add_subtitles(xml, read_style(), supplied=self.words, replace=True)
        self.assertEqual(report['titles'], 2)
        self.assertEqual(len(list(ET.fromstring(replaced).iter('title'))), 2)
        root = ET.fromstring(xml)
        title = root.find('.//title')
        title.remove(title.find('note'))
        title.set('role', 'Titles')
        root.find('resources/effect').set('uid', '/unrelated/template.moti')
        with self.assertRaisesRegex(ValueError, 'identifiable subtitles'):
            self.timeline(ET.tostring(root, encoding='unicode')).check_supported()


if __name__ == '__main__':
    unittest.main()
