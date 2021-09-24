#!/usr/bin/env python3
# Copyright (C) 2021 The Software Heritage developers
# See the AUTHORS file at the top-level directory of this distribution
# License: GNU General Public License version 3, or any later version
# See top-level LICENSE file for more information

from io import open
from os import path

from setuptools import find_packages, setup

here = path.abspath(path.dirname(__file__))

# Get the long description from the README file
with open(path.join(here, "README.rst"), encoding="utf-8") as f:
    long_description = f.read()


def parse_requirements(*names):
    requirements = []
    for name in names:
        if name:
            reqf = "requirements-%s.txt" % name
        else:
            reqf = "requirements.txt"

        if not path.exists(reqf):
            return requirements

        with open(reqf) as f:
            for line in f.readlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                requirements.append(line)
    return requirements


# Edit this part to match your module, replace foo by its name
# Full sample:
#   https://forge.softwareheritage.org/diffusion/DCORE/browse/master/setup.py
setup(
    name="swh.loader.bzr",  # example: swh.loader.pypi
    description="Software Heritage Bazaar/Breezy intent",
    long_description=long_description,
    long_description_content_type="text/x-rst",
    python_requires=">=3.7",
    author="Software Heritage developers",
    author_email="swh-devel@inria.fr",
    url="https://forge.softwareheritage.org/diffusion/DLDBZR/",
    packages=find_packages(),  # packages's modules
    install_requires=parse_requirements(None, "swh"),
    tests_require=parse_requirements("test"),
    setup_requires=["setuptools-scm"],
    use_scm_version=True,
    extras_require={"testing": parse_requirements("test")},
    include_package_data=True,
    entry_points="""
        [swh.workers]
        loader.bzr=swh.loader.bzr:register
        [console_scripts]
        swh-bzr-identify=swh.loader.bzr.identify:main
    """,
    classifiers=[
        "Programming Language :: Python :: 3",
        "Intended Audience :: Developers",
        "License :: OSI Approved :: GNU General Public License v3 (GPLv3)",
        "Operating System :: OS Independent",
        "Development Status :: 3 - Alpha",
    ],
    project_urls={
        "Bug Reports": "https://forge.softwareheritage.org/maniphest",
        "Funding": "https://www.softwareheritage.org/donate",
        "Source": ("https://forge.softwareheritage.org/source/swh-loader-bzr"),
        "Documentation": "https://docs.softwareheritage.org/devel/swh-loader-bzr/",  # NoQA: E501
    },
)
