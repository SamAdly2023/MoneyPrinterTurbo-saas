"""Build VidzyEngine.exe (Windows).

    .venv-engine/Scripts/python.exe engine/build.py

Output: dist-engine/VidzyEngine.exe. Needs the slim environment from
engine/requirements.txt (python -m venv .venv-engine, pip install -r ...).
"""
import os
import shutil
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STAGE = os.path.join(ROOT, "build-engine", "resource")
# The two STHeiti fonts are ~111MB of the 142MB font folder and only a
# fallback for an empty font name - the pipeline always sets one.
SKIP_FONTS = ("STHeitiLight.ttc", "STHeitiMedium.ttc")


def stage_resources() -> None:
    shutil.rmtree(os.path.dirname(STAGE), ignore_errors=True)
    shutil.copytree(os.path.join(ROOT, "resource", "songs"), os.path.join(STAGE, "songs"))
    fonts = os.path.join(STAGE, "fonts")
    shutil.copytree(os.path.join(ROOT, "resource", "fonts"), fonts,
                    ignore=shutil.ignore_patterns(*SKIP_FONTS))


def main() -> None:
    import PyInstaller.__main__

    stage_resources()
    sep = os.pathsep
    PyInstaller.__main__.run([
        os.path.join(ROOT, "engine", "run.py"),
        "--onefile", "--console", "--noconfirm", "--clean",
        "--name", "VidzyEngine",
        "--distpath", os.path.join(ROOT, "dist-engine"),
        "--workpath", os.path.join(ROOT, "build-engine", "work"),
        "--specpath", os.path.join(ROOT, "build-engine"),
        "--paths", ROOT,
        "--add-data", f"{os.path.join(STAGE, 'songs')}{sep}resource/songs",
        "--add-data", f"{os.path.join(STAGE, 'fonts')}{sep}resource/fonts",
        "--add-data", f"{os.path.join(ROOT, 'config.example.toml')}{sep}.",
        # firestore_db picks its backend with importlib at runtime, which the
        # build can't see; the Engine only ever uses the SQLite one.
        "--hidden-import", "app.services.db_sqlite",
        "--collect-all", "imageio_ffmpeg",
        "--exclude-module", "firebase_admin", "--exclude-module", "streamlit",
        "--exclude-module", "litellm", "--exclude-module", "tkinter",
    ])
    exe = os.path.join(ROOT, "dist-engine", "VidzyEngine.exe")
    if os.path.isfile(exe):
        print(f"\nBuilt {exe} ({os.path.getsize(exe) / 1024 / 1024:.0f} MB)")
    else:
        sys.exit("build failed - no exe produced")


if __name__ == "__main__":
    main()
