#!/usr/bin/env python3

import argparse
import json
import math
import os
import re
import subprocess
import sys
import time
from os import path


def main():
    parser = argparse.ArgumentParser(
        prog='webrtc-vmaf',
    )
    parser.add_argument('input', nargs='+')
    parser.add_argument('--codec', default='h264')
    parser.add_argument('--framerate', default=30, type=int)
    parser.add_argument('--width', type=int)
    parser.add_argument('--height', type=int)
    parser.add_argument('--bitrate', action="append", type=int, required=True)
    args = parser.parse_args()

    for bitrate in args.bitrate:
        print(f"computing VMAF for {args.codec} at {bitrate}")
        # scores between different files
        score_total = 0
        fps_total = 0
        for input in args.input:
            score, fps = vmaf_for_input(input,
                                        codec=args.codec,
                                        bitrate=bitrate,
                                        framerate=args.framerate,
                                        width=args.width,
                                        height=args.height,
                                        )
            print(f"  {path.basename(input)}: {score} \tfps: {math.floor(fps)}")
            score_total += score
            fps_total += fps
        num_inputs = len(args.input)
        avg_fps = 0
        if num_inputs > 0:
            avg_fps = math.floor(fps_total / num_inputs)
        if num_inputs > 1:
            print(f"average VMAF: {score_total / num_inputs} \tfps: {avg_fps}")
        else:
            print(f"VMAF: {score_total} \tfps: {avg_fps}")


def vmaf_for_input(input, codec, bitrate, framerate, width=None, height=None):
    """encodes a version of input with specified encoding and bitrate, returning its VMAF score"""
    width, height, duration = get_video_info(input, width, height)

    os.makedirs('tmp_vmaf', exist_ok=True)
    basename = path.basename(input)
    file_name, _ = path.splitext(basename)
    video_output = path.join(
        'tmp_vmaf', f'{file_name}_{codec}_{width}x{height}_{bitrate}.mkv')
    image_output = path.join(
        'tmp_vmaf', f'{file_name}_{codec}_{width}x{height}_{bitrate}.jpeg')

    start_time = time.time()
    encode_file(input, video_output,
                codec=codec,
                width=width,
                height=height,
                bitrate=bitrate,
                framerate=framerate,
                )
    time_spent = time.time() - start_time

    capture_snapshot(video_output, image_output)

    score = compute_vmaf(input, video_output,
                         width=width,
                         height=height,
                         framerate=framerate,
                         )

    return (score, (duration * framerate) / time_spent)


def get_video_info(input, width, height):
    command = [
        'ffprobe',
        '-v', 'error',
        '-show_format',
        '-show_streams',
        '-select_streams', 'v:0',
        '-of', 'json',
        input
    ]

    output = subprocess.run(command, capture_output=True, text=True)

    if output.stderr:
        raise Exception(f"Error running ffprobe: {output.stderr}")

    output_json = json.loads(output.stdout)
    if not width:
        width = output_json['streams'][0]['width']
    if not height:
        height = output_json['streams'][0]['height']
    duration = '0'
    if 'duration' in output_json['streams'][0]:
        duration = output_json['streams'][0]['duration']
    elif 'duration' in output_json['format']:
        duration = output_json['format']['duration']
    return width, height, float(duration)


def encode_file(input, output, codec, width, height, bitrate, framerate):
    bitrate_str = f'{bitrate}K'
    filters = f'fps={framerate},scale={width}x{height}:flags=bicubic,format=yuv420p'

    command = [
        'ffmpeg',
        '-i', input,
        '-b:v', bitrate_str,
        '-filter:v', filters,
        '-threads', '8',
        '-an',  # This option tells FFmpeg to discard any audio
        '-y',
        '-loglevel', 'error',
        '-c:v',  # codec will follow
    ]

    # match encoding settings to what libwebrtc uses
    # these flags are obtained from libwebrtc source code:
    # https://github.com/webrtc-sdk/webrtc/tree/m104_release/modules/video_coding/codecs
    if codec == 'h264':
        # libwebrtc bundles OpenH264, which isn't available with FFmpeg
        # using libx264 as a substitute
        command.extend([
            'libx264',
            '-preset', 'veryfast',
            '-tune', 'zerolatency',
            '-profile:v', 'baseline',
        ])
    elif codec == 'vp8':
        command.extend([
            'libvpx',
            '-minrate', bitrate_str,
            '-maxrate', bitrate_str,
            '-deadline', 'realtime',
            # default complexity speed
            # https://github.com/webrtc-sdk/webrtc/blob/m104_release/modules/video_coding/codecs/vp8/libvpx_vp8_encoder.cc#L593C8-L593C25
            '-cpu-used', '-6',
            '-qmax', '52',
            '-qmin', '2',
        ])
    elif codec == 'vp9':
        command.extend([
            'libvpx-vp9',
            '-minrate', bitrate_str,
            '-maxrate', bitrate_str,
            '-deadline', 'realtime',
            # Chrome's performance settings varies, but most resolutions, it
            # uses 7: https://github.com/webrtc-sdk/webrtc/blob/m104_release/modules/video_coding/codecs/vp9/libvpx_vp9_encoder.cc#L2013
            '-speed', '7',
            '-row-mt', '1',
            '-tile-columns', '3',
            '-tile-rows', '1',
            '-frame-parallel', '1',
            '-qmax', '52',
            '-qmin', '2',
        ])
    elif codec == 'av1':
        command.extend([
            'libaom-av1',
            '-usage', 'realtime',
            '-minrate', bitrate_str,
            '-maxrate', bitrate_str,
            '-cpu-used', '8',
            '-row-mt', '1',
            '-qmax', '52',
            '-qmin', '10',
            '-aq-mode', '3',
            '-enable-global-motion', '0',
            '-enable-intrabc', '0',
            '-enable-restoration', '0',
            '-enable-interintra-comp', '0',
            '-enable-interintra-wedge', '0',
            '-refs', '3',
        ])
        if height >= 360:
            command.extend(['-tile-columns', '3'])
    else:
        raise Exception(
            "Unsupported codec. Please use one of these: 'h264', 'vp8', 'vp9', 'av1'")

    command.append(output)

    output = subprocess.run(command, capture_output=True, text=True)

    if output.stderr:
        raise Exception(f"Error running ffmpeg to encode: {output.stderr}")


def compute_vmaf(input, output, width, height, framerate):
    command = [
        'ffmpeg',
        '-i', input,
        '-i', output,
        '-filter_complex', f'[0:v]scale={width}:{height}:flags=bicubic,framerate={framerate},setpts=PTS-STARTPTS[ref];[1:v]scale={width}:{height}:flags=bicubic,framerate={framerate},setpts=PTS-STARTPTS[distorted];[distorted][ref]libvmaf=n_threads=8',
        '-f', 'null',
        '-',
    ]

    # print(f'running command {" ".join(command)}')
    output = subprocess.run(command, capture_output=True, text=True)
    match = re.search(r"VMAF score: (\d+\.\d+)", output.stderr)
    if match:
        score = float(match.group(1))
        return score
    else:
        raise Exception(f"No score found in the string: {output.stderr}")


def capture_snapshot(input, output):
    command = [
        'ffmpeg',
        '-ss', '00:00:05.000',
        '-i', input,
        '-vframes', '1',
        '-y',
        '-loglevel', 'error',
        output,
    ]

    output = subprocess.run(command, capture_output=True, text=True)

    if output.stderr:
        raise Exception(
            f"Error running ffmpeg to create snapshot: {output.stderr}")


main()
