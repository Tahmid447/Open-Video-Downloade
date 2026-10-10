#!/bin/sh
set -eu
python - <<'PY'
import json
from urllib.request import urlopen
with urlopen('http://127.0.0.1:8000/api/health', timeout=3) as response:
    assert response.status == 200 and json.load(response)['ok']
PY
