def test_import() -> None:
    import importlib

    assert importlib.import_module("oak_controller_rpi") is not None
