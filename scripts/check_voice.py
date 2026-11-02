"""Is the voice-over service reachable from this machine?

    ~/virtualenv/moneyprinterturbo/3.11/bin/python scripts/check_voice.py

Vidzy's voice-overs come from Microsoft's free Edge read-aloud service via the `edge-tts` library.
Microsoft changes that service's handshake now and then; when it does, every video fails at
"## generating audio" with `edge_tts stream timed out after 30s`. This takes about ten seconds and
says whether that is what is happening. It writes nothing outside a temporary folder.
"""

import asyncio
import importlib.metadata as md
import os
import sys
import tempfile
import time


async def main() -> int:
    import edge_tts

    print(f"edge-tts version : {md.version('edge-tts')}")
    out = os.path.join(tempfile.mkdtemp(prefix="voicecheck-"), "test.mp3")
    started = time.time()
    try:
        await asyncio.wait_for(
            edge_tts.Communicate("Voice check. If you can read this, the service answered.", "en-US-AndrewNeural").save(out),
            40,
        )
    except Exception as e:  # noqa: BLE001
        print(f"RESULT           : FAILED after {time.time() - started:.1f}s - {type(e).__name__}: {str(e)[:160]}")
        if "403" in str(e):
            print("MEANING          : Microsoft is rejecting this edge-tts version (HTTP 403). Upgrade it:")
            print("                   pip install --upgrade edge-tts   (then restart the app)")
        elif "NoAudio" in type(e).__name__:
            print("MEANING          : connected but got no audio - often transient; run this again.")
        else:
            print("MEANING          : the service is unreachable or blocked from this network.")
        return 1
    size = os.path.getsize(out)
    print(f"RESULT           : OK - {size} bytes of audio in {time.time() - started:.1f}s")
    return 0 if size > 1000 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
