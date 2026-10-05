"""Transcribe a supplied recording locally, with reusable cached analysis."""
import argparse
from hashlib import sha256
from importlib.metadata import version
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time

from fcpxml_tools import ROOT, write_json

DEFAULT_MODEL = 'large-v3-turbo'
DEFAULT_LANGUAGE = 'en'
ASR_PROFILE = 'speech-passages-v2'
SAMPLE_RATE = 16000
SPEECH_OPTIONS = {'threshold': 0.5, 'min_speech_duration_ms': 100,
                  'min_silence_duration_ms': 650, 'max_speech_duration_s': 14,
                  'speech_pad_ms': 400}
WINDOW_OPTIONS = {'max_gap_seconds': 1.25, 'max_duration_seconds': 22}


def recognition_windows(passages):
    """Share nearby speech context, retaining real silence; never splice audio."""
    windows = []
    for passage in passages:
        if (windows and passage['start'] - windows[-1]['end'] <= WINDOW_OPTIONS['max_gap_seconds'] and
                passage['end'] - windows[-1]['start'] <= WINDOW_OPTIONS['max_duration_seconds']):
            windows[-1]['end'] = passage['end']
        else:
            windows.append(dict(passage))
    return windows


def passage_rows(segments, windows, frames_per_second=100):
    """Keep recognizer text/timing within its original, un-concatenated audio window."""
    by_seek = {int(p['start'] * frames_per_second): (i, p) for i, p in enumerate(windows)}
    rows = []
    for segment in segments:
        seek = min(by_seek, key=lambda value: abs(value - segment.seek))
        if abs(seek - segment.seek) > 1:
            raise ValueError('Recognizer returned an unknown speech passage offset.')
        index, passage = by_seek[seek]
        words = []
        for word in segment.words or []:
            start, end = max(passage['start'], word.start), min(passage['end'], word.end)
            if end <= start:
                continue
            words.append({'start': start, 'end': end, 'word': word.word,
                          'probability': word.probability})
        if words:
            rows.append({'start': words[0]['start'], 'end': words[-1]['end'],
                         'text': ''.join(w['word'] for w in words).strip(), 'words': words,
                         'passage_id': index, 'passage_start': passage['start'],
                         'passage_end': passage['end']})
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True, type=Path)
    parser.add_argument('--output', type=Path, help='Optional new copy of cached analysis JSON')
    parser.add_argument('--model', default=DEFAULT_MODEL)
    parser.add_argument('--language', default=DEFAULT_LANGUAGE, help='Language code or auto (requires multilingual model)')
    parser.add_argument('--threads', type=int, default=6)
    parser.add_argument('--beam-size', type=int, default=5)
    parser.add_argument('--batch-size', type=int, default=4, help='Bounded speech passages per inference batch')
    parser.add_argument('--detect-silence', action='store_true', help='Include acoustic gaps, without deciding edits')
    parser.add_argument('--silence-db', type=float, default=-35)
    parser.add_argument('--silence-min', type=float, default=0.3)
    parser.add_argument('--refresh', action='store_true', help='Recompute matching cached analysis')
    parser.add_argument('--allow-model-download', action='store_true', help='Allow downloading a missing model')
    args = parser.parse_args()
    try:
        source = args.source.expanduser().resolve(strict=True)
        if not source.is_file():
            raise ValueError('--source must be a media file.')
        if args.threads < 1 or args.beam_size < 1 or args.batch_size < 1 or args.silence_min <= 0:
            raise ValueError('Threads, beam size, and minimum silence must be positive.')
        if args.model.endswith('.en') and args.language != 'en':
            raise ValueError('English-only models require --language en. Select a multilingual model for other languages.')
        output = args.output.expanduser().resolve() if args.output else None
        if output and output.exists():
            raise ValueError('Output already exists; choose a new path.')
        stat = source.stat()
        settings = {'model': args.model, 'language': args.language, 'compute_type': 'int8',
                    'threads': args.threads, 'beam_size': args.beam_size, 'word_timestamps': True,
                    'asr_profile': ASR_PROFILE, 'speech_options': SPEECH_OPTIONS,
                    'window_options': WINDOW_OPTIONS,
                    'batch_size': args.batch_size, 'detect_silence': args.detect_silence,
                    'silence_db': args.silence_db, 'silence_min': args.silence_min,
                    'faster_whisper_version': version('faster-whisper')}
        identity = {'source': str(source), 'size': stat.st_size, 'mtime_ns': stat.st_mtime_ns,
                    'ctime_ns': stat.st_ctime_ns, 'settings': settings, 'schema_version': 1}
        key = sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
        cache = ROOT / '.cache' / 'analysis' / f'{key}.json'
        started = time.monotonic()
        if cache.is_file() and not args.refresh:
            result = json.loads(cache.read_text())
            reused = True
        else:
            os.environ.setdefault('HF_HOME', str(ROOT / '.cache' / 'huggingface'))
            os.environ['HF_HUB_DISABLE_TELEMETRY'] = '1'
            os.environ['HF_HUB_DISABLE_XET'] = '1'
            import imageio_ffmpeg
            from faster_whisper import WhisperModel, BatchedInferencePipeline
            from faster_whisper.audio import decode_audio
            from faster_whisper.vad import get_speech_timestamps, VadOptions
            with tempfile.TemporaryDirectory(prefix='fcp-audio-') as directory:
                wav = Path(directory) / 'audio.wav'
                command = [imageio_ffmpeg.get_ffmpeg_exe(), '-hide_banner', '-nostdin', '-y',
                           '-i', str(source), '-vn', '-ac', '1', '-ar', '16000']
                if args.detect_silence:
                    command += ['-af', f'silencedetect=noise={args.silence_db}dB:d={args.silence_min}']
                command += ['-c:a', 'pcm_s16le', str(wav)]
                decode = subprocess.run(command, capture_output=True, text=True)
                if decode.returncode:
                    raise ValueError('Audio extraction failed: ' + decode.stderr[-2000:])
                decode_seconds = time.monotonic() - started
                audio = decode_audio(str(wav), sampling_rate=SAMPLE_RATE)
                speech = get_speech_timestamps(audio, VadOptions(**SPEECH_OPTIONS))
                passages = [{'start': p['start'] / SAMPLE_RATE, 'end': p['end'] / SAMPLE_RATE} for p in speech]
                windows = recognition_windows(passages)
                detected = time.monotonic()
                model = WhisperModel(args.model, device='cpu', compute_type='int8',
                                     cpu_threads=args.threads, download_root=str(ROOT / '.cache' / 'models'),
                                     local_files_only=not args.allow_model_download)
                loaded = time.monotonic()
                duration = len(audio) / SAMPLE_RATE
                language = args.language if args.language != 'auto' else 'und'
                if windows:
                    # Explicit windows avoid VAD's normal concatenation of distant speech.
                    segments, info = BatchedInferencePipeline(model).transcribe(audio,
                        language=None if args.language == 'auto' else args.language,
                        beam_size=args.beam_size, word_timestamps=True, vad_filter=False,
                        clip_timestamps=windows, batch_size=args.batch_size)
                    rows = passage_rows(segments, windows, model.frames_per_second)
                    language = info.language
                else:
                    rows = []
            silences = []
            pending = None
            for line in decode.stderr.splitlines():
                match = re.search(r'silence_start: (-?[\d.]+)', line)
                if match:
                    pending = max(0.0, float(match.group(1)))
                match = re.search(r'silence_end: (-?[\d.]+)', line)
                if match and pending is not None:
                    silences.append({'start': pending, 'end': min(duration, float(match.group(1)))})
                    pending = None
            if pending is not None and pending < duration:
                silences.append({'start': pending, 'end': duration})
            now = source.stat()
            if (now.st_size, now.st_mtime_ns, now.st_ctime_ns) != (stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns):
                raise ValueError('Source changed during analysis; result was not cached.')
            result = {'schema_version': 1, 'identity': identity, 'source': str(source),
                      'coordinate_space': 'file_seconds', 'duration': duration,
                      'language': language, 'segments': rows, 'silences': silences,
                      'speech_passages': passages,
                      'recognition_windows': windows,
                      'timing': {'decode_seconds': decode_seconds,
                                 'speech_detection_seconds': detected - started - decode_seconds,
                                 'model_load_seconds': loaded - detected,
                                 'transcribe_seconds': time.monotonic() - loaded}}
            cache.parent.mkdir(parents=True, exist_ok=True)
            write_json(cache, result)
            reused = False
        if output:
            output.parent.mkdir(parents=True, exist_ok=True)
            write_json(output, result)
        print(json.dumps({'analysis': str(output or cache), 'cache': str(cache), 'cached': reused,
                          'duration_seconds': result['duration'], 'segments': len(result['segments']),
                          'elapsed_seconds': round(time.monotonic() - started, 3)}, indent=2))
    except (ValueError, OSError, RuntimeError) as exc:
        parser.exit(2, f'Error: {exc}\n')


if __name__ == '__main__':
    main()
