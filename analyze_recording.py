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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True, type=Path)
    parser.add_argument('--output', type=Path, help='Optional new copy of cached analysis JSON')
    parser.add_argument('--model', default='small.en')
    parser.add_argument('--language', default='en', help='Language code or auto (requires multilingual model)')
    parser.add_argument('--threads', type=int, default=6)
    parser.add_argument('--beam-size', type=int, default=5)
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
        if args.threads < 1 or args.beam_size < 1 or args.silence_min <= 0:
            raise ValueError('Threads, beam size, and minimum silence must be positive.')
        if args.model.endswith('.en') and args.language != 'en':
            raise ValueError('English-only models require --language en. Select a multilingual model for other languages.')
        output = args.output.expanduser().resolve() if args.output else None
        if output and output.exists():
            raise ValueError('Output already exists; choose a new path.')
        stat = source.stat()
        settings = {'model': args.model, 'language': args.language, 'compute_type': 'int8',
                    'threads': args.threads, 'beam_size': args.beam_size, 'word_timestamps': True,
                    'vad_min_silence_ms': 350, 'detect_silence': args.detect_silence,
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
            from faster_whisper import WhisperModel
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
                model = WhisperModel(args.model, device='cpu', compute_type='int8',
                                     cpu_threads=args.threads, download_root=str(ROOT / '.cache' / 'models'),
                                     local_files_only=not args.allow_model_download)
                loaded = time.monotonic()
                segments, info = model.transcribe(str(wav),
                    language=None if args.language == 'auto' else args.language,
                    beam_size=args.beam_size, word_timestamps=True, vad_filter=True,
                    vad_parameters={'min_silence_duration_ms': 350}, condition_on_previous_text=False)
                rows = [{'start': s.start, 'end': s.end, 'text': s.text,
                         'words': [{'start': w.start, 'end': w.end, 'word': w.word,
                                    'probability': w.probability} for w in s.words or []]} for s in segments]
            silences = []
            pending = None
            for line in decode.stderr.splitlines():
                match = re.search(r'silence_start: (-?[\d.]+)', line)
                if match:
                    pending = max(0.0, float(match.group(1)))
                match = re.search(r'silence_end: (-?[\d.]+)', line)
                if match and pending is not None:
                    silences.append({'start': pending, 'end': min(info.duration, float(match.group(1)))})
                    pending = None
            if pending is not None and pending < info.duration:
                silences.append({'start': pending, 'end': info.duration})
            now = source.stat()
            if (now.st_size, now.st_mtime_ns, now.st_ctime_ns) != (stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns):
                raise ValueError('Source changed during analysis; result was not cached.')
            result = {'schema_version': 1, 'identity': identity, 'source': str(source),
                      'coordinate_space': 'file_seconds', 'duration': info.duration,
                      'language': info.language, 'segments': rows, 'silences': silences,
                      'timing': {'decode_seconds': decode_seconds,
                                 'model_load_seconds': loaded - started - decode_seconds,
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
