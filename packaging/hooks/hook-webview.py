from pathlib import Path

import webview


webview_root = Path(webview.__file__).resolve().parent

datas = [
    (str(webview_root / "js"), "webview/js"),
    (str(webview_root / "lib"), "webview/lib"),
]

binaries = []
