from setuptools import setup, find_packages

setup(
    name="tcg-engine",
    version="0.1.0",
    packages=find_packages(),
    install_requires=[],
    entry_points={
        "console_scripts": [
            "tcg-engine=tcg_engine.cli:main",
        ],
    },
)
