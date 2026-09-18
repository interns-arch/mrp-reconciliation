"""Docker HEALTHCHECK for the MRP Reconciliation container.

/api/mode never needs credentials to answer, but the whole app sits behind
HTTP Basic auth when a password is set (see app.py), so a healthy app can
legitimately answer this with either 200 (no auth configured) or 401
(auth configured, no credentials given here) - both mean "the process is up
and serving requests." Anything else, or no response at all, is unhealthy.
"""

import os
import sys
import urllib.error
import urllib.request

url = f"http://127.0.0.1:{os.environ.get('PORT', '8765')}/api/mode"

try:
    status = urllib.request.urlopen(url, timeout=3).status
except urllib.error.HTTPError as exc:
    status = exc.code
except Exception:
    sys.exit(1)

sys.exit(0 if status in (200, 401) else 1)
