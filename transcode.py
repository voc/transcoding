#!/usr/bin/env python3
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
    env["vaapi_dev"], env["vaapi_driver"], env["vaapi_features"] = select_vaapi_dev()
    # env["icecast_password"] = config.icecast_password
    env["upload_user"] = config.upload_user
    env["upload_pass"] = config.upload_pass
    env["upload_sink"] = config.upload_sink

    parser = argparse.ArgumentParser(description="Transcode voc stream")
    parser.add_argument("--stream", help="stream key")
    parser.add_argument("--source", help="transcoding input")
    parser.add_argument("--sink", help="transcoding sink host")
    parser.add_argument("--type", help="transcode type",
                        choices=["h264-only", "all"])
    parser.add_argument("-o", "--output", help="output type",
                        default="icecast", choices=["icecast", "direct", "null"])
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
        client.info(f"Transcoding for {env['stream']} started…")
        try:
            mainloop(env, args.restart, progress, args.verbose)
        except ExitException:
            pass
        client.info(f"Transcoding for {env['stream']} stopped…")


def check_arg(env, key, flag):
    """Selectively overwrite environment variables if flag is set"""
    if flag is not None:
        env[key] = flag
    if env[key] is None:
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
    return ' '.join([join_all(x) if type(x) is list else x for x in L])


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
    print("probe")
    try:
        cmd = f"ffprobe -v quiet -print_format json -show_format -show_streams {source}"
        buf = subprocess.check_output(shlex.split(cmd))
    except Exception as err:
        print("Probe failed", err)
        return

    result = json.loads(buf.decode("utf-8"))
    print("res", result)
    video_tracks = list(filter(lambda stream: stream['codec_type'] == "video", result['streams']))
    audio_tracks = list(filter(lambda stream: stream['codec_type'] == "audio", result['streams']))

    print(list(video_tracks))
    
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
    # video_track_names = map(lambda stream: stream['tags']['title'], video_tracks)
    # audio_track_names = map(lambda stream: stream['tags']['title'], audio_tracks)
    # print("names {} {}".format(list(video_track_names), list(audio_track_names)))
    # return list(set(video_track_names)), list(set(audio_track_names))


def template_command(env, probed):
    source = env.get("source")
    input_opts = ""
    if source.startswith("rtmp://"):
        input_opts = "-f live_flv"
    elif source.startswith("srt://"):
        input_opts = "-f mpegts"
    env["input_tmpl"] = f"{input_opts} -i '{env['source']}'"

# build command
    if probed["audio_only"]:
        return pipeline_audio_only(env, probed)
    elif env["type"] == "h264-only":
        return pipeline_h264_only(env, probed)
    else:
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
    stream = env["stream"]
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
            encode_vp9_vaapi(),
            output(env, f"{stream}_vpx"),
            transcode_thumbs(env, probed),
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
            encode_vp9_software(),
            output_matroska(env, f"{stream}_vpx"),
            transcode_thumbs(env, probed),
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
            encode_vp9_software("0:v:0"),
            output_matroska(env, f"{stream}_vpx"),
            transcode_thumbs(env, probed),
            transcode_audio(env, probed),
        ]


def pipeline_h264_only(env, probed):
    input_tmpl = env["input_tmpl"]
    stream = env["stream"]
    vaapi_dev = env["vaapi_dev"]
    vaapi_features = env["vaapi_features"]

    print("vaaaapi", vaapi_features)

    # vaapi
    if "h264-enc" in vaapi_features and "jpeg-enc" in vaapi_features:
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
            transcode_thumbs(env, probed),
            transcode_audio(env, probed),
            # output_matroska(env, f"{stream}_audio"),
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
            # transcode_audio(env, probed),
        ]


############################
# Generic Helpers
############################


def output_null(env=None, slug=None):
    return "-max_muxing_queue_size 400 -f null -"


def output_matroska(env, slug):
    return f"""
    -fflags +genpts
    -max_muxing_queue_size 400
    -f matroska
    -password {config.icecast_password}
    -content_type video/webm
    "icecast://{env["sink"]}/{slug}"
"""


def _audio_label(track, index):
    if "tags" in track and len(track["tags"].get("title")) > 0:
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
        res += [encode_h264_vaapi(hd_input, sd_input)]
    else:
        res += [encode_h264_software(hd_input, sd_input)]
    res += [
        encode_h264_audio(env, probed),
        output_h264(env, probed),
    ]
    return res


def encode_h264_vaapi(hd_input, sd_input):
    return f"""
    -map '{hd_input}'
        -metadata:s:v:0 title="HD"
        -c:v:0 h264_vaapi
            -r:v:0 25
            -keyint_min:v:0 75
            -g:v:0 75
            -b:v:0 2800k
            -bufsize:v:0 8400k
            -flags:v:0 +cgop

    -map '{sd_input}'
        -metadata:s:v:1 title="SD"
        -c:v:1 h264_vaapi
            -r:v:1 25
            -keyint_min:v:1 75
            -g:v:1 75
            -b:v:1 800k
            -bufsize:v:1 2400k
            -flags:v:1 +cgop

    -map '0:v:1?'
        -metadata:s:v:2 title="Slides"
        -c:v:2 copy
"""


def encode_h264_software(hd_input, sd_input):
    return f"""
    -map '{hd_input}'
        -metadata:s:v:0 title="HD"
        -c:v:0 libx264
            -maxrate:v:0 2800k
            -crf:v:0 21
            -bufsize:v:0 5600k
            -pix_fmt:v:0 yuv420p
            -profile:v:0 main
            -r:v:0 25
            -keyint_min:v:0 75
            -g:v:0 75
            -flags:v:0 +cgop
            -preset:v:0 veryfast

    -map '{sd_input}'
        -metadata:s:v:1 title="SD"
        -c:v:1 libx264
            -maxrate:v:1 800k
            -crf:v:1 23
            -bufsize:v:1 3600k
            -pix_fmt:v:1 yuv420p
            -profile:v:1 main
            -r:v:1 25
            -keyint_min:v:1 75
            -g:v:1 75
            -flags:v:1 +cgop
            -preset:v:1 veryfast
    -map '0:v:1?'
        -metadata:s:v:2 title="Slides"
        -c:v:2 copy
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
        for _ in range(len(probed["videos"])+1):
            res += [_h264_audio_track(track, 0, map_index)]
            map_index += 1

        # mux alternate audios
        for in_index, track in enumerate(probed["audios"][1:], 1):
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
        output = env.get("upload_sink")
        auth = ""
        auth_opt = ""
        if user is not None and password is not None:
            auth = f"{user}:{password}@"
            auth_opt = "-auth_type basic"

        stream_map = []
        audio_idx = 0
        for i in range(len(probed["videos"])+1):
            stream_map += [f"v:{i},a:{audio_idx},agroup:audio"]
            audio_idx += 1
        
        for i in range(len(probed["audios"])-1):
            stream_map += [f"a:{audio_idx},agroup:audio"]
            audio_idx += 1

        return f"""
        -f hls
            -http_persistent 1 -timeout 5 -ignore_io_errors 1
            -hls_flags delete_segments {auth_opt}
            -var_stream_map "{' '.join(stream_map)}"
            -master_pl_name master.m3u8 -master_pl_publish_rate 10
            -method PUT "http://{auth}{output}/hls/{stream}/out_%v.m3u8"
        """


######
# VP9
######

def output_dash():
    return f"""
    
"""


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

    -c:a libopus -ac:a 2 -b:a 128k
    -af "aresample=async=1:min_hard_comp=0.100000:first_pts=0"

    -map '0:a:0' -metadata:s:a:0 title="Native"
    -map '0:a:1?' -metadata:s:a:1 title="Translated"
    -map '0:a:2?' -metadata:s:a:2 title="Translated-2"
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

    -c:a libopus -ac:a 2 -b:a 128k
    -af "aresample=async=1:min_hard_comp=0.100000:first_pts=0"

    -map '0:a:0' -metadata:s:a:0 title="Native"
    -map '0:a:1?' -metadata:s:a:1 title="Translated"
    -map '0:a:2?' -metadata:s:a:2 title="Translated-2"
"""

######################
# Thumbnails
######################

def transcode_thumbs(env, probed, poster_input="[poster]", thumb_input="[thumb]", use_vaapi=False):
    res = []
    if use_vaapi:
        res += ["-c:v mjpeg_vaapi"]
    else:
        res += ["-c:v mjpeg -pix_fmt:v yuvj420p"]
    
    idx = 0
    res += [f"-map '{poster_input}' -metadata:s:v:{idx} title='Poster'"]
    idx += 1
    if env["output"] == "direct":
        res += [output_thumbs_upload(env, "poster.jpeg")]

    res += [f"-map '{thumb_input}' -metadata:s:v:{idx} title='Thumbnail'"]
    idx += 1
    if env["output"] == "direct":
        res += [output_thumbs_upload(env, "thumb.jpeg")]
    
    if env["output"] == "icecast":
        res += [output_matroska(env, f"{stream}_thumbnail")]
    elif env["output"] == "null":
        res += [output_null()]

    return res

def output_thumbs_upload(env, filename):
    stream = env.get("stream")
    user = env.get("upload_user")
    password = env.get("upload_pass")
    output = env.get("upload_sink")
    auth = ""
    auth_opt = ""
    if user is not None and password is not None:
        auth = f"{user}:{password}@"
        auth_opt = ":auth_type=basic"
    return f"""
    -f image2
        -update 1 -protocol_opts method=PUT:multiple_requests=1:reconnect_on_network_error=1:reconnect_delay_max=5{auth_opt}
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
        res += [output_matroska(env, "{stream}_audio")]
    elif env["output"] == "null":
        res += [output_null()]

    return res


if __name__ == "__main__":
    main()
