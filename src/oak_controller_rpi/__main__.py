def main() -> None:
    print("OAK Controller RPi service placeholder")
    try:
        import depthai as dai  # type: ignore

        devices = dai.Device.getAllAvailableDevices()
        if devices:
            mx_ids = [dev.getMxId() for dev in devices]
            print(f"Found {len(devices)} device(s): {mx_ids}")
        else:
            print("No OAK devices found")
    except Exception as exc:  # noqa: BLE001
        print(f"DepthAI not available or failed to enumerate devices: {exc}")


if __name__ == "__main__":
    main()
