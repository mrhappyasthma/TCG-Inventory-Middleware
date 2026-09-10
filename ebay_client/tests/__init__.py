"""Test package marker.

Present so the suite can be discovered from the project root with
``-t ebay_client``, which puts the package directory on sys.path. Without it
the outer ``ebay_client/`` directory shadows the real package as an empty
namespace package and every import of the public API fails.
"""
