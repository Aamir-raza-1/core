import glob
import os
import shutil

try:
    from setuptools import find_packages, setup
    from setuptools.command.build_py import build_py as _build_py
except ImportError:
    import sys
    print("Error: 'setuptools' is required to run setup.py directly. Please run 'pip install setuptools' or use 'pip install .'", file=sys.stderr)
    sys.exit(1)


class _BundleProfilesBuildPy(_build_py):
    """Bundle top-level profiles/*.yaml and schema.json into engine/profiles/ inside the wheel."""

    def run(self):
        super().run()
        here = os.path.dirname(os.path.abspath(__file__))
        src_dir = os.path.join(here, "profiles")
        if os.path.isdir(src_dir):
            dst_dir = os.path.join(self.build_lib, "engine", "profiles")
            os.makedirs(dst_dir, exist_ok=True)
            for pattern in ("schema.json", "*.yaml"):
                for path in glob.glob(os.path.join(src_dir, pattern)):
                    shutil.copy2(path, os.path.join(dst_dir, os.path.basename(path)))


setup(
    name="ci-proof",
    version="0.1.0",
    description="Evidence-based CI audit and change-aware test selection engine",
    packages=find_packages(include=["engine*"]),
    cmdclass={"build_py": _BundleProfilesBuildPy},
    include_package_data=True,
    entry_points={
        "console_scripts": [
            "ci-proof=engine.cli:main",
        ],
    },
)
