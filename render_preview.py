"""Render an optional SDR review video from an edit manifest, without changing FCPXML."""
import argparse
import json
from pathlib import Path
import subprocess
import tempfile

from fcpxml_tools import time_value


def run(command):
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode:
        raise ValueError('Preview render failed: ' + result.stderr[-2500:])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', required=True, type=Path, help='New .decisions.json from the XML editor')
    parser.add_argument('--output', required=True, type=Path, help='New .mp4 path; never overwritten')
    parser.add_argument('--max-side', type=int, default=960, help='Review resolution, preserving project aspect ratio')
    parser.add_argument('--tone-map', choices=('auto', 'none'), default='auto', help='Convert detected HLG/PQ footage for SDR review')
    args = parser.parse_args()
    try:
        output = args.output.expanduser().resolve()
        if output.exists() or output.suffix != '.mp4':
            raise ValueError('Choose a new output path ending in .mp4.')
        if args.max_side < 2:
            raise ValueError('--max-side must be at least 2.')
        edit = json.loads(args.manifest.read_text())
        if edit.get('schema_version') != 1 or not edit.get('segments'):
            raise ValueError('Use a manifest from the current XML editor, not a legacy example manifest.')
        if edit.get('subtitle_titles', 0):
            raise ValueError('This preview renderer does not render title/subtitle graphics. Preview or export the subtitled XML in Final Cut.')
        frame_duration = time_value(edit['frame_duration'])
        fps = 1 / frame_duration
        width, height = int(edit['video_format']['width']), int(edit['video_format']['height'])
        scale = min(1, args.max_side / max(width, height))
        width, height = max(2, round(width * scale / 2) * 2), max(2, round(height * scale / 2) * 2)
        import av
        import imageio_ffmpeg
        ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
        info = {}
        for segment in edit['segments']:
            source = segment.get('source_path')
            if segment['type'] != 'asset-clip':
                continue
            if not source or not Path(source).is_file():
                raise ValueError(f'Preview media is missing: {source or segment.get("ref")}')
            if source not in info:
                with av.open(source) as container:
                    video = next(iter(container.streams.video), None)
                    info[source] = {'video': video is not None, 'audio': bool(container.streams.audio),
                                    'hdr': video is not None and video.codec_context.color_trc in (16, 18)}
        with tempfile.TemporaryDirectory(prefix='fcp-preview-') as directory:
            directory = Path(directory)
            concat = []
            for index, segment in enumerate(edit['segments']):
                duration = time_value(segment['duration'])
                frames = duration / frame_duration
                if frames.denominator != 1 or frames <= 0:
                    raise ValueError('Manifest duration is not a positive whole number of project frames.')
                seconds = f'{float(duration):.12f}'
                command = [ffmpeg, '-hide_banner', '-loglevel', 'error', '-nostdin', '-y']
                source = segment.get('source_path')
                source_info = info.get(source, {})
                active = segment['type'] == 'asset-clip' and segment.get('enabled', True)
                video_on = active and source_info.get('video') and segment.get('src_enable') != 'audio'
                audio_on = active and source_info.get('audio') and segment.get('src_enable') != 'video'
                inputs = 0
                if video_on or audio_on:
                    command += ['-ss', f'{float(time_value(segment["file_start"])):.12f}', '-i', source]
                    video_input, audio_input = '0:v:0', '0:a:0'
                    inputs += 1
                if not video_on:
                    command += ['-f', 'lavfi', '-i', f'color=c=black:s={width}x{height}:r={fps}']
                    video_input = f'{inputs}:v:0'
                    inputs += 1
                if not audio_on:
                    command += ['-f', 'lavfi', '-i', 'anullsrc=r=48000:cl=stereo']
                    audio_input = f'{inputs}:a:0'
                conversion = ''
                if video_on and source_info['hdr'] and args.tone_map == 'auto':
                    conversion = ('zscale=t=linear:npl=100,format=gbrpf32le,zscale=p=bt709,'
                                  'tonemap=tonemap=mobius:param=0.3:desat=0,zscale=t=bt709:m=bt709:r=tv,')
                filters = (f'[{video_input}]setpts=PTS-STARTPTS,{conversion}fps={fps},'
                           f'scale={width}:{height}:force_original_aspect_ratio=decrease,'
                           f'pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,'
                           f'tpad=stop_mode=clone:stop_duration={float(frame_duration):.12f},'
                           f'trim=end_frame={int(frames)},format=yuv420p[v];'
                           f'[{audio_input}]asetpts=PTS-STARTPTS,aresample=48000,apad,'
                           f'atrim=duration={seconds},aformat=channel_layouts=stereo[a]')
                part = directory / f'part-{index:06d}.mov'
                command += ['-filter_complex', filters, '-map', '[v]', '-map', '[a]',
                            '-t', seconds, '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '20',
                            '-c:a', 'pcm_s16le', str(part)]
                run(command)
                concat += [f"file '{part.name}'", f'duration {seconds}']
            listing = directory / 'concat.txt'
            listing.write_text('\n'.join(concat) + '\n')
            # Encode audio once after joining PCM parts to avoid AAC padding at each cut.
            staged = directory / 'preview.mp4'
            run([ffmpeg, '-hide_banner', '-loglevel', 'error', '-nostdin', '-y', '-f', 'concat',
                 '-safe', '1', '-i', str(listing), '-c:v', 'copy', '-c:a', 'aac', '-b:a', '160k',
                 '-movflags', '+faststart', str(staged)])
            output.parent.mkdir(parents=True, exist_ok=True)
            import shutil
            shutil.copyfile(staged, output)
        print(json.dumps({'output': str(output), 'width': width, 'height': height,
                          'duration_seconds': float(time_value(edit['edited_duration']))}, indent=2))
    except (ValueError, OSError, KeyError, TypeError) as exc:
        parser.exit(2, f'Error: {exc}\n')


if __name__ == '__main__':
    main()
