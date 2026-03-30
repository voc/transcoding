from socket import socket
import yaml
import sys


class TranscodeConfig:
    """Configuration for transcode"""

    def __init__(self, path):
        with open(path) as f:
            conf = yaml.safe_load(f)
        self.upload = UploadConfig(conf.get("upload", {}))
        self.mqtt = MqttConfig(conf.get("mqtt", {}))
        self.icecast_password = conf.get("icecast_password", "")
        self.progress = conf.get("ffmpeg_progress", "")


class UploadConfig:
    """Configuration for upload"""

    def __init__(self, config):
        self.sink = config.get("sink", "localhost:8080")
        self.user = config.get("user", "")
        self.password = config.get("password", "")


class TranscodeControlConfig:
    """Configuration for transcode-control"""

    def __init__(self, path):
        with open(path) as f:
            conf = yaml.safe_load(f)
        self.capacity = conf.get("capacity", 1)
        self.sink = conf.get("sink", "localhost:8080")
        self.config_path = conf.get("configPath", "/tmp/transcode-control")
        self.hostname = conf.get("hostname", socket.gethostname().split(".")[0])
        self.mqtt = MqttConfig(conf.get("mqtt", {}))


class MqttConfig:
    """MQTT configuration"""

    def __init__(self, config):
        self.enabled = config.get("enabled", False)
        self.broker = config.get("broker", "")
        self.port = config.get("port", 0)
        self.user = config.get("user", "")
        self.password = config.get("password", "")
        self.certs = config.get("certs", "")
        self.host = config.get("hostname", socket.gethostname().split(".")[0])


def load_stream_config(env, path):
    try:
        with open(path) as f:
            conf = yaml.safe_load(f)
    except Exception as e:
        print("Failed to load stream config from %s: %s" % (path, e))
        conf = {}
    env["stream"] = conf.get("stream_key", "")
    env["source"] = conf.get("transcoding_source", "")
    env["sink"] = conf.get("transcoding_sink", "")
    env["type"] = conf.get("type", "all")
    env["output"] = conf.get("output", "")
    env["passthrough"] = conf.get("passthrough", "no") == "yes"
    env["restream"] = conf.get("restream", "none")
    env["framerate"] = conf.get("framerate", 25)
