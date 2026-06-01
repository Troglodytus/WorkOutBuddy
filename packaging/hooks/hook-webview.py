from pathlib import Path

import webview


webview_root = Path(webview.__file__).resolve().parent

datas = [
    (str(webview_root / "js"), "webview/js"),
    (str(webview_root / "lib" / "Microsoft.Web.WebView2.Core.dll"), "webview/lib"),
    (str(webview_root / "lib" / "Microsoft.Web.WebView2.WinForms.dll"), "webview/lib"),
    (str(webview_root / "lib" / "WebBrowserInterop.x64.dll"), "webview/lib"),
    (str(webview_root / "lib" / "runtimes" / "win-x64"), "webview/lib/runtimes/win-x64"),
]

binaries = []
