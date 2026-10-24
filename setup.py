try:
    from setuptools import setup, find_packages
except ImportError:
    import sys
    print("Error: 'setuptools' is required to run setup.py directly. Please run 'pip install setuptools' or use 'pip install .'", file=sys.stderr)
    sys.exit(1)

setup(
    name="ci-proof",
    version="0.1.0",
    description="Evidence-based CI audit and change-aware test selection engine",
    packages=find_packages(include=["engine*"]) + ["profiles"],
    package_data={"profiles": ["schema.json", "*.yaml"]},
    include_package_data=True,
    entry_points={
        "console_scripts": [
            "ci-proof=engine.cli:main",
        ],
    },
)
