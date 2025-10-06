#!/usr/bin/env python3
import csv, math, statistics, subprocess, shutil, json, zipfile, logging
from pathlib import Path
from typing import Dict, List, Tuple, Optional

logger = logging.getLogger(__name__)

class StereoPostProcess:
    """
    Inputs:
      left_h264, left_csv  (CSV header: ts_ns,frame_idx)
      right_h264, right_csv
      rgb_h264, rgb_csv (optional)
    Outputs:
      left.mp4, right.mp4, rgb.mp4 (if provided) with pacing from CSV
    API:
      analyze() -> dict
      to_mp4(out_left, out_right, fps_left=None, fps_right=None, out_rgb=None, fps_rgb=None, reencode_fallback=True)
    """

    def __init__(self, left_h264, left_csv, right_h264, right_csv, rgb_h264=None, rgb_csv=None):
        self.l_h264 = Path(left_h264);  self.l_csv  = Path(left_csv)
        self.r_h264 = Path(right_h264); self.r_csv  = Path(right_csv)
        for p in (self.l_h264, self.l_csv, self.r_h264, self.r_csv):
            if not Path(p).exists(): raise FileNotFoundError(p)

        self.l_ts, self.l_idx = self._load_csv(self.l_csv)
        self.r_ts, self.r_idx = self._load_csv(self.r_csv)

        if len(self.l_ts) < 2 or len(self.r_ts) < 2:
            raise ValueError("Need ≥2 timestamps in each CSV")

        # Optional RGB camera
        self.has_rgb = rgb_h264 is not None and rgb_csv is not None
        if self.has_rgb:
            self.rgb_h264 = Path(rgb_h264); self.rgb_csv = Path(rgb_csv)
            if self.rgb_h264.exists() and self.rgb_csv.exists():
                self.rgb_ts, self.rgb_idx = self._load_csv(self.rgb_csv)
                if len(self.rgb_ts) >= 2:
                    self.rgb_stats = self._compute_stream_stats(self.rgb_ts, self.rgb_idx)
                else:
                    self.has_rgb = False
            else:
                self.has_rgb = False

        self.left_stats  = self._compute_stream_stats(self.l_ts, self.l_idx)
        self.right_stats = self._compute_stream_stats(self.r_ts, self.r_idx)
        self.pair_stats  = self._compute_pair_stats()

    # ---------- CSV + stats ----------

    @staticmethod
    def _load_csv(path: Path) -> Tuple[List[int], List[int]]:
        ts_ns: List[int] = []
        idxs:  List[int] = []
        with path.open("r", newline="") as f:
            rdr = csv.DictReader(f)
            if not {"ts_ns","frame_idx"}.issubset(rdr.fieldnames or []):
                raise ValueError(f"{path}: CSV must have headers: ts_ns,frame_idx")
            rows = [(int(r["frame_idx"]), int(r["ts_ns"])) for r in rdr]
        rows.sort(key=lambda x: (x[0], x[1]))
        idxs  = [i for (i, _) in rows]
        ts_ns = [t for (_, t) in rows]
        return ts_ns, idxs

    @staticmethod
    def _median_delta_ns(ts_ns: List[int]) -> Tuple[int, List[int]]:
        deltas = [b - a for a, b in zip(ts_ns[:-1], ts_ns[1:]) if b > a]
        if not deltas: raise ValueError("Non-increasing timestamps")
        return int(statistics.median(deltas)), deltas

    @staticmethod
    def _mad(values: List[int], med: float) -> float:
        return statistics.median([abs(v - med) for v in values])

    @staticmethod
    def _find_drops(indices: List[int]) -> Dict[str, int]:
        if not indices: return {"drops": 0, "runs": 0}
        drops = 0; runs = 0
        for a, b in zip(indices[:-1], indices[1:]):
            gap = b - a
            if gap > 1:
                drops += gap - 1
                runs  += 1
        return {"drops": drops, "runs": runs}

    @staticmethod
    def _linregress(x: List[float], y: List[float]) -> Tuple[float, float]:
        """
        Return slope and intercept (least squares). No numpy.
        """
        n = len(x)
        if n == 0: return 0.0, 0.0
        mx = sum(x)/n; my = sum(y)/n
        num = sum((xi - mx)*(yi - my) for xi, yi in zip(x, y))
        den = sum((xi - mx)**2 for xi in x) or 1e-12
        slope = num / den
        intercept = my - slope*mx
        return slope, intercept

    def _compute_stream_stats(self, ts_ns: List[int], idxs: List[int]) -> Dict:
        med_dt, deltas = self._median_delta_ns(ts_ns)
        fps_med = 1e9 / med_dt
        dur_s = (ts_ns[-1] - ts_ns[0]) / 1e9
        mad_dt = self._mad(deltas, med_dt)
        jitter_pct = (mad_dt / med_dt) * 100.0
        drops = self._find_drops(idxs)
        p95_dt = self._percentile(deltas, 95)
        return {
            "n_frames_csv": len(ts_ns),
            "duration_s": dur_s,
            "fps_median": fps_med,
            "dt_ns_median": med_dt,
            "dt_ns_mad": mad_dt,
            "dt_ns_p95": p95_dt,
            "jitter_pct": jitter_pct,
            "drops": drops["drops"],
            "drop_runs": drops["runs"],
            "index_start": idxs[0],
            "index_end": idxs[-1],
        }

    def _compute_pair_stats(self) -> Dict:
        # Align by frame_idx intersection
        li = set(self.l_idx); ri = set(self.r_idx)
        common = sorted(li & ri)
        left_only  = sorted(li - ri)
        right_only = sorted(ri - li)
        n_common = len(common)

        if n_common == 0:
            return {
                "aligned_pairs": 0, "left_only": len(left_only), "right_only": len(right_only)
            }

        # Maps for ts lookup by idx
        l_map = {i: t for i, t in zip(self.l_idx, self.l_ts)}
        r_map = {i: t for i, t in zip(self.r_idx, self.r_ts)}

        deltas_ns = [r_map[i] - l_map[i] for i in common]  # right - left
        abs_d = [abs(d) for d in deltas_ns]

        # Drift over time: delta vs time (seconds). Use left timestamps.
        t0 = l_map[common[0]] / 1e9
        x_time = [(l_map[i] / 1e9) - t0 for i in common]
        slope_ns_per_s, _ = self._linregress(x_time, deltas_ns)

        return {
            "aligned_pairs": n_common,
            "left_only": len(left_only),
            "right_only": len(right_only),
            "skew_ns_mean": statistics.fmean(deltas_ns),
            "skew_ns_median": statistics.median(deltas_ns),
            "skew_ns_p95_abs": self._percentile(abs_d, 95),
            "skew_ns_min": min(deltas_ns),
            "skew_ns_max": max(deltas_ns),
            "skew_ns_mad": self._mad(deltas_ns, statistics.median(deltas_ns)),
            "drift_ns_per_s": slope_ns_per_s,  # positive => right lags more over time
            "first_common_index": common[0],
            "last_common_index": common[-1],
        }

    @staticmethod
    def _percentile(values: List[int], p: float) -> float:
        if not values: return float("nan")
        s = sorted(values)
        k = (len(s) - 1) * (p/100.0)
        f = math.floor(k); c = math.ceil(k)
        if f == c: return float(s[int(k)])
        return float(s[f] + (s[c] - s[f]) * (k - f))

    # ---------- MP4 writing ----------

    @staticmethod
    def _have_ffmpeg() -> bool:
        return shutil.which("ffmpeg") is not None

    @staticmethod
    def _fmt_fps(fps: float) -> str:
        common = {
            24.0: "24", 25.0: "25", 30.0: "30", 50.0: "50", 60.0: "60",
            23.976: "24000/1001", 29.97: "30000/1001", 59.94: "60000/1001",
        }
        for k, v in common.items():
            if abs(fps - k) < 1e-3: return v
        return f"{fps:.6f}"

    def _remux_copy(self, h264: Path, out_mp4: Path, fps_in: float):
        cmd = [
            "ffmpeg","-y","-fflags","+genpts",
            "-r", self._fmt_fps(fps_in), "-f","h264","-i", str(h264),
            "-c:v","copy","-movflags","+faststart", str(out_mp4),
        ]
        subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def _reencode_libx264(self, h264: Path, out_mp4: Path, fps_tgt: float):
        cmd = [
            "ffmpeg","-y","-fflags","+genpts",
            "-r", self._fmt_fps(fps_tgt), "-f","h264","-i", str(h264),
            "-c:v","libx264","-preset","veryfast","-crf","18","-pix_fmt","yuv420p",
            "-movflags","+faststart","-vsync","cfr", str(out_mp4),
        ]
        subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def _fps_from_stats(self, side: str) -> float:
        if side == "left":
            st = self.left_stats
        elif side == "right":
            st = self.right_stats
        elif side == "rgb":
            st = self.rgb_stats
        else:
            raise ValueError(f"Unknown side: {side}")
        return float(st["fps_median"])

    def to_mp4(self,
               out_left: str | Path,
               out_right: str | Path,
               fps_left: Optional[float] = None,
               fps_right: Optional[float] = None,
               out_rgb: Optional[str | Path] = None,
               fps_rgb: Optional[float] = None,
               reencode_fallback: bool = True) -> Tuple[Path, Path, Optional[Path]]:
        if not self._have_ffmpeg():
            raise RuntimeError("ffmpeg not found in PATH")

        out_left  = Path(out_left);  out_left.parent.mkdir(parents=True, exist_ok=True)
        out_right = Path(out_right); out_right.parent.mkdir(parents=True, exist_ok=True)

        L_fps = fps_left  if fps_left  else self._fps_from_stats("left")
        R_fps = fps_right if fps_right else self._fps_from_stats("right")

        # Left
        try:
            self._remux_copy(self.l_h264, out_left, L_fps)
        except subprocess.CalledProcessError as e:
            if not reencode_fallback:
                raise RuntimeError(f"Left remux failed:\n{e.stderr.decode(errors='ignore')}") from e
            self._reencode_libx264(self.l_h264, out_left, L_fps)

        # Right
        try:
            self._remux_copy(self.r_h264, out_right, R_fps)
        except subprocess.CalledProcessError as e:
            if not reencode_fallback:
                raise RuntimeError(f"Right remux failed:\n{e.stderr.decode(errors='ignore')}") from e
            self._reencode_libx264(self.r_h264, out_right, R_fps)

        # RGB (optional)
        out_rgb_path = None
        if out_rgb and self.has_rgb:
            out_rgb_path = Path(out_rgb)
            out_rgb_path.parent.mkdir(parents=True, exist_ok=True)
            RGB_fps = fps_rgb if fps_rgb else self._fps_from_stats("rgb")
            try:
                self._remux_copy(self.rgb_h264, out_rgb_path, RGB_fps)
            except subprocess.CalledProcessError as e:
                if not reencode_fallback:
                    raise RuntimeError(f"RGB remux failed:\n{e.stderr.decode(errors='ignore')}") from e
                self._reencode_libx264(self.rgb_h264, out_rgb_path, RGB_fps)

        return out_left, out_right, out_rgb_path

    # ---------- Public ----------

    def analyze(self) -> Dict:
        """
        Returns:
          {
            'left':  per-stream stats,
            'right': per-stream stats,
            'rgb':   per-stream stats (if available),
            'pair':  cross-stream skew/jitter/drift + alignment counts
          }
        """
        result = {"left": self.left_stats, "right": self.right_stats, "pair": self.pair_stats}
        if self.has_rgb:
            result["rgb"] = self.rgb_stats
        return result

    def write_stats_json(self, out_path: str | Path) -> Path:
        """Write analysis stats to a JSON file and return the path."""
        out_path = Path(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        stats = self.analyze()
        out_path.write_text(json.dumps(stats, indent=2))
        return out_path

    def finalize(self,
                 output_dir: str | Path,
                 zip_path: Optional[str | Path] = None,
                 make_mp4: bool = True,
                 fps_left: Optional[float] = None,
                 fps_right: Optional[float] = None,
                 fps_rgb: Optional[float] = None,
                 reencode_fallback: bool = True) -> Dict:
        """
        Create outputs in the given directory and zip the result.
        Steps:
          - optionally create left.mp4/right.mp4/rgb.mp4 (copy or reencode)
          - write recording_stats.json
          - zip the entire output_dir to zip_path (default: sibling .zip)
        Returns a dict with paths and success flags.
        """
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        result: Dict[str, object] = {
            "mp4_left": None,
            "mp4_right": None,
            "mp4_rgb": None,
            "stats_json": None,
            "zip_path": None,
            "mp4_ok": False,
            "zip_ok": False,
        }

        # MP4 generation
        if make_mp4:
            try:
                out_rgb_param = output_dir / "rgb.mp4" if self.has_rgb else None
                out_left, out_right, out_rgb = self.to_mp4(
                    output_dir / "left.mp4",
                    output_dir / "right.mp4",
                    fps_left=fps_left,
                    fps_right=fps_right,
                    out_rgb=out_rgb_param,
                    fps_rgb=fps_rgb,
                    reencode_fallback=reencode_fallback,
                )
                result["mp4_left"] = str(out_left)
                result["mp4_right"] = str(out_right)
                if out_rgb:
                    result["mp4_rgb"] = str(out_rgb)
                result["mp4_ok"] = True
                # Delete source H264 files after successful conversion
                try:
                    if self.l_h264.exists():
                        self.l_h264.unlink()
                    if self.r_h264.exists():
                        self.r_h264.unlink()
                    if self.has_rgb and self.rgb_h264.exists():
                        self.rgb_h264.unlink()
                    logger.info(f"Deleted source H264 files: {self.l_h264}, {self.r_h264}" + 
                               (f", {self.rgb_h264}" if self.has_rgb else ""))
                except Exception as e:
                    logger.warning(f"Failed to delete H264 sources: {e}")
            except Exception as e:
                logger.error(f"MP4 generation failed: {e}")

        # Stats JSON
        try:
            stats_path = output_dir / "recording_stats.json"
            self.write_stats_json(stats_path)
            result["stats_json"] = str(stats_path)
        except Exception as e:
            logger.error(f"Failed to write stats JSON: {e}")

        # ZIP archive
        if zip_path is None:
            zip_path = output_dir.with_suffix(".zip")
        zip_path = Path(zip_path)

        try:
            with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
                for file_path in output_dir.rglob('*'):
                    if file_path.is_file():
                        arcname = file_path.relative_to(output_dir)
                        zipf.write(file_path, arcname)
            result["zip_path"] = str(zip_path)
            result["zip_ok"] = zip_path.exists() and zip_path.stat().st_size > 0
        except Exception as e:
            logger.error(f"ZIP creation failed: {e}")

        return result
