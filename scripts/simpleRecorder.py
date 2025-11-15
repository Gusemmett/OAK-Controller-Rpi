#!/usr/bin/env python3
# DepthAI v3: OAK-D-W stereo, hardware-synced, H.265, headless, CSV per stream + IMU CSV

import argparse
import csv
import os
import signal
import threading
import time
from datetime import datetime

import depthai as dai
import json

# ---------- Host nodes ----------

class VideoSaver(dai.node.HostNode):
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
    # Logs ts_ns,frame_idx for EncodedFrame packets
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
        # EncodedFrame exposes device ts + sequence number
        # Fallback to host-synced ts if device ts missing
        ts = getattr(pkt, "getTimestampDevice", None)
        ts = ts() if ts else pkt.getTimestamp()
        ts_ns = int(ts.total_seconds() * 1e9)
        self._w.writerow([ts_ns, pkt.getSequenceNum()])

    def close(self):
        if self._f:
            self._f.close()
            self._f = None
            self._w = None


class IMUSaver(dai.node.HostNode):
    # Logs IMU to imu.csv as: ts_ns,gyro_x,gyro_y,gyro_z,accel_x,accel_y,accel_z
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.path = None
        self._w = None
        self._f = None

    def build(self, *link_args, path="imu.csv"):
        self.link_args(*link_args)
        self.path = path
        return self

    def process(self, imuData):
        if self._w is None:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            self._f = open(self.path, "w", newline="")
            self._w = csv.writer(self._f)
            self._w.writerow(["ts_ns","gyro_x","gyro_y","gyro_z","accel_x","accel_y","accel_z"])

        # imuData is dai.IMUData with a list of packets
        for pkt in imuData.packets:
            accel = getattr(pkt, "acceleroMeter", None)
            gyro  = getattr(pkt, "gyroscope", None)
            if accel is None or gyro is None:
                continue  # write only when both are present

            # Prefer device timestamp if present
            ts_func = getattr(accel, "getTimestampDevice", None) or accel.getTimestamp
            ts = ts_func()
            ts_ns = int(ts.total_seconds() * 1e9)

            self._w.writerow([ts_ns, gyro.x, gyro.y, gyro.z, accel.x, accel.y, accel.z])

    def close(self):
        if self._f:
            self._f.close()
            self._f = None
            self._w = None


# ---------- Pipeline ----------

def build_pipeline(width, height, fps, bitrate_kbps, left_socket, right_socket, imu_rate_hz):
    with dai.Pipeline() as pipeline:
        # Cameras
        camL = pipeline.create(dai.node.Camera).build(left_socket, sensorFps=fps)
        camR = pipeline.create(dai.node.Camera).build(right_socket, sensorFps=fps)

        outL = camL.requestOutput(
            (width, height),
            type=dai.ImgFrame.Type.NV12,
            resizeMode=dai.ImgResizeMode.LETTERBOX,
            fps=fps,
        )
        outR = camR.requestOutput(
            (width, height),
            type=dai.ImgFrame.Type.NV12,
            resizeMode=dai.ImgResizeMode.LETTERBOX,
            fps=fps,
        )

        # On-device sync for sanity-check pairing
        sync = pipeline.create(dai.node.Sync)
        sync.setRunOnHost(False)
        outL.link(sync.inputs["left"])
        outR.link(sync.inputs["right"])
        # We do not consume sync.out here, since we log from encoders

        # Encoders
        encL = pipeline.create(dai.node.VideoEncoder).build(
            outL, frameRate=fps, profile=dai.VideoEncoderProperties.Profile.H265_MAIN
        )
        encR = pipeline.create(dai.node.VideoEncoder).build(
            outR, frameRate=fps, profile=dai.VideoEncoderProperties.Profile.H265_MAIN
        )
        # encL.setBitrateKbps(3000)
        # encR.setBitrateKbps(3000)

        saverL = pipeline.create(VideoSaver).build(encL.out)
        saverR = pipeline.create(VideoSaver).build(encR.out)
        loggerL = pipeline.create(TsLogger).build(encL.out)
        loggerR = pipeline.create(TsLogger).build(encR.out)

        # IMU
        imu = pipeline.create(dai.node.IMU)
        imu.enableIMUSensor(dai.IMUSensor.ACCELEROMETER_RAW, imu_rate_hz)
        imu.enableIMUSensor(dai.IMUSensor.GYROSCOPE_RAW, imu_rate_hz)
        imu.setBatchReportThreshold(10)
        imu.setMaxBatchReports(20)
        imu_saver = pipeline.create(IMUSaver).build(imu.out)

        return pipeline, saverL, saverR, loggerL, loggerR, imu_saver


def _print_stream_stats(csv_path, label=None):
    try:
        with open(csv_path) as f:
            rows = list(csv.DictReader(f))
        if not rows:
            name = label or os.path.basename(csv_path)
            print(f"{name}: no rows")
            return
        ts = [int(r["ts_ns"]) for r in rows if r.get("ts_ns")]
        name = label or os.path.basename(csv_path)
        if len(ts) < 2:
            print(f"{name} frames: {len(ts)} seconds: 0.000000 fps: 0.000")
            return
        dur = (ts[-1] - ts[0]) / 1e9
        fps = ((len(ts) - 1) / dur) if dur > 0 else 0.0
        print(f"{name} frames: {len(ts)} seconds: {dur:.6f} fps: {fps:.3f}")
    except FileNotFoundError:
        name = label or os.path.basename(csv_path)
        print(f"{name}: file not found")
    except Exception as e:
        name = label or os.path.basename(csv_path)
        print(f"{name}: error: {e}")

# add anywhere above main()
def dump_calibration(outdir, width, height, left_sock, right_sock, rgb_sock=None):
    path = os.path.join(outdir, "calibration.json")
    data = {}
    try:
        with dai.Device() as dev:
            cal = dev.readCalibration()

            def cam(sock):
                # Intrinsics
                K = None
                if hasattr(cal, "getCameraIntrinsics"):
                    K = cal.getCameraIntrinsics(sock, width, height)
                elif hasattr(cal, "getCameraMatrix"):
                    try:
                        K = cal.getCameraMatrix(sock, width, height)
                    except Exception:
                        K = cal.getCameraMatrix(sock)

                # Distortion
                dist = None
                for name in ("getDistortionCoefficients", "getDistortionCoeffs", "getDistortion"):
                    if hasattr(cal, name):
                        dist = getattr(cal, name)(sock)
                        break

                fov = cal.getFov(sock) if hasattr(cal, "getFov") else None
                return {
                    "socket": getattr(sock, "name", str(sock)),
                    "width": width,
                    "height": height,
                    "intrinsics": K,
                    "distortion": dist,
                    "fov_deg": fov,
                }

            data = {
                "device_mxid": dev.getMxId() if hasattr(dev, "getMxId") else None,
                "left": cam(left_sock),
                "right": cam(right_sock),
            }

            # Add RGB camera if provided
            if rgb_sock is not None:
                data["rgb"] = cam(rgb_sock)

            # Extrinsics (left -> right)
            if hasattr(cal, "getCameraExtrinsics"):
                try:
                    E = cal.getCameraExtrinsics(left_sock, right_sock)
                    if E:
                        R = [row[:3] for row in E[:3]]
                        t = [E[0][3], E[1][3], E[2][3]]
                        data["extrinsics_left_to_right"] = {"R": R, "T": t, "matrix_4x4": E}
                except Exception:
                    pass

                # Extrinsics (left -> rgb)
                if rgb_sock is not None:
                    try:
                        E = cal.getCameraExtrinsics(left_sock, rgb_sock)
                        if E:
                            R = [row[:3] for row in E[:3]]
                            t = [E[0][3], E[1][3], E[2][3]]
                            data["extrinsics_left_to_rgb"] = {"R": R, "T": t, "matrix_4x4": E}
                    except Exception:
                        pass
    except Exception as e:
        data = {"error": f"failed to read calibration: {e}"}

    with open(path, "w") as f:
        json.dump(data, f, indent=2)
    print(f"Wrote calibration to {path}")



def main():
    ap = argparse.ArgumentParser(description="DepthAI v3 stereo H.265 recorder with hardware sync, frame CSVs, and IMU CSV.")
    ap.add_argument("-o", "--outdir", default=f"run_{datetime.now().strftime('%Y%m%d_%H%M%S')}")
    ap.add_argument("--width", type=int, default=1280)
    ap.add_argument("--height", type=int, default=720)
    ap.add_argument("--fps", type=float, default=25.0)
    ap.add_argument("--bitrate-kbps", type=int, default=4000)
    ap.add_argument("--left-socket", default="CAM_B", help="OAK-D-W default")
    ap.add_argument("--right-socket", default="CAM_C", help="OAK-D-W default")
    ap.add_argument("--imu-rate-hz", type=int, default=200)
    ap.add_argument("--duration", type=float, default=0.0, help="Seconds. 0 runs until Ctrl-C.")
    args = ap.parse_args()

    def parse_socket(name: str) -> dai.CameraBoardSocket:
        try:
            return getattr(dai.CameraBoardSocket, name)
        except AttributeError:
            raise ValueError(f"Invalid socket {name}. Use CAM_A, CAM_B, CAM_C, CAM_D.")

    left_sock = parse_socket(args.left_socket)
    right_sock = parse_socket(args.right_socket)
    rgb_sock = parse_socket("CAM_A")

    os.makedirs(args.outdir, exist_ok=True)
    left_h265  = os.path.join(args.outdir, "left.h265")
    right_h265 = os.path.join(args.outdir, "right.h265")
    left_csv   = os.path.join(args.outdir, "left.csv")
    right_csv  = os.path.join(args.outdir, "right.csv")
    imu_csv    = os.path.join(args.outdir, "imu.csv")

    # Save camera calibration before starting the pipeline
    dump_calibration(args.outdir, args.width, args.height, left_sock, right_sock, rgb_sock)
 
    pipeline, saverL, saverR, loggerL, loggerR, imu_saver = build_pipeline(
        width=args.width,
        height=args.height,
        fps=args.fps,
        bitrate_kbps=args.bitrate_kbps,
        left_socket=left_sock,
        right_socket=right_sock,
        imu_rate_hz=args.imu_rate_hz,
    )
    saverL.filename = left_h265
    saverR.filename = right_h265
    loggerL.path = left_csv
    loggerR.path = right_csv
    imu_saver.path = imu_csv

    quit_event = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: quit_event.set())
    signal.signal(signal.SIGINT,  lambda *_: quit_event.set())

    pipeline.start()
    print(f"Recording:\n  {left_h265}\n  {right_h265}\nIMU CSV: {imu_csv}\nFrame CSVs:\n  {left_csv}\n  {right_csv}")

    t0 = time.monotonic()
    try:
        while pipeline.isRunning() and not quit_event.is_set():
            if args.duration and (time.monotonic() - t0) >= args.duration:
                break
            time.sleep(0.02)
    finally:
        pipeline.stop()
        pipeline.wait()
        saverL.close(); saverR.close()
        loggerL.close(); loggerR.close()
        imu_saver.close()
        print("Stopped.")
        print("Frame CSV stats:")
        _print_stream_stats(left_csv, "left.csv")
        _print_stream_stats(right_csv, "right.csv")


if __name__ == "__main__":
    with dai.Device() as dev:
        print("USB:", dev.getUsbSpeed())
    main()
