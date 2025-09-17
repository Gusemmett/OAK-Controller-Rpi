#!/usr/bin/env python3

import os
import csv
import inspect
from collections import deque
import depthai as dai
import time


class VideoSaver(dai.node.HostNode):
    """Host node for saving H.264 video data to file"""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.filename = None
        self._fh = None

    def build(self, *link_args, filename="video.h264"):
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

class IMUCSVLogger(dai.node.HostNode):
    """
    Logs IMU packets to CSV.

    For now, this mirrors PoseCSVLogger structure but writes:
      ts_ns, pkt, <one column per no-arg method on pkt>

    - Method list is determined from the first received packet by attempting
      no-argument calls and keeping the ones that succeed.
    - Each value is stringified; failures are written as "ERR".
    """
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.path = None
        self._w = None
        self._f = None
        self._methods = None  # type: ignore

    def build(self, *link_args, path="imu.csv"):
        self.link_args(*link_args)
        self.path = path
        return self

    def _discover_methods(self, pkt):
        method_names = []
        for name in dir(pkt):
            if name.startswith("__") and name.endswith("__"):
                continue
            # Skip attributes that look like properties/constants
            try:
                attr = getattr(pkt, name)
            except Exception:
                continue
            if not callable(attr):
                continue
            # Prefer methods that take no parameters (besides implicit self)
            include = False
            try:
                sig = inspect.signature(attr)
                params = [p for p in sig.parameters.values() if p.kind in (
                    inspect.Parameter.POSITIONAL_ONLY,
                    inspect.Parameter.POSITIONAL_OR_KEYWORD,
                    inspect.Parameter.KEYWORD_ONLY,
                )]
                # Bound methods typically expose zero parameters if no args required
                include = len(params) == 0
            except (ValueError, TypeError):
                # Some pybind11 methods don't expose a signature; try calling
                include = True
            if not include:
                continue
            # Test call to verify it actually works without args
            try:
                _ = attr()
                method_names.append(name)
            except Exception:
                # Skip methods requiring arguments or failing
                continue
        # Stable order
        method_names.sort()
        return method_names

    def _ensure_writer(self, pkt):
        if self._w is None:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            self._f = open(self.path, "w", newline="")
            self._w = csv.writer(self._f)
            # Discover methods on first packet
            self._methods = self._discover_methods(pkt)
            header = ["ts_ns", "pkt"] + list(self._methods)
            self._w.writerow(header)

    def process(self, pkt):
        self._ensure_writer(pkt)

        ts_fn = getattr(pkt, "getTimestampDevice", None)
        ts = ts_fn() if callable(ts_fn) else pkt.getTimestamp()
        ts_ns = int(ts.total_seconds() * 1e9)

        row = [ts_ns, str(pkt)]
        for name in self._methods:  # type: ignore
            try:
                val = getattr(pkt, name)()
                # Stringify; keep compact
                row.append(str(val))
            except Exception:
                row.append("ERR")
        self._w.writerow(row)

    def close(self):
        if self._f:
            self._f.close()
            self._f = None
            self._w = None




class PoseCSVLoggerThreaded(dai.node.ThreadedHostNode):
    """
    Threaded CSV logger joining:
      pose: dai.Transform (slam.transform)
      rect: dai.ImgFrame    (slam.passthroughRect)

    CSV header:
      ts_ns,tx,ty,tz,qx,qy,qz,qw

    Behavior:
      - Uses latest rect timestamp as timebase when available.
      - Falls back to pose timestamp if no rect seen yet.
      - Blocks on pose to avoid busy-spin. Drains rect non-blocking.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.path = "slam.csv"
        self.flush_every = 0
        self._f = None
        self._w = None
        self._rows = 0

        self._in_pose = None
        self._in_rect = None

        self._last_rect_ts = None  # datetime.timedelta-like
        self._last_rect_seq = -1

    # DepthAI v3 host-node style: create inputs and link upstream streams
    def link_args(self, pose_stream, rect_stream):
        if self._in_pose is None:
            self._in_pose = self.createInput("pose")
        if self._in_rect is None:
            self._in_rect = self.createInput("rect")

        pose_stream.link(self._in_pose)
        rect_stream.link(self._in_rect)

        # Queue policy: block on pose, keep only latest rect
        self._in_pose.setBlocking(True)
        self._in_rect.setBlocking(False)


    def build(self, pose_stream, rect_stream, path: str = "slam.csv", flush_every: int = 0):
        self.link_args(pose_stream, rect_stream)
        self.path = path
        self.flush_every = int(flush_every) if flush_every else 0
        return self

    def _ensure_writer(self):
        if self._w:
            return
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        self._f = open(self.path, "w", newline="")
        self._w = csv.writer(self._f)
        self._w.writerow(["ts_ns","tx","ty","tz","qx","qy","qz","qw"])

    @staticmethod
    def _ts_from_pkt(pkt):
        # Prefer device ts
        ts = None
        try:
            ts = pkt.getTimestampDevice()
        except Exception:
            pass
        if ts is None:
            try:
                ts = pkt.getTimestamp()
            except Exception:
                pass
        return ts  # may be None

    @staticmethod
    def _ns_from_ts(ts):
        return int(ts.total_seconds() * 1e9)

    @staticmethod
    def _pose_from_pkt(pkt):
        try:
            t = pkt.getTranslation()
            tx, ty, tz = t.x, t.y, t.z
        except Exception:
            tx = ty = tz = None
        try:
            q = pkt.getQuaternion()
            qx, qy, qz, qw = q.qx, q.qy, q.qz, q.qw
        except Exception:
            qx = qy = qz = qw = None
        return tx, ty, tz, qx, qy, qz, qw

    def _drain_rect(self):
        # Non-blocking: keep only the freshest rect timebase
        while self._in_rect.has():
            r = self._in_rect.get()
            ts = self._ts_from_pkt(r)
            if ts is not None:
                self._last_rect_ts = ts
            try:
                self._last_rect_seq = r.getSequenceNum()
            except Exception:
                pass

    # Required by ThreadedHostNode
    def run(self):
        self._ensure_writer()

        while self.isRunning():
            # Keep rect timebase fresh
            self._drain_rect()

            # Block on next pose
            if not self._in_pose.has():
                # Yield briefly to avoid tight spin when shutting down
                time.sleep(0.0005)
                continue

            p = self._in_pose.get()

            # Choose timestamp: rect preferred, else pose, else wall clock
            rect_ts = self._last_rect_ts
            pose_ts = self._ts_from_pkt(p)
            ts_ns = self._ns_from_ts(rect_ts or pose_ts)

            tx, ty, tz, qx, qy, qz, qw = self._pose_from_pkt(p)
            row = [ 
                ts_ns,
                tx if tx is not None else "nan",
                ty if ty is not None else "nan",
                tz if tz is not None else "nan",
                qx if qx is not None else "nan",
                qy if qy is not None else "nan",
                qz if qz is not None else "nan",
                qw if qw is not None else "nan",
            ]
            self._w.writerow(row)
            self._rows += 1

            if self.flush_every and (self._rows % self.flush_every == 0):
                try:
                    self._f.flush()
                    os.fsync(self._f.fileno())
                except Exception:
                    pass

    def close(self):
        if self._f:
            try:
                self._f.flush()
                os.fsync(self._f.fileno())
            except Exception:
                pass
            self._f.close()
            self._f = None
            self._w = None