from pathlib import Path

from setuptools import setup


# Keep generated metadata out of src and avoid treating the hidden parent as a distribution.
Path(".build-metadata").mkdir(exist_ok=True)
setup()
