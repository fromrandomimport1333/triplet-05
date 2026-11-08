"""
Thin setup.py wiring the cffi out-of-line build hook.

setuptools has no pyproject-native key for cffi's "cffi_modules" build hook
(see the cffi documentation's "Out-of-line API mode" / packaging section), so
this file exists only to pass that one argument through. All real metadata
lives in pyproject.toml.
"""
from setuptools import setup

setup(cffi_modules=["build_citybrain.py:ffibuilder"])
