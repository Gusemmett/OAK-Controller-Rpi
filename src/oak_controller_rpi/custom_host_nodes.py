#!/usr/bin/env python3

import os
import csv

import depthai as dai


class VideoSaver(dai.node.HostNode):
    """Host node for saving H.265 video data to file"""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.filename = None
        self._fh = None

    def build(self, *link_args, filename="video.h265"):
        self.link_args(*link_args)
        self.filename = filename
        return self

    def process(self, pkt):
        if self._fh is None:
            os.makedirs(os.path.dirname(self.filename) or ".", exist_ok=True)
            self._fh = open(self.filename, "wb")
        pkt.getData().tofile(self._fh)

    def close(self):
        if self._fh:
            self._fh.close()
            self._fh = None


class TsLogger(dai.node.HostNode):
    """Host node for logging timestamps and frame indices"""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.path = None
        self._w = None
        self._f = None

    def build(self, *link_args, path="stream.csv"):
        self.link_args(*link_args)
        self.path = path
        return self

    def process(self, pkt):
        if self._w is None:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            self._f = open(self.path, "w", newline="")
            self._w = csv.writer(self._f)
            self._w.writerow(["ts_ns", "frame_idx"])

        # Get device timestamp, fallback to host timestamp
        ts = getattr(pkt, "getTimestampDevice", None)
        ts = ts() if ts else pkt.getTimestamp()
        ts_ns = int(ts.total_seconds() * 1e9)
        self._w.writerow([ts_ns, pkt.getSequenceNum()])

    def close(self):
        if self._f:
            self._f.close()
            self._f = None
            self._w = None


