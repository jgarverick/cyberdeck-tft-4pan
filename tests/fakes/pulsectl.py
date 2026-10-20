"""Minimal stand-in for pulsectl used by the end-to-end test. Records every change."""
import json, os

LOG = os.environ.get("FAKE_PULSE_LOG", "/tmp/fake-pulse.log")


def _log(**kw):
    with open(LOG, "a") as fh:
        fh.write(json.dumps(kw) + "\n")


class _Vol:
    def __init__(self, v):
        self.value_flat = v


class _Obj:
    def __init__(self, **kw):
        self.__dict__.update(kw)


SINKS = [
    _Obj(name="alsa_output.hdmi", description="Built-in audio (HDMI)", index=0, mute=False,
         volume=_Vol(0.72), monitor_source_name="alsa_output.hdmi.monitor", proplist={}),
    _Obj(name="bluez_output.headset", description="WH-1000XM5 (Bluetooth)", index=1, mute=False,
         volume=_Vol(0.40), monitor_source_name="bluez_output.headset.monitor", proplist={}),
]
INPUTS = [
    _Obj(name="playback", index=41, sink=0, mute=False, volume=_Vol(1.0), proplist={"application.name": "Firefox"}),
    _Obj(name="playback", index=42, sink=0, mute=False, volume=_Vol(0.58), proplist={"application.name": "Spotify"}),
]
SOURCES = [
    _Obj(name="alsa_output.hdmi.monitor", description="Monitor", index=0, mute=False, volume=_Vol(1.0), proplist={}),
    _Obj(name="alsa_input.usb_mic", description="USB mic", index=1, mute=False, volume=_Vol(0.64), proplist={}),
]
STATE = {"default_sink": "alsa_output.hdmi"}


class Pulse:
    def __init__(self, name=""):
        pass

    def close(self):
        pass

    def server_info(self):
        return _Obj(default_sink_name=STATE["default_sink"], default_source_name="alsa_input.usb_mic")

    def sink_list(self):
        return SINKS

    def sink_input_list(self):
        return INPUTS

    def source_list(self):
        return SOURCES

    def volume_set_all_chans(self, obj, v):
        obj.volume = _Vol(v)
        _log(op="volume", target=getattr(obj, "name", ""), index=obj.index, value=round(v, 2))

    def mute(self, obj, state):
        obj.mute = state
        _log(op="mute", index=obj.index, mute=state)

    def default_set(self, obj):
        STATE["default_sink"] = obj.name
        _log(op="default_sink", name=obj.name)

    def sink_input_move(self, si, sink):
        _log(op="move", input=si, sink=sink)

    def get_peak_sample(self, source, timeout, stream_idx=None):
        import random, time
        time.sleep(timeout / 4)
        return random.uniform(0.2, 0.8)
