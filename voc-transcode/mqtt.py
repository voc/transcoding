import json
import config
import threading
import paho.mqtt.client as mqtt


class Client:
    """MQTT client as context manager"""

    def __init__(self, config: config.MqttConfig):
        self.config = config
        if not self.config.enabled:
            return

        self.client = mqtt.Client()
        self.client.enable_logger()
        self.client.tls_set(ca_certs=self.config.certs)
        self.client.username_pw_set(self.config.user, self.config.password)
        self.mid = None
        self.cv = threading.Condition()
        self.client.on_publish = self.__handle_publish

    def __enter__(self):
        if self.config.enabled:
            self.client.connect(self.config.broker, port=self.config.port)
            self.client.loop_start()
        return self

    def __exit__(self, type, value, traceback):
        if self.config.enabled:
            self.client.loop_stop()
            self.client.disconnect()

    def __handle_publish(self, client, userdata, mid):
        with self.cv:
            self.mid = mid
            self.cv.notify()

    def __publish(self, msg, level, timeout=1):
        """
        Publish MQTT message with timeout
        """
        msg = {
            "level": level,
            "component": "transcoding/" + self.config.host,
            "msg": msg,
        }
        payload = json.dumps(msg)
        res = self.client.publish("/voc/alert", payload)
        if res.rc != mqtt.MQTT_ERR_SUCCESS:
            print("MQTT publish failed with", res.rc)
            return
        sent_mid = lambda: self.mid == res.mid
        with self.cv:
            if not self.cv.wait_for(sent_mid, timeout):
                print("MQTT publish timeout")
                return

    def info(self, msg):
        print(msg)
        if self.config.enabled:
            self.__publish(msg, level="info")
