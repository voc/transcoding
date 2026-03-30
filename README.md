# voc-transcoding scripts

C3VOC Transcoding pipeline for generating live stream outputs from ingested streams.
Essentially runs a big FFmpeg command to transcode the stream to the desired outputs.

## Usage
The transcode script takes a config file to define the task.
### Manual config
Setup config file
```yaml
mqtt_enabled: false
upload_sink: "localhost:8080"

```

Setup stream file
```yaml
stream_key: foo
format: mpegts
output: direct
type: h264-only
transcoding_source: srt://ingest.c3voc.de:1337?streamid=play/q1
transcoding_sink: localhost:8081
```

Run with:
```bash
transcode.py --config /path/to/config.yaml
```

### Automatic transcoding with transcode-control
transcode-control can be used to start/stop transcoding tasks automatically when streams appear/disappear.
It uses information from consul KV to determine active streams. It then starts systemd template units for transcoding tasks.

A suitable template unit file looks like this:
```ini
[Unit]
Description = Transcode Stream %I
Requires = transcode@%i.target
After = transcode@%i.target
ConditionPathExists = /var/lib/voc-transcode/%i

[Service]
Type = simple
ExecStart = /usr/bin/transcode.py --config /etc/voc-transcode.yaml --stream /var/lib/voc-transcode/%i.yaml --restart
Restart = always
RestartSec = 5s
StartLimitInterval = 0
```

The transcode-control script should be run as a systemd service itself.
The transcode-control config path has to match the config path in the template unit file.
As new streams appear, transcode-control will generate the config files and start the corresponding units.