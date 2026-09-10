from setuptools import setup, find_packages

setup(
    name="ebay-client",
    version="0.1.0",
    packages=find_packages(),
    install_requires=[],
    extras_require={
        "notifications": ["cryptography>=41.0.0"],
    },
)
