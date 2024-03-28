#!/usr/bin/env -S python3 -u
import os
import argparse
import shlex
import subprocess
import sys
import time
import random
import signal
import json

import mqtt
import config

def main():
    env = dict()
    env["stream"] = os.getenv("stream_key")
    env["source"] = os.getenv("transcoding_source")
    env["sink"] = os.getenv("transcoding_sink")
    env["type"] = os.getenv("type", "all")
    env["output"] = os.getenv("output")
    env["passthrough"] = os.getenv("passthrough", "no") == "yes"
    env["restream"] = os.getenv("restream", "none")
    env["framerate"] = os.getenv("framerate", 25)
    env["vaapi_dev"], env["vaapi_driver"], env["vaapi_features"] = select_vaapi_dev()
    # env["icecast_password"] = config.icecast_password
    if hasattr(config, "upload_user"):
        env["upload_user"] = config.upload_user
    if hasattr(config, "upload_pass"):
        env["upload_pass"] = config.upload_pass
    if hasattr(config, "sink"):
        env["sink"] = config.sink

    parser = argparse.ArgumentParser(description="Transcode voc stream")
    parser.add_argument("--stream", help="stream key")
    parser.add_argument("--source", help="transcoding input")
    parser.add_argument("--sink", help="transcoding sink host")
    parser.add_argument("--type", help="transcode type",
                        choices=["h264-only", "all"])
    parser.add_argument("-o", "--output", help="output type",
                        choices=["icecast", "direct", "null"])
    parser.add_argument("-progress", help="pass to ffmpeg progress")
    parser.add_argument("--vaapi-features", action="append",
                        help="override vaapi features")
    parser.add_argument("--vaapi-device", help="override vaapi device")
    parser.add_argument("-v", "--verbose", help="set verbosity",
                        default="warning")
    parser.add_argument("--restart", help="restart on failure",
                        default=False, action="store_true")
    args = parser.parse_args()

    # override environment with arguments
    enable_mqtt = False
    if hasattr(config, "mqtt_enabled"):
        enable_mqtt = config.mqtt_enabled
    check_arg(env, "output", args.output)
    if env["output"] == "null":
        enable_mqtt = False
        env["stream"] = ""
        env["sink"] = ""
    check_arg(env, "stream", args.stream)
    check_arg(env, "source", args.source)
    check_arg(env, "sink", args.sink)
    check_arg(env, "type", args.type)

    # handle external ffmpeg-progress
    progress = ""
    if args.progress is not None:
        print("using progress", args.progress)
        progress = f"-progress {args.progress}"

    # override vaapi variables
    if args.vaapi_device is not None:
        print("override vaapi device", args.vaapi_device)
        env["vaapi_dev"] = args.vaapi_device

    if args.vaapi_features is not None:
        print("override", args.vaapi_features)
        env["vaapi_features"] = args.vaapi_features

    print("using vaapi driver", env["vaapi_driver"])
    env_vars = {"LIBVA_DRIVER_NAME": env["vaapi_driver"]}
    os.environ.update(env_vars)

    with mqtt.Client(enable_mqtt) as client:
        client.info(f"Transcoding for {env['stream']}%sstarted…" % (" (passthrough) " if env["passthrough"] else " "))
        try:
            mainloop(env, args.restart, progress, args.verbose)
        except ExitException:
            pass
        client.info(f"Transcoding for {env['stream']} stopped…")


def check_arg(env, key, flag):
    """Selectively overwrite environment variables if flag is set"""
    if flag is not None:
        env[key] = flag
    if key not in env:
        print(f"Param {key} must be set")
        sys.exit(1)


def parse_vainfo(driver: str, device: str):
    """
    calls vainfo for a specific device and driver and returns a list of en/decoder
    :param driver: va driver name
    :param device: path of render device
    :return: list of en/decoder supported
    """
    env_vars = {"LIBVA_DRIVER_NAME": driver}
    os.environ.update(env_vars)
    try:
        vainfo = subprocess.check_output(["vainfo", "--display", "drm", "--device", device],
                                         stderr=subprocess.DEVNULL).decode().split("\n")
    except subprocess.CalledProcessError:
        return []
    except FileNotFoundError:
        print("vainfo was not found. Please install vainfo")
        return []

    features = []
    for line in vainfo:
        if line.strip().startswith("VA"):
            line = line.strip().replace(" ", "").replace("\t", "")
            if line == "VAProfileH264High:VAEntrypointEncSlice":
                features.append("h264-enc")
            elif line == "VAProfileJPEGBaseline:VAEntrypointEncPicture":
                features.append("jpeg-enc")
            elif line == "VAProfileVP9Profile0:VAEntrypointEncSlice":
                features.append("vp9-enc")
    return features


def select_vaapi_dev():
    """
    Selects and returns the most appropriate VAAPI device and driver.
    """
    devnum = 128
    dev = f"/dev/dri/renderD{devnum}"
    bestdriver = None
    bestfeatures = []
    for driver in ["iHD", "i965", "radeonsi", "nouveau"]:
        features = parse_vainfo(driver, dev)
        if len(features) <= len(bestfeatures):
            continue
        bestdriver = driver
        bestfeatures = features

    if bestdriver is None:
        return "", "", []

    return dev, bestdriver, bestfeatures


class ExitException(Exception):
    def __init__(self, signum):
        super(ExitException, self).__init__(f"Caught signal {signum}")


def signal_handler(signum, frame):
    raise ExitException(signum)


def join_all(L):
    return ' '.join([join_all(x) if (type(x) is list or type(x) is tuple) else x for x in L])


def mainloop(env, do_restart, progress="", verbosity="warning"):
    """
    Runs ffmpeg in a loop with
    """
    timer = 0
    sleep_time = 0

    # install signal handlers
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGQUIT, signal_handler)

    # run ffmpeg
    while True:
        try:
            # probe to determine video settings
            probe_result = probe(env["source"])
            if probe_result is None:
                sleep_time = do_sleep(timer, sleep_time)
                continue

            arguments = template_command(env, probe_result)
            arguments.insert(0, f"""/usr/bin/env ffmpeg -hide_banner {progress} -v {verbosity} -nostdin -y
                """)
            print("execute\n", join_all(arguments).replace("\n", " \\\n"))
            call = shlex.split(join_all(arguments))

            timer = time.time()
            subprocess.check_call(call, stdout=sys.stdout, stderr=sys.stderr)
        except subprocess.CalledProcessError:
            pass

        if not do_restart:
            return

        sleep_time = do_sleep(timer, sleep_time)

# Probe source and determine codecs/stream-type
def probe(source):
    print("probing")
    try:
        cmd = f"ffprobe -v quiet -print_format json -show_format -show_streams {source}"
        buf = subprocess.check_output(shlex.split(cmd), timeout=30)
    except subprocess.CalledProcessError as err:
        print("Probe failed", err)
        return

    result = json.loads(buf.decode("utf-8"))
    video_tracks = list(filter(lambda stream: stream['codec_type'] == "video", result['streams']))
    audio_tracks = list(filter(lambda stream: stream['codec_type'] == "audio", result['streams']))

    has_audio = len(audio_tracks) > 0
    has_video = len(video_tracks) > 0
    main_video = None
    main_audio = None
    slide_video = None

    # may switch to detection by tags
    if len(video_tracks) > 0:
        main_video = video_tracks[0]
    if len(audio_tracks) > 0:
        main_audio = audio_tracks[0]
    return {
        "has_video": has_video,
        "has_audio": has_audio,
        "audio_only": has_audio and not has_video,
        "video_only": has_video and not has_audio,
        "main_video": main_video,
        "slide_video": slide_video,
        "main_audio": main_audio,
        "audios": audio_tracks,
        "videos": video_tracks,
    }


def template_command(env, probed):
    source = env.get("source")
    input_opts = ""
    if source.startswith("rtmp://"):
        input_opts = "-f live_flv"
    elif source.startswith("srt://"):
        input_opts = "-f mpegts"
    env["input_tmpl"] = f"{input_opts} -i '{env['source']}'"

    # build command
    if probed.get("audio_only"):
        print("pipeline audio-only")
        return pipeline_audio_only(env, probed)
    elif env.get("type") == "h264-only":
        print("pipeline h264-only")
        return pipeline_h264_only(env, probed)
    else:
        print("pipeline all")
        return pipeline_all(env, probed)


def random_duration():
    return 2 + random.random()


def do_sleep(timer, sleep_time):
    now = time.time()
    if now - timer < 10:
        sleep_time = min(10, sleep_time + random_duration())
    else:
        sleep_time = random_duration()
    print(f"sleeping for {sleep_time:0.1f}s")
    time.sleep(sleep_time)
    return sleep_time


########################
# Transcoding Pipelines
########################

def pipeline_audio_only(env, probed):
    input_tmpl = env["input_tmpl"]
    stream = env["stream"]

    return [
        f"{input_tmpl}",
        transcode_audio(env, probed),
    ]

def pipeline_all(env, probed):
    input_tmpl = env["input_tmpl"]
    vaapi_dev = env["vaapi_dev"]
    vaapi_features = env["vaapi_features"]

    vaapi_opts = f"""
    -init_hw_device vaapi=transcode:{vaapi_dev}
    -hwaccel vaapi
    -hwaccel_output_format vaapi
    -hwaccel_device transcode
    """

    # all vaapi
    if "vp9-enc" in vaapi_features:
        print("running all vaapi transcode")
        return [
            f"""
            {vaapi_opts}
            {input_tmpl}
            -filter_hw_device transcode
            -filter_complex "
                [0:v:0] split=3 [_hd1][_hd2][_hd3];
                [_hd2] hwdownload [hd_vp9];
                [_hd1] scale_vaapi=1024:576,split [sd_h264][sd_vp9];
                [_hd3] framestep=step=500,split [poster][_poster];
                [_poster] scale_vaapi=w=288:h=-1 [thumb]"
            """,
            transcode_h264(env, probed, use_vaapi=True),
            transcode_vp9(env, probed, use_vaapi=True),
            transcode_thumbs(env, probed, use_vaapi=True),
            transcode_audio(env, probed),
        ]

    # h264 + software vp9
    elif "h264-enc" in vaapi_features and "jpeg-enc" in vaapi_features:
        print("falling back to vaapi transcode with software vp9")
        return [
            f"""
            {vaapi_opts}
            {input_tmpl}
            -filter_hw_device transcode
            -filter_complex "
                [0:v:0] split=3 [_hd1][_hd2][_hd3];
                [_hd2] hwdownload [hd_vp9];
                [_hd1] scale_vaapi=1024:576,split [sd_h264][_sd2];
                [_sd2] hwdownload [sd_vp9];
                [_hd3] framestep=step=500,split [poster][_poster];
                [_poster] scale_vaapi=w=288:h=-1 [thumb]"
            """,
            transcode_h264(env, probed, use_vaapi=True),
            transcode_vp9(env, probed),
            transcode_thumbs(env, probed, use_vaapi=True),
            transcode_audio(env, probed),
        ]

    # software
    else:
        print("falling back to software transcode")
        return [
            f"""
            {input_tmpl}
            -filter_complex "
                [0:v:0] split [_hd1][_hd2];
                [_hd1] scale=1024:576,split [sd_h264][sd_vp9];
                [_hd2] framestep=step=500,split [poster][_poster];
                [_poster] scale=w=288:h=-1 [thumb]"
            """,
            transcode_h264(env, probed),
            transcode_vp9(env, probed, hd_input="0:v:0"),
            transcode_thumbs(env, probed),
            transcode_audio(env, probed),
        ]


def pipeline_h264_only(env, probed):
    input_tmpl = env["input_tmpl"]
    vaapi_dev = env["vaapi_dev"]
    vaapi_features = env["vaapi_features"]

    # vaapi
    if "h264-enc" in vaapi_features and "jpeg-enc" in vaapi_features:
        print("using vaapi transcode")
        return [
            f"""
            -init_hw_device vaapi=transcode:{vaapi_dev}
            -hwaccel vaapi
            -hwaccel_output_format vaapi
            -hwaccel_device transcode
            {input_tmpl}
            -filter_hw_device transcode
            -filter_complex "
                [0:v:0] split [_hd1][_hd2];
                [_hd1] scale_vaapi=1024:576 [sd_h264];
                [_hd2] framestep=step=500,split [poster][_poster];
                [_poster] scale_vaapi=w=288:h=-1 [thumb]"
            """,
            transcode_h264(env, probed, use_vaapi=True),
            transcode_thumbs(env, probed, use_vaapi=True),
            transcode_audio(env, probed),
        ]
    # software
    else:
        print("falling back to software transcode")
        return [
            f"""
            {input_tmpl}
            -filter_complex "
                [0:v:0] split [_hd1][_hd2];
                [_hd1] scale=1024:576 [sd_h264];
                [_hd2] framestep=step=500,split [poster][_poster];
                [_poster] scale=w=288:h=-1 [thumb]"
            """,
            transcode_h264(env, probed),
            transcode_thumbs(env, probed),
            transcode_audio(env, probed),
        ]


############################
# Generic Helpers
############################


def output_null(env=None, slug=None):
    return "-max_muxing_queue_size 2000 -f null -"


def output_matroska(env, slug):
    return f"""
    -fflags +genpts
    -max_muxing_queue_size 2000
    -f matroska
    -password {config.icecast_password}
    -content_type video/webm
    "icecast://{env["sink"]}/{slug}"
"""

def _video_label(track, index, passthrough):
    if track is not None and "tags" in track and len(track["tags"].get("title", "")) > 0:
        return track["tags"].get("title")
    if index == 0:
        return "HD"
    if index == 1:
        return "SD"
    if index == 2 and passthrough:
        return "Source"
    if (index == 3 and passthrough) or index == 2:
        return "Slides"
    return "Unnamed"


def _audio_label(track, index):
    if track is not None and "tags" in track and len(track["tags"].get("title", "")) > 0:
        return track["tags"].get("title")
    if index == 0:
        return "Native"
    if index == 1:
        return "Translated"
    return f"Translated-{index}"


############################
# Codecs
############################

#######
# h264
#######
def transcode_h264(env, probed, hd_input="0:v:0", sd_input="[sd_h264]", use_vaapi=False):
    res = []
    if use_vaapi:
        res += [encode_h264_vaapi(hd_input, sd_input, hd_passthrough=env["passthrough"], framerate=env["framerate"], codec=probed["main_video"]["codec_name"])]
    else:
        res += [encode_h264_software(hd_input, sd_input, hd_passthrough=env["passthrough"], framerate=env["framerate"], codec=probed["main_video"]["codec_name"])]
    res += [
        encode_h264_audio(env, probed),
        output_h264(env, probed),
    ]
    if env["restream"] != "none":
        if use_vaapi:
            res += [encode_restream_vaapi(hd_input)]
        else:
            res += [encode_restream_software(hd_input)]
        res += [
            output_restream(env, probed),
        ]
    return res


def encode_h264_vaapi(hd_input, sd_input, hd_passthrough, framerate, codec):
    snippet = f"""
    -map '{hd_input}'
        -metadata:s:v:0 title="HD"
        -c:v:0 h264_vaapi
            -r:v:0 {framerate}
            -keyint_min:v:0 75
            -g:v:0 75
            -b:v:0 2800k
            -bufsize:v:0 8400k
            -flags:v:0 +cgop
    """

    snippet += f"""
    -map '{sd_input}'
        -metadata:s:v:1 title="SD"
        -c:v:1 h264_vaapi
            -r:v:1 {framerate}
            -keyint_min:v:1 75
            -g:v:1 75
            -b:v:1 800k
            -bufsize:v:1 2400k
            -flags:v:1 +cgop
    """

    if hd_passthrough:
        snippet += f"""
        -map '{hd_input}'
            -metadata:s:v:2 title="Source"
            -c:v:2 copy
                -b:v:2 8000k
        """
        if codec == "h264":
            snippet += """
            -bsf:v:2 h264_mp4toannexb
            """
        elif codec == "h265":
            snippet += """
            -bsf:v:2 hevc_mp4toannexb
            """
        snippet += """
        -map '0:v:1?'
            -metadata:s:v:3 title="Slides"
            -c:v:3 copy
        """
    else:
        snippet += """
        -map '0:v:1?'
            -metadata:s:v:2 title="Slides"
            -c:v:2 copy
        """

    return snippet

def encode_h264_software(hd_input, sd_input, framerate, codec, hd_passthrough=False):
    snippet = f"""
    -map '{hd_input}'
        -metadata:s:v:0 title="HD"
        -c:v:0 libx264
            -maxrate:v:0 2800k
            -crf:v:0 21
            -bufsize:v:0 5600k
            -pix_fmt:v:0 yuv420p
            -profile:v:0 main
            -r:v:0 {framerate}
            -keyint_min:v:0 75
            -g:v:0 75
            -flags:v:0 +cgop
            -preset:v:0 veryfast
    """

    snippet += f"""
    -map '{sd_input}'
        -metadata:s:v:1 title="SD"
        -c:v:1 libx264
            -maxrate:v:1 800k
            -crf:v:1 23
            -bufsize:v:1 3600k
            -pix_fmt:v:1 yuv420p
            -profile:v:1 main
            -r:v:1 {framerate}
            -keyint_min:v:1 75
            -g:v:1 75
            -flags:v:1 +cgop
            -preset:v:1 veryfast
    """

    if hd_passthrough:
        snippet += f"""
        -map '{hd_input}'
            -metadata:s:v:2 title="Source"
            -c:v:2 copy
                -b:v:2 8000k
        """
        if codec == "h264":
            snippet += """
            -bsf:v:2 h264_mp4toannexb
            """
        elif codec == "h265":
            snippet += """
            -bsf:v:2 hevc_mp4toannexb
            """
        snippet += """
        -map '0:v:1?'
            -metadata:s:v:3 title="Slides"
            -c:v:3 copy
        """
    else:
        snippet += """
        -map '0:v:1?'
            -metadata:s:v:2 title="Slides"
            -c:v:2 copy
        """

    return snippet

def encode_restream_vaapi(hd_input):
    return f"""
    -c:v h264_vaapi
    -map '{hd_input}'
        -keyint_min:v 75
        -g:v 75
        -b:v 6000k
        -bufsize:v 12800k
        -flags:v +cgop
    """

def encode_restream_software(hd_input):
    return f"""
    -c:v libx264
    -map '{hd_input}'
        -maxrate:v 6000k
        -crf:v 21
        -bufsize:v 12800k
        -pix_fmt:v yuv420p
        -profile:v main
        -keyint_min:v 75
        -g:v 75
        -flags:v +cgop
        -preset:v veryfast
    """


# Use passthrough for AAC streams, otherwise reencode to 2Ch AAC
def _h264_audio_track(track, in_index, map_index):
    label = _audio_label(track, in_index)
    if track.get("codec_name") == "aac" and track.get("profile") == "LC" and track.get("channels") == 2:
        return f"""
    -map 0:a:{in_index} -c:a:{map_index} copy"""

    # reencode
    rate = min(int(track.get("sample_rate", "48000")), 48000)
    return f"""
    -map 0:a:{in_index} -c:a:{map_index} aac -ac:a:{map_index} 2 -b:a:{map_index} 192k -ar:a:{map_index} {rate}"""


def encode_h264_audio(env, probed):
    res = []
    # mux variants for hls output
    if env["output"] == "direct":
        # mux
        numVideos = len(probed["videos"]) + 1 # orig HD, orig slides + transcoded SD

        # mux native once for every video
        map_index = 0
        track = probed["audios"][0]
        if env["passthrough"]:
            numVideos += 1
        for _ in range(numVideos):
            res += [_h264_audio_track(track, 0, map_index)]
            map_index += 1

        # mux alternate audios
        for in_index, track in enumerate(probed["audios"]):
            res += [_h264_audio_track(track, in_index, map_index)]
            map_index += 1

    else:
        # mux everything once for icecast and null
        for index, track in enumerate(probed["audios"]):
            res += [_h264_audio_track(track, index, index)]
    return res


def output_h264(env, probed):
    stream = env.get("stream")
    if env["output"] == "null":
        return output_null()

    # icecast matroska output for separate fanout
    elif env["output"] == "icecast":
        return output_matroska(env, f"{stream}_h264"),

    # hls output
    elif env["output"] == "direct":
        user = env.get("upload_user")
        password = env.get("upload_pass")
        output = env.get("sink")
        auth = ""
        auth_opt = ""
        if user is not None and password is not None:
            auth = f"{user}:{password}@"
            auth_opt = "-auth_type basic"

        stream_map = []
        audio_idx = 0

        numVideos = len(probed["videos"]) + 1 # orig HD, orig slides + transcoded SD

        if env["passthrough"]:
            numVideos += 1
        for i in range(numVideos):
            label = _video_label(None, i, env["passthrough"])
            stream_map += [f"v:{i},a:{audio_idx},agroup:main,name:{label}"]
            audio_idx += 1

        for i in range(len(probed["audios"])):
            label = _audio_label(None, i)
            default = ""
            if i == 0:
                default = ",default:yes"
            stream_map += [f"a:{audio_idx},agroup:main,name:{label},language:{label}{default}"]
            audio_idx += 1

        return f"""
        -f hls -max_muxing_queue_size 2000
            -http_persistent 1 -timeout 5 -ignore_io_errors 1
            -hls_flags delete_segments+second_level_segment_index {auth_opt}
            -hls_time 3 -hls_list_size 200
            -var_stream_map "{' '.join(stream_map)}"
            -strftime 1
            -hls_segment_filename "http://{auth}{output}/hls/{stream}/%v_%s_%%04d.ts"
            -master_pl_name native_hd.m3u8 -master_pl_publish_rate 10
            -method PUT "http://{auth}{output}/hls/{stream}/%v.m3u8"
        """

def output_restream(env, probed):
    if env["restream"].startswith("rtmp://"):
        return f"""
        -c:a copy -map 0:a:0
        -f flv {env['restream']}
        """

######
# VP9
######

def transcode_vp9(env, probed, hd_input="[hd_vp9]", sd_input="[sd_vp9]", use_vaapi=False):
    res = []
    if use_vaapi:
        res += [encode_vp9_vaapi(hd_input, sd_input)]
    else:
        res += [encode_vp9_software(hd_input, sd_input)]
    res += [
        encode_vp9_audio(env, probed),
        output_vp9(env, probed),
    ]
    return res

def encode_vp9_vaapi(hd_input="[hd_vp9]", sd_input="[sd_vp9]"):
    return f"""
    -map '{hd_input}'
        -metadata:s:v:0 title="HD"
        -c:v:0 libvpx-vp9
            -deadline:v:0 realtime
            -cpu-used:v:0 8
            -threads:v:0 6
            -frame-parallel:v:0 1
            -tile-columns:v:0 2

            -r:v:0 25
            -keyint_min:v:0 75
            -g:v:0 75
            -crf:v:0 23
            -row-mt:v:0 1
            -b:v:0 2800k -maxrate:v:0 2800k
            -bufsize:v:0 8400k

    -map '{sd_input}'
        -metadata:s:v:1 title="SD"
        -c:v:1 vp9_vaapi
            -r:v:1 25
            -keyint_min:v:1 75
            -g:v:1 75
            -b:v:1 800k
            -bufsize:v:1 2400k

    -map '0:v:1?'
        -metadata:s:v:2 title="Slides"
        -c:v:2 vp9_vaapi
            -keyint_min:v:2 15
            -g:v:2 15
            -b:v:2 100k
            -bufsize:v:2 750k
"""


def encode_vp9_software(hd_input="[hd_vp9]", sd_input="[sd_vp9]"):
    return f"""
        -c:v libvpx-vp9
        -deadline:v realtime -cpu-used:v 8
        -frame-parallel:v 1 -crf:v 23 -row-mt:v 1

    -map '{hd_input}'
        -metadata:s:v:0 title="HD"
        -threads:v:0 6
        -tile-columns:v:0 2
        -r:v:0 25
        -keyint_min:v:0 75
        -g:v:0 75
        -b:v:0 2800k -maxrate:v:0 2800k
        -bufsize:v:0 8400k

    -map '{sd_input}'
        -metadata:s:v:1 title="SD"
        -threads:v:1 6
        -tile-columns:v:1 2
        -r:v:1 25
        -keyint_min:v:1 75
        -g:v:1 75
        -b:v:1 800k -maxrate:v:1 800k
        -bufsize:v:1 2400k

    -map '0:v:1?'
        -metadata:s:v:2 title="Slides"
        -threads:v:2 4
        -tile-columns:v:2 1
        -keyint_min:v:2 15
        -g:v:2 15
        -b:v:2 100k -maxrate:v:2 100k
        -bufsize:v:2 750k
"""

def _vp9_audio_track(track, in_index, map_index):
    label = _audio_label(track, in_index)

    rate = min(int(track.get("sample_rate", "48000")), 48000)
    return f"""
    -map 0:a:{in_index} -c:a:{map_index} libopus -ac:a:{map_index} 2 -b:a:{map_index} 192k -ar:a:{map_index} {rate}"""


def encode_vp9_audio(env, probed):
    res = ["-af 'aresample=async=1:min_hard_comp=0.100000:first_pts=0'"]
    for index, track in enumerate(probed["audios"]):
        res += [_vp9_audio_track(track, index, index)]
    return res

def _calculate_dash_adaptation_sets(probed):
    # Video Tracks
    sets = [f"id=0,streams=v"]
    set_id = 1
    map_index = len(probed["videos"])+1

    # Audio Tracks
    for i, track in enumerate(probed["audios"]):
        sets += [f"id={set_id},streams={map_index}"]
        set_id += 1
        map_index += 1

    return sets

def output_vp9(env, probed):
    stream = env.get("stream")
    if env["output"] == "null":
        return output_null()

    # icecast matroska output for separate fanout
    elif env["output"] == "icecast":
        return output_matroska(env, f"{stream}_vpx")

    # MPEG-DASH output
    elif env["output"] == "direct":
        user = env.get("upload_user")
        password = env.get("upload_pass")
        output = env.get("sink")
        auth = ""
        auth_opt = ""
        if user is not None and password is not None:
            auth = f"{user}:{password}@"
            auth_opt = "-auth_type basic"

        adaptation_sets = _calculate_dash_adaptation_sets(probed)

        return f"""
	-f dash -max_muxing_queue_size 2000
        -window_size 201 -extra_window_size 10
        -seg_duration 3
        -dash_segment_type webm
        -init_seg_name 'init_$RepresentationID$.webm'
        -media_seg_name 'segment_$RepresentationID$_$Number$.webm'
        -adaptation_sets '{" ".join(adaptation_sets)}'
        http://{auth}{output}/dash/{stream}/manifest.mpd
    """




######################
# Thumbnails
######################

def transcode_thumbs(env, probed, poster_input="[poster]", thumb_input="[thumb]", use_vaapi=False):
    res = []
    codec = "-c:v mjpeg -pix_fmt:v yuvj420p"
    if use_vaapi:
        codec = "-c:v mjpeg_vaapi"

    idx = 0
    res += [f"{codec} -map '{poster_input}' -metadata:s:v:{idx} title='Poster'"]
    idx += 1
    if env["output"] == "direct":
        res += [output_thumbs_upload(env, "poster.jpeg")]

    res += [f"{codec} -map '{thumb_input}' -metadata:s:v:{idx} title='Thumbnail'"]
    idx += 1
    if env["output"] == "direct":
        res += [output_thumbs_upload(env, "thumb.jpeg")]

    if env["output"] == "icecast":
        stream = env.get("stream")
        res += [output_matroska(env, f"{stream}_thumbnail")]
    elif env["output"] == "null":
        res += [output_null()]

    return res

def output_thumbs_upload(env, filename):
    stream = env.get("stream")
    user = env.get("upload_user")
    password = env.get("upload_pass")
    output = env.get("sink")
    auth = ""
    auth_opt = ""
    if user is not None and password is not None:
        auth = f"{user}:{password}@"
        auth_opt = ":auth_type=basic"
    return f"""
    -f image2 -max_muxing_queue_size 2000
        -update 1 -protocol_opts method=PUT:multiple_requests=1{auth_opt}
        'http://{auth}{output}/thumbnail/{stream}/{filename}'
    """


######################
# Audio only formats
######################

def output_audio(env, codec):
    stream = env["stream"]
    content_type = "audio/ogg"
    fmt = "ogg"
    ext = codec
    if codec == "mp3":
        content_type = "audio/mpeg"
        fmt = "mp3"
    elif codec == "aac":
        content_type == "audio/aac"
        fmt = "adts"
    return f"""
    -f {fmt}
        -password {config.icecast_password}
        -content_type {content_type}
        "icecast://{env["sink"]}/{stream}.{ext}"
    """

def transcode_audio(env, probed):
    res = []

    # no audio only for now
    if env["output"] == "direct":
        return res

    o = 0
    for (i, track) in enumerate(probed["audios"]):
        has_aac = False
        has_opus = False
        title = _audio_label(track, i)
        if track.get("codec_name") == "aac" and track.get("profile") == "LC":
            has_aac = True
            res += [f"""
    -map 0:a:{i}
        -c:a:{o} copy
        -metadata:s:a:{o} title='{title}'
    """]
            o += 1
            if env["output"] == "direct":
                res += [output_audio(env, "aac")]
                o = 0

        if track.get("codec_name") == "opus":
            has_opus = True
            res += [f"""
    -map 0:a:{i}
        -c:a:{o} copy
        -metadata:s:a:{o} title='{title}'
    """]
            o += 1
            if env["output"] == "direct":
                res += [output_audio(env, "opus")]
                o = 0

        rate = min(int(track.get("sample_rate", "48000")), 48000)
        if not has_aac:
            res += [f"""
    -map 0:a:{i}
        -c:a:{o} aac -b:a:{o} 256k -ar:a:{o} {rate}
        -metadata:s:a:{o} title='{title}'
    """]
            o += 1
            if env["output"] == "direct":
                res += [output_audio(env, "aac")]
                o = 0

        if not has_opus:
            res += [f"""
    -map 0:a:{i}
        -c:a:{o} libopus -b:a:{o} 192k -ar:a:{o} {rate}
        -metadata:s:a:{o} title='{title}'
    """]
            o += 1
            if env["output"] == "direct":
                res += [output_audio(env, "opus")]
                o = 0

    if env["output"] == "icecast":
        stream = env.get("stream")
        res += [output_matroska(env, f"{stream}_audio")]
    elif env["output"] == "null":
        res += [output_null()]

    return res


if __name__ == "__main__":
    main()
