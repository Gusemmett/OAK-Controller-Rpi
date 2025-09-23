#!/usr/bin/env python3

import depthai as dai

from .custom_host_nodes import PoseCSVLoggerThreaded, DepthLogger


def build_slam_pipeline(p: dai.Pipeline,
                        source_left,
                        source_right,
                        depth_align_socket,
                        imu_output=None,
                        create_pose_logger: bool = True):
    """
    Build and link the SLAM subgraph into an existing pipeline.

    Inputs:
      - p: existing dai.Pipeline
      - source_left, source_right: image streams to feed into StereoDepth
      - depth_align_socket: camera socket to align depth to (e.g., left socket)
      - create_pose_logger: if True, attach a PoseCSVLoggerThreaded host node

    Returns:
      dict of created nodes that may be used by the caller (currently only 'poseLogger' when created)
    """

    nodes = {}

    stereo = p.create(dai.node.StereoDepth)
    stereo.setExtendedDisparity(False)
    stereo.setLeftRightCheck(True)
    stereo.setRectifyEdgeFillColor(0)
    stereo.enableDistortionCorrection(True)
    stereo.initialConfig.setLeftRightCheckThreshold(10)
    stereo.setDepthAlign(depth_align_socket)

    featureTracker = p.create(dai.node.FeatureTracker)
    featureTracker.setHardwareResources(1, 2)
    featureTracker.initialConfig.setCornerDetector(dai.FeatureTrackerConfig.CornerDetector.Type.HARRIS)
    featureTracker.initialConfig.setNumTargetFeatures(1000)
    featureTracker.initialConfig.setMotionEstimator(False)
    featureTracker.initialConfig.FeatureMaintainer.minimumDistanceBetweenFeatures = 49

    odom = p.create(dai.node.RTABMapVIO)
    slam = p.create(dai.node.RTABMapSLAM)
    slam_params = {
        "RGBD/CreateOccupancyGrid": "true",
        "Grid/3D": "true",
        "Rtabmap/SaveWMState": "true",
    }
    slam.setParams(slam_params)

    # Link camera outputs into SLAM path
    source_left.link(stereo.left)
    source_right.link(stereo.right)

    # Feature tracking and VIO/SLAM chain
    featureTracker.passthroughInputImage.link(odom.rect)
    stereo.rectifiedLeft.link(featureTracker.inputImage)
    stereo.depth.link(odom.depth)
    if imu_output is not None:
        imu_output.link(odom.imu)
    featureTracker.outputFeatures.link(odom.features)

    odom.transform.link(slam.odom)
    odom.passthroughRect.link(slam.rect)
    odom.passthroughDepth.link(slam.depth)

    if create_pose_logger:
        poseLogger = p.create(PoseCSVLoggerThreaded).build(slam.transform, slam.passthroughRect)
        nodes["poseLogger"] = poseLogger

    return nodes



def build_depth_pipeline(p: dai.Pipeline,
                         source_left,
                         source_right,
                         depth_align_socket) -> dict:
    """
    Build and link a StereoDepth subgraph into an existing pipeline.

    Inputs:
      - p: existing dai.Pipeline
      - source_left, source_right: image streams to feed into StereoDepth
      - depth_align_socket: camera socket to align depth to (e.g., left socket)

    Returns:
      dict of created nodes (currently exposes 'stereo' for future consumers)
    """

    nodes = {}

    stereo = p.create(dai.node.StereoDepth)
    # Follow example defaults suitable for general purpose visualization
    try:
        stereo.setRectification(True)
    except Exception:
        # Some firmware builds may rectify by default; ignore if unavailable
        pass
    stereo.setExtendedDisparity(True)
    stereo.setLeftRightCheck(True)
    stereo.setRectifyEdgeFillColor(0)
    stereo.enableDistortionCorrection(True)
    stereo.initialConfig.setLeftRightCheckThreshold(10)
    stereo.setDepthAlign(depth_align_socket)

    # Link camera outputs into StereoDepth
    source_left.link(stereo.left)
    source_right.link(stereo.right)

    # Attach a DepthLogger that introspects packets
    depth_logger = p.create(DepthLogger).build(stereo.depth, stereo.confidenceMap)

    nodes["stereo"] = stereo
    nodes["depthLogger"] = depth_logger
    return nodes

