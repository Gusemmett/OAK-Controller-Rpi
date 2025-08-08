try:
    import depthai as dai  # type: ignore
except Exception as exc:  # noqa: BLE001
    print(f"Failed to import depthai: {exc}")
    raise SystemExit(1)


def main() -> None:
    devices = dai.Device.getAllAvailableDevices()
    if devices:
        mx_ids = [dev.getMxId() for dev in devices]
        print(f"Found {len(devices)} device(s): {mx_ids}")
    else:
        print("No OAK devices found")


if __name__ == "__main__":
    main()
