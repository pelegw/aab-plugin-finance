"""`python -m aab_plugin_finance`: serve the plugin API on :8090.

This does the same as the image's CMD. It runs one worker, because the
database has one writer and the in-process locks assume a single process.
"""

import uvicorn

if __name__ == "__main__":
    uvicorn.run("aab_plugin_finance.main:create_app", factory=True,
                host="0.0.0.0", port=8090, workers=1, access_log=False)
