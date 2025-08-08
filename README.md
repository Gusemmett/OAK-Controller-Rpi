# OAK Controller RPi

Workspace for Luxonis OAK camera app development and Raspberry Pi controller code.

## Quickstart

1. Install Pixi: https://pixi.sh
2. Create the environment and install deps:
   
   ```bash
   pixi install
   ```
3. Open a shell in the environment:
   
   ```bash
   pixi shell
   ```
4. Run the RPi service placeholder:
   
   ```bash
   pixi run run:rpi
   ```
5. Enumerate OAK devices via example script:
   
   ```bash
   pixi run run:camera
   ```

## Layout
- `src/oak_controller_rpi`: RPi controller package
- `camera_apps/`: Camera app scripts and examples
- `configs/`: Configuration files (pipelines, app settings)
- `scripts/`: Utility scripts
- `tests/`: Tests

## Notes
- Uses Pixi for dependency and task management.
- DepthAI and OpenCV are pulled from PyPI via Pixi.
- Python 3.11 is targeted to match Raspberry Pi OS (Bookworm).
