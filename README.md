# OAK Controller RPi - MultiCam API

This project implements a MultiCam API server for OAK (OpenCV AI Kit) devices on Raspberry Pi, providing network-discoverable camera recording functionality compatible with other multiCam device implementations.

## Features

- **mDNS Service Discovery**: Advertises as `_multicam._tcp.local.` service
- **TCP Command Interface**: Handles recording commands on port 8080
- **Synchronized Recording**: Support for scheduled recording starts
- **Binary File Transfer**: Efficient video file downloads
- **OAK Integration**: Uses existing DepthAI camera recording pipeline

## Installation

### Environment
Current recomendation for env control with this repo is to use venv. Pixi support will be added when depthai v3 is out of pre-release mode and has wheels for macosx and rpi.

```
python -m venv .env
```

### Install dependencies
```bash
pip install -r requirements.txt
pip install --pre depthai --force-reinstall
```


## Usage

### Start the MultiCam Server

```bash
python3 run_multicam_server.py
```

Options:
- `--port PORT`: TCP server port (default: 8080)  
- `--videos-dir DIR`: Video storage directory (default: ./videos)
- `--log-level LEVEL`: Logging level (DEBUG, INFO, WARNING, ERROR)

### Scripts

#### Slam script
Uses Luxonis' custom slam node to compute slam. Also spins up rerun viewer for point cloud. 
```
python scripts/slamScript.py
```