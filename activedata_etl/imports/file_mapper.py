# encoding: utf-8
#
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.
#
# Author: Kyle Lahnakoski (klahnakoski@mozilla.com)

from __future__ import division
from __future__ import unicode_literals

from future.utils import text_type

from activedata_etl.transforms.grcov_to_es import download_file
from mo_files import TempFile
from mo_json import stream
from mo_logs import Log
from mo_math.vendor.strangman.statstest import a
from mo_times import Timer
from pyLibrary.env.big_data import scompressed2ibytes


class FileMapper(object):
    """
    MAP FROM COVERAGE FILE RESOURCE NAME TO SOURCE FILENAME
    """

    def __init__(self, files_url):
        """
        :param files_url: EXPECTING ZIP FILE OF JSON AS ONE OBJECT IN {filename: [product, component]} FORMAT
        """

        self.lookup = {}
        with TempFile() as tempfile:
            Log.note("download {{url}}", url=a.url)
            download_file(a.url, tempfile.abspath)
            with open(tempfile.abspath, b"rb") as fstream:
                with Timer("process {{url}}", param={"url": a.url}):
                    for data in enumerate(stream.parse(
                        scompressed2ibytes(fstream),
                        {"items": "."},
                        {"."}
                    )):
                        self._add(data.name)

    def _add(self, filename):
        path = list(reversed(filename.split("/")))
        curr = self.lookup
        for i, p in enumerate(path):
            found = curr.get(p)
            if not found:
                curr[p] = filename
            elif isinstance(found, text_type):
                curr[path[i + 1]] = {p: filename}
                self._add(found)
            else:
                curr = found

    def find(self, filename):
        path = list(reversed(filename.split("/")))
        curr = self.lookup
        for i, p in enumerate(path):
            found = curr.get(p)
            if not found:
                Log.error("can not find filename")
            elif isinstance(found, text_type):
                return found
            else:
                curr = found
