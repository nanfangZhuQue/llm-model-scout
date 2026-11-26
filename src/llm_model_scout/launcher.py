import threading
import time
import urllib.error
import urllib.request
import webbrowser


def open_when_ready(port: int, token: str):
    """Open only after this token authenticates, never an unrelated process on the port."""

    def worker():
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        deadline = time.monotonic() + 20
        url = f"http://127.0.0.1:{port}"
        while time.monotonic() < deadline:
            request = urllib.request.Request(
                url + "/providers", headers={"Authorization": f"Bearer {token}"}
            )
            try:
                with opener.open(request, timeout=1) as response:
                    if response.status == 200:
                        webbrowser.open(url)
                        return
            except (urllib.error.URLError, TimeoutError):
                pass
            time.sleep(0.2)

    threading.Thread(target=worker, daemon=True).start()
