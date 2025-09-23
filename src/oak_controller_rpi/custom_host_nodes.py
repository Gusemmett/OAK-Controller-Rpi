#!/usr/bin/env python3

import os
import csv
import inspect
from collections import deque
import depthai as dai
import time
import cv2


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

        ts_ns = ts_ns = int(pkt.getTimestampDevice().total_seconds() * 1e9)
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

    def build(self, *link_args, path="imu.csv"):
        self.link_args(*link_args)
        self.path = path
        return self

    def _ensure_writer(self):
        if self._w is None:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            self._f = open(self.path, "w", newline="")
            self._w = csv.writer(self._f)
            header = ["ts_ns",
                     "accel_x", "accel_y", "accel_z",
                     "gyro_x", "gyro_y", "gyro_z",
                     "mag_x", "mag_y", "mag_z",
                     "rot_i", "rot_j", "rot_k"]
            self._w.writerow(header)

    def process(self, pkt):
        self._ensure_writer()

        ts_ns = int(pkt.getTimestampDevice().total_seconds() * 1e9)

        # Process each packet
        for p in pkt.packets:
            # Extract values from packet components
            accel = p.acceleroMeter
            gyro = p.gyroscope
            mag = p.magneticField
            rot = p.rotationVector

            # Build row with all sensor values
            row = [ts_ns,
                   accel.x, accel.y, accel.z,
                   gyro.x, gyro.y, gyro.z,
                   mag.x, mag.y, mag.z, 
                   rot.i, rot.j, rot.k]
            
            # Write row for this packet
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


class DepthLogger(dai.node.HostNode):
    """
    Threaded logger for StereoDepth outputs.

    Inputs:
      - depth: dai.ImgFrame (RAW16 depth values in millimeters; 0 means invalid)
      - confidence: dai.ImgFrame (RAW8 confidence map)

    Behavior:
      - For each incoming packet on either input, prints available method and
        attribute names (no values) to stdout. Intended for introspection only.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._in_depth = None
        self._in_conf = None
        self.path = "depth"

    def link_args(self, depth_stream, confidence_stream):
        if self._in_depth is None:
            self._in_depth = self.createInput("depth")
        if self._in_conf is None:
            self._in_conf = self.createInput("confidence")

        depth_stream.link(self._in_depth)
        confidence_stream.link(self._in_conf)

        # Process will be called per packet; keep inputs non-blocking
        try:
            self._in_depth.setBlocking(False)
            self._in_conf.setBlocking(False)
        except Exception:
            pass

    def build(self, depth_stream, confidence_stream, path: str = "depth"):
        self.link_args(depth_stream, confidence_stream)
        self.path = path
        try:
            os.makedirs(self.path, exist_ok=True)
        except Exception:
            pass
        return self

    def process(self, depth_packet, confidence_packet):
        # Ensure output dir exists
        try:
            os.makedirs(self.path, exist_ok=True)
        except Exception:
            pass

        # Save depth (RAW16, mm)
        try:
            depth_frame = depth_packet.getFrame()
            depth_seq = depth_packet.getSequenceNum()
            if depth_frame is not None and depth_seq is not None:
                if getattr(depth_frame, 'dtype', None) != 'uint16':
                    depth_frame = depth_frame.astype('uint16')
                depth_path = os.path.join(self.path, f"depth_{depth_seq:06d}.png")
                try:
                    cv2.imwrite(depth_path, depth_frame)
                except Exception:
                    pass
        except Exception:
            pass

        # Save confidence (RAW8)
        try:
            conf_frame = confidence_packet.getFrame()
            conf_seq = confidence_packet.getSequenceNum()
            if conf_frame is not None and conf_seq is not None:
                if getattr(conf_frame, 'dtype', None) != 'uint8':
                    conf_frame = conf_frame.astype('uint8')
                conf_path = os.path.join(self.path, f"confidence_{conf_seq:06d}.png")
                try:
                    cv2.imwrite(conf_path, conf_frame)
                except Exception:
                    pass
        except Exception:
            pass