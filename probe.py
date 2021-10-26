import subprocess
import json

def analyze_tracks(pull_url):
	cmd = "ffprobe -v quiet -print_format json -show_format -show_streams %s" % pull_url
	response = subprocess.check_output(shlex.split(cmd))

	response_parsed = json.loads(response.decode("utf-8"))
	video_tracks = filter(lambda stream: stream['codec_type'] == "video", response_parsed['streams'])
	audio_tracks = filter(lambda stream: stream['codec_type'] == "audio", response_parsed['streams'])

	video_track_names = map(lambda stream: stream['tags']['title'], video_tracks)
	audio_track_names = map(lambda stream: stream['tags']['title'], audio_tracks)

	return list(set(video_track_names)), list(set(audio_track_names))