"""Verify bounded speech recognition timing without loading transcription models."""
from types import SimpleNamespace
import unittest

from analyze_recording import passage_rows, recognition_windows


class AnalysisTests(unittest.TestCase):
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

    def test_shared_context_preserves_original_audio_intervals(self):
        speech = [{'start': 0, 'end': 2}, {'start': 3, 'end': 4}, {'start': 8, 'end': 10}]
        self.assertEqual(recognition_windows(speech), [{'start': 0, 'end': 4}, {'start': 8, 'end': 10}])


if __name__ == '__main__':
    unittest.main()
